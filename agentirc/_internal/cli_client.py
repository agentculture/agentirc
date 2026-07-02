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
from typing import Any

from agentirc._internal.protocol.message import Message
from agentirc.agent_client import AgentClient
from agentirc.protocol import ERROR_TAG, MSGID_TAG, RPL_ENDOFNAMES, SERVER_TIME_TAG

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


async def _graceful_quit(client: AgentClient) -> None:
    """Send a clean QUIT (best-effort) then tear down the connection."""
    if client.connected:
        with contextlib.suppress(ConnectionError):
            await client.send_raw("QUIT", "agentirc-cli")
    await client.close()


async def _wait_for_join(
    raw_iter: "asyncio.AsyncIterator[Message]",
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
    # Armed before connect() whenever we might auto-join — see _wait_for_join.
    raw_iter = client.raw_lines() if is_channel else None

    rc = await _connect_or_hint(client, args.host, args.port, "send")
    if rc is not None:
        return rc

    if is_channel:
        assert raw_iter is not None
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

    await _graceful_quit(client)
    return 0


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
    raw_iter = client.raw_lines()  # armed before connect() — see AgentClient docstring

    rc = await _connect_or_hint(client, args.host, args.port, "join")
    if rc is not None:
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
    raw_iter: "asyncio.AsyncIterator[Message]",
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
    raw_iter = client.raw_lines()  # armed before connect() — see AgentClient docstring

    rc = await _connect_or_hint(client, args.host, args.port, "read")
    if rc is not None:
        return rc

    try:
        if since is not None:
            await client.send_raw("HISTORY", "SINCE", channel, since)
        else:
            await client.send_raw("HISTORY", "RECENT", channel, str(last))

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
    # Auto-reconnect ON (the AgentClient default) — watch is meant to run
    # unattended and ride out transient server-side blips.
    client = AgentClient(args.host, args.port, nick, channels=[channel], reconnect=True)

    rc = await _connect_or_hint(client, args.host, args.port, "watch")
    if rc is not None:
        return rc

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handler_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        handler_installed = True
    except (NotImplementedError, RuntimeError):
        # Windows / no running loop signal support — SIGINT falls back to
        # the interpreter's default KeyboardInterrupt, which unwinds
        # asyncio.run() and skips straight to our caller's cleanup.
        pass

    async def _consume() -> None:
        async for msg in client.messages():
            if msg.channel != channel:
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
