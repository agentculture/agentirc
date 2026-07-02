"""Public reconnecting IRC client transport for agents.

``agentirc.agent_client.AgentClient`` is the client-side counterpart to
``agentirc.ircd.IRCd``: a small, self-healing asyncio IRC client that an
agent harness can point at any agentirc server (local or federated) to
connect, register, join channels, read messages, and send PRIVMSGs — with
transparent auto-reconnect.

Design notes
------------
- **Reconnect is internal.** The consumer never manages the socket. An
  internal run loop owns connect → register → re-join → read; on any
  disconnect it reconnects with exponential backoff (default: 1s, doubling
  to a 60s ceiling), re-runs the NICK/USER handshake (waiting for
  ``001 RPL_WELCOME``), and re-joins every channel the client had joined.
  :attr:`AgentClient.connected` reflects the live state, and
  :meth:`AgentClient.messages` transparently spans reconnects.
- **No history replay here.** Messages other clients send during an outage
  are lost at this layer; catch-up belongs to a separate HISTORY facility.
- **Agents are first-class clients, not bots.** The ``agentirc.io/bot``
  capability is *not* requested by default (that gates bot-only wire
  behaviours like silent joins). ``message-tags`` *is* requested by default
  so parsed IRCv3 tags are surfaced on each :class:`IncomingMessage`.
- **Raw escape hatch.** :meth:`AgentClient.send_raw` writes an arbitrary
  command/params line, and :meth:`AgentClient.raw_lines` yields every parsed
  incoming line (all commands, not just PRIVMSG) — the pair a caller needs to
  drive request/response protocol traffic this module doesn't model directly
  (e.g. ``HISTORY RECENT``/``HISTORY SINCE``). Call ``raw_lines()`` *before*
  awaiting anything that might produce the reply you want to see (ideally
  before :meth:`connect`) — the call synchronously arms the underlying queue,
  closing the race where a reply arrives before a consumer starts reading.

This module is a semver-tracked public surface (see
``docs/api-stability.md``); the public members are :class:`AgentClient` and
:class:`IncomingMessage`. Everything prefixed with ``_`` is implementation
detail and may change without a major bump.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from agentirc._internal.protocol.message import Message
from agentirc.protocol import (
    ERR_ALREADYREGISTRED,
    ERR_ERRONEUSNICKNAME,
    ERR_NICKNAMEINUSE,
    ERR_NONICKNAMEGIVEN,
    RPL_WELCOME,
)

logger = logging.getLogger(__name__)

# Registration numerics that mean "this handshake will never yield a 001".
# Raising on these avoids blocking the registration read until the timeout.
_REGISTRATION_ERRORS: frozenset[str] = frozenset(
    {
        ERR_NONICKNAMEGIVEN,
        ERR_ERRONEUSNICKNAME,
        ERR_NICKNAMEINUSE,
        ERR_ALREADYREGISTRED,
    }
)

# Default IRCv3 capabilities requested at registration. Deliberately excludes
# ``agentirc.io/bot`` — agents are first-class clients, not bots.
_DEFAULT_CAPS: tuple[str, ...] = ("message-tags",)


def _sanitize(text: str) -> str:
    """Strip CR/LF to prevent IRC protocol injection via message content."""
    return text.replace("\r", "").replace("\n", " ")


@dataclass
class IncomingMessage:
    """A message delivered to an :class:`AgentClient` consumer.

    Yielded by :meth:`AgentClient.messages`. Only channel/DM PRIVMSGs are
    surfaced here; other protocol traffic (numerics, JOIN echoes, NOTICEs,
    PINGs) is handled internally.

    Attributes:
        channel: The channel name when the PRIVMSG targeted a channel
            (starts with ``#``); ``None`` for a direct message.
        sender: The nick portion of the message prefix.
        text: The message body.
        tags: Parsed IRCv3 message tags (empty dict when none / the
            ``message-tags`` cap was not negotiated).
        raw: The original wire line, minus its trailing CRLF. Secondary
            escape hatch for callers that need the unparsed message.
    """

    channel: str | None
    sender: str
    text: str
    tags: dict[str, str] = field(default_factory=dict)
    raw: str = ""


class AgentClient:
    """A self-reconnecting asyncio IRC client for agent harnesses.

    Example::

        async with AgentClient("127.0.0.1", 6667, "myserv-agent",
                               channels=["#general"]) as client:
            await client.send("#general", "hello")
            async for msg in client.messages():
                print(msg.sender, msg.text)

    The client may equally be driven explicitly with
    :meth:`connect` / :meth:`close`.
    """

    def __init__(
        self,
        host: str,
        port: int,
        nick: str,
        channels: Sequence[str] | None = None,
        *,
        user: str | None = None,
        realname: str | None = None,
        caps: Sequence[str] = _DEFAULT_CAPS,
        reconnect: bool = True,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        backoff_factor: float = 2.0,
        register_timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._nick = nick
        self._user = user or nick
        self._realname = realname or nick
        # Ordered, de-duplicated set of channels to (re-)join on every connect.
        self._channels: list[str] = []
        for chan in channels or ():
            if chan not in self._channels:
                self._channels.append(chan)
        # Freeze the requested caps; ``agentirc.io/bot`` is intentionally not
        # added for the caller (they may still pass it explicitly).
        self._caps: tuple[str, ...] = tuple(caps)

        self._reconnect = reconnect
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._backoff_factor = backoff_factor
        self._register_timeout = register_timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._recv_buffer = ""
        self._connected = False
        self._closing = False
        self._run_task: asyncio.Task[None] | None = None
        self._registered_event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        # Lazily armed by raw_lines(); None until a consumer opts in, so
        # callers that never touch the raw escape hatch pay no extra queue
        # upkeep. See raw_lines() for the arm-before-connect race note.
        self._raw_queue: asyncio.Queue[Message | None] | None = None

    # -- public state -----------------------------------------------------

    @property
    def nick(self) -> str:
        """The nick this client registers as."""
        return self._nick

    @property
    def caps(self) -> tuple[str, ...]:
        """The IRCv3 capabilities this client requests at registration."""
        return self._caps

    @property
    def channels(self) -> tuple[str, ...]:
        """The channels the client (re-)joins on connect."""
        return tuple(self._channels)

    @property
    def connected(self) -> bool:
        """Whether the client currently has a registered live connection."""
        return self._connected

    # -- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> "AgentClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Start the client and block until the first registration completes.

        Idempotent: a second call while running is a no-op. If
        ``reconnect=False`` and the first connection fails, the underlying
        error is re-raised.
        """
        if self._run_task is not None:
            return
        self._closing = False
        self._connect_error = None
        self._registered_event = asyncio.Event()
        self._run_task = asyncio.create_task(self._run())
        await self._registered_event.wait()
        if not self._connected and self._connect_error is not None:
            raise self._connect_error

    async def close(self) -> None:
        """Stop the client, tear down the connection, and end ``messages()``/``raw_lines()``.

        Awaits the writer's ``wait_closed()`` after ``close()`` so any bytes
        handed to :meth:`send`/:meth:`send_raw` immediately before this call
        are actually flushed to the OS socket before this coroutine returns.
        Without that, a process that calls ``close()`` right after its last
        write (every one-shot CLI verb built on this client does exactly
        that) can race its own exit and silently drop the final write —
        observed in practice with ``agentirc send``: the PRIVMSG landed on
        the wire only intermittently until this awaited close was added.
        """
        self._closing = True
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        writer = self._writer
        self._teardown_connection()
        self._connected = False
        if writer is not None:
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        # Unblock a pending connect() and any messages()/raw_lines() consumer.
        self._registered_event.set()
        self._queue.put_nowait(_CLOSE_SENTINEL)
        if self._raw_queue is not None:
            self._raw_queue.put_nowait(None)

    # -- sending ----------------------------------------------------------

    async def send(self, target: str, text: str) -> None:
        """Send a PRIVMSG to ``target`` (a channel or nick).

        Raises :class:`ConnectionError` if the client is not currently
        connected (e.g. mid-reconnect).
        """
        if not self._connected:
            raise ConnectionError(f"AgentClient({self._nick}) is not connected")
        await self._write("PRIVMSG", _sanitize(target), _sanitize(text))

    async def join(self, channel: str) -> None:
        """Join ``channel`` now (if connected) and on every future reconnect."""
        if channel not in self._channels:
            self._channels.append(channel)
        if self._connected and self._writer is not None:
            await self._write("JOIN", channel)

    async def send_raw(self, command: str, *params: str) -> None:
        """Write an arbitrary ``command params...`` line to the wire.

        Escape hatch for protocol traffic this module doesn't model as a
        dedicated method (e.g. ``HISTORY RECENT``/``HISTORY SINCE``
        requests). Params are passed through verbatim — no CR/LF
        sanitization is applied, unlike :meth:`send`, since callers using
        this method are constructing protocol lines rather than forwarding
        free-form user text. Raises :class:`ConnectionError` if not
        currently connected.
        """
        if not self._connected:
            raise ConnectionError(f"AgentClient({self._nick}) is not connected")
        await self._write(command, *params)

    # -- receiving --------------------------------------------------------

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        """Yield incoming PRIVMSGs, transparently spanning reconnects.

        The iterator terminates when :meth:`close` is called. Intended for a
        single consumer.
        """
        while True:
            item = await self._queue.get()
            if item is _CLOSE_SENTINEL:
                return
            yield item

    def raw_lines(self) -> AsyncIterator[Message]:
        """Return an async iterator over every parsed incoming line.

        Unlike :meth:`messages` (PRIVMSG only), this surfaces the full
        protocol stream — numerics, ``HISTORY``/``HISTORYEND`` replies,
        JOIN echoes, NOTICEs, everything :meth:`_dispatch` sees — so a
        caller can drive request/response exchanges :class:`AgentClient`
        doesn't model directly. Transparently spans reconnects and
        terminates when :meth:`close` is called, same as :meth:`messages`.

        This is a plain (non-``async``) method: calling it synchronously
        arms the underlying queue before returning the iterator, so a reply
        that arrives between "call raw_lines()" and "start iterating it"
        is never lost. Call it before :meth:`connect` (or before writing
        the request whose reply you want to observe) to close that race.
        Intended for a single consumer.
        """
        if self._raw_queue is None:
            self._raw_queue = asyncio.Queue()
        return self._consume_raw()

    async def _consume_raw(self) -> AsyncIterator[Message]:
        queue = self._raw_queue
        assert queue is not None  # armed synchronously by raw_lines()
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    # -- reconnect loop ---------------------------------------------------

    def _backoff_delays(self):
        """Yield the exponential-backoff delay sequence (capped at max).

        Deterministic and side-effect free — the reconnect loop consumes it,
        and it is unit-tested directly.
        """
        delay = self._initial_backoff
        while True:
            yield delay
            delay = min(delay * self._backoff_factor, self._max_backoff)

    async def _run(self) -> None:
        delays = self._backoff_delays()
        while not self._closing:
            try:
                await self._connect_once()
                # Reset the backoff ramp after a fully successful connect.
                delays = self._backoff_delays()
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- any I/O failure => reconnect
                self._connect_error = exc
                logger.debug("AgentClient(%s) connection error: %r", self._nick, exc)
            finally:
                self._mark_disconnected()
            if self._closing or not self._reconnect:
                break
            await asyncio.sleep(next(delays))
        # Permanent exit (closing, or reconnect disabled after a drop): never
        # leave connect() blocked, and end any messages()/raw_lines() consumer
        # cleanly.
        self._registered_event.set()
        self._queue.put_nowait(_CLOSE_SENTINEL)
        if self._raw_queue is not None:
            self._raw_queue.put_nowait(None)

    async def _connect_once(self) -> None:
        """Open the socket, register, and (re-)join channels."""
        self._recv_buffer = ""
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        await self._register()
        await self._rejoin_channels()
        self._connected = True
        self._registered_event.set()

    async def _register(self) -> None:
        """Run the CAP/NICK/USER handshake and wait for ``001 RPL_WELCOME``."""
        if self._caps:
            await self._write("CAP", "REQ", " ".join(self._caps))
        await self._write("NICK", self._nick)
        await self._write("USER", self._user, "0", "*", self._realname)
        if self._caps:
            await self._write("CAP", "END")

        async with asyncio.timeout(self._register_timeout):
            while True:
                msg = await self._read_message()
                if msg is None:
                    raise ConnectionError("connection closed during registration")
                cmd = msg.command.upper()
                if cmd == "PING":
                    await self._write("PONG", msg.params[0] if msg.params else "")
                    continue
                if cmd == RPL_WELCOME:
                    return
                if cmd in _REGISTRATION_ERRORS:
                    raise ConnectionError(f"registration rejected: {cmd} {' '.join(msg.params)}")

    async def _rejoin_channels(self) -> None:
        for chan in self._channels:
            await self._write("JOIN", chan)

    async def _read_loop(self) -> None:
        while not self._closing:
            msg = await self._read_message()
            if msg is None:
                return  # EOF — fall back to the reconnect loop
            await self._dispatch(msg)

    async def _dispatch(self, msg: Message) -> None:
        cmd = msg.command.upper()
        if self._raw_queue is not None:
            await self._raw_queue.put(msg)
        if cmd == "PING":
            await self._write("PONG", msg.params[0] if msg.params else "")
            return
        if cmd == "PRIVMSG":
            incoming = self._build_incoming(msg)
            if incoming is not None:
                await self._queue.put(incoming)
        # All other traffic (numerics, JOIN/PART echoes, NOTICE, ...) is
        # consumed but not surfaced through messages() — use raw_lines() for
        # the full stream.

    @staticmethod
    def _build_incoming(msg: Message) -> IncomingMessage | None:
        if len(msg.params) < 2:
            return None
        target = msg.params[0]
        text = msg.params[1]
        sender = msg.prefix.split("!", 1)[0] if msg.prefix else ""
        channel = target if target.startswith("#") else None
        return IncomingMessage(
            channel=channel,
            sender=sender,
            text=text,
            tags=dict(msg.tags),
            raw=msg.format().rstrip("\r\n"),
        )

    # -- low-level I/O ----------------------------------------------------

    async def _write(self, command: str, *params: str) -> None:
        """Format and write a single IRC line to the socket."""
        writer = self._writer
        if writer is None:
            raise ConnectionError("no active connection")
        wire = Message(command=command, params=list(params)).format().encode("utf-8")
        writer.write(wire)
        await writer.drain()

    async def _read_message(self) -> Message | None:
        """Read and parse the next complete line, or ``None`` on EOF."""
        reader = self._reader
        if reader is None:
            return None
        while True:
            newline = self._recv_buffer.find("\n")
            if newline != -1:
                line = self._recv_buffer[:newline].rstrip("\r")
                self._recv_buffer = self._recv_buffer[newline + 1 :]
                if not line.strip():
                    continue
                return Message.parse(line)
            data = await reader.read(4096)
            if not data:
                return None
            self._recv_buffer += data.decode("utf-8", errors="replace")

    def _mark_disconnected(self) -> None:
        self._connected = False
        self._teardown_connection()

    def _teardown_connection(self) -> None:
        """Best-effort synchronous close of the current socket."""
        writer = self._writer
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass
        self._writer = None
        self._reader = None
        self._recv_buffer = ""


# Sentinel pushed onto the message queue by close() so a blocked messages()
# consumer wakes and terminates cleanly.
_CLOSE_SENTINEL = IncomingMessage(channel=None, sender="", text="", raw="")


__all__ = ["AgentClient", "IncomingMessage"]
