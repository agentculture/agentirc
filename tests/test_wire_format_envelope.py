"""Wire-format envelope (9.5.0a2) tests.

Locks in the public 5-field envelope shape that bot subscribers and
federation peers exchange. Tests:

- Golden-file byte lock: a known ``Event`` encodes to exactly one
  base64 string. Any change to ``_build_event_envelope`` or
  ``_encode_event_data`` that breaks this is a wire-format break and
  must be a major bump per the semver contract.
- Round-trip: encode → decode → reconstruct ``Event`` → equal to the
  original (including the floating-point ``timestamp``).
- Asymmetric sniff tolerance: ``ServerLink._handle_sevent`` decodes
  both 9.5+ envelope shape AND ≤9.4 legacy data-only shape, letting
  9.5 daemons read federation traffic from older peers during an
  in-place upgrade.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from agentirc._internal.protocol.message import Message
from agentirc.ircd import IRCd
from agentirc.protocol import Event, EventType, SEVENT


# ---------------------------------------------------------------------------
# Golden-file byte lock
# ---------------------------------------------------------------------------

# A canonical ``user.join`` event with deterministic fields (fixed timestamp
# so the encoded blob is reproducible across runs) and ``_origin`` in data
# (must be stripped by ``_build_event_envelope``).
_GOLDEN_EVENT = Event(
    type=EventType.JOIN,
    channel="#room",
    nick="alice",
    data={"text": "hi", "_origin": "should-strip"},
    timestamp=1714568400.0,
)

# Locked-in expected wire bytes. Derived from the canonical JSON encoding:
#   {"channel":"#room","data":{"text":"hi"},"nick":"alice","timestamp":1714568400.0,"type":"user.join"}
# Sorted keys, separators=(",", ":"), UTF-8, then base64.
_GOLDEN_BASE64 = (
    "eyJjaGFubmVsIjoiI3Jvb20iLCJkYXRhIjp7InRleHQiOiJoaSJ9LCJuaWNrIjoi"
    "YWxpY2UiLCJ0aW1lc3RhbXAiOjE3MTQ1Njg0MDAuMCwidHlwZSI6InVzZXIuam9pbiJ9"
)


# ---------------------------------------------------------------------------
# Federation-test scaffolding helper
# ---------------------------------------------------------------------------

async def _send_sevent_and_get_event(
    linked_servers,
    payload: dict,
    *,
    verb_channel: str = "#room",
    verb_type: str = "user.join",
    pre_create_channel: bool = True,
):
    """Encode payload as SEVENT, dispatch through the alpha→beta link, return the resulting Event.

    Used by every ``test_handle_sevent_*`` test to eliminate per-test
    scaffolding (the encode + Message + link-lookup + dispatch + event-fetch
    boilerplate). The payload may be a 9.5+ envelope or a ≤9.4 legacy data
    dict; the receiver's ``ServerLink._is_envelope`` sniff handles both.
    """
    alpha, beta = linked_servers
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    alpha_link = next(
        link for link in beta.links.values() if link.peer_name == alpha.config.name
    )
    msg = Message(
        prefix=None,
        command=SEVENT,
        params=[alpha.config.name, "1", verb_type, verb_channel, encoded],
    )
    if pre_create_channel and verb_channel != "*":
        beta.get_or_create_channel(verb_channel)
    before_count = len(beta._event_log)
    await alpha_link._handle_sevent(msg)
    assert len(beta._event_log) == before_count + 1
    _, ev = beta._event_log[-1]
    return ev


def test_envelope_byte_lock():
    """The canonical JSON encoding of a known Event matches the golden b64.

    Locks the public wire format under the semver contract. Any change here
    is a wire-format break — major bump required.
    """
    envelope = IRCd._build_event_envelope(_GOLDEN_EVENT)
    encoded = IRCd._encode_event_data(envelope, "user.join")
    assert encoded == _GOLDEN_BASE64, (
        "Wire format break: _build_event_envelope + _encode_event_data no "
        "longer produces the locked-in canonical encoding. If this is "
        "intentional, a major version bump is required per docs/api-stability.md."
    )


def test_envelope_strips_underscore_keys():
    """``_``-prefixed keys (federation metadata) must not leak into ``data``."""
    envelope = IRCd._build_event_envelope(_GOLDEN_EVENT)
    assert "_origin" not in envelope["data"]
    assert envelope["data"] == {"text": "hi"}


def test_envelope_shape():
    """Exactly five keys, predictable types, top-level nick/channel."""
    envelope = IRCd._build_event_envelope(_GOLDEN_EVENT)
    assert set(envelope.keys()) == {"type", "channel", "nick", "data", "timestamp"}
    assert envelope["type"] == "user.join"
    assert envelope["channel"] == "#room"
    assert envelope["nick"] == "alice"
    assert envelope["data"] == {"text": "hi"}
    # Use pytest.approx to avoid SonarCloud python:S1244 (float equality);
    # the value round-trips exactly in IEEE 754 so default tolerance suffices.
    assert envelope["timestamp"] == pytest.approx(1714568400.0)


def test_envelope_round_trip():
    """encode → decode → reconstruct preserves all 5 envelope fields."""
    envelope = IRCd._build_event_envelope(_GOLDEN_EVENT)
    encoded = IRCd._encode_event_data(envelope, "user.join")
    decoded = json.loads(base64.b64decode(encoded))
    assert decoded == envelope

    # Reconstruct the Event from the decoded envelope (this is what
    # _handle_sevent does on the receive side).
    reconstructed = Event(
        type=decoded["type"],
        channel=decoded["channel"],
        nick=decoded["nick"],
        data=decoded["data"],
        timestamp=decoded["timestamp"],
    )
    assert reconstructed.type == "user.join"
    assert reconstructed.channel == "#room"
    assert reconstructed.nick == "alice"
    assert reconstructed.data == {"text": "hi"}
    assert reconstructed.timestamp == pytest.approx(1714568400.0)


def test_envelope_with_null_channel():
    """A nick-scoped event has channel=None at the envelope's top level."""
    ev = Event(
        type=EventType.AGENT_CONNECT,
        channel=None,
        nick="system-alpha",
        data={"nick": "agent-bob"},
        timestamp=1714568500.5,
    )
    envelope = IRCd._build_event_envelope(ev)
    assert envelope["channel"] is None
    assert envelope["nick"] == "system-alpha"
    assert envelope["data"]["nick"] == "agent-bob"


# ---------------------------------------------------------------------------
# Asymmetric sniff tolerance
# ---------------------------------------------------------------------------

def test_is_envelope_recognises_9_5_shape():
    """Sniff returns True for the 5-field envelope shape."""
    from agentirc.server_link import ServerLink

    decoded = {
        "type": "user.join",
        "channel": "#room",
        "nick": "alice",
        "data": {"text": "hi"},
        "timestamp": 1714568400.0,
    }
    assert ServerLink._is_envelope(decoded) is True


def test_is_envelope_rejects_legacy_data_only_shape():
    """Sniff returns False for the legacy data-only dict (no top-level type/data)."""
    from agentirc.server_link import ServerLink

    legacy = {"nick": "alice", "channel": "#room", "text": "hi"}
    assert ServerLink._is_envelope(legacy) is False


def test_is_envelope_rejects_partial_shapes():
    """Sniff is strict — both `type` (string) AND `data` (dict) must be present."""
    from agentirc.server_link import ServerLink

    # Has `type` but no `data` dict
    assert ServerLink._is_envelope({"type": "user.join", "nick": "alice"}) is False
    # Has `data` but no `type`
    assert ServerLink._is_envelope({"data": {"text": "hi"}}) is False
    # `data` present but not a dict
    assert ServerLink._is_envelope({"type": "user.join", "data": "not-a-dict"}) is False
    # Empty dict
    assert ServerLink._is_envelope({}) is False


# ---------------------------------------------------------------------------
# Federation interop via _handle_sevent
# ---------------------------------------------------------------------------

# These tests exercise _handle_sevent's sniff-and-reconstruct path directly.
# They use the existing `linked_servers` fixture for the integration round-trip;
# the unit-level sniff tests above lock in the helper.

@pytest.mark.asyncio
async def test_handle_sevent_decodes_9_5_envelope(linked_servers):
    """A 9.5+ peer's envelope payload reconstructs an Event with all 5 fields."""
    alpha, _ = linked_servers
    envelope = {
        "type": "user.join",
        "channel": "#room",
        "nick": "alice",
        "data": {"text": "hi"},
        "timestamp": 1714568400.0,
    }
    ev = await _send_sevent_and_get_event(linked_servers, envelope)

    assert str(ev.type) == "user.join"
    assert ev.channel == "#room"
    assert ev.nick == "alice"
    assert ev.data["text"] == "hi"
    # Timestamp from the envelope should round-trip (originating peer's clock).
    assert ev.timestamp == pytest.approx(1714568400.0)
    # Receiver sets _origin to track the originating peer.
    assert ev.data["_origin"] == alpha.config.name


@pytest.mark.asyncio
async def test_handle_sevent_ignores_envelope_channel_claim(linked_servers):
    """Verb-arg channel is authoritative; envelope channel claim is ignored.

    Regression guard for PR #18 review (Qodo 3176230784, Copilot 3176232442):
    a malformed peer must not be able to bypass the trust check by sending
    target="*" while putting a restricted channel name in the envelope. The
    receiver always uses the SEVENT verb-arg channel for both the trust
    check and the resulting Event.channel.
    """
    envelope = {
        "type": "user.join",
        # Peer claims #attack-target in the envelope, but verb-arg is "*".
        "channel": "#attack-target",
        "nick": "alice",
        "data": {"text": "hi"},
        "timestamp": 1714568400.0,
    }
    # verb_channel="*" → no trust check fires, no channel injection.
    ev = await _send_sevent_and_get_event(
        linked_servers, envelope, verb_channel="*", pre_create_channel=False
    )

    # Verb-arg "*" mapped to None. Envelope's "#attack-target" is dropped.
    assert ev.channel is None, (
        f"Envelope channel claim leaked through trust gate: {ev.channel!r}"
    )


@pytest.mark.asyncio
async def test_handle_sevent_strips_underscore_metadata(linked_servers):
    """`_`-prefixed keys in peer-supplied data are stripped before emit_event.

    Regression guard for PR #18 review (Copilot 3176232430): a peer must not
    be able to inject `_render` (or any other server-internal `_`-prefixed
    metadata) via SEVENT and influence local surfacing. The receiver strips
    every `_`-key from the decoded data before adding its own `_origin`.
    """
    alpha, _ = linked_servers
    envelope = {
        "type": "user.join",
        "channel": "#room",
        "nick": "alice",
        "data": {
            "text": "hi",
            "_render": "ATTACKER-CONTROLLED RENDER STRING",
            "_origin": "spoofed-origin",
            "_secret_hint": "should-not-survive",
        },
        "timestamp": 1714568400.0,
    }
    ev = await _send_sevent_and_get_event(linked_servers, envelope)

    # `text` (non-underscore) survives; all peer-supplied `_`-keys do not.
    assert ev.data["text"] == "hi"
    assert "_render" not in ev.data, "_render injection survived sevent decode"
    assert "_secret_hint" not in ev.data
    # `_origin` is set by the receiver to the actual peer name (not the
    # spoofed value). The peer-supplied `_origin` was stripped first.
    assert ev.data["_origin"] == alpha.config.name


@pytest.mark.asyncio
async def test_handle_sevent_decodes_legacy_data_only(linked_servers):
    """A ≤9.4 peer's data-only payload still reconstructs cleanly via sniff fallback.

    Locks asymmetric tolerance: 9.5 receiver tolerates the legacy 9.4 emit
    shape so federations can roll forward one peer at a time. Without this
    sniff, a half-upgraded federation would lose all events from the
    not-yet-upgraded side.
    """
    alpha, _ = linked_servers
    # Legacy shape: bare data dict, no top-level type or data wrapper.
    # 9.4 emitters merged nick into the data dict via setdefault.
    legacy_payload = {"nick": "alice", "channel": "#room", "text": "hi"}

    before = time.time()
    ev = await _send_sevent_and_get_event(linked_servers, legacy_payload)
    after = time.time()

    assert str(ev.type) == "user.join"
    assert ev.channel == "#room"
    assert ev.nick == "alice"
    assert ev.data["text"] == "hi"
    # Legacy peers don't ship a timestamp; receiver fills with time.time().
    assert before <= ev.timestamp <= after
    assert ev.data["_origin"] == alpha.config.name
