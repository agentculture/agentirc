"""Channel grants and roles enforced for authenticated accounts (culture#506 t9)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from agentirc.accounts import AccountsFileError, AccountStore, parse_accounts
from agentirc.config import AccountsConfig, ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd
from tests.test_accounts_sasl import (
    SECRET_BOB,
    _account,
    _connect,
    _has_numeric,
    sasl_ls_flow,
    write_accounts,
)

SECRET_RES = "e5" * 32
SECRET_GUEST = "f6" * 32


def _accts():
    return [
        _account(
            "testserv-res",
            SECRET_RES,
            grants={"#speakroom": "speak", "#readroom": "read"},
        ),
        _account("testserv-op", SECRET_BOB, role="operator", grants={}),
        _account("testserv-guest", SECRET_GUEST, role="guest", grants={"#sbx-1": "speak"}),
    ]


@pytest_asyncio.fixture
async def srv(tmp_path):
    data_dir = str(tmp_path / "data")
    write_accounts(data_dir, _accts())
    config = ServerConfig(
        name="testserv",
        host="127.0.0.1",
        port=0,
        webhook_port=0,
        data_dir=data_dir,
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
        accounts=AccountsConfig(guest_channel_prefixes=["#sbx-"]),
    )
    ircd = IRCd(config)
    await ircd.start()
    ircd.config.port = ircd._server.sockets[0].getsockname()[1]
    assert ircd.accounts.get("testserv-guest") is not None  # loaded, not rejected
    ircd._tmp = tmp_path
    yield ircd
    await ircd.stop()


async def _login(srv, nick, secret):
    c = await _connect(srv)
    await sasl_ls_flow(c, nick, nick, secret)
    await c.recv_until(" 903 ")
    await c.send("CAP END")
    await c.recv_until(" 001 ")
    return c


async def _plain(srv, nick):
    c = await _connect(srv)
    await c.send(f"NICK {nick}")
    await c.send("USER u 0 * :u")
    await c.recv_until(" 001 ")
    return c


async def test_join_without_grant_refused(srv):
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #nogrant")
    lines = await c.recv_all(0.5)
    assert _has_numeric(lines, "474"), lines
    assert "#nogrant" not in srv.channels or not srv.channels["#nogrant"].members


async def test_read_grant_joins_but_cannot_speak(srv):
    other = await _plain(srv, "testserv-other")
    await other.send("JOIN #readroom")
    await other.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #readroom")
    assert any("JOIN" in ln for ln in await c.recv_all(0.5))
    await c.send("PRIVMSG #readroom :hello")
    assert _has_numeric(await c.recv_all(0.5), "404")
    await c.send("NOTICE #readroom :hello")
    assert not any("hello" in ln for ln in await other.recv_all(0.5))


async def test_speak_grant_succeeds_and_dm_not_gated(srv):
    other = await _plain(srv, "testserv-other")
    await other.send("JOIN #speakroom")
    await other.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #speakroom")
    await c.recv_all(0.3)
    await c.send("PRIVMSG #speakroom :hi all")
    assert any("hi all" in ln for ln in await other.recv_all(0.5))
    # DM from an account with only a read grant elsewhere: never gated.
    await c.send("PRIVMSG testserv-other :psst")
    assert any("psst" in ln for ln in await other.recv_all(0.5))
    # DM to a stranger-less nick: no 404 for DMs.
    assert not _has_numeric(await c.recv_all(0.2), "404")


async def test_grant_change_applies_without_reconnect(srv):
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #late")
    assert _has_numeric(await c.recv_all(0.4), "474")
    write_accounts(
        str(srv._tmp / "data"),
        [
            _account("testserv-res", SECRET_RES, grants={"#late": "speak"}),
            _account("testserv-op", SECRET_BOB, role="operator", grants={}),
        ],
    )
    await c.send("JOIN #late")
    assert any("JOIN" in ln for ln in await c.recv_all(0.5))


async def test_operator_role_joins_and_speaks_anywhere(srv):
    c = await _login(srv, "testserv-op", SECRET_BOB)
    await c.send("JOIN #anywhere")
    assert any("JOIN" in ln for ln in await c.recv_all(0.5))
    await c.send("PRIVMSG #anywhere :x")
    assert not _has_numeric(await c.recv_all(0.3), "404")


async def test_no_account_clients_unchanged(srv):
    c = await _plain(srv, "testserv-free")
    await c.send("JOIN #whatever")
    assert any("JOIN" in ln for ln in await c.recv_all(0.5))
    await c.send("PRIVMSG #whatever :ok")
    assert not _has_numeric(await c.recv_all(0.3), "404")


async def test_operator_only_room_verbs(srv):
    op = await _login(srv, "testserv-op", SECRET_BOB)
    await op.send("ROOMCREATE #managed :purpose=x")
    await op.recv_all(0.5)
    assert "#managed" in srv.channels

    res = await _login(srv, "testserv-res", SECRET_RES)
    guest = await _login(srv, "testserv-guest", SECRET_GUEST)
    for who in (res, guest):
        await who.send("ROOMCREATE #mine :purpose=y")
        assert any("Operator role required" in ln for ln in await who.recv_all(0.4))
        await who.send("ROOMMETA #managed purpose newpurpose")
        assert any("Operator role required" in ln for ln in await who.recv_all(0.4))
        await who.send("ROOMKICK #managed testserv-op")
        assert any("Operator role required" in ln for ln in await who.recv_all(0.4))
        await who.send("ROOMARCHIVE #managed")
        assert any("Operator role required" in ln for ln in await who.recv_all(0.4))
        await who.send("TAGS testserv-op :a,b")
        assert any("permission" in ln for ln in await who.recv_all(0.4))
    assert "#mine" not in srv.channels

    await op.send("TAGS testserv-res :a,b")
    assert not any("permission" in ln for ln in await op.recv_all(0.4))
    await op.send("ROOMMETA #managed purpose newpurpose")
    await op.recv_all(0.4)
    assert srv.channels["#managed"].purpose == "newpurpose"
    await op.send("ROOMARCHIVE #managed")
    await op.recv_all(0.5)
    assert "#managed" not in srv.channels  # archived (renamed) by the operator account


def test_guest_grant_outside_sandbox_rejected_at_load(tmp_path):
    doc = {
        "version": 1,
        "accounts": [_account("s-g", "x", role="guest", grants={"#general": "read"})],
    }
    with pytest.raises(AccountsFileError, match="sandbox"):
        parse_accounts(doc, ["#sbx-"])
    with pytest.raises(AccountsFileError):  # default empty = no guest grants
        parse_accounts(doc)
    ok = {
        "version": 1,
        "accounts": [_account("s-g", "x", role="guest", grants={"#sbx-9": "read"})],
    }
    assert "s-g" in parse_accounts(ok, ["#sbx-"])
    # non-guests are not restricted
    res = {
        "version": 1,
        "accounts": [_account("s-r", "x", grants={"#general": "read"})],
    }
    assert "s-r" in parse_accounts(res)


def test_guest_violation_rejects_whole_file_keeps_last_good(tmp_path):
    write_accounts(str(tmp_path), [_account("s-r", "x")])
    store = AccountStore(str(tmp_path), ["#sbx-"])
    assert store.get("s-r")
    write_accounts(
        str(tmp_path),
        [
            _account("s-new", "y"),
            _account("s-g", "z", role="guest", grants={"#other": "read"}),
        ],
    )
    store.reload()
    assert store.get("s-r")
    assert store.get("s-new") is None


def test_guest_prefix_config_validation():
    with pytest.raises(ValueError):
        AccountsConfig(guest_channel_prefixes="#sbx-")
    with pytest.raises(ValueError):
        AccountsConfig(guest_channel_prefixes=["sbx-"])
