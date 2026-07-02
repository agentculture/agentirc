"""DM history storage and query surface (agent-accessibility, task t7).

DMs are stored in the SAME history store (deque + SQLite) as channels, under
a canonical pair key ``@dm:<nickA>:<nickB>`` (nicks lowercased and sorted —
see ``agentirc.skills.history._dm_pair_key``). The ``@dm:`` prefix can never
collide with a ``#``-prefixed channel key, so an exact-match store lookup
can never cross the DM/channel boundary in either direction.

Capture point: ``agentirc/client.py``'s ``_send_to_client`` (the DM relay
site) calls ``HistorySkill.record_dm`` directly — NOT via the ``on_event``
broadcast every registered skill receives, and NOT by widening the
``MESSAGE`` event it already emits. Two things this preserves, both
exercised below:

- ``tests/test_history.py::test_history_does_not_record_dms`` registers a
  *second*, independently-registered ``HistorySkill`` instance and asserts
  it never sees DM content via the event-broadcast path — storing through
  ``on_event`` would have broken that test. ``record_dm`` instead targets
  only the one instance that ``IRCd.get_skill_for_command("HISTORY")``
  resolves (the same instance real ``HISTORY`` queries are routed to).
- EVENTSUB subscriber visibility is governed entirely by the ``MESSAGE``
  event ``_send_to_client`` emits, which this task does not touch — see
  ``test_eventsub_dm_event_shape_unchanged_by_history_capture`` below.

Query surface: ``HISTORY RECENT/SEARCH/SINCE`` accept a non-``#`` target
meaning "my DMs with that nick" — the server canonicalizes
``{requesting client's own nick, target}`` into the pair key, so a
requester can only ever address a pair they themselves belong to. Directly
naming an ``@``-prefixed target (the internal key format) is rejected with
the existing ``no-such-channel`` token rather than a new one.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time

import pytest

from agentirc._internal.protocol.message import Message
from agentirc.config import ServerConfig
from agentirc.ircd import IRCd
from agentirc.protocol import (
    ERROR_TAG,
    ERROR_TOKEN_NO_SUCH_CHANNEL,
    MSGID_TAG,
    SERVER_TIME_TAG,
)
from agentirc.skills.history import _dm_pair_key
from tests.conftest import IRCTestClient

_SERVER_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------
# Small helpers (self-contained per this repo's test-file convention — see
# tests/test_history_since.py / tests/test_error_tokens.py for precedent)
# ---------------------------------------------------------------------------


async def _register(make_client, nick: str, *, tags: bool = False):
    client = await make_client(nick, "u")
    client.nick = nick
    if tags:
        await client.send("CAP REQ :message-tags")
        await client.recv_until("CAP")
    return client


async def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _history_subcmd(client, subcmd: str, target: str, arg: str) -> tuple[list[Message], Message]:
    """Send ``HISTORY <subcmd> <target> <arg>``; return (HISTORY lines, HISTORYEND)."""
    await client.send(f"HISTORY {subcmd} {target} {arg}")
    joined = await client.recv_until("HISTORYEND")
    lines = joined.split("\r\n") if joined else []
    assert lines, f"expected at least a HISTORYEND line, got {joined!r}"
    end = Message.parse(lines[-1])
    assert end.command == "HISTORYEND"
    msgs = [Message.parse(ln) for ln in lines[:-1]]
    return msgs, end


async def _recent(client, target: str, count: int = 100):
    return await _history_subcmd(client, "RECENT", target, str(count))


async def _search(client, target: str, term: str):
    return await _history_subcmd(client, "SEARCH", target, term)


async def _since(client, target: str, cursor: str):
    return await _history_subcmd(client, "SINCE", target, cursor)


async def _boot_ircd(data_dir: str) -> IRCd:
    config = ServerConfig(name="testserv", host="127.0.0.1", port=0, data_dir=data_dir)
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
# Capture: one pair key, both directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_lands_in_history_both_directions_one_pair_key(server, make_client):
    alice = await _register(make_client, "testserv-alice-p1")
    bob = await _register(make_client, "testserv-bob-p1")

    await alice.send("PRIVMSG testserv-bob-p1 :hi bob")
    await bob.recv()
    await bob.send("PRIVMSG testserv-alice-p1 :hi alice")
    await alice.recv()
    await asyncio.sleep(0.05)

    # Alice queries by Bob's bare nick — resolves to the shared pair key.
    msgs, end = await _recent(alice, "testserv-bob-p1", 10)
    assert [(m.params[1], m.params[3]) for m in msgs] == [
        ("testserv-alice-p1", "hi bob"),
        ("testserv-bob-p1", "hi alice"),
    ]
    # Wire reply echoes the literal requested target, not the internal key.
    assert end.params[0] == "testserv-bob-p1"

    # Bob sees the exact same pair, addressed by Alice's nick — one shared
    # store entry set regardless of who asks or which direction a message
    # went.
    msgs2, end2 = await _recent(bob, "testserv-alice-p1", 10)
    assert [(m.params[1], m.params[3]) for m in msgs2] == [
        ("testserv-alice-p1", "hi bob"),
        ("testserv-bob-p1", "hi alice"),
    ]
    assert end2.params[0] == "testserv-alice-p1"


@pytest.mark.asyncio
async def test_dm_search_finds_matching_entries_for_participants(server, make_client):
    alice = await _register(make_client, "testserv-alice-p2")
    bob = await _register(make_client, "testserv-bob-p2")

    await alice.send("PRIVMSG testserv-bob-p2 :the quick brown fox")
    await bob.recv()
    await alice.send("PRIVMSG testserv-bob-p2 :lazy dog sleeps")
    await bob.recv()
    await asyncio.sleep(0.05)

    msgs, _ = await _search(alice, "testserv-bob-p2", "fox")
    assert len(msgs) == 1
    assert msgs[0].params[3] == "the quick brown fox"

    # Bob (the recipient) can search the same pair from his side too.
    msgs2, _ = await _search(bob, "testserv-alice-p2", "dog")
    assert len(msgs2) == 1
    assert msgs2[0].params[3] == "lazy dog sleeps"


# ---------------------------------------------------------------------------
# Reconnect + SINCE msgid/time tags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_history_survives_reconnect_and_replays_with_msgid_tags(server, make_client):
    """A↔B exchange DMs; B disconnects; B reconnects and SINCE-replays the
    earlier exchange, tagged with msgid/time for a message-tags client."""
    alice = await _register(make_client, "testserv-alice-r1")
    bob = await _register(make_client, "testserv-bob-r1")

    await alice.send("PRIVMSG testserv-bob-r1 :first")
    await bob.recv()
    await bob.send("PRIVMSG testserv-alice-r1 :second")
    await alice.recv()
    await asyncio.sleep(0.05)

    await bob.close()
    assert await _wait_for(lambda: "testserv-bob-r1" not in server.clients)

    bob2 = await _register(make_client, "testserv-bob-r1", tags=True)
    msgs, end = await _since(bob2, "testserv-alice-r1", "*")

    texts = [m.params[3] for m in msgs]
    assert texts == ["first", "second"]
    assert end.params[0] == "testserv-alice-r1"

    for m in msgs:
        assert m.tags.get(MSGID_TAG), f"missing msgid tag on {m!r}"
        assert _SERVER_TIME_RE.match(m.tags.get(SERVER_TIME_TAG, "")), m.tags


@pytest.mark.asyncio
async def test_dm_since_replay_untagged_for_plain_client(server, make_client):
    """The same SINCE replay carries no tags for a client that never
    negotiated message-tags (byte-shape unaffected by DM history)."""
    alice = await _register(make_client, "testserv-alice-r2")
    bob = await _register(make_client, "testserv-bob-r2")

    await alice.send("PRIVMSG testserv-bob-r2 :hello")
    await bob.recv()
    await asyncio.sleep(0.05)

    msgs, _ = await _since(alice, "testserv-bob-r2", "*")
    assert len(msgs) == 1
    assert not msgs[0].tags


# ---------------------------------------------------------------------------
# Participant-only access: third parties never see someone else's DMs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_third_party_query_returns_only_own_empty_pair(server, make_client):
    alice = await _register(make_client, "testserv-alice-tp")
    bob = await _register(make_client, "testserv-bob-tp")
    carol = await _register(make_client, "testserv-carol-tp")

    await alice.send("PRIVMSG testserv-bob-tp :private stuff between a and b")
    await bob.recv()
    await asyncio.sleep(0.05)

    # Carol asks about "her DMs with Alice" — a real, distinct pair (Carol,
    # Alice) that has no messages in it. She must never see the Alice↔Bob
    # exchange.
    msgs, end = await _recent(carol, "testserv-alice-tp", 10)
    assert msgs == []
    assert end.params[0] == "testserv-alice-tp"

    msgs2, _ = await _since(carol, "testserv-alice-tp", "*")
    assert msgs2 == []

    msgs3, _ = await _search(carol, "testserv-alice-tp", "private")
    assert msgs3 == []


# ---------------------------------------------------------------------------
# Channel/DM store isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_history_never_returns_dm_entries_and_vice_versa(server, make_client):
    alice = await _register(make_client, "testserv-alice-iso")
    bob = await _register(make_client, "testserv-bob-iso")

    chan = "#dm-isolation"
    await alice.send(f"JOIN {chan}")
    await alice.recv_all(timeout=0.3)
    await bob.send(f"JOIN {chan}")
    await bob.recv_all(timeout=0.3)
    await alice.recv_all(timeout=0.3)

    await alice.send(f"PRIVMSG {chan} :channel text unique123")
    await bob.recv()
    await alice.send("PRIVMSG testserv-bob-iso :dm text unique456")
    await bob.recv()
    await asyncio.sleep(0.05)

    chan_msgs, _ = await _recent(alice, chan, 20)
    chan_texts = [m.params[3] for m in chan_msgs]
    assert "channel text unique123" in chan_texts
    assert "dm text unique456" not in chan_texts

    dm_msgs, _ = await _recent(alice, "testserv-bob-iso", 20)
    dm_texts = [m.params[3] for m in dm_msgs]
    assert "dm text unique456" in dm_texts
    assert "channel text unique123" not in dm_texts


# ---------------------------------------------------------------------------
# Direct @dm: addressing is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_at_dm_target_rejected_with_no_such_channel_token(server, make_client):
    tagged = await _register(make_client, "testserv-alice-rej", tags=True)
    plain = await _register(make_client, "testserv-plain-rej", tags=False)

    pair_key = _dm_pair_key("testserv-alice-rej", "testserv-bob-rej")
    assert pair_key.startswith("@dm:")

    await tagged.send(f"HISTORY RECENT {pair_key} 5")
    line = await tagged.recv()
    msg = Message.parse(line)
    assert msg.command == "403"
    assert msg.params[1] == pair_key
    assert msg.tags.get(ERROR_TAG) == ERROR_TOKEN_NO_SUCH_CHANNEL

    await plain.send(f"HISTORY RECENT {pair_key} 5")
    line2 = await plain.recv()
    msg2 = Message.parse(line2)
    assert msg2.command == "403"
    assert not msg2.tags


@pytest.mark.asyncio
async def test_direct_at_dm_target_rejected_for_since_and_search(server, make_client):
    client = await _register(make_client, "testserv-alice-rej2")
    pair_key = _dm_pair_key("testserv-alice-rej2", "testserv-bob-rej2")

    await client.send(f"HISTORY SINCE {pair_key} *")
    line = await client.recv()
    assert Message.parse(line).command == "403"
    # No HISTORYEND follows a rejected target.
    leftover = await client.recv_all(timeout=0.2)
    assert not any("HISTORYEND" in ln for ln in leftover)

    await client.send(f"HISTORY SEARCH {pair_key} foo")
    line2 = await client.recv()
    assert Message.parse(line2).command == "403"


# ---------------------------------------------------------------------------
# Retention prune covers DM entries (mirrors the t6 prune-boundary pattern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_prune_boundary_removes_old_entries_keeps_new(tmp_path):
    data_dir = str(tmp_path)
    ircd = await _boot_ircd(data_dir)
    skill = ircd.get_skill_for_command("HISTORY")
    assert skill is not None

    now = time.time()
    old_ts = now - 86400 * 60  # 60 days ago — beyond the 30-day default retention
    for i in range(3):
        skill.record_dm("testserv-alice-pr", "testserv-bob-pr", f"old {i}", old_ts + i, None)
    for i in range(4):
        skill.record_dm("testserv-alice-pr", "testserv-bob-pr", f"new {i}", now + i, None)
    await asyncio.sleep(0.05)
    await ircd.stop()

    # Restart triggers HistorySkill._restore_history -> store.prune(30) —
    # the "old" DM rows are deleted exactly like channel rows would be.
    ircd2 = await _boot_ircd(data_dir)
    try:
        client = await _wire_client(ircd2, "testserv-bob-pr")
        msgs, _ = await _since(client, "testserv-alice-pr", "*")
        texts = [m.params[3] for m in msgs]
        assert texts == [f"new {i}" for i in range(4)]
        assert all("old" not in t for t in texts)
        await client.close()
    finally:
        await ircd2.stop()


# ---------------------------------------------------------------------------
# Offline DM target: byte-identical characterization (see t1's golden test
# in tests/test_wire_format_envelope.py, untouched by this task)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_dm_err_nosuchnick_unchanged_and_not_stored(server, make_client):
    carol = await make_client(nick="testserv-carol", user="carol")

    await carol.send("PRIVMSG testserv-nonexistent :hi")
    line = await carol.recv()
    assert line == ":testserv 401 testserv-carol testserv-nonexistent :No such nick"

    # And, the actual scope decision under test: never stored anywhere.
    carol.nick = "testserv-carol"
    msgs, end = await _recent(carol, "testserv-nonexistent", 10)
    assert msgs == []
    assert end.params[0] == "testserv-nonexistent"


# ---------------------------------------------------------------------------
# EVENTSUB visibility is unchanged by DM history capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eventsub_dm_event_shape_unchanged_by_history_capture(server, make_client):
    """A bot subscribed to ``type=message`` still sees exactly the same DM
    MESSAGE event shape as before this task — no new key, no widening.

    This is a belt-and-suspenders confirmation of the module docstring's
    capture-point claim: ``record_dm`` is called directly, bypassing
    ``IRCd.emit_event``/``on_event`` entirely, so it cannot alter what
    reaches an EVENTSUB subscriber.
    """
    bot = await make_client("testserv-watcher-dm", "watcherdm")
    await bot.send("CAP REQ :agentirc.io/bot")
    await bot.recv_until("CAP")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)

    await bot.send("EVENTSUB subdm type=message")
    await asyncio.sleep(0.05)

    alice = await make_client("testserv-alice-ev", "aliceev")
    bobx = await make_client("testserv-bob-ev", "bobev")
    await alice.send("PRIVMSG testserv-bob-ev :peek-a-boo")
    await bobx.recv()
    await asyncio.sleep(0.1)

    block = await bot.recv_until("EVENT")
    line = next(ln for ln in block.split("\r\n") if " EVENT subdm " in ln)
    parts = line.split(" ", 5)
    assert parts[1] == "EVENT"
    assert parts[3] == "message"
    assert parts[4] == "*"  # channel-less DM -> "*" on the wire

    b64 = line.split(":", 2)[-1]
    envelope = json.loads(base64.b64decode(b64))
    assert envelope["type"] == "message"
    assert envelope["channel"] is None
    assert envelope["nick"] == "testserv-alice-ev"
    assert envelope["data"]["text"] == "peek-a-boo"
    assert envelope["data"]["target"] == "testserv-bob-ev"
    # No new key leaked onto the event (e.g. no internal pair-key field).
    assert set(envelope["data"].keys()) <= {"text", "target", "msgid", "notice"}
