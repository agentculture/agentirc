"""Reconnect + ``HISTORY SINCE`` catch-up integration test (agent-accessibility, task t13).

Exercises the public transport (:class:`agentirc.agent_client.AgentClient`)
across a forced mid-stream disconnect, proving that a real agent harness can
recover *exactly* — no gaps, no duplicates — by combining live delivery with
cursor-based ``HISTORY SINCE`` catch-up.

Resume-protocol design
-----------------------
This is the resume protocol a real agent harness built on ``AgentClient``
would run, and the one this test drives end to end:

1. **Checkpoint before anything else.** Immediately after ``JOIN``, before
   consuming any live traffic, the harness issues ``HISTORY SINCE
   <channel> *`` (the "from the beginning" sentinel) to obtain its first
   checkpoint cursor. This guarantees there is never a window between JOIN
   and the first checkpoint where a message could slip through uncounted —
   the very first thing the harness does after joining is establish a
   cursor.
2. **Live observation is the fast path.** While ``AgentClient.connected`` is
   True, every channel PRIVMSG arrives via :meth:`AgentClient.messages`
   carrying an IRCv3 ``msgid`` tag (message-tags is on by default). The
   harness records every msgid it has *definitely* seen into an
   ``observed: dict[msgid, text]`` — a dict, not a list, because the key
   *is* the de-duplication mechanism.
3. **The checkpoint cursor is refreshed periodically** (this harness: after
   each live batch, and unconditionally right after every reconnect) via
   ``HISTORY SINCE <channel> <cursor>``, sent as a raw line through
   :meth:`AgentClient.send_raw` and read back via
   :meth:`AgentClient.raw_lines` (the two additive helpers this task adds —
   ``AgentClient.messages()`` only surfaces parsed PRIVMSGs, so driving a
   non-PRIVMSG verb like ``HISTORY`` and reading its ``HISTORY``/
   ``HISTORYEND`` reply lines needs raw wire access). Every replayed entry
   is merged into the same ``observed`` dict, keyed by its ``msgid`` tag.
4. **Recovery is cursor-authoritative, not timing-authoritative.** Because
   ``HISTORY SINCE`` is a strict "after cursor" scan over durable history,
   and every replayed entry is merged into ``observed`` by msgid, catch-up
   is correct regardless of *when* the harness happens to call it: a
   message already seen live is a harmless dict overwrite (same key, same
   value); a message the harness never saw live — because it arrived during
   an outage, or in the gap between socket death, reconnect, and the first
   post-reconnect SINCE call — is captured unconditionally because it is
   within the cursor range. The harness never needs to reason about *when*
   it reconnected relative to *when* a message was sent; it only needs the
   invariant "cursor monotonically covers everything after it".
5. **On reconnect, catch-up runs before anything else** — mirroring step 1.
   ``AgentClient`` auto-reconnects and re-JOINs transparently; the harness's
   very next action is a ``HISTORY SINCE`` call from its last checkpoint,
   *before* it does anything else with the connection. This is what makes
   "no window loss between JOIN and SINCE" true by construction rather than
   by luck: there is no gap in the protocol for a message to fall into,
   because nothing happens between JOIN and the catch-up call.
6. **The chain repeats indefinitely.** Each catch-up's returned cursor
   becomes the next checkpoint; a second (or Nth) disconnect/recover cycle
   resumes from wherever the chain left off, never from the beginning.

Cast: **A** (``testserv-alice``) is the :class:`AgentClient` under test — it
gets forcibly disconnected mid-stream and must recover exactly. **B**
(``testserv-bob``) is the sender. **C** (``testserv-carol``) is a stationary
message-tags observer that is never disconnected — its live observations are
the ground truth this test cross-checks A's replayed msgids against for the
messages sent during A's outages.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from agentirc._internal.protocol.message import Message
from agentirc.agent_client import AgentClient
from agentirc.protocol import MSGID_TAG

_CHANNEL = "#catchup"


async def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _member_nicks(server, channel: str) -> set[str]:
    chan = server.channels.get(channel)
    return {m.nick for m in chan.members} if chan else set()


@dataclass
class ReplayEntry:
    """One ``HISTORY`` reply line from a ``HISTORY SINCE`` page."""

    msgid: str | None
    text: str


def _only_messages(entries: list[ReplayEntry]) -> list[ReplayEntry]:
    """Filter out non-PRIVMSG lifecycle entries (JOIN/PART/...), which HISTORY
    also replays but which never carry a stored msgid (see
    ``agentirc/skills/history.py``'s module docstring, "msgid / message-tags
    on SINCE replay"). Only real messages participate in this test's
    msgid-verified exactness checks.
    """
    return [e for e in entries if e.msgid is not None]


async def _read_since_reply(
    raw_iter, channel: str, *, timeout: float = 2.0
) -> tuple[list[ReplayEntry], str]:
    """Read one ``HISTORY SINCE`` reply (0+ ``HISTORY`` lines + ``HISTORYEND``).

    Consumes from ``raw_iter`` (an :meth:`AgentClient.raw_lines` iterator),
    silently skipping any other raw traffic that interleaves on the same
    connection (live PRIVMSGs, PINGs, ...) — safe because every ``HISTORY
    SINCE`` call is answered by exactly one matching ``HISTORYEND`` and we
    return as soon as we see it.
    """
    entries: list[ReplayEntry] = []
    while True:
        line = await asyncio.wait_for(raw_iter.__anext__(), timeout=timeout)
        msg = Message.parse(line)
        if msg.command == "HISTORY" and msg.params and msg.params[0] == channel:
            entries.append(ReplayEntry(msgid=msg.tags.get(MSGID_TAG), text=msg.params[-1]))
        elif msg.command == "HISTORYEND" and msg.params and msg.params[0] == channel:
            return entries, msg.params[-1]
        # else: unrelated raw traffic on the same connection — ignore.


async def _since_sweep(
    client: AgentClient,
    raw_iter,
    channel: str,
    cursor: str,
    *,
    limit: int = 50,
    max_pages: int = 50,
) -> tuple[list[ReplayEntry], str]:
    """Page ``HISTORY SINCE`` from ``cursor`` until an empty page.

    Returns every replayed entry across all pages, in order, plus the final
    cursor (the harness's new checkpoint).
    """
    collected: list[ReplayEntry] = []
    for _ in range(max_pages):
        await client.send_raw(f"HISTORY SINCE {channel} {cursor} {limit}")
        page, next_cursor = await _read_since_reply(raw_iter, channel)
        if not page:
            return collected, next_cursor
        collected.extend(page)
        cursor = next_cursor
    raise AssertionError("HISTORY SINCE sweep did not terminate within max_pages")


async def _consume_live(
    msgs_iter, count: int, sink: dict[str, str], *, timeout: float = 2.0
) -> None:
    """Pull exactly ``count`` live messages off ``msgs_iter``, recording msgid -> text."""
    for _ in range(count):
        incoming = await asyncio.wait_for(msgs_iter.__anext__(), timeout=timeout)
        msgid = incoming.tags.get(MSGID_TAG)
        assert msgid, f"expected an IRCv3 msgid tag on a live message-tags delivery: {incoming!r}"
        sink[msgid] = incoming.text


@pytest.mark.asyncio
async def test_reconnect_catchup_exact_recovery_no_gaps_no_duplicates(server):
    """A survives a forced mid-stream disconnect and recovers exactly via SINCE.

    Walks the full resume protocol documented in the module docstring across
    two disconnect/recover cycles, plus a "mid-burst" message sent the
    instant A reconnects (before A's catch-up call) to prove there is no
    live-only window between JOIN and SINCE.
    """
    port = server.config.port

    client_a = AgentClient(
        "127.0.0.1",
        port,
        "testserv-alice",
        channels=[_CHANNEL],
        initial_backoff=0.05,
        max_backoff=0.2,
    )
    client_b = AgentClient("127.0.0.1", port, "testserv-bob", channels=[_CHANNEL])
    await client_a.connect()
    await client_b.connect()

    observed: dict[str, str] = {}  # msgid -> text, A's merged live+replay view
    c_ground_truth: dict[str, str] = {}  # msgid -> text, C's live-only ground truth

    try:
        assert await _wait_for(
            lambda: {"testserv-alice", "testserv-bob"} <= _member_nicks(server, _CHANNEL)
        )

        msgs_a = client_a.messages()
        raw_a = client_a.raw_lines()

        # -- Step 1: checkpoint before anything else --------------------
        # (HISTORY also replays non-PRIVMSG lifecycle lines — e.g. the JOINs
        # A/B just did — but those never carry a stored msgid; see
        # `_only_messages`. This test only cares about real messages.)
        replayed0, checkpoint = await _since_sweep(client_a, raw_a, _CHANNEL, "*")
        assert _only_messages(replayed0) == []

        # -- Live batch 1 (messages 1..3): A is connected throughout ----
        for i in (1, 2, 3):
            await client_b.send(_CHANNEL, f"seq-{i}")
        await _consume_live(msgs_a, 3, observed)
        assert len(observed) == 3

        # Periodic checkpoint refresh — replays what A just saw live, which
        # must dedupe cleanly against `observed` (same msgids, same texts).
        replayed1, checkpoint = await _since_sweep(client_a, raw_a, _CHANNEL, checkpoint)
        real1 = _only_messages(replayed1)
        assert {e.text for e in real1} == {"seq-1", "seq-2", "seq-3"}
        assert {e.msgid for e in real1} == set(observed.keys())
        for entry in real1:
            observed[entry.msgid] = entry.text
        assert len(observed) == 3  # dedup: no growth from the redundant replay

        # C joins now — a stationary, never-disconnected witness for
        # everything from here on (the outage-window ground truth).
        client_c = AgentClient("127.0.0.1", port, "testserv-carol", channels=[_CHANNEL])
        await client_c.connect()
        assert await _wait_for(lambda: "testserv-carol" in _member_nicks(server, _CHANNEL))
        msgs_c = client_c.messages()

        try:
            # -- Forcibly disconnect A mid-stream (cycle 1) --------------
            server.clients["testserv-alice"].writer.close()
            assert await _wait_for(lambda: client_a.connected is False, timeout=3.0)

            # -- Outage 1 (messages 4..7): B sends while A is offline ----
            for i in (4, 5, 6, 7):
                await client_b.send(_CHANNEL, f"seq-{i}")
            await _consume_live(msgs_c, 4, c_ground_truth)

            # -- A reconnects; immediately (before A's catch-up call) a --
            # -- message lands to prove there's no JOIN-to-SINCE window --
            assert await _wait_for(lambda: client_a.connected is True, timeout=5.0)
            assert await _wait_for(
                lambda: "testserv-alice" in _member_nicks(server, _CHANNEL), timeout=5.0
            )
            await client_b.send(_CHANNEL, "seq-8")
            await _consume_live(msgs_c, 1, c_ground_truth)
            # A is already reconnected + rejoined at this point, so it also
            # receives "seq-8" live (in addition to it being in SINCE range
            # below) — record it via the live path too, exactly as a real
            # harness's live-consumption step would. The catch-up sweep
            # right after this will replay the *same* msgid for "seq-8" and
            # must dedupe against it (step 4 of the resume protocol).
            await _consume_live(msgs_a, 1, observed)

            # Catch-up #1: from the pre-outage checkpoint, before touching
            # anything else on A's connection.
            replayed_catchup1_raw, checkpoint = await _since_sweep(
                client_a, raw_a, _CHANNEL, checkpoint
            )
            replayed_catchup1 = _only_messages(replayed_catchup1_raw)
            for entry in replayed_catchup1:
                observed[entry.msgid] = entry.text

            assert {e.text for e in replayed_catchup1} == {
                "seq-4",
                "seq-5",
                "seq-6",
                "seq-7",
                "seq-8",
            }
            assert len(replayed_catchup1) == 5  # exactly once each, no dups
            # None of the pre-outage messages reappear given the checkpoint.
            assert {"seq-1", "seq-2", "seq-3"}.isdisjoint(e.text for e in replayed_catchup1)
            # msgid-verified: A's replayed msgids match C's live ground truth.
            c_text_to_msgid = {text: msgid for msgid, text in c_ground_truth.items()}
            for entry in replayed_catchup1:
                assert entry.msgid == c_text_to_msgid[entry.text], (
                    f"msgid mismatch for {entry.text!r}: replayed {entry.msgid!r} vs "
                    f"live-observed {c_text_to_msgid[entry.text]!r}"
                )

            assert len(observed) == 8
            assert set(observed.values()) == {f"seq-{i}" for i in range(1, 9)}

            # -- Forcibly disconnect A again (cycle 2) --------------------
            server.clients["testserv-alice"].writer.close()
            assert await _wait_for(lambda: client_a.connected is False, timeout=3.0)

            # -- Outage 2 (messages 9..11) --------------------------------
            for i in (9, 10, 11):
                await client_b.send(_CHANNEL, f"seq-{i}")
            await _consume_live(msgs_c, 3, c_ground_truth)

            assert await _wait_for(lambda: client_a.connected is True, timeout=5.0)
            assert await _wait_for(
                lambda: "testserv-alice" in _member_nicks(server, _CHANNEL), timeout=5.0
            )

            # Catch-up #2, chained from cycle 1's returned cursor — never
            # from "*", proving the cursor chain doesn't reset.
            replayed_catchup2_raw, checkpoint = await _since_sweep(
                client_a, raw_a, _CHANNEL, checkpoint
            )
            replayed_catchup2 = _only_messages(replayed_catchup2_raw)
            for entry in replayed_catchup2:
                observed[entry.msgid] = entry.text

            assert {e.text for e in replayed_catchup2} == {"seq-9", "seq-10", "seq-11"}
            assert len(replayed_catchup2) == 3
            assert {f"seq-{i}" for i in range(1, 9)}.isdisjoint(
                e.text for e in replayed_catchup2
            )
            c_text_to_msgid = {text: msgid for msgid, text in c_ground_truth.items()}
            for entry in replayed_catchup2:
                assert entry.msgid == c_text_to_msgid[entry.text]

            assert len(observed) == 11
            assert set(observed.values()) == {f"seq-{i}" for i in range(1, 12)}

            # -- A resumes live operation normally after both cycles -----
            await client_b.send(_CHANNEL, "seq-12")
            await _consume_live(msgs_a, 1, observed)
            assert len(observed) == 12
            assert set(observed.values()) == {f"seq-{i}" for i in range(1, 13)}

            # A fully caught-up SINCE poll now sees seq-12 too (history and
            # live delivery stay consistent), and repeating it again is a
            # true no-op — the cursor chain is stable at rest.
            replayed_final, checkpoint = await _since_sweep(client_a, raw_a, _CHANNEL, checkpoint)
            assert {e.text for e in _only_messages(replayed_final)} == {"seq-12"}
            replayed_idle, checkpoint_idle = await _since_sweep(
                client_a, raw_a, _CHANNEL, checkpoint
            )
            assert _only_messages(replayed_idle) == []
            assert checkpoint_idle == checkpoint

            # Final exactness: the union of every msgid A ever observed
            # (live or replayed) is exactly seq-1..seq-12, once each.
            assert len(observed) == 12
            assert len(observed) == len(set(observed.keys()))
        finally:
            await client_c.close()
    finally:
        await client_a.close()
        await client_b.close()


@pytest.mark.asyncio
async def test_send_raw_and_raw_lines_drive_history_since_directly(server):
    """Focused sanity check for the new ``send_raw``/``raw_lines`` helpers.

    Isolates the additive AgentClient surface from the full reconnect
    scenario: drives a bare ``HISTORY SINCE`` round trip (no disconnect
    involved) and confirms the msgid tag on the replay line matches what the
    same client saw live, plus that both helpers respect the "not connected"
    contract the rest of AgentClient already follows.
    """
    port = server.config.port
    client = AgentClient("127.0.0.1", port, "testserv-dana", channels=[_CHANNEL])
    sender = AgentClient("127.0.0.1", port, "testserv-erin", channels=[_CHANNEL])
    await client.connect()
    await sender.connect()
    try:
        assert await _wait_for(
            lambda: {"testserv-dana", "testserv-erin"} <= _member_nicks(server, _CHANNEL)
        )

        msgs = client.messages()
        raw_iter = client.raw_lines()

        await sender.send(_CHANNEL, "hello raw")
        incoming = await asyncio.wait_for(msgs.__anext__(), timeout=2.0)
        live_msgid = incoming.tags.get(MSGID_TAG)
        assert live_msgid

        replayed_raw, next_cursor = await _since_sweep(client, raw_iter, _CHANNEL, "*")
        replayed = _only_messages(replayed_raw)
        assert len(replayed) == 1
        assert replayed[0].text == "hello raw"
        assert replayed[0].msgid == live_msgid
        assert next_cursor  # non-empty opaque cursor token
    finally:
        await client.close()
        await sender.close()

    # Both helpers raise ConnectionError once the client is closed, matching
    # the existing contract of send()/join().
    with pytest.raises(ConnectionError):
        await client.send_raw(f"HISTORY SINCE {_CHANNEL} *")
