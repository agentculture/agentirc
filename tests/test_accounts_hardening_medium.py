"""Security hardening of agent accounts, MEDIUM findings (culture#506 h2, deviation d6).

- M1: an evicted session (revoke / reclaim) stops acting at once: buffered
  lines are dropped, eviction is idempotent, concurrent reclaims leave
  exactly one owner.
- M2: eviction never waits on a channel member that stops reading.
- M3: untrusted peers are refused at ``AUTHENTICATE PLAIN``, before they send
  a secret; ``accounts.trust_loopback``; IPv4-mapped CIDRs; a warning for /0.
- M4: one local process cannot lock out every local agent: the limiter is
  keyed per (address, authcid), only credential failures count, a wrong
  mechanism closes the connection.
- M5: a malformed accounts file at startup fails closed for the nicks it
  names; an empty table after a non-empty one is logged loudly.
- M6: a nick squatted before its account existed is evicted on reload.
- M7: case variants and non-ASCII lookalikes of account nicks are refused;
  channel grants match exactly.

Real servers on random ports, real TCP clients.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest
import pytest_asyncio

from agentirc._internal.protocol.message import Message
from agentirc.accounts import (
    ACCOUNTS_FILENAME,
    AccountsFileError,
    AccountStore,
    AuthFailureLimiter,
    is_trusted_peer,
    parse_accounts,
)
from agentirc.client import Client
from agentirc.config import AccountsConfig, ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd
from agentirc.skill import EventType
from tests.test_accounts_grants import SECRET_GUEST, SECRET_RES, _accts, _login, _plain
from tests.test_accounts_sasl import (
    SECRET_ALICE,
    _account,
    _connect,
    _has_numeric,
    _read_to_close,
    _welcomed,
    plain_payload,
    sasl_ls_flow,
    write_accounts,
)


async def _boot(tmp_path, accounts=None, *, raw=None, **acfg):
    data_dir = str(tmp_path / "data")
    if raw is not None:
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, ACCOUNTS_FILENAME), "w", encoding="utf-8") as f:
            f.write(raw)
    else:
        write_accounts(data_dir, _accts() if accounts is None else accounts)
    acfg.setdefault("guest_channel_prefixes", ["#sbx-"])
    config = ServerConfig(
        name="testserv",
        host="127.0.0.1",
        port=0,
        webhook_port=0,
        data_dir=data_dir,
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
        accounts=AccountsConfig(**acfg),
    )
    ircd = IRCd(config)
    await ircd.start()
    ircd.config.port = ircd._server.sockets[0].getsockname()[1]
    return ircd


@pytest_asyncio.fixture
async def srv(tmp_path):
    ircd = await _boot(tmp_path)
    yield ircd
    await ircd.stop()


async def _close(*clients):
    for c in clients:
        await c.close()


def _server_side(srv, nick) -> Client:
    return srv.clients[nick]


def _auth_records(caplog):
    return [r.getMessage() for r in caplog.records if "auth failure" in r.getMessage()]


async def _ready_for_cap_end(srv, nick, secret):
    """A connection that has logged in (903) and waits for CAP END."""
    c = await _connect(srv)
    await sasl_ls_flow(c, nick, nick, secret)
    assert " 903 " in await c.recv_until(" 903 ")
    return c


# ---------------------------------------------------------------------------
# M1: evicted sessions stop acting
# ---------------------------------------------------------------------------


async def test_evicted_session_drops_buffered_lines(srv):
    held = await _plain(srv, "testserv-other")
    holder = _server_side(srv, "testserv-other")
    watcher = await _plain(srv, "testserv-watch")
    await watcher.send("JOIN #pub")
    await watcher.recv_all(0.3)
    await holder.evict("Nick reclaimed by an authenticated session")
    assert holder._registered is False
    assert holder.account is None
    # Lines that were already buffered when the eviction landed:
    await holder._dispatch(Message.parse("JOIN #pub"))
    await holder._dispatch(Message.parse("PRIVMSG #pub :as-evicted"))
    await holder._dispatch(Message.parse("PRIVMSG testserv-watch :dm-as-evicted"))
    lines = await watcher.recv_all(0.5)
    assert not any("as-evicted" in ln or "JOIN #pub" in ln for ln in lines), lines
    assert holder not in srv.channels["#pub"].members
    await _close(held, watcher)


async def test_evicted_session_buffered_socket_lines_dropped(srv):
    """Lines in the same TCP read as the eviction are not dispatched."""
    res = await _login(srv, "testserv-res", SECRET_RES)
    watcher = await _plain(srv, "testserv-watch")
    await watcher.send("JOIN #speakroom")
    await res.send("JOIN #speakroom")
    await res.recv_all(0.3)
    await watcher.recv_all(0.3)
    holder = _server_side(srv, "testserv-res")
    orig = holder._dispatch

    async def dispatch_then_evict(msg):
        await orig(msg)
        if msg.command == "PING":
            await holder.evict("account revoked")

    holder._dispatch = dispatch_then_evict
    # One write: PING (evicts after dispatch), then two lines already buffered.
    res.writer.write(b"PING :x\r\nPRIVMSG testserv-watch :after-evict\r\nQUIT :q\r\n")
    await res.writer.drain()
    lines = await watcher.recv_all(0.6)
    assert not any("after-evict" in ln for ln in lines), lines
    assert sum(" QUIT " in ln for ln in lines) == 1, lines
    await _close(res, watcher)


async def test_evict_is_idempotent(srv):
    held = await _plain(srv, "testserv-other")
    holder = _server_side(srv, "testserv-other")
    quits = []
    orig_emit = srv.emit_event

    async def spy(event):
        if event.type == EventType.QUIT:
            quits.append(event.nick)
        await orig_emit(event)

    srv.emit_event = spy
    await holder.evict("account revoked")
    await holder.evict("account revoked")
    assert quits == ["testserv-other"]
    await held.close()


async def test_concurrent_reclaims_leave_exactly_one_owner(srv, monkeypatch):
    old = await _login(srv, "testserv-res", SECRET_RES)
    a = await _ready_for_cap_end(srv, "testserv-res", SECRET_RES)
    b = await _ready_for_cap_end(srv, "testserv-res", SECRET_RES)
    orig_evict = Client.evict

    async def slow_evict(self, reason):
        await asyncio.sleep(0.3)  # widen the race window
        await orig_evict(self, reason)

    monkeypatch.setattr(Client, "evict", slow_evict)
    await a.send("CAP END")
    await b.send("CAP END")
    await asyncio.sleep(1.5)
    owners = [
        c
        for c in srv._local_connections
        if c.nick == "testserv-res" and c._registered and not c._closing
    ]
    assert len(owners) == 1, owners
    assert srv.clients.get("testserv-res") is owners[0]
    await _close(old, a, b)


# ---------------------------------------------------------------------------
# M2: a non-reading member never stalls eviction
# ---------------------------------------------------------------------------


async def _stall(srv, nick):
    """Make the server-side session of *nick* behave like a non-reading peer."""
    stuck = _server_side(srv, nick)

    async def never(*_a, **_k):
        await asyncio.Event().wait()

    stuck.send = never
    stuck.send_raw = never
    return stuck


async def test_revoke_not_blocked_by_a_non_reading_member(tmp_path):
    accounts = _accts() + [_account("testserv-two", SECRET_ALICE, grants={"#speakroom": "speak"})]
    srv = await _boot(tmp_path, accounts)
    try:
        res = await _login(srv, "testserv-res", SECRET_RES)
        two = await _login(srv, "testserv-two", SECRET_ALICE)
        stuck = await _plain(srv, "testserv-stuck")
        for c in (stuck, res, two):
            await c.send("JOIN #speakroom")
            await c.recv_all(0.3)
        await _stall(srv, "testserv-stuck")
        t0 = time.monotonic()
        write_accounts(srv.config.data_dir, _accts()[1:])  # both res and two revoked
        _lines, closed_res = await _read_to_close(res, 5.0)
        _lines, closed_two = await _read_to_close(two, 5.0)
        assert closed_res
        assert closed_two
        assert time.monotonic() - t0 < 5.0
        await _close(res, two, stuck)
    finally:
        await srv.stop()


async def test_reclaim_not_blocked_by_a_non_reading_member(srv):
    old = await _login(srv, "testserv-res", SECRET_RES)
    stuck = await _plain(srv, "testserv-stuck")
    for c in (stuck, old):
        await c.send("JOIN #speakroom")
        await c.recv_all(0.3)
    await _stall(srv, "testserv-stuck")
    new = await _ready_for_cap_end(srv, "testserv-res", SECRET_RES)
    await new.send("CAP END")
    async with asyncio.timeout(3.0):
        assert " 001 " in await new.recv_until(" 001 ")
    _lines, closed = await _read_to_close(old, 2.0)
    assert closed
    await _close(old, new, stuck)


# ---------------------------------------------------------------------------
# M3: trusted peers
# ---------------------------------------------------------------------------


@pytest.fixture
def untrusted_host(monkeypatch):
    orig_init = Client.__init__

    def fake_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.host = "203.0.113.7"  # TEST-NET-3, never loopback

    monkeypatch.setattr(Client, "__init__", fake_init)


async def test_untrusted_peer_refused_before_sending_a_secret(srv, untrusted_host, caplog):
    c = await _connect(srv)
    await c.send("CAP LS 302")
    ls = await c.recv_until(" LS ")
    assert "sasl=PLAIN" in ls  # cap list identical for everyone
    await c.send("NICK testserv-res")
    await c.send("USER u 0 * :u")
    await c.send("CAP REQ :sasl")
    await c.recv_until(" ACK ")
    with caplog.at_level(logging.INFO):
        await c.send("AUTHENTICATE PLAIN")
        lines, closed = await _read_to_close(c, 2.0)
    assert not any(ln.startswith("AUTHENTICATE") for ln in lines), lines
    assert _has_numeric(lines, "904")
    assert closed
    assert any("reason=untrusted_peer" in m for m in _auth_records(caplog))
    assert not srv.auth_limiter._events  # never counted
    await c.close()


async def test_trust_loopback_false_refuses_loopback(tmp_path):
    srv = await _boot(tmp_path, trust_loopback=False)
    try:
        c = await _connect(srv)
        await c.send("CAP LS 302")
        await c.send("NICK testserv-res")
        await c.send("USER u 0 * :u")
        await c.send("CAP REQ :sasl")
        await c.recv_until(" ACK ")
        await c.send("AUTHENTICATE PLAIN")
        lines, closed = await _read_to_close(c, 2.0)
        assert _has_numeric(lines, "904")
        assert closed
        await c.close()
    finally:
        await srv.stop()


async def test_trust_loopback_false_with_loopback_cidr_succeeds(tmp_path):
    srv = await _boot(tmp_path, trust_loopback=False, trusted_cidrs=["127.0.0.0/8"])
    try:
        c = await _login(srv, "testserv-res", SECRET_RES)
        await c.close()
    finally:
        await srv.stop()


def test_trust_loopback_config_default_and_validation():
    assert AccountsConfig().trust_loopback is True
    with pytest.raises(ValueError):
        AccountsConfig(trust_loopback="no")


def test_is_trusted_peer_trust_loopback_flag():
    assert is_trusted_peer("127.0.0.1", [])
    assert not is_trusted_peer("127.0.0.1", [], trust_loopback=False)
    assert not is_trusted_peer("::1", [], trust_loopback=False)
    assert is_trusted_peer("::ffff:127.0.0.1", ["127.0.0.0/8"], trust_loopback=False)


def test_ipv4_mapped_cidr_is_normalised():
    cfg = AccountsConfig(trusted_cidrs=["::ffff:10.0.0.0/104", "::ffff:192.0.2.1"])
    assert cfg.trusted_cidrs == ["10.0.0.0/8", "192.0.2.1/32"]
    assert is_trusted_peer("10.1.2.3", cfg.trusted_cidrs)
    assert is_trusted_peer("::ffff:10.1.2.3", cfg.trusted_cidrs)
    # Even an un-normalised list (direct callers) matches.
    assert is_trusted_peer("10.1.2.3", ["::ffff:10.0.0.0/104"])
    assert not is_trusted_peer("11.1.2.3", ["::ffff:10.0.0.0/104"])


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "::/0"])
def test_trust_everything_cidr_warns(caplog, cidr):
    with caplog.at_level(logging.WARNING, logger="agentirc.config"):
        AccountsConfig(trusted_cidrs=[cidr])
    assert any("trusts every" in r.getMessage() for r in caplog.records), caplog.records


def test_narrow_cidr_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="agentirc.config"):
        AccountsConfig(trusted_cidrs=["100.64.0.0/10"])
    assert not caplog.records


# ---------------------------------------------------------------------------
# M4: limiter scope
# ---------------------------------------------------------------------------


async def _sasl_attempt(srv, nick, secret=None, *, payload=None, mech="PLAIN"):
    c = await _connect(srv)
    await c.send("CAP LS 302")
    await c.send(f"NICK {nick}")
    await c.send("USER u 0 * :u")
    await c.send("CAP REQ :sasl")
    await c.recv_until(" ACK ")
    await c.send(f"AUTHENTICATE {mech}")
    if mech == "PLAIN":
        await c.send(f"AUTHENTICATE {payload or plain_payload(nick, secret)}")
    lines, closed = await _read_to_close(c, timeout=1.5)
    await c.close()
    return lines, closed


async def test_wrong_mechanism_closes_and_is_not_counted(srv):
    srv.auth_limiter = AuthFailureLimiter(2, 60)
    for _ in range(3):
        lines, closed = await _sasl_attempt(srv, "testserv-res", mech="EXTERNAL")
        assert _has_numeric(lines, "908")
        assert _has_numeric(lines, "904")
        assert closed
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.close()


async def test_malformed_payloads_are_not_counted(srv):
    srv.auth_limiter = AuthFailureLimiter(2, 60)
    for _ in range(3):
        lines, closed = await _sasl_attempt(srv, "testserv-res", payload="!!!notbase64!!!")
        assert closed
        assert _has_numeric(lines, "904")
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.close()


async def test_failures_on_one_account_do_not_lock_out_another(srv):
    srv.auth_limiter = AuthFailureLimiter(2, 60)
    for _ in range(3):
        await _sasl_attempt(srv, "testserv-guest", "bad" + "q" * 20)
        await _sasl_attempt(srv, "testserv-nobody", "bad" + "q" * 20)
    c = await _login(srv, "testserv-res", SECRET_RES)  # other account: unaffected
    await c.close()
    # The targeted account itself is still throttled, even with the right secret.
    lines, closed = await _sasl_attempt(srv, "testserv-guest", SECRET_GUEST)
    assert closed
    assert not _has_numeric(lines, "903")


# ---------------------------------------------------------------------------
# M5: fail closed on a malformed file at startup
# ---------------------------------------------------------------------------

_TYPO = (
    '{"version": 1, "accounts": [{"nick": "testserv-res", "secret_sha256": "'
    + "0" * 64
    + '", "role": "resident", "grants": {}, "state": "active"},]}'  # trailing comma
)


async def test_malformed_file_at_startup_reserves_named_nicks(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger="agentirc.accounts"):
        srv = await _boot(tmp_path, raw=_TYPO)
    try:
        assert any("fail" in r.getMessage().lower() for r in caplog.records)
        c = await _connect(srv)
        await c.send("NICK testserv-res")
        await c.send("USER u 0 * :u")
        lines, closed = await _read_to_close(c, 1.5)
        assert not _welcomed(lines)
        assert _has_numeric(lines, "904")
        assert closed
        # A nick the bad file does not name still registers.
        p = await _plain(srv, "testserv-zzz")
        # A registered client cannot NICK to it either.
        await p.send("NICK testserv-res")
        assert " 432 " in await p.recv_until(" 432 ")
        # Fixing the file lifts the reservation and SASL works again.
        write_accounts(srv.config.data_dir, _accts())
        res = await _login(srv, "testserv-res", SECRET_RES)
        await _close(c, p, res)
    finally:
        await srv.stop()


async def test_unrecoverable_file_at_startup_refuses_all_local_registrations(tmp_path):
    srv = await _boot(tmp_path, raw="\x00garbage")
    try:
        c = await _connect(srv)
        await c.send("NICK testserv-zzz")
        await c.send("USER u 0 * :u")
        lines, closed = await _read_to_close(c, 1.5)
        assert not _welcomed(lines)
        assert closed
        write_accounts(srv.config.data_dir, _accts())
        await asyncio.sleep(0.05)
        p = await _plain(srv, "testserv-zzz")
        await _close(c, p)
    finally:
        await srv.stop()


def test_store_fail_closed_flags(tmp_path):
    (tmp_path / ACCOUNTS_FILENAME).write_text(_TYPO)
    store = AccountStore(str(tmp_path))
    assert store.get("testserv-res") is None  # no usable credential
    assert store.is_reserved("testserv-res")
    assert not store.is_reserved("testserv-zzz")
    write_accounts(str(tmp_path), [])
    store.reload()
    assert not store.is_reserved("testserv-res")


def test_valid_empty_or_missing_file_is_not_fail_closed(tmp_path):
    assert not AccountStore(str(tmp_path)).is_reserved("testserv-zzz")
    write_accounts(str(tmp_path), [])
    assert not AccountStore(str(tmp_path)).is_reserved("testserv-zzz")


def test_runtime_malformed_file_keeps_last_good_table_only(tmp_path):
    write_accounts(str(tmp_path), [_account("testserv-a", SECRET_ALICE)])
    store = AccountStore(str(tmp_path))
    (tmp_path / "t").write_text("{nope")
    os.replace(tmp_path / "t", tmp_path / ACCOUNTS_FILENAME)
    assert store.reload() is False
    assert store.get("testserv-a") is not None
    assert not store.is_reserved("testserv-zzz")  # no lockdown at runtime


def test_table_becoming_empty_is_logged_loudly(tmp_path, caplog):
    write_accounts(str(tmp_path), [_account("testserv-a", SECRET_ALICE)])
    store = AccountStore(str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="agentirc.accounts"):
        os.remove(tmp_path / ACCOUNTS_FILENAME)
        store.reload()
    assert any(
        r.levelno >= logging.WARNING and "no accounts" in r.getMessage() for r in caplog.records
    )
    write_accounts(str(tmp_path), [_account("testserv-a", SECRET_ALICE)])
    store.reload()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agentirc.accounts"):
        write_accounts(str(tmp_path), [])
        store.reload()
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_missing_data_dir_is_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="agentirc.accounts"):
        AccountStore(str(tmp_path / "not-mounted"))
    assert any("does not exist" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# M6: squatter evicted when the account appears
# ---------------------------------------------------------------------------


async def test_squatter_evicted_when_account_created(srv):
    sq = await _plain(srv, "testserv-newacct")
    write_accounts(srv.config.data_dir, _accts() + [_account("testserv-newacct", SECRET_ALICE)])
    lines, closed = await _read_to_close(sq, 5.0)
    assert closed, lines
    assert any(ln.startswith("ERROR") for ln in lines), lines
    assert "testserv-newacct" not in srv.clients
    # The real agent can now log in.
    c = await _login(srv, "testserv-newacct", SECRET_ALICE)
    await _close(sq, c)


async def test_case_variant_squatter_evicted_when_account_created(srv):
    sq = await _plain(srv, "testserv-NewAcct")
    write_accounts(srv.config.data_dir, _accts() + [_account("testserv-newacct", SECRET_ALICE)])
    _lines, closed = await _read_to_close(sq, 5.0)
    assert closed
    await sq.close()


async def test_unrelated_plain_sessions_survive_reload(srv):
    p = await _plain(srv, "testserv-plain")
    write_accounts(srv.config.data_dir, _accts() + [_account("testserv-newacct", SECRET_ALICE)])
    await asyncio.sleep(1.8)
    await p.send("PING :alive")
    assert "PONG" in await p.recv_until("PONG")
    await p.close()


# ---------------------------------------------------------------------------
# M7: case and lookalike nicks, exact grants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nick", ["testserv-RES", "testserv-Res"])
async def test_case_variant_of_account_nick_refused(srv, nick):
    c = await _connect(srv)
    await c.send(f"NICK {nick}")
    await c.send("USER u 0 * :u")
    lines = await c.recv_all(0.6)
    assert _has_numeric(lines, "432"), lines
    assert not _welcomed(lines), lines
    p = await _plain(srv, "testserv-plain")
    await p.send(f"NICK {nick}")
    assert " 432 " in await p.recv_until(" 432 ")
    await _close(c, p)


async def test_non_ascii_nick_refused(srv):
    c = await _connect(srv)
    await c.send("NICK testserv-rеs")  # Cyrillic e
    await c.send("USER u 0 * :u")
    lines = await c.recv_all(0.6)
    assert _has_numeric(lines, "432"), lines
    assert not _welcomed(lines), lines
    await c.close()


async def test_ascii_nicks_unchanged(srv):
    p = await _plain(srv, "testserv-Mixed_Case.nick")
    await p.close()


def test_account_nicks_must_be_ascii_and_case_unique():
    non_ascii = {"version": 1, "accounts": [_account("testserv-rеs", SECRET_RES)]}
    with pytest.raises(AccountsFileError):
        parse_accounts(non_ascii)
    case_variants = {
        "version": 1,
        "accounts": [
            _account("testserv-gwen", SECRET_RES),
            _account("testserv-Gwen", SECRET_ALICE),
        ],
    }
    with pytest.raises(AccountsFileError):
        parse_accounts(case_variants)


async def test_grants_match_channel_names_exactly(srv):
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #SpeakRoom")
    lines = await c.recv_all(0.5)
    assert _has_numeric(lines, "474"), lines
    await c.send("JOIN #speakroom")
    lines = await c.recv_all(0.5)
    assert not _has_numeric(lines, "474"), lines
    await c.close()


def test_guest_sandbox_prefix_is_exact():
    doc = {
        "version": 1,
        "accounts": [
            _account("testserv-g", SECRET_GUEST, role="guest", grants={"#SBX-1": "speak"})
        ],
    }
    with pytest.raises(AccountsFileError):
        parse_accounts(doc, ["#sbx-"])


def test_store_channel_access_exact(tmp_path):
    write_accounts(str(tmp_path), [_account("testserv-a", SECRET_ALICE, grants={"#ops": "read"})])
    store = AccountStore(str(tmp_path))
    assert store.channel_access("testserv-a", "#ops") == "read"
    assert store.channel_access("testserv-a", "#OPS") is None
    assert store.channel_access("testserv-a", "#Ops") is None
