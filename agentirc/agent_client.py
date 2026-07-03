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
  :meth:`AgentClient.send_raw` (write a raw pre-formatted line) and
  :meth:`AgentClient.raw_lines` (read every raw wire line, not just
  PRIVMSGs) are thin, additive escape hatches for callers that drive
  non-PRIVMSG verbs — e.g. ``HISTORY SINCE <channel> <cursor>`` — directly
  and need the unfiltered reply; see ``tests/test_reconnect_catchup.py`` for
  a worked reconnect+catch-up example.
- **Agents are first-class clients, not bots.** The ``agentirc.io/bot``
  capability is *not* requested by default (that gates bot-only wire
  behaviours like silent joins). ``message-tags`` *is* requested by default
  so parsed IRCv3 tags are surfaced on each :class:`IncomingMessage`.

This module is a semver-tracked public surface (see
``docs/api-stability.md``); the public members are :class:`AgentClient` and
:class:`IncomingMessage`. Everything prefixed with ``_`` is implementation
detail and may change without a major bump.
"""

from __future__ import annotations

import asyncio
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
        # Sanitized the same way `join()` sanitizes: every entry in
        # `self._channels` must be safe to replay verbatim on reconnect.
        self._channels: list[str] = []
        for chan in channels or ():
            chan = _sanitize(chan)
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
        # Raw bytes awaiting a newline. Kept as bytes (not str) so a
        # multibyte UTF-8 codepoint split across two `reader.read()` chunks
        # is never decoded until the full byte sequence has arrived —
        # decoding a partial chunk would emit a lossy U+FFFD replacement
        # character for the trailing bytes.
        self._recv_buffer: bytes = b""
        self._connected = False
        self._closing = False
        self._run_task: asyncio.Task[None] | None = None
        self._registered_event = asyncio.Event()
        self._connect_error: BaseException | None = None
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        # Raw-line capture is opt-in (see `raw_lines`) so callers who never
        # touch it pay no cost: the queue stays empty and unused.
        self._raw_capture = False
        self._raw_queue: asyncio.Queue[str | None] = asyncio.Queue()

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
        """Stop the client, tear down the connection, and end ``messages()``."""
        self._closing = True
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None
        self._teardown_connection()
        self._connected = False
        # Unblock a pending connect() and any messages()/raw_lines() consumer.
        self._registered_event.set()
        self._queue.put_nowait(_CLOSE_SENTINEL)
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
        """Join ``channel`` now (if connected) and on every future reconnect.

        ``channel`` is run through :func:`_sanitize` first, the same as
        :meth:`send`/:meth:`send_raw` — a channel containing embedded
        CR/LF would otherwise both emit a malformed/extra wire command on
        this call *and* get replayed verbatim on every future reconnect via
        :attr:`_channels`. Sanitizing before the membership check and the
        store means :attr:`_channels` only ever holds the safe value.
        """
        channel = _sanitize(channel)
        if channel not in self._channels:
            self._channels.append(channel)
        if self._connected and self._writer is not None:
            await self._write("JOIN", channel)

    async def send_raw(self, line: str) -> None:
        """Send a raw, pre-formatted IRC line verbatim (plus CRLF).

        Escape hatch for verbs :meth:`send`/:meth:`join` don't model — e.g.
        ``HISTORY SINCE <channel> <cursor>`` catch-up. ``line`` should be a
        single command without a trailing CRLF; embedded CR/LF are stripped
        for injection safety, same as :meth:`send`. Raises
        :class:`ConnectionError` if the client is not currently connected.
        """
        if not self._connected:
            raise ConnectionError(f"AgentClient({self._nick}) is not connected")
        writer = self._writer
        if writer is None:
            raise ConnectionError("no active connection")
        writer.write(f"{_sanitize(line)}\r\n".encode("utf-8"))
        await writer.drain()

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

    async def raw_lines(self) -> AsyncIterator[str]:
        """Yield every raw wire line received, not just PRIVMSGs.

        Complements :meth:`messages` (which only surfaces parsed PRIVMSGs)
        for callers driving verbs like ``HISTORY SINCE`` via :meth:`send_raw`
        that need to read the raw, unfiltered reply (e.g. ``HISTORY`` /
        ``HISTORYEND`` lines). Capture starts lazily the first time this
        iterator is consumed, so callers who never use it pay no cost.
        Transparently spans reconnects, the same way :meth:`messages` does.
        The iterator terminates when :meth:`close` is called. Intended for a
        single consumer.
        """
        self._raw_capture = True
        while True:
            item = await self._raw_queue.get()
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
        self._raw_queue.put_nowait(None)

    async def _connect_once(self) -> None:
        """Open the socket, register, and (re-)join channels."""
        self._recv_buffer = b""
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
        if self._raw_capture:
            self._raw_queue.put_nowait(msg.format().rstrip("\r\n"))
        cmd = msg.command.upper()
        if cmd == "PING":
            await self._write("PONG", msg.params[0] if msg.params else "")
            return
        if cmd == "PRIVMSG":
            incoming = self._build_incoming(msg)
            if incoming is not None:
                await self._queue.put(incoming)
        # All other traffic (numerics, JOIN/PART echoes, NOTICE, ...) is
        # consumed but not surfaced through messages().

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
        """Read and parse the next complete line, or ``None`` on EOF.

        Buffers raw bytes and splits on ``b"\\n"`` *before* decoding, so a
        multibyte UTF-8 codepoint straddling two ``reader.read()`` chunks is
        held in the buffer intact rather than decoded prematurely (which
        would silently corrupt it into a U+FFFD replacement character).
        Only complete lines are decoded, with ``errors="replace"`` still
        applied so genuinely invalid bytes degrade gracefully instead of
        raising.
        """
        reader = self._reader
        if reader is None:
            return None
        while True:
            newline = self._recv_buffer.find(b"\n")
            if newline != -1:
                raw_line = self._recv_buffer[:newline]
                self._recv_buffer = self._recv_buffer[newline + 1 :]
                line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
                if not line.strip():
                    continue
                return Message.parse(line)
            data = await reader.read(4096)
            if not data:
                return None
            self._recv_buffer += data

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
        self._recv_buffer = b""


# Sentinel pushed onto the message queue by close() so a blocked messages()
# consumer wakes and terminates cleanly.
_CLOSE_SENTINEL = IncomingMessage(channel=None, sender="", text="", raw="")


__all__ = ["AgentClient", "IncomingMessage"]
