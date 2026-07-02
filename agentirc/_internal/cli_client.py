"""Implementations for the agent-facing CLI verbs: send, read, watch, join.

These are one-shot (``send``, ``read``, ``join``) or streaming (``watch``)
shell entry points for a script or agent harness that wants to talk to a
*running* ``agentirc`` daemon over real TCP without writing any Python. All
four are thin wrappers around the public transport
:class:`agentirc.agent_client.AgentClient`; the two request/response verbs
(``read``, ``join``) additionally drive :meth:`AgentClient.raw_lines` /
:meth:`AgentClient.send_raw` (see that module) since neither HISTORY replies
nor the JOIN/NAMES confirmation sequence are modelled as first-class
``AgentClient`` methods.

Kept out of ``agentirc/cli.py`` per CLAUDE.md's "keep cli.py thin" — that
module only wires argparse + the ``_HANDLERS`` dispatch table; the actual
verb bodies live here.

Wire contract this module relies on (see ``agentirc/skills/history.py`` and
``agentirc/client.py`` for the authoritative behavior):

- ``HISTORY RECENT <channel> <count>`` / ``HISTORY SINCE <channel> <cursor>``
  reply with zero or more ``HISTORY <channel> <nick> <timestamp> <text>``
  lines followed by a terminating ``HISTORYEND <channel> [next-cursor]``
  line. SINCE-mode ``HISTORY`` lines optionally carry ``msgid``/``time``
  IRCv3 tags; RECENT-mode lines never do.
- A malformed request (bad count, bad cursor, unknown subcommand) gets a
  ``NOTICE <nick> :<reason>`` reply tagged with ``agentirc.io/error``
  (``ERROR_TAG``) rather than a numeric — there is no dedicated error verb.
- A successful ``JOIN`` is followed by ``RPL_NAMREPLY`` (353) then
  ``RPL_ENDOFNAMES`` (366, ``params[1]`` is the channel — numerics carry the
  requesting nick in ``params[0]``); we treat 366 as "join confirmed".

``AgentClient.raw_lines()``/``send_raw()`` shape (post-t13 merge): these are
the public helpers from ``agentirc.agent_client`` (added by task t13, merged
into this branch from ``feat/agent-accessibility``) — *not* a bespoke pair
this module invents. ``send_raw(line: str)`` takes one already-formatted
wire line (no trailing CRLF); ``raw_lines()`` is an ``async def`` generator
that yields raw wire-text ``str`` lines and only arms its internal capture
flag lazily, on the first ``__anext__()`` of the returned generator — see
``_armed_raw_messages`` below for why this module always primes it before
``connect()`` rather than relying on that laziness.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import secrets
import signal
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

from agentirc._internal.protocol.message import Message
from agentirc.agent_client import AgentClient
from agentirc.protocol import (
    ERR_NOSUCHNICK,
    ERROR_TAG,
    MSGID_TAG,
    RPL_ENDOFNAMES,
    SERVER_TIME_TAG,
)

# Default page size for `read` when neither --last nor --since is given.
DEFAULT_READ_LAST = 20

# How long the one-shot verbs (read/join) wait for a server reply before
# giving up and reporting a timeout.
_REPLY_TIMEOUT_SECONDS = 10.0


def _default_nick() -> str:
    """``agent-<random 4 hex>`` — the documented default nick."""
    return f"agent-{secrets.token_hex(2)}"


def _emit_line(ts: str, nick: str, text: str, msgid: str | None, json_mode: bool) -> None:
    """Print one message line to stdout: plain ``<ts> <nick> <text>`` or JSON."""
    if json_mode:
        obj: dict[str, Any] = {"ts": ts, "nick": nick, "text": text}
        if msgid is not None:
            obj["msgid"] = msgid
        print(json.dumps(obj), flush=True)
    else:
        print(f"{ts} {nick} {text}", flush=True)


async def _connect_or_hint(
    client: AgentClient, host: str, port: int, verb: str
) -> int | None:
    """Attempt ``client.connect()``; on failure print a stderr hint.

    Returns an exit code if the connect failed (caller should return it
    immediately), or ``None`` on success. Exception ordering matters:
    ``ConnectionRefusedError`` and ``TimeoutError`` are both ``OSError``
    subclasses, so the more specific branches must come first.
    """
    try:
        await client.connect()
    except ConnectionRefusedError:
        print(
            f"agentirc {verb}: connection refused — is a server listening on "
            f"{host}:{port}?",
            file=sys.stderr,
        )
        return 1
    except (TimeoutError, asyncio.TimeoutError):
        print(
            f"agentirc {verb}: registration timed out connecting to {host}:{port}",
            file=sys.stderr,
        )
        return 1
    except ConnectionError as exc:
        print(f"agentirc {verb}: registration failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"agentirc {verb}: connection to {host}:{port} failed: {exc}", file=sys.stderr)
        return 1
    return None


async def _armed_raw_messages(client: AgentClient) -> AsyncIterator[Message]:
    """Prime ``client.raw_lines()`` and return a parsed-:class:`Message` iterator.

    ``AgentClient.raw_lines()`` only arms its internal raw-capture flag when
    its async-generator body actually starts running — i.e. on the first
    ``__anext__()`` (see ``agent_client.py``). Auto-join happens inside
    :meth:`AgentClient.connect` (via its internal run task re-joining
    configured channels right after registration), so a caller that waits
    until *after* ``connect()`` returns to start consuming ``raw_lines()``
    can lose a fast same-host join confirmation: the internal run task may
    already have read and dispatched it — synchronously, with no scheduler
    hand-off in between — before this coroutine's caller even gets a turn.

    This coroutine closes that race: it creates the generator, schedules a
    background task that steps it to its first suspension point (arming
    capture), and yields the event loop once (``asyncio.sleep(0)``) so that
    step actually runs before returning. Callers must ``await`` this *before*
    calling :meth:`AgentClient.connect`.

    For request/response call sites where the request is sent and then
    immediately read back in the same coroutine with no intervening
    ``await`` that can yield (e.g. ``read``'s ``send_raw`` + drain loop),
    this priming isn't strictly required — but using it everywhere keeps a
    single, uniformly-safe pattern rather than two.

    A caller can legitimately abandon the returned iterator without ever
    consuming it (e.g. ``connect()`` itself fails, so nobody ever gets to
    ``_wait_for_join``/``_collect_history``) — ``close()`` still resolves
    ``primer`` at that point (with a ``StopAsyncIteration``, via the
    ``None`` close-sentinel), but nothing would otherwise retrieve that
    result once the abandoned iterator is garbage-collected, which asyncio
    logs as "Task exception was never retrieved". The done-callback below
    reads (and thereby marks retrieved) whatever ``primer`` resolves to,
    independent of whether the iterator is ever iterated.
    """
    agen = client.raw_lines()
    primer = asyncio.ensure_future(agen.__anext__())
    primer.add_done_callback(lambda t: t.exception())
    await asyncio.sleep(0)  # let `primer` run up to its first suspension

    async def _iterate() -> AsyncIterator[Message]:
        try:
            first = await primer
        except StopAsyncIteration:
            return
        yield Message.parse(first)
        async for line in agen:
            yield Message.parse(line)

    return _iterate()


async def _graceful_quit(client: AgentClient) -> None:
    """Send a clean QUIT (best-effort) then tear down the connection.

    Safe to call unconditionally, including after a failed ``connect()`` —
    :attr:`AgentClient.connected` is ``False`` in that case so the QUIT send
    is skipped, but ``close()`` still runs, which is what actually retires
    any ``raw_lines()`` primer task a caller armed via
    :func:`_armed_raw_messages` before the failed connect.
    """
    if client.connected:
        with contextlib.suppress(ConnectionError):
            await client.send_raw("QUIT :agentirc-cli")
    await client.close()


async def _wait_for_join(
    raw_iter: AsyncIterator[Message],
    channel: str,
    timeout: float = _REPLY_TIMEOUT_SECONDS,
) -> tuple[bool, str | None]:
    """Consume ``raw_iter`` until the server confirms (or rejects) a JOIN.

    Returns ``(confirmed, error_token)``. This is more than cosmetic: the
    server's own JOIN handler echoes the JOIN line back to every member of
    the channel *including the joiner itself* (see ``client.py``'s
    ``_handle_join``). If this process closes its socket before that
    self-echo write completes, the write raises ``ConnectionError`` on the
    server side, which is uncaught in the server's per-client read loop and
    silently ends that connection's handling — dropping any of *our*
    already-sent-but-not-yet-read lines (e.g. a PRIVMSG queued right behind
    the JOIN) along with it. Waiting for the join confirmation numeric
    (``RPL_ENDOFNAMES``) before doing anything else on a freshly-joined
    channel closes that race: by the time we see it, the server has already
    finished writing back to us, so a subsequent close can't interrupt it.
    """
    confirmed = False
    error_token: str | None = None
    try:
        async with asyncio.timeout(timeout):
            async for msg in raw_iter:
                cmd = msg.command.upper()
                if cmd == RPL_ENDOFNAMES and len(msg.params) > 1 and msg.params[1] == channel:
                    confirmed = True
                    break
                if cmd == "NOTICE" and ERROR_TAG in msg.tags:
                    error_token = msg.tags[ERROR_TAG]
                    break
    except (asyncio.TimeoutError, TimeoutError):
        pass
    return confirmed, error_token


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


async def _send_main(args: argparse.Namespace) -> int:
    target = args.target
    nick = args.nick or _default_nick()
    is_channel = target.startswith("#")
    channels = [target] if is_channel else None
    client = AgentClient(args.host, args.port, nick, channels=channels, reconnect=False)
    # Armed before connect() in both modes — channel sends need the JOIN
    # confirmation sequence, DM sends need to catch a 401 (see below).
    raw_iter = await _armed_raw_messages(client)

    rc = await _connect_or_hint(client, args.host, args.port, "send")
    if rc is not None:
        await _graceful_quit(client)
        return rc

    if is_channel:
        confirmed, error_token = await _wait_for_join(raw_iter, target)
        if error_token is not None:
            print(
                f"agentirc send: server rejected join to {target} ({error_token})",
                file=sys.stderr,
            )
            await _graceful_quit(client)
            return 1
        if not confirmed:
            print(
                f"agentirc send: timed out waiting to join {target}", file=sys.stderr
            )
            await _graceful_quit(client)
            return 1

    try:
        await client.send(target, args.text)
    except ConnectionError as exc:
        print(f"agentirc send: failed to send: {exc}", file=sys.stderr)
        await _graceful_quit(client)
        return 1

    if not is_channel:
        # IRC has no positive delivery ack, but an absent recipient does get
        # a definite ERR_NOSUCHNICK — and the server drops the DM (offline
        # DMs are deliberately unstored). A short bounded listen turns that
        # silent loss into a non-zero exit; silence within the window is
        # taken as delivered.
        error = await _wait_for_dm_error(raw_iter, target, timeout=0.6)
        if error is not None:
            print(
                f"agentirc send: no such nick {target}"
                " (recipient not connected; the DM was not delivered or stored)",
                file=sys.stderr,
            )
            await _graceful_quit(client)
            return 1

    await _graceful_quit(client)
    return 0


async def _wait_for_dm_error(
    raw_iter: AsyncIterator[Message], target: str, timeout: float
) -> Message | None:
    """Watch the raw stream up to ``timeout`` for a 401 naming ``target``."""
    with contextlib.suppress(TimeoutError, StopAsyncIteration):
        async with asyncio.timeout(timeout):
            while True:
                msg = await raw_iter.__anext__()
                if msg.command == ERR_NOSUCHNICK and target in msg.params:
                    return msg
    return None


def cmd_send(args: argparse.Namespace) -> int:
    return asyncio.run(_send_main(args))


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


async def _join_main(args: argparse.Namespace) -> int:
    channel = args.channel
    if not channel.startswith("#"):
        print("agentirc join: channel must start with '#'", file=sys.stderr)
        return 2

    nick = args.nick or _default_nick()
    client = AgentClient(args.host, args.port, nick, channels=[channel], reconnect=False)
    raw_iter = await _armed_raw_messages(client)  # armed before connect() — see _armed_raw_messages

    rc = await _connect_or_hint(client, args.host, args.port, "join")
    if rc is not None:
        await _graceful_quit(client)
        return rc

    try:
        confirmed, error_token = await _wait_for_join(raw_iter, channel)
    finally:
        await _graceful_quit(client)

    if error_token is not None:
        print(
            f"agentirc join: server rejected join to {channel} ({error_token})",
            file=sys.stderr,
        )
        return 1
    if not confirmed:
        print(
            f"agentirc join: timed out waiting for join confirmation on {channel}",
            file=sys.stderr,
        )
        return 1

    print(f"Joined {channel} as {nick}")
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    return asyncio.run(_join_main(args))


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


async def _collect_history(
    raw_iter: AsyncIterator[Message],
    channel: str,
    timeout: float = _REPLY_TIMEOUT_SECONDS,
) -> tuple[list[Message], str | None, str | None]:
    """Consume ``raw_iter`` until ``HISTORYEND`` for *channel*.

    Returns ``(history_lines, next_cursor, error_token)``. ``error_token``
    is ``"timeout"`` if no reply arrived in time, an ``ERROR_TAG`` value if
    the server rejected the request, or ``None`` on a clean completion.
    """
    lines: list[Message] = []
    next_cursor: str | None = None
    try:
        async with asyncio.timeout(timeout):
            async for msg in raw_iter:
                cmd = msg.command.upper()
                if cmd == "HISTORY" and msg.params and msg.params[0] == channel:
                    lines.append(msg)
                elif cmd == "HISTORYEND" and msg.params and msg.params[0] == channel:
                    if len(msg.params) > 1:
                        next_cursor = msg.params[1]
                    return lines, next_cursor, None
                elif cmd == "NOTICE" and ERROR_TAG in msg.tags:
                    return lines, next_cursor, msg.tags[ERROR_TAG]
    except (asyncio.TimeoutError, TimeoutError):
        return lines, next_cursor, "timeout"
    return lines, next_cursor, "timeout"


async def _read_main(args: argparse.Namespace) -> int:
    channel = args.channel
    nick = args.nick or _default_nick()
    since = args.since
    last = args.last
    if since is None and last is None:
        last = DEFAULT_READ_LAST

    client = AgentClient(args.host, args.port, nick, reconnect=False)
    raw_iter = await _armed_raw_messages(client)  # armed before connect() — see _armed_raw_messages

    rc = await _connect_or_hint(client, args.host, args.port, "read")
    if rc is not None:
        await _graceful_quit(client)
        return rc

    try:
        if since is not None:
            await client.send_raw(f"HISTORY SINCE {channel} {since}")
        else:
            await client.send_raw(f"HISTORY RECENT {channel} {last}")

        lines, next_cursor, error_token = await _collect_history(raw_iter, channel)
    finally:
        await _graceful_quit(client)

    if error_token == "timeout":
        print(
            f"agentirc read: timed out waiting for HISTORY reply from {channel}",
            file=sys.stderr,
        )
        return 1
    if error_token is not None:
        print(
            f"agentirc read: server rejected HISTORY request ({error_token})",
            file=sys.stderr,
        )
        return 1

    for msg in lines:
        if len(msg.params) < 4:
            continue  # malformed — shouldn't happen per the HISTORY wire contract
        _, hnick, ts, text = msg.params[0], msg.params[1], msg.params[2], msg.params[3]
        msgid = msg.tags.get(MSGID_TAG)
        _emit_line(ts, hnick, text, msgid, args.json)

    if since is not None:
        print(f"next-cursor: {next_cursor}", file=sys.stderr)
        if args.json:
            print(json.dumps({"next_cursor": next_cursor}))

    return 0


def cmd_read(args: argparse.Namespace) -> int:
    return asyncio.run(_read_main(args))


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


async def _watch_main(args: argparse.Namespace) -> int:
    channel = args.channel
    nick = args.nick or _default_nick()
    is_channel = channel.startswith("#")
    # Auto-reconnect ON (the AgentClient default) — watch is meant to run
    # unattended and ride out transient server-side blips. A bare-nick target
    # is a DM watch: nothing to JOIN.
    client = AgentClient(
        args.host,
        args.port,
        nick,
        channels=[channel] if is_channel else None,
        reconnect=True,
    )

    rc = await _connect_or_hint(client, args.host, args.port, "watch")
    if rc is not None:
        await _graceful_quit(client)
        return rc

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handler_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_installed = True
    except RuntimeError:
        # Windows / no running loop signal support — SIGINT falls back to
        # the interpreter's default KeyboardInterrupt, which unwinds
        # asyncio.run() and skips straight to our caller's cleanup.
        pass

    async def _consume() -> None:
        async for msg in client.messages():
            if is_channel:
                if msg.channel != channel:
                    continue
            elif msg.channel is not None or msg.sender != channel:
                # DM watch: IncomingMessage.channel is None for DMs, so
                # match on the sending peer instead.
                continue
            ts = msg.tags.get(SERVER_TIME_TAG) or f"{time.time():.6f}"
            msgid = msg.tags.get(MSGID_TAG)
            _emit_line(ts, msg.sender, msg.text, msgid, args.json)

    consumer = asyncio.ensure_future(_consume())
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({consumer, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (consumer, stopper):
            if not task.done():
                task.cancel()
        await asyncio.gather(consumer, stopper, return_exceptions=True)
        if handler_installed:
            loop.remove_signal_handler(signal.SIGINT)
        await _graceful_quit(client)

    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_watch_main(args))
    except KeyboardInterrupt:
        # Fallback path for platforms where add_signal_handler isn't
        # available (see _watch_main) — still exit cleanly on SIGINT.
        return 0


__all__ = ["cmd_send", "cmd_read", "cmd_watch", "cmd_join"]
