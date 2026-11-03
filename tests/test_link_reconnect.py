# tests/test_link_reconnect.py
"""S2S link auto-reconnect tests."""

import asyncio

import pytest

from agentirc.config import LinkConfig, ServerConfig
from agentirc.ircd import IRCd
from tests.conftest import TEST_LINK_PASSWORD


@pytest.mark.asyncio
async def test_link_drop_triggers_retry(tmp_path):
    """When a linked peer drops (non-SQUIT), the server schedules retry."""
    from tests._helpers import boot_linked_pair, link_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    await link_pair(server_a, server_b)
    assert "beta" in server_a.links

    # Kill server B abruptly (non-SQUIT drop)
    await server_b.stop()

    # Wait for alpha to detect the link drop and schedule retry
    for _ in range(50):
        if "beta" in server_a._link_retry_state:
            break
        await asyncio.sleep(0.05)

    assert "beta" in server_a._link_retry_state
    assert server_a._link_retry_state["beta"]["task"] is not None

    # Cleanup
    await server_a.stop()


@pytest.mark.asyncio
async def test_squit_does_not_trigger_retry(tmp_path):
    """When a peer sends SQUIT, no retry should be scheduled."""
    from tests._helpers import boot_linked_pair, link_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    await link_pair(server_a, server_b)
    assert "beta" in server_a.links

    # Have server B send SQUIT to server A (graceful delink)
    link_to_alpha = server_b.links["alpha"]
    await link_to_alpha.send_raw("SQUIT beta :Shutting down gracefully")

    # Wait for alpha to process the SQUIT and remove the link
    for _ in range(50):
        if "beta" not in server_a.links:
            break
        await asyncio.sleep(0.05)

    assert "beta" not in server_a.links
    # SQUIT should NOT trigger retry
    assert "beta" not in server_a._link_retry_state

    # Cleanup
    await server_a.stop()
    await server_b.stop()


@pytest.mark.asyncio
async def test_incoming_connection_cancels_retry(tmp_path):
    """When a peer reconnects inbound while retry is pending, retry is cancelled."""
    from tests._helpers import boot_linked_pair, link_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    await link_pair(server_a, server_b)
    assert "beta" in server_a.links

    # Stash config_a/b for the post-restart retry-redirect step (was in
    # the inline boot block; now needs explicit reference for the
    # reconnect logic that follows).
    config_a = server_a.config
    config_b = server_b.config

    # Kill server B to trigger retry on A
    await server_b.stop()

    # Wait for retry to be scheduled on A
    for _ in range(50):
        if "beta" in server_a._link_retry_state:
            break
        await asyncio.sleep(0.05)
    assert "beta" in server_a._link_retry_state

    # Restart server B and have it connect inbound to A
    server_b2 = IRCd(config_b)
    await server_b2.start()
    server_b2.config.port = server_b2._server.sockets[0].getsockname()[1]
    # Update A's link config so retry would point at the new B port
    config_a.links[0].port = server_b2.config.port

    # B2 connects to A (inbound connection to A)
    await server_b2.connect_to_peer(
        "127.0.0.1", server_a.config.port, TEST_LINK_PASSWORD
    )
    for _ in range(50):
        if "beta" in server_a.links:
            break
        await asyncio.sleep(0.05)

    assert "beta" in server_a.links
    # Retry state should be cleared by the incoming connection
    assert "beta" not in server_a._link_retry_state

    # Cleanup
    await server_a.stop()
    await server_b2.stop()


@pytest.mark.asyncio
async def test_reconnect_after_initial_failure():
    """If initial connect_to_peer fails, server schedules retry."""
    link_password = TEST_LINK_PASSWORD
    config_a = ServerConfig(
        name="alpha",
        host="127.0.0.1",
        port=0,
        links=[
            LinkConfig(
                name="beta", host="127.0.0.1", port=16999, password=link_password
            )
        ],
    )

    server_a = IRCd(config_a)
    await server_a.start()
    server_a.config.port = server_a._server.sockets[0].getsockname()[1]

    # Simulate initial connection failure (port 16999 has nothing listening)
    for lc in config_a.links:
        try:
            await server_a.connect_to_peer(lc.host, lc.port, lc.password, lc.trust)
        except Exception:
            server_a.maybe_retry_link(lc.name)

    # Retry should be scheduled
    assert "beta" in server_a._link_retry_state

    # Clean up
    server_a._link_retry_state["beta"]["task"].cancel()
    await server_a.stop()


# --- Failures before the handshake completes (culture#510, culture#519) ---


async def _wait_for(predicate, attempts: int = 100) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _cancel_retry(server: IRCd, peer: str) -> None:
    state = server._link_retry_state.get(peer)
    if state and state["task"]:
        state["task"].cancel()


async def _server_with_peer_at(port: int) -> IRCd:
    server = IRCd(
        ServerConfig(
            name="alpha",
            host="127.0.0.1",
            port=0,
            links=[LinkConfig(name="beta", host="127.0.0.1", port=port, password="x")],
        )
    )
    await server.start()
    return server


async def _hang_up(_reader, writer):
    writer.close()


async def _mismatched_pair(tmp_path) -> tuple[IRCd, IRCd]:
    """Two booted servers where beta refuses alpha's password."""
    from tests._helpers import boot_linked_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    server_a.config.links[0].port = server_b.config.port
    server_b.config.links[0].password = "a-different-secret"
    return server_a, server_b


async def _dial_beta(server_a: IRCd, server_b: IRCd) -> None:
    await server_a.connect_to_peer("127.0.0.1", server_b.config.port, TEST_LINK_PASSWORD, "full")
    assert await _wait_for(lambda: "beta" in server_a._link_retry_state)


async def _stop_after_retry(server_a: IRCd, server_b: IRCd) -> None:
    await _cancel_retry(server_a, "beta")
    await server_a.stop()
    await server_b.stop()


@pytest.mark.asyncio
async def test_rejected_handshake_logs_reason_and_retries(tmp_path, caplog):
    """A peer refusing the password used to end the link silently, with no retry."""
    server_a, server_b = await _mismatched_pair(tmp_path)

    with caplog.at_level("INFO"):
        await _dial_beta(server_a, server_b)

    assert "beta" not in server_a.links
    # Beta's own log says "Bad password" too, so check what alpha heard from it.
    assert "Peer beta sent ERROR: Bad password" in caplog.text
    assert "beta" in caplog.text
    assert TEST_LINK_PASSWORD not in caplog.text
    assert "a-different-secret" not in caplog.text

    await _stop_after_retry(server_a, server_b)


@pytest.mark.asyncio
async def test_peer_hanging_up_before_handshake_retries(caplog):
    """A peer that accepts and closes leaves no name to retry by, unless we kept it."""

    peer = await asyncio.start_server(_hang_up, "127.0.0.1", 0)
    port = peer.sockets[0].getsockname()[1]
    server = await _server_with_peer_at(port)

    with caplog.at_level("WARNING"):
        await server.connect_to_peer("127.0.0.1", port, "x", "full")
        assert await _wait_for(lambda: "beta" in server._link_retry_state)

    assert "before the handshake completed" in caplog.text

    await _cancel_retry(server, "beta")
    await server.stop()
    peer.close()
    await peer.wait_closed()


@pytest.mark.asyncio
async def test_retry_recovers_once_the_peer_accepts(tmp_path, caplog):
    """The retry loop finishes the job when the cause clears."""
    server_a, server_b = await _mismatched_pair(tmp_path)

    await _dial_beta(server_a, server_b)

    server_b.config.links[0].password = TEST_LINK_PASSWORD
    server_a._link_retry_state["beta"]["delay"] = 0.05
    with caplog.at_level("INFO"):
        linked = await _wait_for(lambda: "beta" in server_a.links, attempts=200)

    assert linked
    assert "Linked to peer beta" in caplog.text

    await server_a.stop()
    await server_b.stop()


@pytest.mark.asyncio
async def test_unexpected_link_error_is_logged_and_retried(
    tmp_path, caplog, monkeypatch
):
    """An exception in the link task used to vanish with the task."""
    from agentirc.server_link import ServerLink
    from tests._helpers import boot_linked_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    server_a.config.links[0].port = server_b.config.port

    async def explode(_self, _buffer):
        raise RuntimeError("boom in the link task")

    real_process = ServerLink._process_buffer

    async def explode_on_alpha(self, buffer):
        if self.server is server_a:
            await explode(self, buffer)
        return await real_process(self, buffer)

    monkeypatch.setattr(ServerLink, "_process_buffer", explode_on_alpha)

    with caplog.at_level("ERROR"):
        await _dial_beta(server_a, server_b)

    assert "boom in the link task" in caplog.text

    await _stop_after_retry(server_a, server_b)


@pytest.mark.asyncio
async def test_stopping_server_does_not_retry_a_failed_handshake(caplog):
    peer = await asyncio.start_server(_hang_up, "127.0.0.1", 0)
    port = peer.sockets[0].getsockname()[1]
    server = await _server_with_peer_at(port)
    server._stopping = True
    await server.connect_to_peer("127.0.0.1", port, "x", "full")
    await asyncio.sleep(0.1)
    assert "beta" not in server._link_retry_state

    server._stopping = False
    await server.stop()
    peer.close()
    await peer.wait_closed()


@pytest.mark.asyncio
async def test_silent_peer_times_out_and_retries(caplog, monkeypatch):
    """A peer that accepts and never speaks would otherwise hang the link forever."""
    import agentirc.server_link as server_link

    monkeypatch.setattr(server_link, "HANDSHAKE_TIMEOUT_S", 0.1)

    async def stay_silent(reader, _writer):
        await reader.read()

    peer = await asyncio.start_server(stay_silent, "127.0.0.1", 0)
    port = peer.sockets[0].getsockname()[1]
    server = await _server_with_peer_at(port)

    with caplog.at_level("WARNING"):
        await server.connect_to_peer("127.0.0.1", port, "x", "full")
        assert await _wait_for(lambda: "beta" in server._link_retry_state)

    assert "before the handshake completed" in caplog.text

    await _cancel_retry(server, "beta")
    await server.stop()
    peer.close()


@pytest.mark.asyncio
async def test_linked_session_has_no_read_deadline(tmp_path, monkeypatch):
    """The deadline covers only the handshake: a quiet healthy link stays up."""
    import agentirc.server_link as server_link
    from tests._helpers import boot_linked_pair, link_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    monkeypatch.setattr(server_link, "HANDSHAKE_TIMEOUT_S", 0.5)
    await link_pair(server_a, server_b)
    await asyncio.sleep(1.0)
    assert "beta" in server_a.links
    assert "alpha" in server_b.links

    await server_a.stop()
    await server_b.stop()


@pytest.mark.asyncio
async def test_rejected_duplicate_does_not_evict_the_healthy_link(tmp_path):
    """A connection claiming an already linked peer's name must not remove the real link."""
    from tests._helpers import boot_linked_pair, link_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    await link_pair(server_a, server_b)
    healthy = server_a.links["beta"]

    reader, writer = await asyncio.open_connection("127.0.0.1", server_a.config.port)
    writer.write(b"PASS wrong-password\r\nSERVER beta 1 :beta\r\n")
    await writer.drain()
    await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    await asyncio.sleep(0.1)

    assert server_a.links.get("beta") is healthy
    assert "beta" not in server_a._link_retry_state

    await server_a.stop()
    await server_b.stop()


@pytest.mark.asyncio
async def test_crashed_link_marks_its_span_as_failed(tmp_path, monkeypatch):
    """Logging the exception mustn't hide it from tracing."""
    from opentelemetry.trace import StatusCode

    from agentirc.server_link import ServerLink
    from tests._helpers import boot_linked_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    server_a.config.links[0].port = server_b.config.port
    real_process = ServerLink._process_buffer
    statuses = []

    async def explode_on_alpha(self, buffer):
        if self.server is server_a:
            span = self._session_span
            real_status = span.set_status
            span.set_status = lambda status, *a, **k: (
                statuses.append(status.status_code),
                real_status(status, *a, **k),
            )
            raise RuntimeError("boom")
        return await real_process(self, buffer)

    monkeypatch.setattr(ServerLink, "_process_buffer", explode_on_alpha)
    await _dial_beta(server_a, server_b)
    assert StatusCode.ERROR in statuses

    await _stop_after_retry(server_a, server_b)


@pytest.mark.asyncio
async def test_returning_peer_with_the_right_password_clears_a_half_open_link(tmp_path):
    """A peer that lost power leaves a stale entry; its return must not be refused forever."""
    from tests._helpers import boot_linked_pair, link_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    await link_pair(server_a, server_b)
    assert "beta" in server_a.links

    reader, writer = await asyncio.open_connection("127.0.0.1", server_a.config.port)
    writer.write(f"PASS {TEST_LINK_PASSWORD}\r\nSERVER beta 1 :beta\r\n".encode())
    await writer.drain()
    await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()

    # The stale entry is gone, so beta's next dial can succeed.
    assert await _wait_for(lambda: "beta" not in server_a.links)

    await _stop_after_retry(server_a, server_b)


@pytest.mark.asyncio
async def test_handshake_deadline_is_absolute_not_per_read(monkeypatch, caplog):
    """A peer dripping traffic and never authenticating must still time out."""
    import agentirc.server_link as server_link

    monkeypatch.setattr(server_link, "HANDSHAKE_TIMEOUT_S", 0.3)

    async def drip(_reader, writer):
        try:
            while True:
                writer.write(b"NOTICE :still here\r\n")
                await writer.drain()
                await asyncio.sleep(0.05)
        except (ConnectionError, asyncio.CancelledError):
            pass

    peer = await asyncio.start_server(drip, "127.0.0.1", 0)
    port = peer.sockets[0].getsockname()[1]
    server = await _server_with_peer_at(port)

    with caplog.at_level("WARNING"):
        await server.connect_to_peer("127.0.0.1", port, "x", "full")
        assert await _wait_for(lambda: "beta" in server._link_retry_state, attempts=40)

    assert "timed out" in caplog.text

    await _cancel_retry(server, "beta")
    await server.stop()
    peer.close()


@pytest.mark.asyncio
async def test_unauthenticated_inbound_error_is_not_logged_as_a_peer(tmp_path, caplog):
    """An unauthenticated client can claim any name: its ERROR text must not reach the log."""
    from tests._helpers import boot_linked_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    reader, writer = await asyncio.open_connection("127.0.0.1", server_a.config.port)
    with caplog.at_level("WARNING"):
        # PASS first, or the connection isn't treated as a server link at all.
        writer.write(b"PASS anything\r\nERROR :spoofed refusal\r\n")
        await writer.drain()
        await asyncio.sleep(0.2)
    writer.close()

    assert "spoofed refusal" not in caplog.text

    await server_a.stop()
    await server_b.stop()


@pytest.mark.asyncio
async def test_old_half_open_link_closing_later_does_not_evict_the_new_one(tmp_path):
    """Once replaced, a stale link's late close must leave its successor alone."""
    from tests._helpers import boot_linked_pair, link_pair

    server_a, server_b = await boot_linked_pair(tmp_path)
    await link_pair(server_a, server_b)
    stale = server_a.links["beta"]
    replacement = object()
    server_a.links["beta"] = replacement

    await server_a._remove_link(stale)

    assert server_a.links["beta"] is replacement
    del server_a.links["beta"]

    await _stop_after_retry(server_a, server_b)


@pytest.mark.asyncio
async def test_peer_error_reason_is_truncated_and_cleaned(caplog):
    """A dialled peer's ERROR is untrusted text: no control characters, bounded length."""

    async def refuse(reader, writer):
        # Read first: closing with unread data resets the socket and loses the ERROR.
        await reader.read(4096)
        writer.write(b"ERROR :" + b"\x1b[31m" + b"x" * 400 + b"\r\n")
        await writer.drain()
        writer.close()

    peer = await asyncio.start_server(refuse, "127.0.0.1", 0)
    port = peer.sockets[0].getsockname()[1]
    server = await _server_with_peer_at(port)

    with caplog.at_level("WARNING"):
        await server.connect_to_peer("127.0.0.1", port, "x", "full")
        assert await _wait_for(lambda: "beta" in server._link_retry_state)

    assert "Peer beta sent ERROR" in caplog.text
    assert "\x1b" not in caplog.text
    assert "x" * 300 not in caplog.text

    await _cancel_retry(server, "beta")
    await server.stop()
    peer.close()
