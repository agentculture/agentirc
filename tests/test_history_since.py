"""HISTORY SINCE cursor-based pagination (agent-accessibility, task t6).

Covers the cursor codec, deterministic non-overlapping pagination on both the
in-memory-deque and SQLite-backed paths, tie-breaking under identical
timestamps, a retention-prune boundary crossed mid-pagination, the
``invalid-cursor`` stable error token, msgid/time message-tags passthrough on
replay lines, and a byte-unchanged sanity check for RECENT/SEARCH (the
authoritative golden-file lock-in for those two lives, untouched, in
``tests/test_wire_format_envelope.py``).

See ``agentirc/skills/history.py``'s module docstring for the cursor
encoding and the SQLite-vs-deque authoritative-backend decision this file
exercises.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time

import pytest

from agentirc._internal.protocol.message import Message
from agentirc.config import ServerConfig
from agentirc.ircd import IRCd
from agentirc.protocol import (
    ERROR_TAG,
    ERROR_TOKEN_INVALID_CURSOR,
    MSGID_TAG,
    SERVER_TIME_TAG,
)
from agentirc.skill import Event, EventType
from agentirc.skills.history import _decode_cursor, _encode_cursor
from tests.conftest import IRCTestClient

_SERVER_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


async def _register(make_client, nick: str, *, tags: bool = False):
    client = await make_client(nick, "u")
    client.nick = nick
    if tags:
        await client.send("CAP REQ :message-tags")
        await client.recv_until("CAP")
    return client


async def _since_page(
    client, channel: str, cursor: str, limit: int | None = None
) -> tuple[list[Message], str]:
    """Send one HISTORY SINCE call, return (parsed HISTORY lines, next-cursor)."""
    if limit is None:
        await client.send(f"HISTORY SINCE {channel} {cursor}")
    else:
        await client.send(f"HISTORY SINCE {channel} {cursor} {limit}")
    joined = await client.recv_until("HISTORYEND")
    lines = joined.split("\r\n") if joined else []
    assert lines, "expected at least a HISTORYEND line"
    end_msg = Message.parse(lines[-1])
    assert end_msg.command == "HISTORYEND"
    next_cursor = end_msg.params[-1]
    history_msgs = [Message.parse(ln) for ln in lines[:-1]]
    return history_msgs, next_cursor


async def _sweep_all(
    client, channel: str, limit: int, *, max_pages: int = 1000
) -> list[Message]:
    """Page SINCE with a fixed limit until an empty page; return all entries in order."""
    collected: list[Message] = []
    cursor = "*"
    seen_cursors = set()
    for _ in range(max_pages):
        msgs, next_cursor = await _since_page(client, channel, cursor, limit)
        if not msgs:
            break
        collected.extend(msgs)
        assert (
            next_cursor not in seen_cursors or next_cursor == cursor
        ), "cursor must advance (or hold steady only on an empty page)"
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return collected


async def _emit_messages(
    ircd, channel: str, texts: list[str], *, base_ts: float, step: float = 1.0
):
    for i, text in enumerate(texts):
        await ircd.emit_event(
            Event(
                type=EventType.MESSAGE,
                channel=channel,
                nick="testserv-alice",
                data={"text": text},
                timestamp=base_ts + i * step,
            )
        )


async def _boot_ircd(data_dir: str | None = None, **extra) -> IRCd:
    config = ServerConfig(
        name="testserv", host="127.0.0.1", port=0, data_dir=data_dir or "", **extra
    )
    ircd = IRCd(config)
    await ircd.start()
    ircd.config.port = ircd._server.sockets[0].getsockname()[1]
    return ircd


async def _wire_client(ircd, nick: str) -> IRCTestClient:
    reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
    client = IRCTestClient(reader, writer)
    await client.send(f"NICK {nick}")
    await client.send("USER u 0 * :u")
    await client.recv_all(timeout=0.5)
    client.nick = nick
    return client


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def test_cursor_roundtrip():
    token = _encode_cursor(1000.5, 42)
    assert _decode_cursor(token) == (1000.5, 42)


def test_cursor_begin_tokens_decode_to_none():
    assert _decode_cursor("*") is None
    assert _decode_cursor("") is None


def test_cursor_opaque_distinct_pairs_distinct_tokens():
    a = _encode_cursor(1000.0, 1)
    b = _encode_cursor(1000.0, 2)
    c = _encode_cursor(1000.1, 1)
    assert len({a, b, c}) == 3


@pytest.mark.parametrize(
    "bad",
    [
        "bm9jb2xvbmhlcmU=",  # valid base64, decodes to "nocolonhere" — no ':'
        "!",  # discarded-to-empty by lenient b64 decode, still no ':'
        "not-numbers-at-all",
    ],
)
def test_cursor_decode_malformed_raises_value_error(bad):
    with pytest.raises(ValueError):
        _decode_cursor(bad)


# ---------------------------------------------------------------------------
# Full pagination sweep — memory-only backend (no data_dir)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3, 7, 100])
async def test_since_full_sweep_memory_backend(server, make_client, limit):
    """Paging with any page size until empty sees every message exactly once, in order."""
    channel = f"#sweep-mem-{limit}"
    texts = [f"msg {i}" for i in range(23)]
    base_ts = time.time()
    await _emit_messages(server, channel, texts, base_ts=base_ts)
    await asyncio.sleep(0.05)

    client = await _register(make_client, f"testserv-sweeper-{limit}")
    collected = await _sweep_all(client, channel, limit)

    got_texts = [m.params[-1].lstrip(":") for m in collected]
    assert got_texts == texts
    assert len(got_texts) == len(set(got_texts))


@pytest.mark.asyncio
async def test_since_empty_channel_returns_begin_cursor(server, make_client):
    client = await _register(make_client, "testserv-emptychan")
    msgs, next_cursor = await _since_page(client, "#never-touched-since", "*")
    assert msgs == []
    assert next_cursor == "*"


@pytest.mark.asyncio
async def test_since_caught_up_echoes_cursor_unchanged(server, make_client):
    channel = "#caught-up"
    await _emit_messages(server, channel, ["only one"], base_ts=time.time())
    await asyncio.sleep(0.05)

    client = await _register(make_client, "testserv-caughtup")
    msgs, cursor1 = await _since_page(client, channel, "*", limit=10)
    assert len(msgs) == 1

    # Nothing new has happened — polling again with the returned cursor
    # should yield an empty page and echo the same cursor back.
    msgs2, cursor2 = await _since_page(client, channel, cursor1, limit=10)
    assert msgs2 == []
    assert cursor2 == cursor1


# ---------------------------------------------------------------------------
# Cursor determinism under identical timestamps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_deterministic_under_identical_timestamps_memory(
    server, make_client
):
    channel = "#tie-break-mem"
    same_ts = time.time()
    texts = [f"tied {i}" for i in range(6)]
    for text in texts:
        await server.emit_event(
            Event(
                type=EventType.MESSAGE,
                channel=channel,
                nick="testserv-alice",
                data={"text": text},
                timestamp=same_ts,
            )
        )
    await asyncio.sleep(0.05)

    client = await _register(make_client, "testserv-tiebreak")
    collected = await _sweep_all(client, channel, limit=2)
    got_texts = [m.params[-1].lstrip(":") for m in collected]
    # Insertion order is preserved via the monotonic id tie-break even
    # though every entry shares one timestamp.
    assert got_texts == texts


@pytest.mark.asyncio
async def test_since_deterministic_under_identical_timestamps_sqlite():
    with tempfile.TemporaryDirectory() as data_dir:
        ircd = await _boot_ircd(data_dir=data_dir)
        channel = "#tie-break-sql"
        same_ts = time.time()
        texts = [f"tied {i}" for i in range(6)]
        for text in texts:
            await ircd.emit_event(
                Event(
                    type=EventType.MESSAGE,
                    channel=channel,
                    nick="testserv-alice",
                    data={"text": text},
                    timestamp=same_ts,
                )
            )
        await asyncio.sleep(0.05)

        client = await _wire_client(ircd, "testserv-tiebreaksql")
        collected = await _sweep_all(client, channel, limit=2)
        got_texts = [m.params[-1].lstrip(":") for m in collected]
        assert got_texts == texts

        await client.close()
        await ircd.stop()


# ---------------------------------------------------------------------------
# Full pagination sweep — SQLite-backed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 4, 100])
async def test_since_full_sweep_sqlite_backend(limit):
    with tempfile.TemporaryDirectory() as data_dir:
        ircd = await _boot_ircd(data_dir=data_dir)
        channel = f"#sweep-sql-{limit}"
        texts = [f"row {i}" for i in range(17)]
        await _emit_messages(ircd, channel, texts, base_ts=time.time())
        await asyncio.sleep(0.05)

        client = await _wire_client(ircd, f"testserv-sqlsweep{limit}")
        collected = await _sweep_all(client, channel, limit)
        got_texts = [m.params[-1].lstrip(":") for m in collected]
        assert got_texts == texts
        assert len(got_texts) == len(set(got_texts))

        await client.close()
        await ircd.stop()


@pytest.mark.asyncio
async def test_since_sqlite_pagination_survives_restart():
    """Entries appended before a restart remain pageable afterwards via SINCE."""
    with tempfile.TemporaryDirectory() as data_dir:
        ircd = await _boot_ircd(data_dir=data_dir)
        channel = "#restart-since"
        texts = [f"pre-restart {i}" for i in range(5)]
        await _emit_messages(ircd, channel, texts, base_ts=time.time())
        await asyncio.sleep(0.05)
        await ircd.stop()

        ircd2 = await _boot_ircd(data_dir=data_dir)
        client = await _wire_client(ircd2, "testserv-restartsince")
        collected = await _sweep_all(client, channel, limit=2)
        got_texts = [m.params[-1].lstrip(":") for m in collected]
        assert got_texts == texts

        await client.close()
        await ircd2.stop()


# ---------------------------------------------------------------------------
# Retention-prune boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_pagination_across_prune_boundary():
    """Old entries pruned on restart don't crash or duplicate a SINCE sweep."""
    with tempfile.TemporaryDirectory() as data_dir:
        ircd = await _boot_ircd(data_dir=data_dir)
        channel = "#prune-since"
        now = time.time()
        old_ts = now - 86400 * 60  # 60 days ago — beyond the 30-day default retention
        await _emit_messages(
            ircd, channel, [f"old {i}" for i in range(3)], base_ts=old_ts
        )
        await _emit_messages(ircd, channel, [f"new {i}" for i in range(4)], base_ts=now)
        await asyncio.sleep(0.05)
        await ircd.stop()

        # Restart triggers HistorySkill._restore_history -> store.prune(30) —
        # the "old" rows are deleted; surviving "new" rows keep their ids
        # (gaps in the id sequence), which the SINCE range query must
        # tolerate without crashing or skipping/duplicating anything.
        ircd2 = await _boot_ircd(data_dir=data_dir)
        client = await _wire_client(ircd2, "testserv-pruner")
        collected = await _sweep_all(client, channel, limit=2)
        got_texts = [m.params[-1].lstrip(":") for m in collected]

        assert got_texts == [f"new {i}" for i in range(4)]
        assert all("old" not in t for t in got_texts)
        assert len(got_texts) == len(set(got_texts))

        await client.close()
        await ircd2.stop()


# ---------------------------------------------------------------------------
# Bad cursor -> invalid-cursor token (structural check; the parameterized
# error-token inventory test lives in tests/test_error_tokens.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_invalid_cursor_gets_notice_and_no_historyend(server, make_client):
    client = await _register(make_client, "testserv-badcursor", tags=True)
    await client.send("HISTORY SINCE #whatever bm9jb2xvbmhlcmU=")
    line = await client.recv()
    msg = Message.parse(line)
    assert msg.command == "NOTICE"
    assert msg.tags.get(ERROR_TAG) == ERROR_TOKEN_INVALID_CURSOR
    # No HISTORYEND follows a rejected cursor.
    leftover = await client.recv_all(timeout=0.2)
    assert not any("HISTORYEND" in ln for ln in leftover)


# ---------------------------------------------------------------------------
# msgid / message-tags passthrough on SINCE replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_replay_tags_msgid_and_time_for_message_tags_client(
    server, make_client
):
    channel = "#tagged-since"
    await server.emit_event(
        Event(
            type=EventType.MESSAGE,
            channel=channel,
            nick="testserv-alice",
            data={"text": "hello", "msgid": "msgid-abc-123"},
            timestamp=1_700_000_000.0,
        )
    )
    await asyncio.sleep(0.05)

    tagged = await _register(make_client, "testserv-tagged-since", tags=True)
    plain = await _register(make_client, "testserv-plain-since", tags=False)

    tagged_msgs, _ = await _since_page(tagged, channel, "*")
    plain_msgs, _ = await _since_page(plain, channel, "*")

    assert len(tagged_msgs) == 1
    assert len(plain_msgs) == 1

    tmsg = tagged_msgs[0]
    pmsg = plain_msgs[0]

    assert tmsg.tags.get(MSGID_TAG) == "msgid-abc-123"
    assert _SERVER_TIME_RE.match(tmsg.tags.get(SERVER_TIME_TAG, "")), tmsg.tags

    assert not pmsg.tags


@pytest.mark.asyncio
async def test_since_replay_untagged_when_entry_has_no_msgid(server, make_client):
    """Entries without a stored msgid (e.g. lifecycle/system events) replay untagged
    even for a message-tags-negotiated client."""
    channel = "#no-msgid-since"
    await server.emit_event(
        Event(
            type=EventType.MESSAGE,
            channel=channel,
            nick="testserv-alice",
            data={"text": "no msgid here"},  # no "msgid" key at all
            timestamp=1_700_000_100.0,
        )
    )
    await asyncio.sleep(0.05)

    tagged = await _register(make_client, "testserv-nomsgid-since", tags=True)
    msgs, _ = await _since_page(tagged, channel, "*")
    assert len(msgs) == 1
    assert not msgs[0].tags


# ---------------------------------------------------------------------------
# RECENT / SEARCH byte-unchanged sanity check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_still_untagged_and_no_cursor_even_with_message_tags(
    server, make_client
):
    """RECENT keeps its pre-t6 shape: no tags, HISTORYEND's trailing param is the
    literal "End of history" string, never a cursor — even for a client that
    negotiated message-tags. The golden-file lock-in lives in
    tests/test_wire_format_envelope.py (untouched by this task); this is a
    belt-and-suspenders regression guard scoped to the SINCE addition.
    """
    channel = "#recent-unchanged"
    await server.emit_event(
        Event(
            type=EventType.MESSAGE,
            channel=channel,
            nick="testserv-alice",
            data={"text": "hi", "msgid": "should-not-appear"},
            timestamp=time.time(),
        )
    )
    await asyncio.sleep(0.05)

    client = await _register(make_client, "testserv-recent-unchanged", tags=True)
    await client.send(f"HISTORY RECENT {channel} 10")
    joined = await client.recv_until("HISTORYEND")
    lines = joined.split("\r\n")

    end_msg = Message.parse(lines[-1])
    assert end_msg.params == [channel, "End of history"]

    history_msgs = [Message.parse(ln) for ln in lines[:-1]]
    assert len(history_msgs) == 1
    assert not history_msgs[0].tags


@pytest.mark.asyncio
async def test_search_still_untagged_with_message_tags(server, make_client):
    channel = "#search-unchanged"
    await server.emit_event(
        Event(
            type=EventType.MESSAGE,
            channel=channel,
            nick="testserv-alice",
            data={"text": "findme", "msgid": "should-not-appear-either"},
            timestamp=time.time(),
        )
    )
    await asyncio.sleep(0.05)

    client = await _register(make_client, "testserv-search-unchanged", tags=True)
    await client.send(f"HISTORY SEARCH {channel} :findme")
    joined = await client.recv_until("HISTORYEND")
    lines = joined.split("\r\n")

    history_msgs = [Message.parse(ln) for ln in lines[:-1]]
    assert len(history_msgs) == 1
    assert not history_msgs[0].tags
