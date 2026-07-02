"""Tests for server liveness: periodic PING and dead-connection reaping (t5).

Agentirc-native (not vendored). Exercises the ``IRCd`` liveness sweep loop
(``agentirc/ircd.py:_liveness_sweep_loop``) against real in-process servers
booted with tiny ``ping_interval``/``pong_timeout`` values so the whole
suite stays fast, plus a couple of unit-level checks on
``Client._handle_pong``.

Scope per the design directive: LOCAL TCP clients only. Federation links,
``RemoteClient`` ghosts, and ``VirtualClient`` (no socket) are untouched by
the sweep and are not exercised here — see ``agentirc/ircd.py``'s
``_liveness_sweep_loop`` docstring.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from agentirc.agent_client import AgentClient
from agentirc.config import ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd
from agentirc._internal.protocol.message import Message
from tests.conftest import IRCTestClient


async def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


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
    ping_interval: float = 60.0,
    pong_timeout: float = 120.0,
    name: str = "testserv",
) -> IRCd:
    """Boot an IRCd on a free loopback port with tunable liveness knobs.

    The shared ``server`` fixture in ``tests/conftest.py`` uses the 60s/120s
    defaults and can't be parametrized, so liveness tests boot their own
    instance the same way ``tests/test_agent_client.py``'s ``_boot_ircd``
    does for its kill-and-restart scenario.
    """
    config = ServerConfig(
        name=name,
        host="127.0.0.1",
        port=_free_port(),
        webhook_port=0,
        ping_interval=ping_interval,
        pong_timeout=pong_timeout,
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
    )
    ircd = IRCd(config)
    await ircd.start()
    return ircd


async def _register(client: IRCTestClient, nick: str) -> None:
    """Drive NICK/USER and drain exactly the welcome burst (bounded, fast)."""
    await client.send(f"NICK {nick}")
    await client.send(f"USER {nick} 0 * :{nick}")
    await client.recv_until("004")


async def _join(client: IRCTestClient, channel: str) -> None:
    """Drive JOIN and drain exactly the NAMES burst (bounded, fast)."""
    await client.send(f"JOIN {channel}")
    await client.recv_until("366")


# ---------------------------------------------------------------------------
# Unit-level: _handle_pong updates liveness state directly
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Minimal stand-in for asyncio.StreamWriter — only what Client.__init__ needs."""

    def get_extra_info(self, _name, default=None):
        return default


def test_handle_pong_updates_last_activity():
    """`_handle_pong` (previously a no-op) explicitly stamps last_activity."""
    from agentirc.client import Client

    client = Client(reader=None, writer=_FakeWriter(), server=None)
    client.last_activity = 0.0
    client._handle_pong(Message(command="PONG", params=["testserv"]))
    assert client.last_activity > 0.0


def test_new_client_has_fresh_liveness_state():
    """A freshly constructed Client starts alive with no ping outstanding."""
    from agentirc.client import Client
    import time as _time

    before = _time.time()
    client = Client(reader=None, writer=_FakeWriter(), server=None)
    assert client.last_activity >= before
    assert client.last_ping_sent is None


# ---------------------------------------------------------------------------
# Config: disabled loop (interval <= 0) starts no task, sends no PINGs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_loop_starts_no_task(tmp_path):
    """ping_interval=0 disables the sweep loop entirely."""
    ircd = await _boot_ircd(tmp_path, ping_interval=0, pong_timeout=0.2)
    try:
        assert ircd._liveness_task is None
    finally:
        await ircd.stop()


@pytest.mark.asyncio
async def test_disabled_loop_sends_no_pings(tmp_path):
    """With the loop off, an idle client never receives a PING line."""
    ircd = await _boot_ircd(tmp_path, ping_interval=0, pong_timeout=0.2)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
        idle = IRCTestClient(reader, writer)
        try:
            await _register(idle, "testserv-idle")

            await asyncio.sleep(0.4)
            lines = await idle.recv_all(timeout=0.2)
            assert not any("PING" in line for line in lines)
            assert "testserv-idle" in ircd.clients
        finally:
            await idle.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# Enabled loop: idle-but-responsive clients survive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_client_receives_ping_after_interval(tmp_path):
    """An idle registered client receives a server-initiated PING once it
    has been quiet for longer than ping_interval."""
    ircd = await _boot_ircd(tmp_path, ping_interval=0.1, pong_timeout=1.0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
        idle = IRCTestClient(reader, writer)
        try:
            await _register(idle, "testserv-idle")
            line = await idle.recv_until("PING")
            assert "PING" in line
            assert ircd.config.name in line
        finally:
            await idle.close()
    finally:
        await ircd.stop()


@pytest.mark.asyncio
async def test_idle_but_pong_responsive_client_never_reaped(tmp_path):
    """A client that answers every server PING with PONG is never dropped,
    even across many ping_interval + pong_timeout windows."""
    ircd = await _boot_ircd(tmp_path, ping_interval=0.1, pong_timeout=0.1)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
        idle = IRCTestClient(reader, writer)
        try:
            await _register(idle, "testserv-idle")

            loop = asyncio.get_event_loop()
            deadline = loop.time() + 0.8  # several 0.1+0.1s reap windows
            saw_ping = False
            while loop.time() < deadline:
                try:
                    line = await idle.recv(timeout=0.15)
                except (asyncio.TimeoutError, ConnectionError):
                    continue
                if "PING" in line:
                    saw_ping = True
                    token = line.split(" ", 1)[1] if " " in line else ":testserv"
                    await idle.send(f"PONG {token}")

            assert saw_ping
            assert "testserv-idle" in ircd.clients
        finally:
            await idle.close()
    finally:
        await ircd.stop()


@pytest.mark.asyncio
async def test_active_chatter_ignoring_ping_never_reaped(tmp_path):
    """A client that keeps sending PRIVMSGs (and never reads/answers PING)
    stays connected — inbound activity of any kind counts as liveness."""
    ircd = await _boot_ircd(tmp_path, ping_interval=0.1, pong_timeout=0.1)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
        chatty = IRCTestClient(reader, writer)
        try:
            await _register(chatty, "testserv-chatty")
            await _join(chatty, "#general")

            loop = asyncio.get_event_loop()
            deadline = loop.time() + 0.8  # several 0.1+0.1s reap windows
            while loop.time() < deadline:
                await chatty.send("PRIVMSG #general :still here")
                await asyncio.sleep(0.03)

            assert "testserv-chatty" in ircd.clients
        finally:
            await chatty.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# Enabled loop: a dead peer is reaped through the normal disconnect path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_killed_peer_reaped_within_interval_plus_timeout(tmp_path):
    """A TCP peer that goes silent (no QUIT, no FIN) is dropped by the
    server within ping_interval + pong_timeout, and its nick leaves the
    channel/NAMES — exactly as a natural disconnect would."""
    ircd = await _boot_ircd(tmp_path, ping_interval=0.1, pong_timeout=0.1)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
        dead = IRCTestClient(reader, writer)
        await _register(dead, "testserv-dead")
        await _join(dead, "#general")

        # Sanity: the dead peer really is present before we go silent.
        assert "testserv-dead" in ircd.clients
        assert any(m.nick == "testserv-dead" for m in ircd.channels["#general"].members)

        # Hard-stop: no QUIT, no close() — the socket stays open at the OS
        # level and the peer (this test) simply never reads or writes again.
        # The reap sweep, not a TCP-level FIN/RST, is what must notice this.

        assert await _wait_for(lambda: "testserv-dead" not in ircd.clients, timeout=3.0)
        assert await _wait_for(
            lambda: "#general" not in ircd.channels
            or not any(m.nick == "testserv-dead" for m in ircd.channels["#general"].members),
            timeout=1.0,
        )

        # A fresh observer's NAMES view confirms the roster is clean.
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", ircd.config.port)
        watcher = IRCTestClient(reader2, writer2)
        try:
            await _register(watcher, "testserv-watch")
            await watcher.send("JOIN #general")
            lines = await watcher.recv_all(timeout=0.5)
            names_lines = [line for line in lines if " 353 " in line]
            assert names_lines
            assert "testserv-dead" not in names_lines[0]
        finally:
            await watcher.close()

        # The server actually closed its side — proves reap, not just our
        # own bookkeeping. Drain whatever PING bytes piled up in the OS
        # receive buffer while we weren't reading, then expect EOF (b"").
        eof_seen = False
        try:
            async with asyncio.timeout(1.0):
                while True:
                    data = await reader.read(4096)
                    if data == b"":
                        eof_seen = True
                        break
        except OSError:
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass
        assert eof_seen
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# AgentClient answers PING internally — never reaped while healthy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_client_survives_liveness_cycles(tmp_path):
    """AgentClient answers server PING transparently (see
    AgentClient._dispatch), so it is never reaped by the liveness sweep."""
    # Not 0.1/0.1: with a reap threshold that tight, event-loop starvation
    # under `-n auto` parallel load can delay the PONG past the deadline and
    # reap a perfectly healthy client (observed as an intermittent failure).
    # 0.3/0.7 still exercises multiple ping windows but tolerates jitter.
    ircd = await _boot_ircd(tmp_path, ping_interval=0.3, pong_timeout=0.7)
    try:
        client = AgentClient("127.0.0.1", ircd.config.port, "testserv-alice")
        await client.connect()
        try:
            assert client.connected is True
            # Outlive several ping_interval + pong_timeout windows while the
            # application layer does nothing — only PONGs keep it alive.
            await asyncio.sleep(1.5)
            assert client.connected is True
            assert "testserv-alice" in ircd.clients
        finally:
            await client.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# The default (loop ON, 60s/120s) does not disturb a normal short-lived test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_liveness_config_does_not_fire_in_a_normal_test(server, make_client):
    """Sanity check for the acceptance criterion 'loop ON by default': the
    shared `server` fixture uses ServerConfig defaults (60s/120s), so a
    normal-speed test never sees a PING."""
    assert server.config.ping_interval == 60.0
    assert server.config.pong_timeout == 120.0
    assert server._liveness_task is not None

    client = await make_client(nick="testserv-ori", user="ori")
    lines = await client.recv_all(timeout=0.3)
    assert not any("PING" in line for line in lines)
