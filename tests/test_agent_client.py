"""Tests for the public reconnecting client transport, ``agentirc.agent_client``.

Agentirc-native (not vendored). Exercises the ``AgentClient`` connect /
register / join / send / receive roundtrip against a real in-process
``IRCd``, plus the kill-and-restart auto-reconnect + channel re-join
behaviour and the deterministic exponential-backoff sequence.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from agentirc.agent_client import AgentClient, IncomingMessage
from agentirc.config import ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd


async def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
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


async def _boot_ircd(tmp_path, port: int, name: str = "testserv") -> IRCd:
    """Boot an IRCd on a fixed loopback port with isolated audit output."""
    config = ServerConfig(
        name=name,
        host="127.0.0.1",
        port=port,
        webhook_port=0,
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
    )
    ircd = IRCd(config)
    await ircd.start()
    return ircd


@pytest.mark.asyncio
async def test_connect_register_join_send_receive(server):
    """An AgentClient connects, joins, and exchanges a channel PRIVMSG."""
    from tests.conftest import IRCTestClient

    port = server.config.port
    client = AgentClient(
        "127.0.0.1",
        port,
        "testserv-alice",
        channels=["#general"],
    )
    await client.connect()
    try:
        assert client.connected is True
        # The join is driven over TCP; wait until the server sees us in-channel.
        assert await _wait_for(
            lambda: "#general" in server.channels
            and any(m.nick == "testserv-alice" for m in server.channels["#general"].members)
        )

        # A vanilla test client joins and speaks; the AgentClient should read it.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        peer = IRCTestClient(reader, writer)
        await peer.send("NICK testserv-bob")
        await peer.send("USER bob 0 * :bob")
        await peer.recv_all(timeout=0.5)
        await peer.send("JOIN #general")
        await peer.recv_all(timeout=0.5)

        msgs = client.messages()
        await peer.send("PRIVMSG #general :hello from bob")
        incoming = await asyncio.wait_for(msgs.__anext__(), timeout=2.0)

        assert isinstance(incoming, IncomingMessage)
        assert incoming.channel == "#general"
        assert incoming.sender == "testserv-bob"
        assert incoming.text == "hello from bob"
        assert isinstance(incoming.tags, dict)
        assert "PRIVMSG" in incoming.raw

        await peer.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_from_agent_client_reaches_peer(server):
    """AgentClient.send delivers a PRIVMSG that a peer receives."""
    from tests.conftest import IRCTestClient

    port = server.config.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    peer = IRCTestClient(reader, writer)
    await peer.send("NICK testserv-bob")
    await peer.send("USER bob 0 * :bob")
    await peer.recv_all(timeout=0.5)
    await peer.send("JOIN #general")
    await peer.recv_all(timeout=0.5)

    client = AgentClient("127.0.0.1", port, "testserv-alice", channels=["#general"])
    await client.connect()
    try:
        assert await _wait_for(
            lambda: "#general" in server.channels
            and any(m.nick == "testserv-alice" for m in server.channels["#general"].members)
        )
        # Drain peer's view of alice joining.
        await peer.recv_all(timeout=0.5)

        await client.send("#general", "ping from alice")
        line = await peer.recv()
        assert "PRIVMSG" in line
        assert "#general" in line
        assert "ping from alice" in line
        assert "testserv-alice" in line
    finally:
        await client.close()
        await peer.close()


@pytest.mark.asyncio
async def test_context_manager_lifecycle(server):
    """``async with AgentClient(...)`` connects on enter and closes on exit."""
    port = server.config.port
    async with AgentClient("127.0.0.1", port, "testserv-alice") as client:
        assert client.connected is True
    assert client.connected is False


@pytest.mark.asyncio
async def test_kill_and_restart_reconnects_and_rejoins(tmp_path):
    """Client survives a server kill, reconnects on the same port, and re-joins."""
    from tests.conftest import IRCTestClient

    port = _free_port()
    ircd = await _boot_ircd(tmp_path, port)

    client = AgentClient(
        "127.0.0.1",
        port,
        "testserv-alice",
        channels=["#general"],
        initial_backoff=0.05,
        max_backoff=0.2,
    )
    await client.connect()
    try:
        assert client.connected is True
        assert await _wait_for(
            lambda: "#general" in ircd.channels
            and any(m.nick == "testserv-alice" for m in ircd.channels["#general"].members)
        )

        # Kill the server abruptly. IRCd.stop() itself now closes every
        # still-connected client's socket before returning (see ircd.py's
        # stop() — needed since Python 3.12.1, asyncio.Server.wait_closed()
        # blocks until all accepted connections have actually detached, so
        # stop() would otherwise hang forever with a client still attached).
        # The explicit close below is therefore redundant (a harmless
        # no-op on an already-closing transport) but kept so this test still
        # models "drop the live client sockets" directly and doesn't rely
        # solely on stop()'s internal behavior. ``clients`` is a public IRCd
        # attribute (see docs/api-stability.md).
        live_writers = [getattr(c, "writer", None) for c in list(ircd.clients.values())]
        await ircd.stop()
        for writer in live_writers:
            if writer is not None:
                writer.close()

        # The client should notice the drop and flip to disconnected.
        assert await _wait_for(lambda: client.connected is False, timeout=3.0)

        # Bring a fresh IRCd back up on the same port.
        ircd2 = await _boot_ircd(tmp_path, port)
        try:
            # The client should transparently reconnect and re-register.
            assert await _wait_for(lambda: client.connected is True, timeout=5.0)
            # ...and re-join #general on the fresh server.
            assert await _wait_for(
                lambda: "#general" in ircd2.channels
                and any(m.nick == "testserv-alice" for m in ircd2.channels["#general"].members),
                timeout=5.0,
            )

            # A new peer on the fresh server can now reach the reconnected client.
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            peer = IRCTestClient(reader, writer)
            await peer.send("NICK testserv-bob")
            await peer.send("USER bob 0 * :bob")
            await peer.recv_all(timeout=0.5)
            await peer.send("JOIN #general")
            await peer.recv_all(timeout=0.5)

            msgs = client.messages()
            await peer.send("PRIVMSG #general :back online")
            incoming = await asyncio.wait_for(msgs.__anext__(), timeout=3.0)
            assert incoming.channel == "#general"
            assert incoming.text == "back online"
            assert incoming.sender == "testserv-bob"

            await peer.close()
        finally:
            await ircd2.stop()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_backoff_sequence_is_exponential_and_capped():
    """Backoff starts at ~1s, doubles, and saturates at the configured max."""
    client = AgentClient(
        "127.0.0.1",
        6667,
        "testserv-alice",
        initial_backoff=1.0,
        max_backoff=60.0,
        backoff_factor=2.0,
    )
    delays = client._backoff_delays()
    seq = [next(delays) for _ in range(9)]
    assert seq == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


@pytest.mark.asyncio
async def test_backoff_sequence_custom_factor():
    """A custom initial/factor/max still produces a capped doubling-style ramp."""
    client = AgentClient(
        "127.0.0.1",
        6667,
        "testserv-alice",
        initial_backoff=0.5,
        max_backoff=4.0,
        backoff_factor=3.0,
    )
    delays = client._backoff_delays()
    seq = [next(delays) for _ in range(5)]
    assert seq == [0.5, 1.5, 4.0, 4.0, 4.0]


@pytest.mark.asyncio
async def test_connect_failure_raises_when_reconnect_disabled():
    """With reconnect disabled, a failed initial connect surfaces the error."""
    dead_port = _free_port()  # nothing listening here
    client = AgentClient("127.0.0.1", dead_port, "testserv-alice", reconnect=False)
    with pytest.raises(ConnectionError):
        await client.connect()
    assert client.connected is False
    await client.close()


@pytest.mark.asyncio
async def test_messages_iterator_terminates_after_close(server):
    """close() ends the messages() async iterator instead of blocking."""
    client = AgentClient("127.0.0.1", server.config.port, "testserv-alice")
    await client.connect()
    collected = []

    async def _drain():
        async for msg in client.messages():
            collected.append(msg)

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0.05)
    await client.close()
    # The drain coroutine should complete promptly now that the stream ended.
    await asyncio.wait_for(task, timeout=2.0)
    assert collected == []


@pytest.mark.asyncio
async def test_does_not_request_bot_capability_by_default(server):
    """Default caps request message-tags but never the bot capability."""
    from agentirc.protocol import BOT_CAP

    client = AgentClient("127.0.0.1", server.config.port, "testserv-alice")
    assert BOT_CAP not in client.caps
    assert "message-tags" in client.caps

    await client.connect()
    try:
        # Server-side: the connection did not negotiate the bot cap.
        assert await _wait_for(lambda: "testserv-alice" in server.clients)
        conn = server.clients["testserv-alice"]
        assert BOT_CAP not in conn.caps
        assert "message-tags" in conn.caps
    finally:
        await client.close()
