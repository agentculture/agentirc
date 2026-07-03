"""Tests for the ``VERBS`` runtime verb-discovery query (task t9).

Acceptance contract (docs/plans/2026-07-01-...-agent-accessibility-release):
a single query verb returns a versioned, machine-parseable list of exactly
the verbs the running server accepts, derived from the live dispatch
surface (loaded skills + client handlers) -- NOT a hardcoded list -- plus
the negotiable capability names and the error-token vocabulary version.

The "not hardcoded" lock (``test_new_skill_verb_appears_without_code_change``)
is the load-bearing test here: it registers a brand-new skill with a
brand-new command *after* the server is already running and asserts the verb
shows up in the very next ``VERBS`` reply with zero changes to
``agentirc/client.py``. A hardcoded/cached verb list would fail this test;
a live enumeration passes it trivially.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from agentirc import __version__ as agentirc_version
from agentirc.client import Client
from agentirc.protocol import ERROR_TOKENS_VERSION, VERBS_DISCOVERY_VERSION
from agentirc.skill import Skill


def _independently_computed_verbs(server) -> list[str]:
    """Recompute the expected verb list from the same public structures
    ``Client._live_verbs`` reads, WITHOUT calling that method -- this is
    the "independent enumeration" the not-hardcoded lock relies on.

    Reuses ``Client._VERB_TOKEN_RE`` (the RFC-2812-grammar shape filter
    that excludes ``_handle_mode``'s internal ``CHANNEL_MODE``/``USER_MODE``
    routing targets) rather than re-typing the regex literal, so the two
    copies of the filter can't silently drift apart -- but the union +
    enumeration logic itself is written fresh here, not delegated to the
    implementation.
    """
    verbs = {
        name[len("_handle_") :].upper() for name in dir(Client) if name.startswith("_handle_")
    }
    verbs = {v for v in verbs if Client._VERB_TOKEN_RE.match(v)}
    for skill in server.skills:
        verbs.update(skill.commands)
    return sorted(verbs)


def _decode_verbs_line(line: str) -> tuple[str, dict]:
    """Parse a ``:<server> VERBS <version> :<base64-json>`` line.

    Returns ``(version, payload)``.
    """
    assert line.startswith(":"), f"expected a server-prefixed line, got {line!r}"
    _, rest = line[1:].split(" ", 1)
    verb, version, b64_part = rest.split(" ", 2)
    assert verb == "VERBS"
    assert b64_part.startswith(":")
    payload = json.loads(base64.b64decode(b64_part[1:]))
    return version, payload


class _FrobSkill(Skill):
    """A minimal skill claiming a command nobody else does, registered at
    runtime -- proves VERBS reflects the *live* skill registry, not a
    snapshot taken at server-start."""

    name = "frob-test-skill"
    commands = {"FROBTEST"}


@pytest.mark.asyncio
async def test_registered_client_gets_verbs_reply(server, make_client):
    """A registered client's VERBS query gets exactly one VERBS reply line."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_lines = [l for l in lines if " VERBS " in l]
    assert len(verbs_lines) == 1, f"expected exactly one VERBS line, got {lines!r}"


@pytest.mark.asyncio
async def test_payload_decodes_as_canonical_json_with_four_keys(server, make_client):
    """The trailing base64 blob decodes to canonical JSON with exactly the
    four documented keys -- no more, no less."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    version, payload = _decode_verbs_line(verbs_line)

    assert version == str(VERBS_DISCOVERY_VERSION)
    assert set(payload.keys()) == {"verbs", "caps", "error_tokens_version", "server_version"}

    # Canonical encoding: sorted keys, compact separators -- re-encoding
    # must byte-match what's on the wire (this is what "canonical" buys
    # a parser: no need to normalize before comparing/hashing).
    b64_part = verbs_line.split(" ", 3)[3][1:]
    raw = base64.b64decode(b64_part).decode("utf-8")
    assert raw == json.dumps(payload, separators=(",", ":"), sort_keys=True)


@pytest.mark.asyncio
async def test_verbs_list_matches_live_dispatch_surface(server, make_client):
    """The verbs list exactly matches an independently-computed enumeration
    of the live dispatch surface (Client handlers union skill commands) --
    the core "not a hardcoded list" assertion."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)

    assert payload["verbs"] == _independently_computed_verbs(server)
    # Sorted, per the payload contract.
    assert payload["verbs"] == sorted(payload["verbs"])


@pytest.mark.asyncio
async def test_new_skill_verb_appears_without_code_change(server, make_client):
    """Registering a brand-new skill at runtime (after the server already
    started) makes its command show up in the very next VERBS reply, with
    zero edits to agentirc/client.py. This is the lock against a hardcoded
    or cached verb list."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, before_payload = _decode_verbs_line(verbs_line)
    assert "FROBTEST" not in before_payload["verbs"]

    await server.register_skill(_FrobSkill())

    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, after_payload = _decode_verbs_line(verbs_line)

    assert "FROBTEST" in after_payload["verbs"]
    assert after_payload["verbs"] == _independently_computed_verbs(server)


@pytest.mark.asyncio
async def test_verbs_appears_in_its_own_payload(server, make_client):
    """VERBS is discoverable via VERBS itself -- no special-casing needed
    since it's found the same way as any other Client handler."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)
    assert "VERBS" in payload["verbs"]


@pytest.mark.asyncio
async def test_caps_matches_supported_caps(server, make_client):
    """caps mirrors Client._SUPPORTED_CAPS exactly (sorted), not a literal
    copy-pasted into the handler."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)

    assert payload["caps"] == sorted(Client._SUPPORTED_CAPS)


@pytest.mark.asyncio
async def test_error_tokens_version_matches_protocol_constant(server, make_client):
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)

    assert payload["error_tokens_version"] == ERROR_TOKENS_VERSION


@pytest.mark.asyncio
async def test_server_version_matches_agentirc_dunder_version(server, make_client):
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)

    assert payload["server_version"] == agentirc_version


@pytest.mark.asyncio
async def test_unregistered_client_is_refused_consistently(server, make_client):
    """An unregistered connection issuing VERBS gets no reply -- the same
    silent pre-registration refusal ``_handle_join`` gives an unregistered
    client (see agentirc/client.py's ``_handle_join``/``_handle_user_mode``),
    not an error numeric."""
    client = await make_client()  # no NICK/USER
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.3)
    assert not any(" VERBS " in l for l in lines), f"unregistered VERBS got a reply: {lines!r}"

    # Cross-check: JOIN (an already-established pre-registration verb)
    # is refused the exact same way on this same unregistered connection --
    # confirms VERBS mirrors existing behavior rather than inventing a new
    # refusal shape.
    await client.send("JOIN #should-not-work")
    lines = await client.recv_all(timeout=0.3)
    assert not lines, f"unregistered JOIN unexpectedly got a reply too: {lines!r}"


@pytest.mark.asyncio
async def test_sample_of_listed_verbs_are_genuinely_dispatchable(server, make_client):
    """Spot-check that sending a representative sample of the listed verbs
    never yields ERR_UNKNOWNCOMMAND (421) -- i.e. the list isn't advertising
    verbs the dispatcher doesn't actually recognise. Covers a Client-level
    RFC verb, a channel verb, one verb from each of the four skills, and a
    bot-gated verb (which must fail with EVENTERR, not 421, absent the cap).
    QUIT is deliberately excluded from the sample -- it tears the connection
    down, which isn't useful to spot-check here.
    """
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)
    verbs = set(payload["verbs"])

    sample = [
        "PING",
        "CAP",
        "JOIN",
        "LIST",
        "WHO",
        "ROOMCREATE",
        "THREAD",
        "HISTORY",
        "ICON",
        "EVENTSUB",
        "VERBS",
    ]
    assert set(sample) <= verbs, "sample drifted out of sync with the live verbs list"

    for verb in sample:
        await client.send(verb)
        reply_lines = await client.recv_all(timeout=0.3)
        joined = "\n".join(reply_lines)
        assert "421" not in joined, f"{verb} yielded ERR_UNKNOWNCOMMAND: {reply_lines!r}"


@pytest.mark.asyncio
async def test_pass_is_not_listed(server, make_client):
    """PASS is deliberately absent -- on this server it's consumed by the
    S2S/C2S sniff in IRCd._handle_connection before a Client exists at all
    (this server repurposes it as the S2S-link auth handshake), so a live
    Client has no _handle_pass and no skill claims it. Listing it would
    break the "every listed verb is genuinely dispatchable" guarantee."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)

    assert "PASS" not in payload["verbs"]


@pytest.mark.asyncio
async def test_internal_mode_routing_helpers_are_not_listed(server, make_client):
    """CHANNEL_MODE/USER_MODE are _handle_mode's internal routing targets,
    not real verbs -- they must not leak into the discovery list even
    though they technically satisfy the getattr(f"_handle_{cmd.lower()}")
    dispatch convention."""
    client = await make_client("testserv-alice", "alice")
    await client.send("VERBS")
    lines = await client.recv_all(timeout=0.5)
    verbs_line = next(l for l in lines if " VERBS " in l)
    _, payload = _decode_verbs_line(verbs_line)

    assert "CHANNEL_MODE" not in payload["verbs"]
    assert "USER_MODE" not in payload["verbs"]


@pytest.mark.asyncio
async def test_concurrent_verbs_queries_do_not_interfere(server, make_client):
    """Two clients querying VERBS concurrently each get their own,
    identical reply -- no shared mutable state leaks between them."""
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")

    await asyncio.gather(alice.send("VERBS"), bob.send("VERBS"))
    alice_lines, bob_lines = await asyncio.gather(
        alice.recv_all(timeout=0.5), bob.recv_all(timeout=0.5)
    )
    alice_line = next(l for l in alice_lines if " VERBS " in l)
    bob_line = next(l for l in bob_lines if " VERBS " in l)
    _, alice_payload = _decode_verbs_line(alice_line)
    _, bob_payload = _decode_verbs_line(bob_line)
    assert alice_payload == bob_payload
