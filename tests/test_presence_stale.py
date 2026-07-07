"""Acceptance tests for the stale-busy watchdog (t6).

Culture issue #53's handoff, verbatim: "a kill -9'd resident that last
reported busy is flagged within stale-T with zero cooperation from the dead
process; a slow-but-alive resident heartbeating through a long LLM call is
NOT flagged." These are real-TCP acceptance tests, not unit tests against
``PresenceSkill`` internals -- every assertion here observes state the same
way culture's residents CLI would: over the wire, via ``PRESENCE LIST``.
(``tests/test_presence.py`` covers the unit-level ``presumed_hung`` logic
and reaches into ``PresenceSkill.registry`` directly; that's a deliberate,
documented difference in altitude, not a duplicate.)

A kill -9 is simulated as a client that goes SILENT WITHOUT DISCONNECTING:
the socket stays open at the OS level and the process (this test) simply
never writes to it again. A clean FIN legitimately flips the row to
``offline`` instead (a different, and also-tested, code path -- see
``test_offline_after_clean_quit_never_flags`` below) -- that nuance is
spelled out in
``docs/specs/2026-07-07-agentirc-now-speaks-presence-the-ircd-parses-resid.md``.

Agentirc-native (not vendored). Boots its own ``IRCd`` via ``_boot_ircd``,
the same pattern ``tests/test_liveness.py`` uses for tunable timing knobs
the shared ``server`` fixture can't parametrize -- here, a fast
``PresenceConfig`` (``heartbeat_interval_seconds=1``,
``stale_after_seconds=2``) so the whole file stays fast.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from agentirc.config import PresenceConfig, ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd
from tests.conftest import IRCTestClient


def _free_port() -> int:
    """Grab a free TCP port on the loopback interface."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


async def _boot_ircd(
    tmp_path,
    *,
    heartbeat_interval_seconds: int = 1,
    stale_after_seconds: int = 2,
    name: str = "testserv",
) -> IRCd:
    """Boot an IRCd on a free loopback port with a tuned ``presence:`` block.

    The shared ``server`` fixture in ``tests/conftest.py`` uses
    ``PresenceConfig`` defaults (30s/90s) and can't be parametrized, so this
    suite boots its own instance the same way ``tests/test_liveness.py``'s
    ``_boot_ircd`` does for the liveness sweep's ping/pong knobs.
    """
    config = ServerConfig(
        name=name,
        host="127.0.0.1",
        port=_free_port(),
        webhook_port=0,
        presence=PresenceConfig(
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            stale_after_seconds=stale_after_seconds,
        ),
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
    )
    ircd = IRCd(config)
    await ircd.start()
    return ircd


async def _connect(ircd: IRCd) -> IRCTestClient:
    reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
    return IRCTestClient(reader, writer)


async def _register(client: IRCTestClient, nick: str) -> None:
    """Drive NICK/USER and drain exactly the welcome burst (bounded, fast)."""
    await client.send(f"NICK {nick}")
    await client.send(f"USER {nick} 0 * :{nick}")
    await client.recv_until("004")


def _presence_line(payload: dict) -> str:
    return f"PRESENCE :{json.dumps(payload)}"


async def _list_presence(observer: IRCTestClient) -> dict[str, dict]:
    """Send ``PRESENCE LIST``, read until ``PRESENCEEND``, return {nick: row}.

    This is the one and only way these acceptance tests observe presence
    state -- no reaching into ``PresenceSkill.registry`` (that's what the
    unit-level tests in ``tests/test_presence.py`` do).
    """
    await observer.send("PRESENCE LIST")
    combined = await observer.recv_until("PRESENCEEND")
    rows: dict[str, dict] = {}
    for line in combined.split("\r\n"):
        if "PRESENCELIST :" not in line:
            continue
        _, _, json_part = line.partition("PRESENCELIST :")
        row = json.loads(json_part)
        rows[row["nick"]] = row
    return rows


async def _poll_until(
    observer: IRCTestClient,
    nick: str,
    predicate,
    *,
    timeout: float = 5.0,
    interval: float = 0.15,
) -> dict[str, dict]:
    """Poll ``PRESENCE LIST`` until ``predicate(rows[nick])`` holds, or timeout.

    Same poll-loop shape as ``tests._helpers.wait_for`` (a ``loop.time()``
    deadline, then one final check past it) but can't reuse it directly:
    ``wait_for``'s contract is a *sync* predicate, and every check here
    needs an awaited network round trip (send ``PRESENCE LIST``, read until
    ``PRESENCEEND``). ``tests/test_liveness.py``'s
    ``test_idle_but_pong_responsive_client_never_reaped`` hits the identical
    constraint and likewise falls back to a manual ``loop.time()`` loop
    instead of ``wait_for``.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    rows: dict[str, dict] = {}
    while loop.time() < deadline:
        rows = await _list_presence(observer)
        if nick in rows and predicate(rows[nick]):
            return rows
        await asyncio.sleep(interval)
    return await _list_presence(observer)


# ---------------------------------------------------------------------------
# 1. Silent stall (kill -9 simulation) IS flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_stall_is_flagged_presumed_hung(tmp_path):
    """A busy resident that goes silent WITHOUT disconnecting is flagged
    ``presumed_hung: true`` within ``stale_after_seconds``, with zero
    cooperation from the stalled process (it writes nothing after the
    initial publish -- no more PRESENCE, no PING/PONG, nothing)."""
    ircd = await _boot_ircd(tmp_path)
    try:
        stalled = await _connect(ircd)
        observer = await _connect(ircd)
        try:
            await _register(stalled, "testserv-stalled")
            await _register(observer, "testserv-observer")

            sent_calls: list[str] = []
            original_send = stalled.send

            async def _tracked_send(text: str) -> None:
                sent_calls.append(text)
                await original_send(text)

            stalled.send = _tracked_send

            publish = _presence_line({"state": "working", "since": "T0"})
            await stalled.send(publish)
            # From here the stalled client writes NOTHING more -- the
            # kill -9 simulation. The socket stays open at the OS level;
            # only the read-time watchdog, not client cooperation, can
            # notice this.

            rows = await _poll_until(
                observer,
                "testserv-stalled",
                lambda row: row["presumed_hung"] is True,
                timeout=5.0,
            )
            row = rows["testserv-stalled"]
            assert row["presumed_hung"] is True
            # Stale, not offline/away -- state is unchanged from the last
            # publish; this is a hang, not a departure.
            assert row["state"] == "working"

            # The stalled client really did send zero further bytes.
            assert sent_calls == [publish]
        finally:
            await stalled.close()
            await observer.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# 2. Heartbeating through a long call is NEVER flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeating_through_long_call_never_flagged(tmp_path):
    """A resident that republishes every ~0.5s for ~4s (well past
    ``stale_after_seconds=2``) is never flagged, at any observed instant --
    simulating a slow-but-alive resident heartbeating through a long LLM
    call."""
    ircd = await _boot_ircd(tmp_path)
    try:
        heartbeating = await _connect(ircd)
        observer = await _connect(ircd)
        try:
            await _register(heartbeating, "testserv-alive")
            await _register(observer, "testserv-observer")

            loop = asyncio.get_event_loop()
            deadline = loop.time() + 4.0
            beat = 0
            observations = 0
            while loop.time() < deadline:
                await heartbeating.send(
                    _presence_line(
                        {"state": "thinking", "since": "T0", "task": f"beat-{beat}"}
                    )
                )
                beat += 1

                rows = await _list_presence(observer)
                row = rows.get("testserv-alive")
                assert row is not None
                assert row["presumed_hung"] is False
                observations += 1

                await asyncio.sleep(0.5)

            # At least several observations happened across the window
            # (sanity: the loop actually polled, it didn't just fall through).
            assert observations >= 4

            # One more observation right at the end of the window.
            rows = await _list_presence(observer)
            assert rows["testserv-alive"]["presumed_hung"] is False
        finally:
            await heartbeating.close()
            await observer.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# 3. One heartbeat un-flags a previously stale-flagged resident
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_heartbeat_unflags_after_stall(tmp_path):
    """After a stall flags a resident ``presumed_hung``, a single fresh
    publish immediately flips it back to false."""
    ircd = await _boot_ircd(tmp_path)
    try:
        recovering = await _connect(ircd)
        observer = await _connect(ircd)
        try:
            await _register(recovering, "testserv-recovers")
            await _register(observer, "testserv-observer")

            await recovering.send(_presence_line({"state": "working", "since": "T0"}))

            rows = await _poll_until(
                observer,
                "testserv-recovers",
                lambda row: row["presumed_hung"] is True,
                timeout=5.0,
            )
            assert rows["testserv-recovers"]["presumed_hung"] is True

            # One fresh heartbeat -- server stamps a new last_refresh.
            await recovering.send(_presence_line({"state": "working", "since": "T1"}))

            rows = await _poll_until(
                observer,
                "testserv-recovers",
                lambda row: row["presumed_hung"] is False,
                timeout=5.0,
            )
            assert rows["testserv-recovers"]["presumed_hung"] is False
        finally:
            await recovering.close()
            await observer.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# 4. Idle never flags, no matter how long it stays silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_never_flags_even_after_stale_after(tmp_path):
    """A resident that publishes ``idle`` and then goes silent for well
    past ``stale_after_seconds`` is never flagged -- idle is expected to go
    quiet, that's not a hang."""
    ircd = await _boot_ircd(tmp_path)
    try:
        idle = await _connect(ircd)
        observer = await _connect(ircd)
        try:
            await _register(idle, "testserv-idle")
            await _register(observer, "testserv-observer")

            await idle.send(_presence_line({"state": "idle", "since": "T0"}))

            # Silent for a comfortable margin past stale_after (2s).
            await asyncio.sleep(3.0)

            rows = await _list_presence(observer)
            row = rows["testserv-idle"]
            assert row["state"] == "idle"
            assert row["presumed_hung"] is False
        finally:
            await idle.close()
            await observer.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# 5. Offline (clean QUIT) never flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_after_clean_quit_never_flags(tmp_path):
    """A busy resident that QUITs cleanly flips to ``offline`` (retained
    row); waiting well past ``stale_after_seconds`` afterward never flags
    it -- a known, clean disconnect isn't a hang."""
    ircd = await _boot_ircd(tmp_path)
    try:
        quitting = await _connect(ircd)
        observer = await _connect(ircd)
        try:
            await _register(quitting, "testserv-quitter")
            await _register(observer, "testserv-observer")

            await quitting.send(_presence_line({"state": "working", "since": "T0"}))
            await quitting.send("QUIT :bye")

            rows = await _poll_until(
                observer,
                "testserv-quitter",
                lambda row: row["state"] == "offline",
                timeout=3.0,
            )
            assert rows["testserv-quitter"]["state"] == "offline"

            # Wait comfortably past stale_after -- still never flagged.
            await asyncio.sleep(3.0)
            rows = await _list_presence(observer)
            row = rows["testserv-quitter"]
            assert row["state"] == "offline"
            assert row["presumed_hung"] is False
        finally:
            await quitting.close()
            await observer.close()
    finally:
        await ircd.stop()
