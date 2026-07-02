"""Client-facing BACKFILL (task t8).

Makes real the recovery path docs/extension-api.md's Backpressure section
promises: after an ``EVENTERR <sub-id> :backpressure-overflow`` drops a
subscription, a bot-CAP client re-subscribes and issues ``BACKFILL`` to
replay missed ``message`` events from the history store. Prior to this
task ``BACKFILL`` was implemented server-to-server only
(``agentirc/server_link.py``); a client sending it got
``ERR_UNKNOWNCOMMAND``.

See ``agentirc/client.py``'s ``_handle_backfill`` docstring for the exact
wire contract this file exercises.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from agentirc._internal.protocol.message import Message
from agentirc.config import ServerConfig
from agentirc.ircd import IRCd
from agentirc.protocol import MSGID_TAG
from tests.conftest import IRCTestClient


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


async def _make_bot(make_client, nick: str, user: str) -> IRCTestClient:
    """Connect, register, negotiate ``agentirc.io/bot``, drain the ACK."""
    c = await make_client(nick, user)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_until("CAP")
    await asyncio.sleep(0.05)
    await c.recv_all(timeout=0.2)
    return c


async def _wire_client(ircd: IRCd, nick: str, *, bot: bool = False, tags: bool = False):
    reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
    client = IRCTestClient(reader, writer)
    if bot:
        await client.send("CAP REQ :agentirc.io/bot")
        await client.recv_until("CAP")
    if tags:
        await client.send("CAP REQ :message-tags")
        await client.recv_until("CAP")
    await client.send(f"NICK {nick}")
    await client.send("USER u 0 * :u")
    await client.recv_all(timeout=0.5)
    client.nick = nick
    return client


def _event_lines(block: str) -> list[str]:
    return [ln for ln in block.split("\r\n") if " EVENT backfill " in ln]


def _backfillend_line(block: str) -> str:
    return next(ln for ln in block.split("\r\n") if " BACKFILLEND " in ln)


def _parse_backfill_event(line: str) -> tuple[str, dict]:
    """Parse one BACKFILL-replayed ``EVENT`` line -> ``(channel, envelope)``."""
    parts = line.split(" ", 6)
    assert parts[1] == "EVENT", line
    assert parts[2] == "backfill", line
    assert parts[3] == "message", line
    channel = parts[4]
    b64 = parts[6]
    if b64.startswith(":"):
        b64 = b64[1:]
    return channel, json.loads(base64.b64decode(b64))


# ---------------------------------------------------------------------------
# Gating — mirrors EVENTSUB's bot-cap / registration gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_requires_bot_cap(server, make_client):
    c = await make_client("testserv-b8-nocap", "u")
    await c.send("BACKFILL #room *")
    line = await c.recv_until("EVENTERR")
    assert "EVENTERR #room :bot-capability-required" in line


@pytest.mark.asyncio
async def test_backfill_requires_registration(server, make_client):
    c = await make_client()
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_until("CAP")
    await c.send("BACKFILL #room *")
    line = await c.recv_until("EVENTERR")
    assert "EVENTERR #room :not-registered" in line


@pytest.mark.asyncio
async def test_backfill_missing_params(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8-missing", "u")
    await bot.send("BACKFILL #room")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR #room :missing-params" in line


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_no_such_channel(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8-nsc", "u")
    await bot.send("BACKFILL #never-existed *")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR #never-existed :no-such-channel" in line


@pytest.mark.asyncio
async def test_backfill_rejects_bare_nick_and_dm_key_targets(server, make_client):
    """Neither a bare nick nor a directly-named ``@dm:`` key ever resolves —
    only ``#``-prefixed, currently-existing channel names (or ``*``) do.
    This is the mechanism that keeps DM history unreachable via BACKFILL."""
    bot = await _make_bot(make_client, "testserv-b8-bare", "u")

    await bot.send("BACKFILL somenick *")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR somenick :no-such-channel" in line

    await bot.send("BACKFILL @dm:alice:bob *")
    line2 = await bot.recv_until("EVENTERR")
    assert "EVENTERR @dm:alice:bob :no-such-channel" in line2


@pytest.mark.asyncio
async def test_backfill_invalid_cursor(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8-badcur", "u")
    await bot.send("JOIN #b8badcur")
    await bot.recv_until("366")
    await bot.send("BACKFILL #b8badcur bm9jb2xvbmhlcmU=")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR #b8badcur :invalid-cursor" in line


@pytest.mark.asyncio
async def test_backfill_invalid_count(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8-badcount", "u")
    await bot.send("JOIN #b8badcount")
    await bot.recv_until("366")
    await bot.send("BACKFILL #b8badcount * notanumber")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR #b8badcount :invalid-count" in line

    await bot.send("BACKFILL #b8badcount * -1")
    line2 = await bot.recv_until("EVENTERR")
    assert "EVENTERR #b8badcount :invalid-count" in line2


# ---------------------------------------------------------------------------
# Replay content: message-only, lifecycle-skipped, cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_replays_messages_and_skips_lifecycle_entries(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8life", "u")
    await bot.send("JOIN #b8life")
    await bot.recv_until("366")  # bot's own (silent) join still lands a lifecycle entry

    alice = await make_client("testserv-b8lifealice", "u")
    await alice.send("JOIN #b8life")  # alice's join -> another lifecycle entry
    await alice.recv_until("366")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.1)

    await alice.send("PRIVMSG #b8life :hello there")
    await asyncio.sleep(0.1)
    await bot.recv_all(timeout=0.1)

    await bot.send("BACKFILL #b8life *")
    block = await bot.recv_until("BACKFILLEND")
    lines = _event_lines(block)
    assert len(lines) == 1

    channel, envelope = _parse_backfill_event(lines[0])
    assert channel == "#b8life"
    assert envelope["type"] == "message"
    assert envelope["channel"] == "#b8life"
    assert envelope["nick"] == "testserv-b8lifealice"
    assert envelope["data"]["text"] == "hello there"
    assert "msgid" in envelope["data"]

    end_line = _backfillend_line(block)
    end_parts = end_line.split(" ")
    assert end_parts[-2] == "#b8life"
    next_cursor = end_parts[-1]

    # Polling again with the returned cursor is caught-up: zero EVENT lines,
    # cursor echoed back unchanged — mirrors HISTORY SINCE's own semantics.
    await bot.send(f"BACKFILL #b8life {next_cursor}")
    block2 = await bot.recv_until("BACKFILLEND")
    assert _event_lines(block2) == []
    end_line2 = _backfillend_line(block2)
    assert end_line2.split(" ")[-1] == next_cursor


@pytest.mark.asyncio
async def test_backfill_star_scopes_to_callers_joined_channels(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8star", "u")
    await bot.send("JOIN #b8star-a")
    await bot.recv_until("366")
    await bot.send("JOIN #b8star-b")
    await bot.recv_until("366")

    alice = await make_client("testserv-b8staralice", "u")
    for ch in ("#b8star-a", "#b8star-b", "#b8star-c"):
        await alice.send(f"JOIN {ch}")
        await alice.recv_until("366")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.1)

    await alice.send("PRIVMSG #b8star-a :from a")
    await alice.send("PRIVMSG #b8star-b :from b")
    await alice.send("PRIVMSG #b8star-c :from c")  # bot never joined #b8star-c
    await asyncio.sleep(0.1)

    await bot.send("BACKFILL * *")
    block = await bot.recv_until("BACKFILLEND")
    lines = _event_lines(block)
    texts = {_parse_backfill_event(ln)[1]["data"]["text"] for ln in lines}
    assert texts == {"from a", "from b"}


@pytest.mark.asyncio
async def test_backfill_paginates_with_limit_without_gaps_or_duplicates(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8page", "u")
    await bot.send("JOIN #b8page")
    await bot.recv_until("366")

    alice = await make_client("testserv-b8pagealice", "u")
    await alice.send("JOIN #b8page")
    await alice.recv_until("366")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.1)

    texts = [f"msg {i}" for i in range(11)]
    for text in texts:
        await alice.send(f"PRIVMSG #b8page :{text}")
    await asyncio.sleep(0.2)

    collected: list[str] = []
    cursor = "*"
    seen_cursors: set[str] = set()
    for _ in range(50):
        await bot.send(f"BACKFILL #b8page {cursor} 3")
        block = await bot.recv_until("BACKFILLEND")
        lines = _event_lines(block)
        for ln in lines:
            _, envelope = _parse_backfill_event(ln)
            collected.append(envelope["data"]["text"])
        next_cursor = _backfillend_line(block).split(" ")[-1]
        assert next_cursor not in seen_cursors or next_cursor == cursor, (
            "cursor must advance (or hold steady only once caught up)"
        )
        if next_cursor == cursor and not lines:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    assert collected == texts
    assert len(collected) == len(set(collected))


# ---------------------------------------------------------------------------
# DM history is never reachable via BACKFILL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_never_replays_dm_history(server, make_client):
    bot = await _make_bot(make_client, "testserv-b8dm", "u")
    await bot.send("JOIN #b8dmchan")
    await bot.recv_until("366")

    alice = await make_client("testserv-b8dmalice", "u")
    await alice.send("JOIN #b8dmchan")
    await alice.recv_until("366")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.1)

    await alice.send("PRIVMSG #b8dmchan :public hello")
    await alice.send("PRIVMSG testserv-b8dm :super secret dm content")
    await asyncio.sleep(0.1)
    await bot.recv_all(timeout=0.2)  # drain the DM PRIVMSG itself (delivered normally)

    await bot.send("BACKFILL #b8dmchan *")
    block = await bot.recv_until("BACKFILLEND")
    lines = _event_lines(block)
    assert len(lines) == 1
    _, envelope = _parse_backfill_event(lines[0])
    assert envelope["data"]["text"] == "public hello"
    assert "super secret dm content" not in block

    await bot.send("BACKFILL * *")
    block2 = await bot.recv_until("BACKFILLEND")
    assert "super secret dm content" not in block2

    # Direct DM-key / bare-nick addressing is rejected outright.
    await bot.send("BACKFILL testserv-b8dmalice *")
    err = await bot.recv_until("EVENTERR")
    assert "EVENTERR testserv-b8dmalice :no-such-channel" in err


# ---------------------------------------------------------------------------
# Full overflow -> re-subscribe -> BACKFILL recovery walkthrough
# (docs/extension-api.md's "Recovering with BACKFILL" section, verified
# end-to-end against a live server, msgid-verified against HISTORY SINCE as
# a stationary witness independent of the subscription that overflowed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extension_api_backpressure_recovery_walkthrough():
    config = ServerConfig(
        name="testserv",
        host="127.0.0.1",
        port=0,
        webhook_port=0,
        event_subscription_queue_max=3,
    )
    ircd = IRCd(config)
    await ircd.start()
    ircd.config.port = ircd._server.sockets[0].getsockname()[1]

    try:
        bot = await _wire_client(ircd, "testserv-b8walk", bot=True)
        await bot.send("JOIN #b8walk")
        await bot.recv_until("366")

        alice = await _wire_client(ircd, "testserv-b8walkalice")
        await alice.send("JOIN #b8walk")
        await alice.recv_until("366")
        await asyncio.sleep(0.05)
        await bot.recv_all(timeout=0.1)

        # --- Subscribing to events ---
        await bot.send("EVENTSUB msgs type=message channel=#b8walk")
        await asyncio.sleep(0.05)
        await bot.recv_all(timeout=0.1)

        # A live event, received normally, before anything goes wrong.
        await alice.send("PRIVMSG #b8walk :live one")
        line = await bot.recv_until("EVENT")
        live_line = next(ln for ln in line.split("\r\n") if " EVENT msgs " in ln)
        assert " message #b8walk testserv-b8walkalice " in live_line

        # --- Backpressure: force a deterministic overflow ---
        # Cancel the drain task so the queue fills exactly at queue_max
        # instead of racing real-time delivery (same technique
        # tests/test_event_subscriptions.py's own backpressure test uses).
        bot_client = ircd.clients["testserv-b8walk"]
        sub = ircd.subscription_registry.get(bot_client, "msgs")
        assert sub is not None
        assert sub.drain_task is not None
        sub.drain_task.cancel()
        await asyncio.sleep(0.02)

        flood_count = ircd.subscription_registry.queue_max + 1
        flood_texts = [f"flood {i}" for i in range(flood_count)]
        for text in flood_texts:
            await alice.send(f"PRIVMSG #b8walk :{text}")
        await asyncio.sleep(0.2)

        overflow_line = await bot.recv_until("EVENTERR")
        assert "EVENTERR msgs :backpressure-overflow" in overflow_line

        # --- Recover: re-subscribe with a fresh sub-id, then BACKFILL ---
        await bot.send("EVENTSUB msgs2 type=message channel=#b8walk")
        await asyncio.sleep(0.05)
        await bot.recv_all(timeout=0.1)

        await bot.send("BACKFILL #b8walk *")
        block = await bot.recv_until("BACKFILLEND")
        event_lines = _event_lines(block)
        assert len(event_lines) == 1 + flood_count  # "live one" + every flood message

        replayed_texts = []
        replayed_msgids = []
        for ln in event_lines:
            channel, envelope = _parse_backfill_event(ln)
            assert channel == "#b8walk"
            assert envelope["nick"] == "testserv-b8walkalice"
            replayed_texts.append(envelope["data"]["text"])
            replayed_msgids.append(envelope["data"]["msgid"])

        assert replayed_texts == ["live one", *flood_texts]

        end_line = _backfillend_line(block)
        end_parts = end_line.split(" ")
        assert end_parts[-2] == "#b8walk"
        next_cursor = end_parts[-1]

        # A usable next-cursor: polling again is caught up (no gaps left).
        await bot.send(f"BACKFILL #b8walk {next_cursor}")
        block2 = await bot.recv_until("BACKFILLEND")
        assert _event_lines(block2) == []

        # --- Stationary witness: HISTORY SINCE, unaffected by the
        # subscription's overflow, independently confirms the exact same
        # msgids in the exact same order. ---
        witness = await _wire_client(ircd, "testserv-b8walkwitness", tags=True)
        await witness.send("HISTORY SINCE #b8walk *")
        hist_block = await witness.recv_until("HISTORYEND")
        hist_lines = hist_block.split("\r\n")
        hist_msgs = [Message.parse(ln) for ln in hist_lines[:-1]]
        # Filter out lifecycle entries (system-<server> nick) the same way
        # BACKFILL does, for an apples-to-apples comparison.
        witness_msgids = [
            m.tags.get(MSGID_TAG) for m in hist_msgs if not m.params[1].startswith("system-")
        ]
        assert witness_msgids == replayed_msgids
        await witness.close()
    finally:
        await ircd.stop()
