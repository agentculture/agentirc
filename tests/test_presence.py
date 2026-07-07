"""Tests for the PRESENCE skill: publish parsing, the per-nick registry,
latest-wins updates, offline retention on disconnect (task t3), and the
`PRESENCE LIST` query surface with read-time `presumed_hung` (task t4).

Wire contract: docs/protocol/extensions/presence.md (culture@b69705e),
summarized in
docs/specs/2026-07-07-agentirc-now-speaks-presence-the-ircd-parses-resid.md.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from agentirc.ircd import IRCd
from agentirc.skills.presence import PresenceRecord, PresenceSkill
from tests._helpers import wait_for

# Fixed key order the wire contract promises culture's parser -- see
# PresenceSkill._serialize_row.
_ROW_KEYS = [
    "nick",
    "server",
    "state",
    "since",
    "task",
    "tokens_in",
    "tokens_out",
    "presumed_hung",
    "last_refresh",
]

_LAST_REFRESH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _extract_rows(combined: str) -> list[dict]:
    """Parse every `PRESENCELIST :<json>` line out of a recv_until() blob, in order."""
    rows = []
    for line in combined.split("\r\n"):
        if "PRESENCELIST :" not in line:
            continue
        _, _, json_part = line.partition("PRESENCELIST :")
        rows.append(json.loads(json_part))
    return rows


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
async def test_list_on_empty_registry_only_end_line(server, make_client):
    """No one has ever published -- LIST answers with just the terminator."""
    alice = await make_client("testserv-alice", "alice")

    await alice.send("PRESENCE LIST")
    combined = await alice.recv_until("PRESENCEEND")

    lines = [line for line in combined.split("\r\n") if line]
    assert len(lines) == 1
    assert lines[0].endswith("PRESENCEEND :End of presence list")
    assert "PRESENCELIST" not in combined


@pytest.mark.asyncio
async def test_list_returns_one_row_per_resident_nick_sorted_then_end(
    server, make_client
):
    """Byte-level: two publishers, then a third client LISTs.

    Exactly two PRESENCELIST lines (nick-sorted), then exactly one
    PRESENCEEND terminator line. Each trailing JSON param parses cleanly
    and carries exactly the nine contract keys in the specified order.
    """
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    mallory = await make_client("testserv-mallory", "mallory")

    # Publish out of nick order to prove the reply sorts, not insertion-orders.
    await _publish_and_sync(
        bob,
        {
            "state": "working",
            "since": "T-bob",
            "task": "bob's task",
            "tokens_in": 7,
            "tokens_out": 3,
        },
    )
    await _publish_and_sync(alice, {"state": "idle", "since": "T-alice"})

    await mallory.send("PRESENCE LIST")
    combined = await mallory.recv_until("PRESENCEEND")

    lines = [line for line in combined.split("\r\n") if line]
    list_lines = [line for line in lines if "PRESENCELIST :" in line]
    end_lines = [line for line in lines if "PRESENCEEND" in line]

    assert len(list_lines) == 2
    assert len(end_lines) == 1
    assert end_lines[0].endswith("PRESENCEEND :End of presence list")
    # PRESENCEEND is strictly the last line.
    assert lines[-1] is end_lines[0]

    rows = _extract_rows(combined)
    assert [row["nick"] for row in rows] == ["testserv-alice", "testserv-bob"]
    for row in rows:
        assert list(row.keys()) == _ROW_KEYS


@pytest.mark.asyncio
async def test_list_state_only_row_has_null_task_and_tokens(server, make_client):
    """A state-only publisher's row shows task/tokens_in/tokens_out as None,
    since round-trips as sent, and last_refresh matches the Z-suffix ISO shape.
    """
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "idle", "since": "2026-07-06T00:00:00Z"})

    await alice.send("PRESENCE LIST")
    combined = await alice.recv_until("PRESENCEEND")
    rows = _extract_rows(combined)

    assert len(rows) == 1
    row = rows[0]
    assert row["nick"] == "testserv-alice"
    assert row["task"] is None
    assert row["tokens_in"] is None
    assert row["tokens_out"] is None
    assert row["since"] == "2026-07-06T00:00:00Z"
    assert _LAST_REFRESH_RE.match(row["last_refresh"])


@pytest.mark.asyncio
async def test_list_is_observe_only_never_mutates_registry(server, make_client):
    """Querying PRESENCE LIST must never mutate any record (no side effects)."""
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0", "task": "t"})

    before = skill.get_record("testserv-alice")
    before_snapshot = (
        before.state,
        before.since,
        before.task,
        before.tokens_in,
        before.tokens_out,
        before.last_refresh,
        before.server,
    )

    await alice.send("PRESENCE LIST")
    await alice.recv_until("PRESENCEEND")

    after = skill.get_record("testserv-alice")
    after_snapshot = (
        after.state,
        after.since,
        after.task,
        after.tokens_in,
        after.tokens_out,
        after.last_refresh,
        after.server,
    )
    assert after_snapshot == before_snapshot


@pytest.mark.asyncio
async def test_list_extra_params_are_tolerated(server, make_client):
    """`PRESENCE LIST EXTRA JUNK` still answers the list (extra params ignored)."""
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "idle", "since": "T0"})

    await alice.send("PRESENCE LIST EXTRA JUNK")
    combined = await alice.recv_until("PRESENCEEND")

    rows = _extract_rows(combined)
    assert len(rows) == 1
    assert combined.rstrip("\r\n").endswith("PRESENCEEND :End of presence list")


@pytest.mark.asyncio
async def test_list_subcommand_is_case_insensitive(server, make_client):
    """`presence list` (lowercase subcommand) still answers the list."""
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "idle", "since": "T0"})

    await alice.send("PRESENCE list")
    combined = await alice.recv_until("PRESENCEEND")

    rows = _extract_rows(combined)
    assert len(rows) == 1


# ---- presumed_hung (stale-busy watchdog) -----------------------------------


@pytest.mark.asyncio
async def test_presumed_hung_true_when_busy_and_stale(server, make_client):
    skill = _find_presence_skill(server)
    stale_after = server.config.presence.stale_after_seconds
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0"})

    # Reach into the registry directly (no sleeps) and push last_refresh
    # well past the staleness threshold.
    skill.get_record("testserv-alice").last_refresh = time.time() - stale_after - 10

    await alice.send("PRESENCE LIST")
    combined = await alice.recv_until("PRESENCEEND")
    rows = _extract_rows(combined)
    assert len(rows) == 1
    assert rows[0]["presumed_hung"] is True


@pytest.mark.asyncio
async def test_presumed_hung_false_when_fresh(server, make_client):
    skill = _find_presence_skill(server)
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0"})

    # Freshly published -- last_refresh is "now", nowhere near stale.
    skill.get_record("testserv-alice").last_refresh = time.time()

    await alice.send("PRESENCE LIST")
    combined = await alice.recv_until("PRESENCEEND")
    rows = _extract_rows(combined)
    assert rows[0]["presumed_hung"] is False


@pytest.mark.asyncio
async def test_presumed_hung_never_flags_idle_even_when_ancient(server, make_client):
    skill = _find_presence_skill(server)
    stale_after = server.config.presence.stale_after_seconds
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "idle", "since": "T0"})

    skill.get_record("testserv-alice").last_refresh = time.time() - stale_after * 100

    await alice.send("PRESENCE LIST")
    combined = await alice.recv_until("PRESENCEEND")
    rows = _extract_rows(combined)
    assert rows[0]["presumed_hung"] is False


@pytest.mark.asyncio
async def test_presumed_hung_never_flags_offline_even_when_ancient(server, make_client):
    skill = _find_presence_skill(server)
    stale_after = server.config.presence.stale_after_seconds
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0"})

    await alice.send("QUIT :bye")
    assert await wait_for(
        lambda: (skill.get_record("testserv-alice") or PresenceRecord("", "")).state
        == "offline",
        timeout=2.0,
    )
    skill.get_record("testserv-alice").last_refresh = time.time() - stale_after * 100

    bob = await make_client("testserv-bob", "bob")
    await bob.send("PRESENCE LIST")
    combined = await bob.recv_until("PRESENCEEND")
    rows = _extract_rows(combined)
    assert len(rows) == 1
    assert rows[0]["state"] == "offline"
    assert rows[0]["presumed_hung"] is False


@pytest.mark.asyncio
async def test_presumed_hung_boundary_is_strictly_greater_than(server):
    """Age exactly equal to stale_after must NOT flag (strict `>`, not `>=`).

    Exercised as a direct, sleep-free unit check against the skill's pure
    `_presumed_hung` helper rather than over the wire: a real TCP round trip
    can't guarantee bit-exact equality between `now - last_refresh` and
    `stale_after` (scheduling jitter would always push the age a hair past
    the boundary), but the boundary rule itself is trivially unit-testable.
    """
    skill = _find_presence_skill(server)
    stale_after = server.config.presence.stale_after_seconds
    record = PresenceRecord(state="working", since="T0", last_refresh=1_000_000.0)

    # Exactly at the boundary: age == stale_after -- NOT flagged.
    assert skill._presumed_hung(record, now=1_000_000.0 + stale_after) is False
    # A hair past the boundary -- flagged.
    assert skill._presumed_hung(record, now=1_000_000.0 + stale_after + 0.001) is True
    # A hair under the boundary -- not flagged.
    assert skill._presumed_hung(record, now=1_000_000.0 + stale_after - 0.001) is False


@pytest.mark.asyncio
async def test_presumed_hung_flips_back_false_after_another_publish(
    server, make_client
):
    """One more heartbeat from a stale-flagged client flips the flag back."""
    skill = _find_presence_skill(server)
    stale_after = server.config.presence.stale_after_seconds
    alice = await make_client("testserv-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0"})
    skill.get_record("testserv-alice").last_refresh = time.time() - stale_after - 10

    await alice.send("PRESENCE LIST")
    combined = await alice.recv_until("PRESENCEEND")
    assert _extract_rows(combined)[0]["presumed_hung"] is True

    # One more heartbeat -- server stamps a fresh last_refresh.
    await _publish_and_sync(alice, {"state": "working", "since": "T1"})

    await alice.send("PRESENCE LIST")
    combined = await alice.recv_until("PRESENCEEND")
    rows = _extract_rows(combined)
    assert len(rows) == 1
    assert rows[0]["presumed_hung"] is False


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
