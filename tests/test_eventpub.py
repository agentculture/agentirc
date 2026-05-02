"""Tests for the EVENTPUB verb (9.5.0).

Spec: ``docs/superpowers/specs/2026-05-01-bot-extension-api-design.md``
§ Decision E. Symmetric to EVENTSUB — bots emit custom-typed events back
into the stream. Server-side validation enforces ``EVENT_TYPE_RE`` and
sets ``nick`` and ``timestamp`` so bots cannot spoof the actor or the
wall-clock.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest


async def _make_bot(make_client, nick: str, user: str):
    c = await make_client(nick, user)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_until("CAP")
    await asyncio.sleep(0.05)
    await c.recv_all(timeout=0.2)
    return c


def _b64_json(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")


@pytest.mark.asyncio
async def test_eventpub_requires_bot_cap(server, make_client):
    """A non-bot client's EVENTPUB is rejected."""
    c = await make_client("testserv-alice", "alice")
    await c.send("JOIN #room")
    await c.recv_until("366")
    await asyncio.sleep(0.05)
    await c.recv_all(timeout=0.2)
    payload = _b64_json({"text": "hello"})
    await c.send(f"EVENTPUB welcome.greeted #room :{payload}")
    line = await c.recv_until("EVENTERR")
    assert "EVENTERR welcome.greeted :bot-capability-required" in line


@pytest.mark.asyncio
async def test_eventpub_happy_path(server, make_client):
    """Bot A emits → Bot B subscribed to the matching type receives one EVENT line."""
    bot_a = await _make_bot(make_client, "testserv-emitter", "emitter")
    bot_b = await _make_bot(make_client, "testserv-listener", "listener")

    # Pre-create #emitroom so EVENTPUB doesn't hit the no-such-channel guard.
    server.get_or_create_channel("#emitroom")

    await bot_b.send("EVENTSUB sub1 type=welcome.greeted")
    await asyncio.sleep(0.05)
    await bot_b.recv_all(timeout=0.2)

    payload = _b64_json({"greeted_user": "newbie"})
    await bot_a.send(f"EVENTPUB welcome.greeted #emitroom :{payload}")
    await asyncio.sleep(0.1)

    block = await bot_b.recv_until("EVENT")
    line = next(ln for ln in block.split("\r\n") if " EVENT sub1 " in ln)
    parts = line.split(" ", 5)
    assert parts[1] == "EVENT"
    assert parts[2] == "sub1"
    assert parts[3] == "welcome.greeted"
    assert parts[4] == "#emitroom"
    # nick is server-set from emitter's connection nick (NOT spoofable).
    assert parts[5].startswith("testserv-emitter ")

    # Decoded envelope reflects server-set nick + greeted_user data.
    b64 = line.split(":", 2)[-1]
    envelope = json.loads(base64.b64decode(b64))
    assert envelope["type"] == "welcome.greeted"
    assert envelope["channel"] == "#emitroom"
    assert envelope["nick"] == "testserv-emitter"
    assert envelope["data"]["greeted_user"] == "newbie"


@pytest.mark.asyncio
async def test_eventpub_invalid_type_rejected(server, make_client):
    """Single-segment type fails ``EVENT_TYPE_RE`` (which requires a dot)."""
    bot = await _make_bot(make_client, "testserv-spoofer", "spoofer")
    payload = _b64_json({"text": "fake"})
    # Single-segment "message" is reserved for built-in vocabulary.
    await bot.send(f"EVENTPUB message * :{payload}")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR message :invalid-type" in line


@pytest.mark.asyncio
async def test_eventpub_no_such_channel(server, make_client):
    """Channel-scoped emission to a non-existent channel is rejected."""
    bot = await _make_bot(make_client, "testserv-roomless", "roomless")
    payload = _b64_json({"text": "lost"})
    await bot.send(f"EVENTPUB welcome.greeted #nowhere :{payload}")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR welcome.greeted :no-such-channel" in line


@pytest.mark.asyncio
async def test_eventpub_invalid_payload(server, make_client):
    """Non-JSON-object payload is rejected."""
    bot = await _make_bot(make_client, "testserv-broken", "broken")
    server.get_or_create_channel("#broken")
    # Encode a JSON list instead of a dict.
    bad = base64.b64encode(b"[1,2,3]").decode("ascii")
    await bot.send(f"EVENTPUB welcome.greeted #broken :{bad}")
    line = await bot.recv_until("EVENTERR")
    assert "EVENTERR welcome.greeted :invalid-payload" in line


@pytest.mark.asyncio
async def test_eventpub_strips_underscore_keys(server, make_client):
    """Server-internal ``_render`` and other ``_``-prefixed keys are stripped."""
    bot_a = await _make_bot(make_client, "testserv-injector", "injector")
    bot_b = await _make_bot(make_client, "testserv-observer", "observer")
    server.get_or_create_channel("#stripsroom")

    await bot_b.send("EVENTSUB sub1 type=welcome.greeted")
    await asyncio.sleep(0.05)
    await bot_b.recv_all(timeout=0.2)

    payload = _b64_json(
        {
            "text": "hi",
            "_render": "ATTACKER-CONTROLLED",
            "_origin": "spoofed",
            "_secret": "leak",
        }
    )
    await bot_a.send(f"EVENTPUB welcome.greeted #stripsroom :{payload}")
    await asyncio.sleep(0.1)

    block = await bot_b.recv_until("EVENT")
    line = next(ln for ln in block.split("\r\n") if " EVENT sub1 " in ln)
    b64 = line.split(":", 2)[-1]
    envelope = json.loads(base64.b64decode(b64))
    # `text` survives, all `_`-keys do not.
    assert envelope["data"]["text"] == "hi"
    assert "_render" not in envelope["data"]
    assert "_secret" not in envelope["data"]
    # `_origin` is also not in the public envelope (server adds it after
    # encode for federation; the public-wire envelope strips at emit time).


@pytest.mark.asyncio
async def test_eventpub_reflexive_subscription(server, make_client):
    """A bot subscribed to its own emitted type receives its own EVENT line."""
    bot = await _make_bot(make_client, "testserv-mirror", "mirror")
    server.get_or_create_channel("#mirrorroom")

    await bot.send("EVENTSUB sub1 type=welcome.greeted")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)

    payload = _b64_json({"text": "self"})
    await bot.send(f"EVENTPUB welcome.greeted #mirrorroom :{payload}")
    await asyncio.sleep(0.1)

    block = await bot.recv_until("EVENT")
    line = next(ln for ln in block.split("\r\n") if " EVENT sub1 " in ln)
    parts = line.split(" ", 5)
    # Reflexive: the bot sees its own emission with its own nick.
    assert parts[3] == "welcome.greeted"
    assert parts[5].startswith("testserv-mirror ")


@pytest.mark.asyncio
async def test_eventpub_global_emission_with_star_channel(server, make_client):
    """``EVENTPUB <type> * :<b64>`` emits a nick-scoped event (channel=None)."""
    bot_a = await _make_bot(make_client, "testserv-broadcaster", "broadcaster")
    bot_b = await _make_bot(make_client, "testserv-globlistener", "globlistener")

    await bot_b.send("EVENTSUB sub1 type=server.heartbeat")
    await asyncio.sleep(0.05)
    await bot_b.recv_all(timeout=0.2)

    payload = _b64_json({"uptime": 42})
    await bot_a.send(f"EVENTPUB server.heartbeat * :{payload}")
    await asyncio.sleep(0.1)

    block = await bot_b.recv_until("EVENT")
    line = next(ln for ln in block.split("\r\n") if " EVENT sub1 " in ln)
    parts = line.split(" ", 5)
    # Channel rendered as `*` for nick-scoped events.
    assert parts[4] == "*"
    b64 = line.split(":", 2)[-1]
    envelope = json.loads(base64.b64decode(b64))
    assert envelope["channel"] is None
