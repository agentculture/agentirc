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


# ---------------------------------------------------------------------------
# Client wire-shape characterization (task t1, agent-accessibility release)
# ---------------------------------------------------------------------------
#
# These lock the CURRENT client-facing wire shape ahead of
# docs/specs/2026-07-01-agentirc-ships-an-agent-accessibility-release-ai-a.md.
# They are characterization tests — they assert what the server does
# *today*, not what it should do — so later tasks in that release can prove
# they preserve backward compat. The spec's honesty condition that "the
# 9.5.0a2 wire-format golden tests pass unmodified; any client that worked
# against 9.7.0 registers and chats against this release with no changes"
# extends to three additional surfaces baselined here: PRIVMSG relay, skill
# NOTICE/ad-hoc-numeric error replies, and HISTORY replay lines. Values were
# captured by driving a real server via the `server`/`make_client` fixtures
# (raw TCP, no guessing) — see
# docs/specs/2026-07-02-agent-accessibility-gap-verification.md for the
# corresponding before-state citation re-verification.


@pytest.mark.asyncio
async def test_client_privmsg_channel_relay_wire_shape(server, make_client):
    """Locks the literal wire line a channel member sees for a PRIVMSG relay.

    Sender prefix is ``nick!user@host`` built from ``Client.prefix``
    (agentirc/client.py); no IRCv3 tags are present because neither client
    negotiated ``message-tags``. Before-state: client.py:320 — no
    msgid/server-time tags are ever stamped today.
    """
    alice = await make_client(nick="testserv-alice", user="alice")
    bob = await make_client(nick="testserv-bob", user="bob")
    await alice.send("JOIN #wire")
    await alice.recv_all(timeout=0.5)
    await bob.send("JOIN #wire")
    await bob.recv_all(timeout=0.5)
    await alice.recv_all(timeout=0.5)  # drain alice's view of bob joining

    await alice.send("PRIVMSG #wire :locked shape")
    line = await bob.recv()

    assert line == ":testserv-alice!alice@127.0.0.1 PRIVMSG #wire :locked shape"


@pytest.mark.asyncio
async def test_client_privmsg_dm_relay_wire_shape(server, make_client):
    """Locks the literal wire line for a DM relay (no channel target).

    Before-state: client.py:914-916 — DMs are relayed with the same framing
    as a channel PRIVMSG; only routing differs.
    """
    carol = await make_client(nick="testserv-carol", user="carol")
    dave = await make_client(nick="testserv-dave", user="dave")

    await carol.send("PRIVMSG testserv-dave :direct hello")
    line = await dave.recv()

    assert line == ":testserv-carol!carol@127.0.0.1 PRIVMSG testserv-dave :direct hello"


@pytest.mark.asyncio
async def test_client_privmsg_dm_to_absent_nick_wire_shape(server, make_client):
    """Locks the literal ERR_NOSUCHNICK wire line for a DM to an absent nick.

    Before-state: client.py:914-916 — DM to an absent nick returns
    ERR_NOSUCHNICK (401); the DM is never stored anywhere (no history
    fallback, no queued delivery).
    """
    carol = await make_client(nick="testserv-carol", user="carol")

    await carol.send("PRIVMSG testserv-nonexistent :hi")
    line = await carol.recv()

    assert line == ":testserv 401 testserv-carol testserv-nonexistent :No such nick"


@pytest.mark.asyncio
async def test_client_roomcreate_bad_channel_name_notice_wire_shape(server, make_client):
    """Locks the bare-NOTICE-prose shape for a rooms-skill validation error.

    Before-state: skills/rooms.py:48 — error replies mix bare NOTICE prose
    (this one) with ad-hoc numerics (see the THREAD 400/404/405 tests
    below) instead of a stable named reason token.
    """
    alice = await make_client(nick="testserv-alice", user="alice")

    await alice.send("ROOMCREATE notachannel :purpose=x")
    line = await alice.recv()

    assert line == ":testserv NOTICE testserv-alice :Channel name must start with #"


@pytest.mark.asyncio
async def test_client_history_unknown_subcommand_notice_wire_shape(server, make_client):
    """Locks the bare-NOTICE-prose shape for an unrecognised HISTORY subcommand.

    Before-state: skills/history.py:150.
    """
    bob = await make_client(nick="testserv-bob", user="bob")

    await bob.send("HISTORY BOGUS")
    line = await bob.recv()

    assert line == ":testserv NOTICE testserv-bob :Unknown HISTORY subcommand: BOGUS"


@pytest.mark.asyncio
async def test_client_history_recent_invalid_count_notice_wire_shape(server, make_client):
    """Locks the bare-NOTICE-prose shape for a non-integer HISTORY RECENT count.

    Before-state: skills/history.py:169.
    """
    bob = await make_client(nick="testserv-bob", user="bob")

    await bob.send("HISTORY RECENT #wire notanumber")
    line = await bob.recv()

    assert line == ":testserv NOTICE testserv-bob :Invalid count"


@pytest.mark.asyncio
async def test_client_thread_unknown_subcommand_notice_wire_shape(server, make_client):
    """Locks the bare-NOTICE-prose shape for an unrecognised THREAD subcommand."""
    carol = await make_client(nick="testserv-carol", user="carol")

    await carol.send("THREAD BOGUS")
    line = await carol.recv()

    assert line == ":testserv NOTICE testserv-carol :Unknown THREAD subcommand: BOGUS"


@pytest.mark.asyncio
async def test_client_thread_create_invalid_name_adhoc_numeric_wire_shape(server, make_client):
    """Locks the ad-hoc ``400`` numeric shape for an invalid THREAD CREATE name.

    Before-state: skills/threads.py:165 — the other half of the "mixed"
    error-reply claim: threads.py uses hand-rolled numeric-looking command
    strings ("400"/"404"/"405") that are not real IRC numerics and carry no
    stable reason token, alongside rooms/history's bare NOTICE prose above.
    """
    carol = await make_client(nick="testserv-carol", user="carol")
    await carol.send("JOIN #general")
    await carol.recv_all(timeout=0.5)

    await carol.send("THREAD CREATE #general --bad-name :hello")
    line = await carol.recv()

    assert line == (
        ":testserv 400 testserv-carol --bad-name "
        ":Invalid thread name (alphanumeric + hyphens, 1-32 chars)"
    )


@pytest.mark.asyncio
async def test_client_thread_reply_nonexistent_adhoc_numeric_wire_shape(server, make_client):
    """Locks the ad-hoc ``404`` numeric shape for THREAD REPLY to an unknown thread.

    Before-state: skills/threads.py:274.
    """
    carol = await make_client(nick="testserv-carol", user="carol")
    await carol.send("JOIN #general")
    await carol.recv_all(timeout=0.5)

    await carol.send("THREAD REPLY #general no-thread :hello")
    line = await carol.recv()

    assert line == ":testserv 404 testserv-carol no-thread :No such thread"


@pytest.mark.asyncio
async def test_client_thread_reply_archived_adhoc_numeric_wire_shape(server, make_client):
    """Locks the ad-hoc ``405`` numeric shape for THREAD REPLY to a closed thread.

    Before-state: skills/threads.py:285.
    """
    carol = await make_client(nick="testserv-carol", user="carol")
    await carol.send("JOIN #general")
    await carol.recv_all(timeout=0.5)
    await carol.send("THREAD CREATE #general done-thread :starting")
    await carol.recv_all(timeout=0.5)
    await carol.send("THREADCLOSE #general done-thread :all done")
    await carol.recv_all(timeout=0.5)

    await carol.send("THREAD REPLY #general done-thread :too late")
    line = await carol.recv()

    assert line == ":testserv 405 testserv-carol done-thread :Thread is closed"


@pytest.mark.asyncio
async def test_client_history_recent_replay_wire_shape(server, make_client):
    """Locks the literal HISTORY RECENT replay-line shape and HISTORYEND terminator.

    Before-state: skills/history.py:117-124 and history_store.py:42-60 —
    HISTORY RECENT is last-N only (a count, not a since-timestamp/cursor);
    each replay line is
    ``:<server> HISTORY <channel> <nick> <timestamp> :<text>`` and the
    reply is terminated by
    ``:<server> HISTORYEND <channel> :End of history``. There is no
    trailing cursor/id field on either line.
    """
    alice = await make_client(nick="testserv-alice", user="alice")
    await alice.send("JOIN #wire-history")
    await alice.recv_all(timeout=0.5)
    await alice.send("PRIVMSG #wire-history :locked history line")

    await alice.send("HISTORY RECENT #wire-history 10")
    joined = await alice.recv_until("HISTORYEND")
    lines = joined.split("\r\n")

    # Terminator: exact literal wire line, no cursor/id field.
    assert lines[-1] == ":testserv HISTORYEND #wire-history :End of history"

    # The replay line for the message we just sent.
    message_lines = [ln for ln in lines[:-1] if "locked history line" in ln]
    assert len(message_lines) == 1
    prefix, verb, channel, nick, timestamp, text = message_lines[0].split(" ", 5)
    assert prefix == ":testserv"
    assert verb == "HISTORY"
    assert channel == "#wire-history"
    assert nick == "testserv-alice"
    float(timestamp)  # locks "str(entry.timestamp)" shape — a bare float, no cursor/id
    assert text == ":locked history line"


@pytest.mark.asyncio
async def test_client_history_recent_empty_channel_wire_shape(server, make_client):
    """Locks the HISTORYEND-only shape when a channel has no history.

    Before-state: skills/history.py:117-124 — ``get_recent`` returns ``[]``
    for an unknown/empty channel; the wire reply is HISTORYEND alone, no
    HISTORY lines and no error.
    """
    alice = await make_client(nick="testserv-alice", user="alice")

    await alice.send("HISTORY RECENT #never-touched 10")
    line = await alice.recv()

    assert line == ":testserv HISTORYEND #never-touched :End of history"
