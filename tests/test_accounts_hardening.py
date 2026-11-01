"""Security hardening of agent accounts (culture#506 h1, plan deviation d6).

Red tests for the CRITICAL/HIGH findings of the independent security review:

- C1: an unregistered connection must not act as any nick (account or not):
  before registration only CAP/NICK/USER/PASS/PING/PONG/AUTHENTICATE/QUIT
  are dispatched, everything else gets ``451 ERR_NOTREGISTERED``; a
  pre-registration QUIT emits no event (so no SQUITUSER reaches peers).
- H1: read grants hold on every read path (HISTORY, BACKFILL, EVENTSUB,
  THREADCLOSE PROMOTE auto-join), not just JOIN.
- H2: speak grants hold on every write path (THREAD CREATE/REPLY,
  THREADCLOSE, TOPIC set, EVENTPUB) and non-operator account sessions are
  never auto-opped.
- H3: a malformed accounts file never crashes the server, the watcher, or
  registration.

Real servers on random ports, real TCP clients.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os

import pytest
import pytest_asyncio

from agentirc.accounts import ACCOUNTS_FILENAME, AccountStore
from agentirc.config import AccountsConfig, ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd
from tests.test_accounts_grants import SECRET_RES, _accts, _login, _plain
from tests.test_accounts_sasl import (
    SECRET_ALICE,
    SECRET_BOB,
    _account,
    _connect,
    _has_numeric,
    _read_to_close,
    _welcomed,
    write_accounts,
)


@pytest_asyncio.fixture
async def srv(tmp_path):
    """Same server as ``test_accounts_grants.srv``: res/op/guest accounts."""
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
    ircd._tmp = tmp_path
    yield ircd
    await ircd.stop()


@pytest_asyncio.fixture
async def linked(tmp_path):
    """alpha <-> beta, with an ``alpha-alice`` operator account on alpha."""
    from tests._helpers import boot_linked_pair, link_pair

    data_a = str(tmp_path / "data_alpha")
    write_accounts(data_a, [_account("alpha-alice", SECRET_ALICE, role="operator")])
    server_a, server_b = await boot_linked_pair(
        tmp_path,
        with_audit=True,
        webhook_port=0,
        data_dirs=(data_a, str(tmp_path / "data_beta")),
    )
    await link_pair(server_a, server_b)
    yield server_a, server_b
    await server_a.stop()
    await server_b.stop()


async def _close(*clients):
    for c in clients:
        await c.close()


def _decoded_event_payloads(lines):
    out = []
    for line in lines:
        if " EVENT " not in line:
            continue
        tok = line.rsplit(" :", 1)[-1]
        try:
            out.append(base64.b64decode(tok, validate=True).decode("utf-8", "replace"))
        except (binascii.Error, ValueError):
            continue
    return out


async def _secret_room(srv):
    """A plain (no-account) client creates #secret and talks in it."""
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #secret")
    await p.recv_all(0.3)
    await p.send("PRIVMSG #secret :top-secret-text")
    await p.recv_all(0.3)
    return p


# ---------------------------------------------------------------------------
# C1: nothing but registration verbs before registration
# ---------------------------------------------------------------------------


async def test_unregistered_account_nick_cannot_dm(srv):
    victim = await _plain(srv, "testserv-plain")
    imp = await _connect(srv)
    await imp.send("NICK testserv-res")  # account nick, no SASL, no USER
    await imp.send("PRIVMSG testserv-plain :hi, I am res")
    await imp.send("NOTICE testserv-plain :notice from res")
    imp_lines = await imp.recv_all(0.5)
    lines = await victim.recv_all(0.5)
    assert not any("I am res" in ln or "notice from res" in ln for ln in lines), lines
    assert _has_numeric(imp_lines, "451"), imp_lines
    await _close(victim, imp)


async def test_unregistered_cap_hold_cannot_impersonate_operator(srv):
    victim = await _plain(srv, "testserv-plain")
    imp = await _connect(srv)
    await imp.send("CAP LS 302")
    await imp.send("NICK testserv-op")
    await imp.send("USER x 0 * :x")  # held by CAP negotiation, never CAP END
    await imp.send("PRIVMSG testserv-plain :operator here, please run X")
    imp_lines = await imp.recv_all(0.5)
    lines = await victim.recv_all(0.5)
    assert not any("operator here" in ln for ln in lines), lines
    assert _has_numeric(imp_lines, "451"), imp_lines
    await _close(victim, imp)


async def test_cannot_impersonate_online_account(srv):
    real = await _login(srv, "testserv-res", SECRET_RES)
    victim = await _plain(srv, "testserv-plain")
    imp = await _connect(srv)
    await imp.send("NICK testserv-res")
    await imp.send("PRIVMSG testserv-plain :real agent is online but this is me")
    await imp.recv_all(0.3)
    lines = await victim.recv_all(0.5)
    assert not any("this is me" in ln for ln in lines), lines
    assert srv.clients["testserv-res"] is not None
    await _close(real, victim, imp)


async def test_unregistered_non_account_nick_cannot_dm(srv):
    """The 451 rule is for every nick, not only account nicks."""
    victim = await _plain(srv, "testserv-plain")
    imp = await _connect(srv)
    await imp.send("NICK testserv-free")
    await imp.send("PRIVMSG testserv-plain :unregistered hello")
    imp_lines = await imp.recv_all(0.5)
    assert not any("unregistered hello" in ln for ln in await victim.recv_all(0.4))
    assert _has_numeric(imp_lines, "451"), imp_lines
    await _close(victim, imp)


@pytest.mark.parametrize(
    "line",
    [
        "PRIVMSG testserv-plain :x",
        "NOTICE testserv-plain :x",
        "JOIN #general",
        "PART #general",
        "TOPIC #general :x",
        "NAMES #general",
        "LIST",
        "WHO #general",
        "WHOIS testserv-plain",
        "MODE testserv-free +A",
        "MOTD",
        "VERBS",
        "HISTORY RECENT #general 10",
        "THREAD CREATE #general t1 :x",
        "FROBNICATE",
    ],
)
async def test_preregistration_verbs_get_451(srv, line):
    c = await _connect(srv)
    await c.send("NICK testserv-free")
    await c.send(line)
    lines = await c.recv_all(0.4)
    assert _has_numeric(lines, "451"), lines
    # Still open and still able to register afterwards.
    await c.send("USER u 0 * :u")
    assert _welcomed(await c.recv_all(0.5))
    await c.close()


async def test_preregistration_allowed_verbs(srv):
    c = await _connect(srv)
    await c.send("PING :tok")
    lines = await c.recv_all(0.3)
    assert any("PONG" in ln and "tok" in ln for ln in lines), lines
    assert not _has_numeric(lines, "451")
    await c.send("CAP LS 302")
    await c.send("NICK testserv-free")
    await c.send("USER u 0 * :u")
    await c.send("CAP END")
    lines = await c.recv_all(0.5)
    assert _welcomed(lines), lines
    assert not _has_numeric(lines, "451"), lines
    await c.close()


async def test_preregistration_quit_emits_no_event(srv):
    real = await _login(srv, "testserv-res", SECRET_RES)
    await real.send("JOIN #speakroom")
    watcher = await _plain(srv, "testserv-watch")
    await watcher.send("CAP REQ :agentirc.io/bot")
    await watcher.send("JOIN #speakroom")
    await watcher.send("EVENTSUB s1 type=user.quit")
    await watcher.recv_all(0.4)
    imp = await _connect(srv)
    await imp.send("NICK testserv-res")
    await imp.send("QUIT :bye")
    await _read_to_close(imp, 1.0)
    lines = await watcher.recv_all(0.6)
    assert not any("QUIT" in ln or " EVENT " in ln for ln in lines), lines
    assert srv.clients.get("testserv-res") is not None
    await _close(real, watcher, imp)


async def test_preregistration_quit_does_not_drop_agent_on_peer(linked):
    server_a, server_b = linked
    real = await _login(server_a, "alpha-alice", SECRET_ALICE)
    for _ in range(50):
        if "alpha-alice" in server_b.remote_clients:
            break
        await asyncio.sleep(0.05)
    assert "alpha-alice" in server_b.remote_clients
    imp = await _connect(server_a)
    await imp.send("NICK alpha-alice")
    await imp.send("QUIT :spoofed")
    await _read_to_close(imp, 1.0)
    await asyncio.sleep(0.3)
    assert "alpha-alice" in server_b.remote_clients
    await _close(real, imp)


# ---------------------------------------------------------------------------
# H1: read grants on every read path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "HISTORY RECENT #secret 10",
        "HISTORY SEARCH #secret top-secret",
        "HISTORY SINCE #secret * 10",
    ],
)
async def test_history_respects_read_grant(srv, line):
    p = await _secret_room(srv)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send(line)
    lines = await c.recv_all(0.5)
    assert not any("top-secret-text" in ln for ln in lines), lines
    await _close(p, c)


async def test_history_allowed_with_read_grant_and_for_operator_and_plain(srv):
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #readroom")
    await p.send("PRIVMSG #readroom :readable-text")
    await p.recv_all(0.3)
    res = await _login(srv, "testserv-res", SECRET_RES)
    await res.send("HISTORY RECENT #readroom 10")
    assert any("readable-text" in ln for ln in await res.recv_all(0.5))
    op = await _login(srv, "testserv-op", SECRET_BOB)
    await op.send("HISTORY RECENT #readroom 10")
    assert any("readable-text" in ln for ln in await op.recv_all(0.5))
    other = await _plain(srv, "testserv-other")  # no account: ungated as before
    await other.send("HISTORY RECENT #readroom 10")
    assert any("readable-text" in ln for ln in await other.recv_all(0.5))
    await _close(p, res, op, other)


async def test_backfill_respects_read_grant(srv):
    p = await _secret_room(srv)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_all(0.3)
    await c.send("BACKFILL #secret * 10")
    lines = await c.recv_all(0.5)
    assert not any(" EVENT " in ln for ln in lines), lines
    assert not any("top-secret-text" in p_ for p_ in _decoded_event_payloads(lines))
    assert any("no-such-channel" in ln for ln in lines), lines
    await _close(p, c)


async def test_backfill_star_skips_channels_whose_grant_was_removed(srv):
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #readroom")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.send("JOIN #readroom")
    await c.recv_all(0.3)
    await p.send("PRIVMSG #readroom :after-join-text")
    await p.recv_all(0.3)
    # Remove the read grant; the session stays joined (no live kick yet).
    accts = [a for a in _accts() if a["nick"] != "testserv-res"]
    accts.append(_account("testserv-res", SECRET_RES, grants={"#speakroom": "speak"}))
    write_accounts(str(srv._tmp / "data"), accts)
    await c.send("BACKFILL * * 10")
    lines = await c.recv_all(0.5)
    assert not any("after-join-text" in d for d in _decoded_event_payloads(lines)), lines
    await _close(p, c)


async def test_eventsub_respects_read_grant(srv):
    p = await _secret_room(srv)
    await p.send("JOIN #readroom")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_all(0.3)
    await c.send("EVENTSUB s1")
    await c.send("EVENTSUB s2 channel=#secret")
    await c.recv_all(0.3)
    await p.send("PRIVMSG #secret :later-secret")
    await p.send("PRIVMSG #readroom :later-readable")
    lines = await c.recv_all(0.8)
    decoded = _decoded_event_payloads(lines)
    assert not any("later-secret" in d for d in decoded), decoded
    assert not any(" #secret " in ln for ln in lines), lines
    assert any("later-readable" in d for d in decoded), decoded
    await _close(p, c)


async def test_eventsub_hides_other_peoples_dms_from_account(srv):
    a = await _plain(srv, "testserv-a")
    b = await _plain(srv, "testserv-b")
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.send("EVENTSUB s1")
    await c.recv_all(0.3)
    await a.send("PRIVMSG testserv-b :private-dm-text")
    await a.send("PRIVMSG testserv-res :dm-to-res")
    lines = await c.recv_all(0.8)
    decoded = _decoded_event_payloads(lines)
    assert not any("private-dm-text" in d for d in decoded), decoded
    assert any("dm-to-res" in d for d in decoded), decoded
    await _close(a, b, c)


async def test_eventsub_unchanged_for_plain_clients(srv):
    p = await _secret_room(srv)
    bot = await _plain(srv, "testserv-bot")
    await bot.send("CAP REQ :agentirc.io/bot")
    await bot.send("EVENTSUB s1")
    await bot.recv_all(0.3)
    await p.send("PRIVMSG #secret :plain-visible")
    decoded = _decoded_event_payloads(await bot.recv_all(0.8))
    assert any("plain-visible" in d for d in decoded), decoded
    await _close(p, bot)


async def test_promote_does_not_force_join_account_without_grant(srv):
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #speakroom")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #speakroom")
    await c.recv_all(0.3)
    await p.send("THREAD CREATE #speakroom t1 :start")
    await c.send("THREAD REPLY #speakroom t1 :res reply")
    await asyncio.sleep(0.2)
    await p.send("THREADCLOSE PROMOTE #speakroom t1 #breakout-x")
    await asyncio.sleep(0.3)
    breakout = srv.channels.get("#breakout-x")
    assert breakout is not None
    assert srv.clients["testserv-res"] not in breakout.members
    assert srv.clients["testserv-plain"] in breakout.members
    lines = await c.recv_all(0.4)
    assert not any("JOIN #breakout-x" in ln or "NOTICE #breakout-x" in ln for ln in lines), lines
    await _close(p, c)


async def test_promote_by_account_needs_grant_on_breakout(srv):
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #speakroom")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #speakroom")
    await c.recv_all(0.3)
    await c.send("THREAD CREATE #speakroom t2 :mine")
    await c.recv_all(0.3)
    await c.send("THREADCLOSE PROMOTE #speakroom t2 #nogrant-breakout")
    await c.recv_all(0.4)
    assert "#nogrant-breakout" not in srv.channels
    await _close(p, c)


# ---------------------------------------------------------------------------
# H2: speak grants on every write path
# ---------------------------------------------------------------------------


async def _reader_in_readroom(srv):
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #readroom")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #readroom")
    await c.recv_all(0.3)
    await p.recv_all(0.2)
    return p, c


async def test_thread_create_needs_speak(srv):
    p, c = await _reader_in_readroom(srv)
    await c.send("THREAD CREATE #readroom t1 :spoken-via-thread")
    refused = await c.recv_all(0.4)
    assert not any("spoken-via-thread" in ln for ln in await p.recv_all(0.4))
    assert _has_numeric(refused, "404"), refused
    await _close(p, c)


async def test_thread_reply_needs_speak(srv):
    p, c = await _reader_in_readroom(srv)
    await p.send("THREAD CREATE #readroom t1 :plain-start")
    await p.recv_all(0.3)
    await c.recv_all(0.3)
    await c.send("THREAD REPLY #readroom t1 :reply-via-thread")
    refused = await c.recv_all(0.4)
    assert not any("reply-via-thread" in ln for ln in await p.recv_all(0.4))
    assert _has_numeric(refused, "404"), refused
    await _close(p, c)


async def test_threadclose_summary_needs_speak(srv):
    p, c = await _reader_in_readroom(srv)
    await p.send("THREAD CREATE #readroom t1 :plain-start")
    await p.recv_all(0.3)
    await c.recv_all(0.3)
    srv.get_skill_for_command("THREAD").get_thread("#readroom", "t1").messages.append(
        type(srv.get_skill_for_command("THREAD").get_thread("#readroom", "t1").messages[0])(
            nick="testserv-res", text="x", timestamp=0.0
        )
    )  # make res a participant (as if it had a speak grant earlier)
    await c.send("THREADCLOSE #readroom t1 :closing-summary")
    await c.recv_all(0.4)
    assert not any("closing-summary" in ln for ln in await p.recv_all(0.4))
    assert not srv.get_skill_for_command("THREAD").get_thread("#readroom", "t1").archived
    await _close(p, c)


async def test_topic_set_needs_speak(srv):
    p, c = await _reader_in_readroom(srv)
    await p.send("TOPIC #readroom :plain-topic")
    await p.recv_all(0.3)
    await c.recv_all(0.3)
    await c.send("TOPIC #readroom :changed-by-reader")
    lines = await c.recv_all(0.4)
    assert srv.channels["#readroom"].topic == "plain-topic"
    assert _has_numeric(lines, "482"), lines
    # Querying the topic is reading: still allowed.
    await c.send("TOPIC #readroom")
    assert any("plain-topic" in ln for ln in await c.recv_all(0.4))
    await _close(p, c)


async def test_topic_set_with_speak_grant_works(srv):
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #speakroom")
    await c.recv_all(0.3)
    await c.send("TOPIC #speakroom :speaker-topic")
    await c.recv_all(0.3)
    assert srv.channels["#speakroom"].topic == "speaker-topic"
    await c.close()


@pytest.mark.parametrize("channel", ["#secret", "#readroom"])
async def test_eventpub_needs_speak(srv, channel):
    p = await _secret_room(srv)
    await p.send("JOIN #readroom")
    await p.send("CAP REQ :agentirc.io/bot")
    await p.send("EVENTSUB s1 type=custom.*")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_all(0.3)
    payload = base64.b64encode(json.dumps({"text": "injected"}).encode()).decode()
    await c.send(f"EVENTPUB custom.thing {channel} {payload}")
    c_lines = await c.recv_all(0.4)
    lines = await p.recv_all(0.6)
    assert not any("custom.thing" in ln for ln in lines), lines
    assert any("EVENTERR" in ln for ln in c_lines), c_lines
    await _close(p, c)


async def test_eventpub_with_speak_grant_works(srv):
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #speakroom")
    await p.send("CAP REQ :agentirc.io/bot")
    await p.send("EVENTSUB s1 type=custom.*")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_all(0.3)
    payload = base64.b64encode(json.dumps({"text": "ok"}).encode()).decode()
    await c.send(f"EVENTPUB custom.thing #speakroom {payload}")
    assert any("custom.thing" in ln for ln in await p.recv_all(0.6))
    await _close(p, c)


async def test_resident_first_joiner_is_not_auto_opped(srv):
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #speakroom")
    await c.recv_all(0.4)
    channel = srv.channels["#speakroom"]
    assert not channel.is_operator(srv.clients["testserv-res"])
    await c.send("MODE #speakroom +o testserv-res")
    assert _has_numeric(await c.recv_all(0.4), "482")
    await c.close()


async def test_resident_not_auto_promoted_when_op_leaves(srv):
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #speakroom")
    await p.recv_all(0.3)
    c = await _login(srv, "testserv-res", SECRET_RES)
    await c.send("JOIN #speakroom")
    await c.recv_all(0.3)
    await p.send("PART #speakroom")
    await asyncio.sleep(0.3)
    channel = srv.channels["#speakroom"]
    assert not channel.is_operator(srv.clients["testserv-res"])
    await _close(p, c)


async def test_operator_account_and_plain_first_joiner_still_auto_opped(srv):
    op = await _login(srv, "testserv-op", SECRET_BOB)
    await op.send("JOIN #oproom")
    await op.recv_all(0.4)
    assert srv.channels["#oproom"].is_operator(srv.clients["testserv-op"])
    p = await _plain(srv, "testserv-plain")
    await p.send("JOIN #plainroom")
    await p.recv_all(0.4)
    assert srv.channels["#plainroom"].is_operator(srv.clients["testserv-plain"])
    await _close(op, p)


# ---------------------------------------------------------------------------
# H3: a malformed accounts file never crashes anything
# ---------------------------------------------------------------------------

_HASH = "0" * 64

_BAD_DOCS = [
    {
        "version": 1,
        "accounts": [{"nick": "x-a", "secret_sha256": _HASH, "role": [], "state": "active"}],
    },
    {
        "version": 1,
        "accounts": [{"nick": "x-a", "secret_sha256": _HASH, "role": {}, "state": "active"}],
    },
    {
        "version": 1,
        "accounts": [{"nick": "x-a", "secret_sha256": _HASH, "role": "resident", "state": {}}],
    },
    {
        "version": 1,
        "accounts": [
            {
                "nick": "x-a",
                "secret_sha256": _HASH,
                "role": "resident",
                "state": "active",
                "grants": {"#a": []},
            }
        ],
    },
    {"version": [], "accounts": []},
    {"version": {}, "accounts": []},
]


@pytest.mark.parametrize("bad", _BAD_DOCS)
def test_malformed_accounts_never_raise_at_startup(tmp_path, bad, caplog):
    (tmp_path / ACCOUNTS_FILENAME).write_text(json.dumps(bad))
    with caplog.at_level(logging.ERROR, logger="agentirc.accounts"):
        store = AccountStore(str(tmp_path))
    assert store.get("x-a") is None
    assert any("rejected" in r.getMessage() for r in caplog.records)
    # Never logs record contents.
    assert not any(_HASH in r.getMessage() for r in caplog.records)


def test_deeply_nested_accounts_never_raise(tmp_path):
    (tmp_path / ACCOUNTS_FILENAME).write_text("[" * 200000 + "]" * 200000)
    store = AccountStore(str(tmp_path))
    assert store.get("x-a") is None


@pytest.mark.parametrize("bad", _BAD_DOCS)
def test_malformed_reload_keeps_last_good_table(tmp_path, bad):
    write_accounts(str(tmp_path), [_account("x-good", SECRET_ALICE)])
    store = AccountStore(str(tmp_path))
    assert store.get("x-good") is not None
    tmp = tmp_path / "accounts.json.tmp"
    tmp.write_text(json.dumps(bad))
    os.replace(tmp, tmp_path / ACCOUNTS_FILENAME)
    assert store.reload() is False
    assert store.get("x-good") is not None


def test_bad_file_is_not_reparsed_on_every_lookup(tmp_path, monkeypatch):
    write_accounts(str(tmp_path), [_account("x-good", SECRET_ALICE)])
    store = AccountStore(str(tmp_path))
    (tmp_path / "accounts.json.tmp").write_text(json.dumps(_BAD_DOCS[0]))
    os.replace(tmp_path / "accounts.json.tmp", tmp_path / ACCOUNTS_FILENAME)
    store.get("x-good")  # first lookup notices the change and rejects the file
    import agentirc.accounts as accounts_mod

    calls = []
    real = accounts_mod.parse_accounts
    monkeypatch.setattr(
        accounts_mod, "parse_accounts", lambda *a, **k: calls.append(1) or real(*a, **k)
    )
    for _ in range(5):
        assert store.get("x-good") is not None
    assert calls == []


async def test_malformed_file_at_runtime_keeps_server_working(srv):
    with open(srv.accounts.path, "w", encoding="utf-8") as f:
        json.dump(_BAD_DOCS[0], f)
    await asyncio.sleep(1.5)
    assert not srv._accounts_task.done()
    # Plain registrations still work...
    c = await _connect(srv)
    await c.send("NICK testserv-zzz")
    await c.send("USER u 0 * :u")
    assert _welcomed(await c.recv_all(0.8))
    # ...and the last good table is still enforced.
    c2 = await _connect(srv)
    await c2.send("NICK testserv-res")
    await c2.send("USER u 0 * :u")
    lines, closed = await _read_to_close(c2, 1.5)
    assert not _welcomed(lines)
    assert _has_numeric(lines, "904")
    res = await _login(srv, "testserv-res", SECRET_RES)
    await _close(c, c2, res)


async def test_watcher_survives_an_exception(srv, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("synthetic")

    monkeypatch.setattr(srv.accounts, "maybe_reload", boom)
    await asyncio.sleep(2.3)
    assert len(calls) >= 2
    assert not srv._accounts_task.done()


async def test_stop_survives_a_failed_watcher_task(tmp_path):
    data_dir = str(tmp_path / "data")
    write_accounts(data_dir, [_account("testserv-a", SECRET_ALICE)])
    config = ServerConfig(
        name="testserv",
        host="127.0.0.1",
        port=0,
        webhook_port=0,
        data_dir=data_dir,
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
    )
    ircd = IRCd(config)
    await ircd.start()
    ircd._accounts_task.cancel()

    async def _failed():
        raise TypeError("synthetic watcher failure")

    task = asyncio.create_task(_failed())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    ircd._accounts_task = task
    await ircd.stop()  # must not raise
    assert ircd._accounts_task is None
