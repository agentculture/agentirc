"""Tests for PRESENCE federation across server links (task t5).

Presence updates ride the existing generic event bus -- no new S2S verb, no
hop counts. Every accepted local publish and every local offline flip
(QUIT/disconnect) emits `Event(type=EventType.PRESENCE)` through
`IRCd.emit_event`, which relays any event without an `_origin` tag to every
linked peer via the generic `SEVENT` fallback (see
`agentirc/skills/presence.py` module docstring and
`docs/specs/2026-07-07-agentirc-now-speaks-presence-the-ircd-parses-resid.md`).

Template: `tests/test_events_federation.py` (the generic event-bus
federation suite this module mirrors for the PRESENCE-specific surface).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentirc.ircd import IRCd
from agentirc.skill import EventType
from agentirc.skills.presence import PresenceSkill
from tests._helpers import boot_linked_pair, link_pair, wait_for
from tests.conftest import IRCTestClient

# Fixed nine-key shape the wire contract promises culture's parser -- see
# PresenceSkill._serialize_row and tests/test_presence.py.
_ROW_KEYS = {
    "nick",
    "server",
    "state",
    "since",
    "task",
    "tokens_in",
    "tokens_out",
    "presumed_hung",
    "last_refresh",
}


def _find_presence_skill(server: IRCd) -> PresenceSkill:
    for skill in server.skills:
        if isinstance(skill, PresenceSkill):
            return skill
    raise AssertionError("PresenceSkill is not registered on this server")


def _presence_line(payload: dict) -> str:
    return f"PRESENCE :{json.dumps(payload)}"


async def _publish_and_sync(client: IRCTestClient, payload: dict) -> None:
    """Send a PRESENCE publish, then round-trip a PING/PONG.

    Publish is fire-and-forget (no reply, ever), so a PING/PONG round-trip
    is the only way to know the local server has finished processing the
    line -- see the identical helper in tests/test_presence.py. This proves
    only that the *local* server accepted the publish; the federated relay
    to a linked peer is a separate async hop, so cross-server assertions
    still need `wait_for`.
    """
    await client.send(_presence_line(payload))
    await client.send("PING :sync")
    await client.recv_until("PONG")


async def _make_raw_client(server: IRCd, nick: str, user: str) -> IRCTestClient:
    """Connect directly to *server* without going through a linked-pair fixture.

    Used by the burst-on-link test, which needs two servers booted but
    deliberately NOT linked yet -- the pre-linked `make_client_a`/`make_client_b`
    fixtures depend on the `linked_servers` fixture and can't express that.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", server.config.port)
    client = IRCTestClient(reader, writer)
    await client.send(f"NICK {nick}")
    await client.send(f"USER {user} 0 * :{user}")
    await client.recv_all(timeout=0.5)
    return client


def _presence_event_count(server: IRCd) -> int:
    return sum(
        1 for _seq, event in server._event_log if event.type == EventType.PRESENCE
    )


@pytest.mark.asyncio
async def test_publish_on_a_appears_on_b_with_server_attribution(
    linked_servers, make_client_a, make_client_b
):
    """A resident publishing on A shows up in B's registry, attributed to A."""
    alpha, beta = linked_servers
    beta_skill = _find_presence_skill(beta)

    alice = await make_client_a("alpha-alice", "alice")
    await _publish_and_sync(
        alice,
        {
            "state": "working",
            "since": "2026-07-07T00:00:00Z",
            "task": "reviewing PR",
            "tokens_in": 10,
            "tokens_out": 5,
        },
    )

    assert await wait_for(lambda: beta_skill.get_record("alpha-alice") is not None)
    remote_record = beta_skill.get_record("alpha-alice")
    assert remote_record.server == "alpha"
    assert remote_record.state == "working"
    assert remote_record.task == "reviewing PR"
    assert remote_record.tokens_in == 10
    assert remote_record.tokens_out == 5


@pytest.mark.asyncio
async def test_list_on_b_shows_both_local_and_remote_residents(
    linked_servers, make_client_a, make_client_b
):
    """PRESENCE LIST on B aggregates a local resident and a remote (A) one."""
    alpha, beta = linked_servers
    beta_skill = _find_presence_skill(beta)

    alice = await make_client_a("alpha-alice", "alice")
    bob = await make_client_b("beta-bob", "bob")

    await _publish_and_sync(
        alice, {"state": "working", "since": "2026-07-07T00:00:00Z"}
    )
    await _publish_and_sync(bob, {"state": "idle", "since": "2026-07-07T00:01:00Z"})

    assert await wait_for(lambda: beta_skill.get_record("alpha-alice") is not None)

    await bob.send("PRESENCE LIST")
    combined = await bob.recv_until("PRESENCEEND")

    rows = []
    for line in combined.split("\r\n"):
        if "PRESENCELIST :" not in line:
            continue
        _, _, json_part = line.partition("PRESENCELIST :")
        rows.append(json.loads(json_part))

    by_nick = {row["nick"]: row for row in rows}
    assert set(by_nick) == {"alpha-alice", "beta-bob"}
    assert by_nick["alpha-alice"]["server"] == "alpha"
    assert by_nick["beta-bob"]["server"] == "beta"
    for row in rows:
        assert set(row) == _ROW_KEYS


@pytest.mark.asyncio
async def test_single_publish_does_not_loop(linked_servers, make_client_a):
    """One publish relays exactly once per side -- no relay loop.

    Mirrors test_federated_event_does_not_loop's count_until_idle style, but
    counts presence.update entries in each server's `_event_log` across an
    idle window (presence.update never surfaces as a #system PRIVMSG, so
    there is no PRIVMSG line to count here -- see the NO_SURFACE assertion
    in test_presence_publish_produces_no_system_privmsg below).
    """
    alpha, beta = linked_servers

    alice = await make_client_a("alpha-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0"})

    assert await wait_for(lambda: _presence_event_count(beta) >= 1)
    await asyncio.sleep(0.3)
    first_a, first_b = _presence_event_count(alpha), _presence_event_count(beta)
    await asyncio.sleep(0.5)
    second_a, second_b = _presence_event_count(alpha), _presence_event_count(beta)

    assert first_a == second_a == 1
    assert first_b == second_b == 1


@pytest.mark.asyncio
async def test_burst_on_link_delivers_pre_link_presence(tmp_path):
    """A resident that published BEFORE the link existed is still learned on link-up."""
    server_a, server_b = await boot_linked_pair(tmp_path, webhook_port=0)
    try:
        alice = await _make_raw_client(server_a, "alpha-alice", "alice")
        await _publish_and_sync(alice, {"state": "working", "since": "T0"})

        beta_skill = _find_presence_skill(server_b)
        # Not linked yet: server_b has no way to know about alpha-alice.
        assert beta_skill.get_record("alpha-alice") is None

        await link_pair(server_a, server_b)

        assert await wait_for(lambda: beta_skill.get_record("alpha-alice") is not None)
        record = beta_skill.get_record("alpha-alice")
        assert record.server == "alpha"
        assert record.state == "working"

        await alice.close()
    finally:
        await server_a.stop()
        await server_b.stop()


@pytest.mark.asyncio
async def test_unlink_flips_remote_rows_offline(linked_servers, make_client_a):
    """Tearing down the link flips the departed server's rows to offline."""
    alpha, beta = linked_servers
    beta_skill = _find_presence_skill(beta)

    alice = await make_client_a("alpha-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0"})

    assert await wait_for(
        lambda: beta_skill.get_record("alpha-alice") is not None
        and beta_skill.get_record("alpha-alice").state == "working"
    )

    link = alpha.links["beta"]
    link.writer.close()
    try:
        await link.writer.wait_closed()
    except ConnectionError:
        pass

    assert await wait_for(
        lambda: beta_skill.get_record("alpha-alice") is not None
        and beta_skill.get_record("alpha-alice").state == "offline"
    )
    # offline rows never presumed_hung, no matter how stale last_refresh gets.
    assert (
        beta_skill._presumed_hung(beta_skill.get_record("alpha-alice"), 10**9) is False
    )


@pytest.mark.asyncio
async def test_presence_publish_produces_no_system_privmsg(server, make_client):
    """PRESENCE publishes never surface as #system PRIVMSGs (NO_SURFACE)."""
    alice = await make_client("testserv-alice", "alice")
    watcher = await make_client("testserv-watcher", "watcher")
    await watcher.send("JOIN #system")
    await watcher.recv_until("JOIN")
    await asyncio.sleep(0.05)
    await watcher.recv_all(timeout=0.2)  # flush any queued join-event PRIVMSGs

    await _publish_and_sync(alice, {"state": "working", "since": "T0"})

    lines = await watcher.recv_all(timeout=0.4)
    assert not any("PRIVMSG #system" in line for line in lines)


@pytest.mark.asyncio
async def test_quit_on_a_flips_remote_row_offline_on_b(linked_servers, make_client_a):
    """A resident's QUIT on A federates as an offline flip visible on B."""
    alpha, beta = linked_servers
    beta_skill = _find_presence_skill(beta)

    alice = await make_client_a("alpha-alice", "alice")
    await _publish_and_sync(alice, {"state": "working", "since": "T0"})

    assert await wait_for(
        lambda: beta_skill.get_record("alpha-alice") is not None
        and beta_skill.get_record("alpha-alice").state == "working"
    )

    await alice.send("QUIT :bye")
    await alice.close()

    assert await wait_for(
        lambda: beta_skill.get_record("alpha-alice") is not None
        and beta_skill.get_record("alpha-alice").state == "offline"
    )
