"""Tests for the EVENTSUB / EVENTUNSUB / EVENT / EVENTERR verbs (9.5.0).

Spec: ``docs/superpowers/specs/2026-05-01-bot-extension-api-design.md``
§ Decision B. Bots negotiate ``agentirc.io/bot`` and stream events with
AND-ed ``type=`` / ``channel=`` / ``nick=`` filters; backpressure on
overflow drops the subscription.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING, cast

import pytest

from agentirc.protocol import Event, EventType

if TYPE_CHECKING:
    from agentirc.client import Client


async def _make_bot(make_client, nick: str, user: str):
    """Connect, register, negotiate ``agentirc.io/bot``, drain the ACK."""
    c = await make_client(nick, user)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_until("CAP")
    await asyncio.sleep(0.05)
    await c.recv_all(timeout=0.2)
    return c


@pytest.mark.asyncio
async def test_eventsub_requires_bot_cap(make_client):
    """A non-bot client's EVENTSUB is rejected with bot-capability-required."""
    c = await make_client("testserv-alice", "alice")
    await c.send("EVENTSUB sub1 type=user.join")
    line = await c.recv_until("EVENTERR")
    assert "EVENTERR sub1 :bot-capability-required" in line


@pytest.mark.asyncio
async def test_eventsub_happy_path(server, make_client):
    """A bot subscribed to ``user.join`` receives one EVENT line per matching event."""
    bot = await _make_bot(make_client, "testserv-watcher", "watcher")
    await bot.send("EVENTSUB sub1 type=user.join channel=#room")
    await asyncio.sleep(0.05)

    # Trigger a user.join event on #room.
    other = await make_client("testserv-newbie", "newbie")
    await other.send("JOIN #room")
    await other.recv_until("366")
    await asyncio.sleep(0.1)

    # Bot should receive an EVENT line for the join.
    block = await bot.recv_until("EVENT")
    line = next(ln for ln in block.split("\r\n") if " EVENT sub1 " in ln)
    # Wire format: :server EVENT <sub-id> <type> <channel-or-*> <nick> :<b64>
    parts = line.split(" ", 5)
    assert parts[1] == "EVENT"
    assert parts[2] == "sub1"
    assert parts[3] == "user.join"
    assert parts[4] == "#room"
    assert parts[5].startswith("testserv-newbie ")

    # The trailing base64 decodes to the canonical 5-field envelope.
    b64 = line.split(":", 2)[-1]
    envelope = json.loads(base64.b64decode(b64))
    assert envelope["type"] == "user.join"
    assert envelope["channel"] == "#room"
    assert envelope["nick"] == "testserv-newbie"


@pytest.mark.asyncio
async def test_eventsub_filter_type_glob(server, make_client):
    """``type=user.*`` matches user.join, user.part, etc."""
    bot = await _make_bot(make_client, "testserv-globber", "globber")
    await bot.send("EVENTSUB sub1 type=user.* channel=#globroom")
    await asyncio.sleep(0.05)

    other = await make_client("testserv-mover", "mover")
    await other.send("JOIN #globroom")
    await other.recv_until("366")
    await asyncio.sleep(0.05)
    await other.send("PART #globroom")
    await asyncio.sleep(0.1)

    # Bot should receive both user.join and user.part EVENT lines.
    received_types = []
    chunks = await bot.recv_all(timeout=0.5)
    for chunk in chunks:
        for line in chunk.split("\r\n"):
            if " EVENT sub1 " in line:
                received_types.append(line.split(" ")[3])
    assert "user.join" in received_types
    assert "user.part" in received_types


@pytest.mark.asyncio
async def test_eventsub_filter_channel_exact_match(server, make_client):
    """``channel=#a`` does NOT match events on ``#b``."""
    bot = await _make_bot(make_client, "testserv-channelbot", "channelbot")
    await bot.send("EVENTSUB sub1 type=user.join channel=#alpha")
    await asyncio.sleep(0.05)

    other = await make_client("testserv-betatester", "betatester")
    await other.send("JOIN #beta")
    await other.recv_until("366")
    await asyncio.sleep(0.2)

    # Bot should receive nothing for sub1.
    chunks = await bot.recv_all(timeout=0.3)
    for chunk in chunks:
        for line in chunk.split("\r\n"):
            assert " EVENT sub1 " not in line, (
                f"Channel filter leaked event from #beta: {line!r}"
            )


@pytest.mark.asyncio
async def test_eventunsub_stops_stream(server, make_client):
    """After EVENTUNSUB, no further EVENT lines arrive for that sub-id."""
    bot = await _make_bot(make_client, "testserv-unsubber", "unsubber")
    await bot.send("EVENTSUB sub1 type=user.join channel=#unsubroom")
    await asyncio.sleep(0.05)

    other = await make_client("testserv-first", "first")
    await other.send("JOIN #unsubroom")
    await other.recv_until("366")
    await asyncio.sleep(0.1)
    # Drain the first event.
    await bot.recv_until("EVENT")

    await bot.send("EVENTUNSUB sub1")
    await asyncio.sleep(0.05)

    # Drain anything in flight, then trigger another join.
    await bot.recv_all(timeout=0.2)
    other2 = await make_client("testserv-second", "second")
    await other2.send("JOIN #unsubroom")
    await other2.recv_until("366")
    await asyncio.sleep(0.2)

    chunks = await bot.recv_all(timeout=0.3)
    for chunk in chunks:
        for line in chunk.split("\r\n"):
            assert " EVENT sub1 " not in line, f"EVENTUNSUB didn't stop stream: {line!r}"


@pytest.mark.asyncio
async def test_eventsub_sub_id_collision(server, make_client):
    """Re-using an active sub-id returns ``EVENTERR :sub-id-in-use``."""
    bot = await _make_bot(make_client, "testserv-dup", "dup")
    await bot.send("EVENTSUB sub1 type=*")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)

    await bot.send("EVENTSUB sub1 type=user.join")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR sub1 :sub-id-in-use" in line


@pytest.mark.asyncio
async def test_eventsub_invalid_sub_id_format(server, make_client):
    """Sub-id must match ``[A-Za-z0-9._:-]{1,32}``."""
    bot = await _make_bot(make_client, "testserv-invalid", "invalid")
    await bot.send("EVENTSUB sub#bad type=*")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR sub#bad :invalid-sub-id" in line


@pytest.mark.asyncio
async def test_eventsub_unknown_filter(server, make_client):
    """Unknown filter keys are rejected."""
    bot = await _make_bot(make_client, "testserv-bogus", "bogus")
    await bot.send("EVENTSUB sub1 bogus=value")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR sub1 :unknown-filter bogus" in line


@pytest.mark.asyncio
async def test_eventsub_duplicate_filter_rejected(server, make_client):
    """Per-spec, each filter parameter appears at most once."""
    bot = await _make_bot(make_client, "testserv-dupfilt", "dupfilt")
    await bot.send("EVENTSUB sub1 type=user.join type=user.part")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR sub1 :duplicate-filter type" in line


@pytest.mark.asyncio
async def test_eventsub_invalid_channel_filter_rejected(server, make_client):
    """`channel=room` (no `#`) is rejected so subscribers don't silently miss matches."""
    bot = await _make_bot(make_client, "testserv-noprefix", "noprefix")
    await bot.send("EVENTSUB sub1 channel=room")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR sub1 :invalid-channel-filter room" in line


@pytest.mark.asyncio
async def test_eventunsub_cap_gated(server, make_client):
    """A non-bot client's EVENTUNSUB is rejected with bot-capability-required.

    Regression guard for PR #20 review (Copilot 3176303659): EVENTUNSUB must
    enforce the same CAP gate as EVENTSUB so the wire contract stays
    consistent.
    """
    c = await make_client("testserv-bareuser", "bareuser")
    await c.send("EVENTUNSUB sub1")
    line = await c.recv_until("EVENTERR")
    assert "EVENTERR sub1 :bot-capability-required" in line


@pytest.mark.asyncio
async def test_eventsub_requires_registration(server, make_client):
    """An unregistered (no NICK/USER) connection is rejected.

    Regression guard for PR #20 review (Copilot 3176303652): a CAP-only
    socket must not be able to subscribe before claiming an identity.
    """
    # ``make_client()`` without args opens a TCP connection but doesn't
    # send NICK/USER, so the server still considers the client unregistered.
    c = await make_client()
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_until("CAP")
    await c.send("EVENTSUB sub1 type=*")
    line = await c.recv_until("EVENTERR")
    assert "EVENTERR sub1 :not-registered" in line


@pytest.mark.asyncio
async def test_eventsub_disconnect_cleanup(server, make_client):
    """Closing the connection cancels all subscriptions for that client."""
    bot = await _make_bot(make_client, "testserv-leaver", "leaver")
    await bot.send("EVENTSUB sub1 type=*")
    await asyncio.sleep(0.05)

    bot_client = server.clients.get("testserv-leaver")
    assert bot_client is not None
    assert server.subscription_registry.list_for_client(bot_client)

    await bot.close()
    # Give the disconnect path time to run.
    for _ in range(50):
        if not server.subscription_registry.list_for_client(bot_client):
            break
        await asyncio.sleep(0.05)
    assert not server.subscription_registry.list_for_client(bot_client), (
        "Subscription leaked after client disconnect"
    )


@pytest.mark.asyncio
async def test_subscription_registry_backpressure_drops_sub(server):
    """Filling the queue past its bound triggers EVENTERR :backpressure-overflow."""
    # We exercise the registry directly with a fake client to avoid the
    # need for a real TCP queue depth (StreamWriter has no public maxsize).
    from agentirc._internal.event_subscriptions import SubscriptionRegistry

    sent_lines: list[str] = []

    class FakeClient:
        async def send_raw(self, line: str) -> None:  # NOSONAR python:S7503: must be async to duck-type Client.send_raw which the registry awaits
            sent_lines.append(line)

        @property
        def server(self):  # for the drain task's name
            return server

        nick = "testserv-fake"

    # FakeClient duck-types Client (send_raw, nick, server) — the registry
    # only touches those attributes. ``cast`` tells static analysers
    # (mypy / SonarCloud python:S5655) to treat the scaffold as a Client at
    # the call sites; it is a no-op at runtime.
    registry = SubscriptionRegistry(queue_max=2)
    fake = cast("Client", FakeClient())
    sub = registry.add(fake, "subA", type_glob="*")
    assert sub is not None

    # Cancel the drain task immediately so the queue stays full.
    if sub.drain_task is not None:
        sub.drain_task.cancel()

    base = Event(type=EventType.JOIN, channel="#x", nick="alice", data={})
    # First two fill the queue.
    await registry.dispatch(base)
    await registry.dispatch(base)
    # Third overflows and triggers the backpressure drop.
    await registry.dispatch(base)

    overflows = [ln for ln in sent_lines if "backpressure-overflow" in ln]
    assert len(overflows) == 1
    assert "EVENTERR subA :backpressure-overflow" in overflows[0]
    # Subscription is gone from the registry after the drop.
    assert registry.get(fake, "subA") is None


def test_subscription_filter_matches_unit():
    """Unit-level checks for the AND-ed glob/exact filter logic."""
    from agentirc._internal.event_subscriptions import (
        CHANNEL_ANY,
        CHANNEL_NICK_SCOPED_ONLY,
        Subscription,
    )

    e_join = Event(type="user.join", channel="#room", nick="alice", data={})
    e_part = Event(type="user.part", channel="#room", nick="alice", data={})
    e_global = Event(type="server.wake", channel=None, nick="system", data={})

    # type-glob match.
    sub = Subscription(sub_id="s1", type_glob="user.*")
    assert sub.matches(e_join)
    assert sub.matches(e_part)
    assert not sub.matches(e_global)

    # channel exact match.
    sub2 = Subscription(sub_id="s2", channel="#room")
    assert sub2.matches(e_join)
    assert not sub2.matches(e_global)

    # channel="*" matches everything including None.
    sub3 = Subscription(sub_id="s3", channel=CHANNEL_ANY)
    assert sub3.matches(e_join)
    assert sub3.matches(e_global)

    # channel="" matches only nick-scoped events.
    sub4 = Subscription(sub_id="s4", channel=CHANNEL_NICK_SCOPED_ONLY)
    assert not sub4.matches(e_join)
    assert sub4.matches(e_global)

    # nick-glob match.
    sub5 = Subscription(sub_id="s5", nick_glob="ali*")
    assert sub5.matches(e_join)
    assert not sub5.matches(e_global)
