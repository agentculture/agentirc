"""Tests for the PRESENCE skill core (task t3): publish parsing, the
per-nick registry, latest-wins updates, and offline retention on disconnect.

Wire contract: docs/protocol/extensions/presence.md (culture@b69705e),
summarized in
docs/specs/2026-07-07-agentirc-now-speaks-presence-the-ircd-parses-resid.md.
The query surface (`PRESENCE LIST` / `PRESENCELIST` / `PRESENCEEND`) is task
t4 and is deliberately NOT exercised here beyond the no-op/no-crash check.
"""

from __future__ import annotations

import json

import pytest

from agentirc.ircd import IRCd
from agentirc.skills.presence import PresenceRecord, PresenceSkill
from tests._helpers import wait_for


def _find_presence_skill(server: IRCd) -> PresenceSkill:
    for skill in server.skills:
        if isinstance(skill, PresenceSkill):
            return skill
    raise AssertionError("PresenceSkill is not registered on this server")


def _presence_line(payload: dict) -> str:
    return f"PRESENCE :{json.dumps(payload)}"


async def _publish_and_sync(client, payload: dict) -> None:
    """Send a PRESENCE publish, then round-trip a PING/PONG.

    Publish is fire-and-forget (no reply, ever), so there is nothing to
    `recv()` to know the server has processed it. Sending a PING right
    after and waiting for its PONG proves the PRESENCE line was already
    dispatched -- the client's inbound lines are processed strictly in
    order on one connection.
    """
    await client.send(_presence_line(payload))
    await client.send("PING :sync")
    await client.recv_until("PONG")


@pytest.mark.asyncio
async def test_publish_full_payload_stored(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    payload = {
        "state": "working",
        "since": "2026-07-06T14:32:00Z",
        "task": "reviewing PR #53",
        "tokens_in": 120,
        "tokens_out": 45,
    }
    await _publish_and_sync(alice, payload)

    record = skill.get_record("testserv-alice")
    assert record is not None
    assert record.state == "working"
    assert record.since == "2026-07-06T14:32:00Z"
    assert record.task == "reviewing PR #53"
    assert record.tokens_in == 120
    assert record.tokens_out == 45
    assert record.server == server.config.name
    assert isinstance(record.last_refresh, float)
    assert record.last_refresh > 0


@pytest.mark.asyncio
async def test_publish_state_only_stores_none_for_task_and_tokens(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    payload = {"state": "idle", "since": "2026-07-06T00:00:00Z"}
    await _publish_and_sync(alice, payload)

    record = skill.get_record("testserv-alice")
    assert record is not None
    assert record.state == "idle"
    assert record.task is None
    assert record.tokens_in is None
    assert record.tokens_out is None


@pytest.mark.asyncio
async def test_publish_task_truncated_to_128_chars(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    long_task = "x" * 200
    payload = {"state": "thinking", "since": "2026-07-06T00:00:00Z", "task": long_task}
    await _publish_and_sync(alice, payload)

    record = skill.get_record("testserv-alice")
    assert record is not None
    assert record.task == "x" * 128
    assert len(record.task) == 128


async def _assert_dropped_silently_and_connection_usable(
    client, skill: PresenceSkill, nick: str, raw_line: str, expected_before
) -> None:
    """Send a raw (possibly malformed) PRESENCE line and assert it was a no-op.

    Asserts: (1) the registry entry is unchanged from `expected_before`
    (None if there was never a valid prior publish); (2) no reply/ack line
    of any kind arrived for the PRESENCE line, proven by round-tripping a
    PING and finding nothing else in the collected text; (3) the connection
    is still alive and responsive (the PONG itself proves this).
    """
    await client.send(raw_line)
    await client.send("PING :sync")
    combined = await client.recv_until("PONG")

    assert "PONG" in combined
    assert "PRESENCE" not in combined
    assert "421" not in combined
    assert skill.get_record(nick) == expected_before


@pytest.mark.asyncio
async def test_publish_invalid_state_enum_is_dropped_silently(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    # Establish a known-good baseline first so we can prove the invalid
    # publish left the registry untouched (not just "still None").
    await _publish_and_sync(alice, {"state": "idle", "since": "T0"})
    before = skill.get_record("testserv-alice")
    assert before is not None

    bad_line = _presence_line({"state": "bogus-state", "since": "T1"})
    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", bad_line, before
    )


@pytest.mark.asyncio
async def test_publish_malformed_json_is_dropped_silently(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", "PRESENCE :{not valid json at all", None
    )


@pytest.mark.asyncio
async def test_publish_missing_since_is_dropped_silently(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    bad_line = _presence_line({"state": "idle"})
    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", bad_line, None
    )


@pytest.mark.asyncio
async def test_publish_whitespace_only_since_is_dropped_silently(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    bad_line = _presence_line({"state": "idle", "since": "   "})
    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", bad_line, None
    )


@pytest.mark.asyncio
async def test_publish_missing_state_is_dropped_silently(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    bad_line = _presence_line({"since": "T0"})
    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", bad_line, None
    )


@pytest.mark.asyncio
async def test_publish_empty_trailing_is_dropped_silently(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", "PRESENCE :", None
    )


@pytest.mark.asyncio
async def test_publish_wrong_field_types_is_dropped_silently(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    # tokens_in as a bool (JSON true) and state as a number are both
    # type-invalid, not just semantically invalid.
    bad_line = _presence_line({"state": 42, "since": "T0"})
    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", bad_line, None
    )

    bad_line_2 = _presence_line(
        {"state": "idle", "since": "T0", "tokens_in": True}
    )
    await _assert_dropped_silently_and_connection_usable(
        alice, skill, "testserv-alice", bad_line_2, None
    )


@pytest.mark.asyncio
async def test_second_publish_fully_replaces_first(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    await _publish_and_sync(
        alice,
        {
            "state": "thinking",
            "since": "T1",
            "task": "task-one",
            "tokens_in": 10,
            "tokens_out": 5,
        },
    )
    first = skill.get_record("testserv-alice")
    assert first is not None
    assert first.state == "thinking"

    await _publish_and_sync(
        alice,
        {
            "state": "working",
            "since": "T2",
            "task": "task-two",
            "tokens_in": 20,
            "tokens_out": 15,
        },
    )
    second = skill.get_record("testserv-alice")
    assert second is not None
    assert second.state == "working"
    assert second.since == "T2"
    assert second.task == "task-two"
    assert second.tokens_in == 20
    assert second.tokens_out == 15
    assert second.last_refresh >= first.last_refresh


@pytest.mark.asyncio
async def test_presence_works_without_any_cap_req(server, make_client):
    """No CAP LS/REQ exchange happens anywhere -- PRESENCE still works."""
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")

    # Confirm this connection genuinely negotiated no capabilities.
    server_client = server.clients["testserv-alice"]
    assert server_client.caps == set()

    await _publish_and_sync(alice, {"state": "listening", "since": "T0"})

    record = skill.get_record("testserv-alice")
    assert record is not None
    assert record.state == "listening"


@pytest.mark.asyncio
async def test_quit_flips_row_to_offline_and_retains_it(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-dave", "dave")

    await _publish_and_sync(alice, {"state": "listening", "since": "T0"})
    assert skill.get_record("testserv-dave").state == "listening"

    await alice.send("QUIT :bye")

    assert await wait_for(
        lambda: (skill.get_record("testserv-dave") or PresenceRecord("", "")).state
        == "offline",
        timeout=2.0,
    )
    record = skill.get_record("testserv-dave")
    assert record is not None
    assert record.state == "offline"


@pytest.mark.asyncio
async def test_socket_close_flips_row_to_offline_and_reconnect_overwrites(
    server, make_client
):
    """A client that disconnects without an explicit QUIT still flips offline.

    This codebase only emits a disconnect-shaped event to skills for a plain
    TCP close when the client negotiated the `+A` (agent) or `+C` (console)
    user mode (see `IRCd._emit_disconnect_events`) -- mirroring the
    established test pattern in `test_events_lifecycle.py`
    (`test_agent_disconnect_on_close`). A mode-less client that drops the
    socket without sending QUIT currently triggers no event at all on this
    IRCd (a pre-existing gap in `IRCd._remove_client`, out of scope for this
    task's file boundaries -- see the module docstring in
    `agentirc/skills/presence.py`).
    """
    skill = _find_presence_skill(server)
    nick = "testserv-carol"
    alice = await make_client(nick, "carol")
    await alice.send(f"MODE {nick} +A")

    await _publish_and_sync(
        alice,
        {
            "state": "working",
            "since": "T1",
            "task": "task-a",
            "tokens_in": 5,
            "tokens_out": 2,
        },
    )
    assert skill.get_record(nick).state == "working"

    await alice.close()

    assert await wait_for(
        lambda: (skill.get_record(nick) or PresenceRecord("", "")).state == "offline",
        timeout=2.0,
    )
    offline_record = skill.get_record(nick)
    assert offline_record is not None
    assert offline_record.state == "offline"
    assert offline_record.task is None
    # Token counters are retained across the offline flip.
    assert offline_record.tokens_in == 5
    assert offline_record.tokens_out == 2

    # Reconnect with the same nick and publish -- fully overwrites the row.
    bob = await make_client(nick, "carol")
    await _publish_and_sync(bob, {"state": "idle", "since": "T2"})

    record = skill.get_record(nick)
    assert record is not None
    assert record.state == "idle"
    assert record.since == "T2"
    assert record.task is None
    assert record.tokens_in is None
    assert record.tokens_out is None


@pytest.mark.asyncio
async def test_presence_list_is_a_noop_pending_t4(server, make_client):
    """PRESENCE LIST neither crashes nor replies yet -- t4 implements it."""
    alice = await make_client("testserv-alice", "alice")

    await alice.send("PRESENCE LIST")
    await alice.send("PING :sync")
    combined = await alice.recv_until("PONG")

    assert "PONG" in combined
    assert "PRESENCELIST" not in combined
    assert "PRESENCEEND" not in combined


@pytest.mark.asyncio
async def test_degrade_regression_server_without_skill_answers_421(server, make_client):
    """An IRCd whose skills list has PresenceSkill removed 421s PRESENCE.

    Regression-asserts the pre-existing (9.11.0) degrade contract: culture's
    residents CLI must see a plain 421 from a server that doesn't speak
    PRESENCE, with no special-cased error text.
    """
    server.skills = [s for s in server.skills if not isinstance(s, PresenceSkill)]

    alice = await make_client("testserv-alice", "alice")
    await alice.send("PRESENCE :{}")
    resp = await alice.recv()
    assert "421" in resp


@pytest.mark.asyncio
async def test_other_unknown_verb_still_421s_on_full_server(server, make_client):
    """A vanilla client's unrelated unknown verb is unaffected by PRESENCE."""
    _find_presence_skill(server)  # sanity: skill IS registered on this server
    alice = await make_client("testserv-alice", "alice")

    await alice.send("BOGUSVERB foo bar")
    resp = await alice.recv()
    assert "421" in resp
