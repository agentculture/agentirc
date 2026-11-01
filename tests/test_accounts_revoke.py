"""Live revoke, failed-auth logging, rate limiting, telemetry (culture#506 t10).

Real servers on random ports, real TCP clients.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from agentirc.accounts import AuthFailureLimiter
from agentirc.config import AccountsConfig
from tests.telemetry._metrics_helpers import get_counter_value
from tests.test_accounts_sasl import (
    SECRET_ALICE,
    SECRET_BOB,
    _account,
    _boot,
    _connect,
    _read_to_close,
    _sha,
    default_accounts,
    plain_payload,
    sasl_ls_flow,
    write_accounts,
)


async def _login(server, nick="testserv-alice", secret=SECRET_ALICE):
    c = await _connect(server)
    await sasl_ls_flow(c, nick, nick, secret)
    await c.recv_until(" 903 ")
    await c.send("CAP END")
    assert " 001 " in await c.recv_until(" 001 ")
    return c


def _rewrite(server, accounts):
    write_accounts(server.config.data_dir, accounts)


# ---------------------------------------------------------------------------
# Live revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "suspended", "removed", "rekeyed"])
async def test_live_session_closed_within_5s_of_file_change(tmp_path, change):
    server = await _boot(tmp_path)
    try:
        alice = await _login(server)
        watcher = await _connect(server)
        await watcher.send("NICK testserv-watcher")
        await watcher.send("USER w 0 * :w")
        await watcher.recv_until(" 001 ")
        await alice.send("JOIN #general")
        await alice.recv_until("366")
        await watcher.send("JOIN #general")
        await watcher.recv_until("366")
        await watcher.recv_all(timeout=0.3)

        accounts = default_accounts()
        if change == "removed":
            accounts = [a for a in accounts if a["nick"] != "testserv-alice"]
        elif change == "rekeyed":
            accounts[0] = _account("testserv-alice", SECRET_BOB)
        else:
            accounts[0] = _account("testserv-alice", SECRET_ALICE, state=change)
        t0 = time.monotonic()
        _rewrite(server, accounts)  # nobody connects: only the periodic check can notice

        lines, closed = await _read_to_close(alice, timeout=5.0)
        elapsed = time.monotonic() - t0
        assert closed, f"session still open after {elapsed:.1f}s: {lines}"
        assert elapsed < 5.0
        assert any(line.startswith("ERROR") and "account" in line for line in lines), lines
        # Channel members see the QUIT, and the nick is released.
        seen = await watcher.recv_all(timeout=1.0)
        assert any("QUIT" in line and "testserv-alice" in line for line in seen), seen
        assert "testserv-alice" not in server.clients
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unaffected_session_stays_open(tmp_path):
    server = await _boot(tmp_path)
    try:
        alice = await _login(server)
        accounts = default_accounts()
        # Grant change only (not revoke): session stays.
        accounts[0] = _account("testserv-alice", SECRET_ALICE, grants={"#x": "speak"})
        _rewrite(server, accounts)
        lines, closed = await _read_to_close(alice, timeout=2.5)
        assert not closed, lines
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_sighup_reload_also_closes_revoked_session(tmp_path):
    server = await _boot(tmp_path)
    try:
        alice = await _login(server)
        accounts = default_accounts()
        accounts[0] = _account("testserv-alice", SECRET_ALICE, state="revoked")
        _rewrite(server, accounts)
        server.reload_accounts()
        _, closed = await _read_to_close(alice, timeout=1.0)
        assert closed
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_periodic_task_cancelled_on_stop(tmp_path):
    server = await _boot(tmp_path)
    task = server._accounts_task
    assert task is not None and not task.done()
    await server.stop()
    assert task.done()


# ---------------------------------------------------------------------------
# Failed-auth logging
# ---------------------------------------------------------------------------


async def _fail_sasl(server, nick, secret, *, payload=None):
    c = await _connect(server)
    await c.send("CAP LS 302")
    await c.send(f"NICK {nick}")
    await c.send("USER u 0 * :u")
    await c.send("CAP REQ :sasl")
    await c.recv_until(" ACK ")
    await c.send("AUTHENTICATE PLAIN")
    # A rate-limited peer is refused right here, before the "+" prompt.
    await c.send(f"AUTHENTICATE {payload or plain_payload(nick, secret)}")
    lines, closed = await _read_to_close(c, timeout=1.0)
    await c.close()
    return lines, closed


def _auth_records(caplog):
    return [r.getMessage() for r in caplog.records if "auth failure" in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nick,secret,reason",
    [
        ("testserv-alice", "wrongsecret-" + "z" * 20, "bad_secret"),
        ("testserv-nobody", "nosuchsecret-" + "y" * 20, "unknown_account"),
        ("testserv-rev", "c3" * 32, "revoked"),
        ("testserv-sus", "d4" * 32, "suspended"),
    ],
)
async def test_failed_auth_logs_nick_peer_reason_never_secret(
    tmp_path, caplog, nick, secret, reason
):
    server = await _boot(tmp_path)
    try:
        payload = plain_payload(nick, secret)
        with caplog.at_level(logging.INFO):
            await _fail_sasl(server, nick, secret, payload=payload)
        records = _auth_records(caplog)
        assert len(records) == 1, records
        line = records[0]
        assert nick in line and "127.0.0.1" in line and f"reason={reason}" in line
        everything = "\n".join(r.getMessage() for r in caplog.records)
        for forbidden in (secret, payload, _sha(secret), _sha(SECRET_ALICE)):
            assert forbidden not in everything
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_failed_auth_reasons_malformed_and_nick_without_auth(tmp_path, caplog):
    server = await _boot(tmp_path)
    try:
        with caplog.at_level(logging.INFO):
            await _fail_sasl(server, "testserv-alice", "x", payload="!!!notbase64!!!")
            c = await _connect(server)
            await c.send("NICK testserv-alice")
            await c.send("USER u 0 * :u")
            await _read_to_close(c, timeout=1.0)
            await c.close()
        text = "\n".join(_auth_records(caplog))
        assert "reason=malformed" in text
        assert "reason=nick_without_auth" in text
        assert "!!!notbase64!!!" not in "\n".join(r.getMessage() for r in caplog.records)
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def test_limiter_sliding_window():
    now = [0.0]
    lim = AuthFailureLimiter(limit=3, window=10.0, clock=lambda: now[0])
    assert not lim.blocked("1.1.1.1")
    for _ in range(3):
        lim.record("1.1.1.1")
    assert lim.blocked("1.1.1.1")
    assert not lim.blocked("2.2.2.2")
    now[0] = 10.5
    assert not lim.blocked("1.1.1.1")


def test_limiter_disabled_with_zero_limit():
    lim = AuthFailureLimiter(limit=0, window=10.0)
    for _ in range(100):
        lim.record("1.1.1.1")
    assert not lim.blocked("1.1.1.1")


def test_accounts_config_rate_limit_keys_validated():
    cfg = AccountsConfig(auth_failure_limit=3, auth_failure_window=30)
    assert cfg.auth_failure_limit == 3 and cfg.auth_failure_window == 30
    assert AccountsConfig().auth_failure_limit == 10
    assert AccountsConfig().auth_failure_window == 60
    with pytest.raises(ValueError):
        AccountsConfig(auth_failure_limit=-1)
    with pytest.raises(ValueError):
        AccountsConfig(auth_failure_window=0)


@pytest.mark.asyncio
async def test_rate_limited_peer_refused_without_checking_secret(tmp_path, caplog):
    server = await _boot(tmp_path)
    server.config.accounts.auth_failure_limit = 3
    server.config.accounts.auth_failure_window = 60
    server.auth_limiter = AuthFailureLimiter(3, 60)
    try:
        for _ in range(3):
            await _fail_sasl(server, "testserv-alice", "bad" + "q" * 20)
        # Over the limit: even the CORRECT secret is refused, and the secret
        # is never verified.
        calls = []
        real_verify = server.accounts.verify
        server.accounts.verify = lambda *a, **k: calls.append(a) or real_verify(*a, **k)
        with caplog.at_level(logging.INFO):
            lines, closed = await _fail_sasl(server, "testserv-alice", SECRET_ALICE)
        assert closed
        assert any(" 904 " in line for line in lines)
        assert not calls
        assert not any(" 900 " in line or " 903 " in line for line in lines)
        assert any("reason=rate_limited" in m for m in _auth_records(caplog))
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_successful_logins_not_throttled_and_limit_not_loopback_exempt(tmp_path):
    server = await _boot(tmp_path)
    server.auth_limiter = AuthFailureLimiter(2, 60)
    try:
        for _ in range(4):
            c = await _login(server)
            await c.close()
            await asyncio.sleep(0.05)
        for _ in range(2):
            await _fail_sasl(server, "testserv-alice", "bad" + "q" * 20)
        lines, closed = await _fail_sasl(server, "testserv-alice", SECRET_ALICE)
        assert closed and not any(" 903 " in line for line in lines)
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failures_counter(metrics_reader, tmp_path):
    server = await _boot(tmp_path)
    try:
        await _fail_sasl(server, "testserv-alice", "bad" + "q" * 20)
        await _fail_sasl(server, "testserv-nobody", "bad" + "q" * 20)
        await _fail_sasl(server, "testserv-nobody", "bad" + "q" * 20)
        name = "culture.auth.failures"
        assert get_counter_value(metrics_reader, name) == 3
        assert get_counter_value(metrics_reader, name, {"reason": "bad_secret"}) == 1
        assert get_counter_value(metrics_reader, name, {"reason": "unknown_account"}) == 2
        data = metrics_reader.get_metrics_data()
        attrs = {
            k
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == name
            for p in m.data.data_points
            for k in p.attributes
        }
        assert attrs == {"reason"}  # low cardinality: no nick, no address
    finally:
        await server.stop()
