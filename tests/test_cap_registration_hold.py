"""CAP negotiation holds registration until CAP END (IRCv3 behaviour)."""

import asyncio

import pytest


def _has_welcome(lines):
    return any(" 001 " in line for line in lines)


async def _raw(server_a):
    from tests.conftest import IRCTestClient

    reader, writer = await asyncio.open_connection("127.0.0.1", server_a.config.port)
    return IRCTestClient(reader, writer)


@pytest.mark.asyncio
async def test_cap_ls_nick_user_holds_until_end_and_defers_snick(linked_servers):
    server_a, server_b = linked_servers
    c = await _raw(server_a)
    try:
        await c.send("CAP LS")
        await c.send("NICK alpha-alice")
        await c.send("USER alice 0 * :alice")
        lines = await c.recv_all(timeout=0.7)
        assert any("CAP" in line and "LS" in line for line in lines)
        assert not _has_welcome(lines)
        # No SNICK relayed to the linked peer yet.
        assert "alpha-alice" not in server_b.remote_clients

        await c.send("CAP END")
        out = await c.recv_until(" 001 ")
        assert " 001 " in out
        await asyncio.sleep(0.3)
        assert "alpha-alice" in server_b.remote_clients
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_cap_ls_end_before_nick_user(server):
    c = await _raw(server)
    try:
        await c.send("CAP LS")
        await c.send("CAP END")
        await c.recv_all(timeout=0.4)
        await c.send("NICK testserv-bob")
        await c.send("USER bob 0 * :bob")
        out = await c.recv_until(" 001 ")
        assert " 001 " in out
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_nick_user_then_cap_ls_then_end(server):
    c = await _raw(server)
    try:
        await c.send("CAP LS")
        await c.send("NICK testserv-carol")
        await c.send("USER carol 0 * :carol")
        await c.send("CAP REQ :message-tags")
        lines = await c.recv_all(timeout=0.5)
        assert any("ACK" in line for line in lines)
        assert not _has_welcome(lines)
        await c.send("CAP END")
        out = await c.recv_until(" 001 ")
        assert " 001 " in out
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_cap_req_unknown_nak_still_needs_end(server):
    c = await _raw(server)
    try:
        await c.send("CAP LS")
        await c.send("CAP REQ :no-such-cap")
        await c.send("NICK testserv-dave")
        await c.send("USER dave 0 * :dave")
        lines = await c.recv_all(timeout=0.6)
        assert any("NAK" in line for line in lines)
        assert not _has_welcome(lines)
        await c.send("CAP END")
        out = await c.recv_until(" 001 ")
        assert " 001 " in out
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_cap_ls_never_end_does_not_register(server):
    c = await _raw(server)
    try:
        await c.send("CAP LS")
        await c.send("NICK testserv-erin")
        await c.send("USER erin 0 * :erin")
        lines = await c.recv_all(timeout=1.5)
        assert not _has_welcome(lines)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_bare_cap_req_without_ls_does_not_hold(server):
    """Legacy clients that REQ before NICK/USER without LS register as before."""
    c = await _raw(server)
    try:
        await c.send("CAP REQ :message-tags")
        await c.send("NICK testserv-gina")
        await c.send("USER gina 0 * :gina")
        out = await c.recv_until(" 001 ")
        assert " 001 " in out
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_no_cap_registers_immediately_and_snick_relayed(linked_servers):
    server_a, server_b = linked_servers
    c = await _raw(server_a)
    try:
        await c.send("NICK alpha-fred")
        await c.send("USER fred 0 * :fred")
        out = await c.recv_until(" 001 ")
        assert " 001 " in out
        await asyncio.sleep(0.3)
        assert "alpha-fred" in server_b.remote_clients
    finally:
        await c.close()
