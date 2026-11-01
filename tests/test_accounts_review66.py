"""Code-review findings on agentirc#66 (agent accounts, culture#506).

1. Channel-less EVENTSUB events are scoped for account sessions: the channel
   names they carry (``data.channels`` / ``data.channel`` / a ``#`` target /
   a ``room_id``) are filtered to what the session can read.
2. Channel-less (``*``) EVENTPUB is refused for ``guest`` accounts and for
   sessions whose account is no longer active.
3. ROOMINVITE needs read access on the channel for an account session, and
   an unreadable channel looks exactly like a nonexistent one.
4. ``AuthFailureLimiter`` is a bounded LRU with O(1) amortised record/blocked.
5. Account lookups read the cached table; the file is ``stat()``ed only by
   the watcher, SIGHUP and (throttled) the registration/SASL path.

Real servers on random ports, real TCP clients.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
import pytest_asyncio

from agentirc.accounts import AccountStore, AuthFailureLimiter
from agentirc.config import AccountsConfig, ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd
from tests.test_accounts_grants import SECRET_GUEST, SECRET_RES, _accts, _login, _plain
from tests.test_accounts_sasl import (
    SECRET_BOB,
    _account,
    _connect,
    default_accounts,
    write_accounts,
)

SECRET_WATCH = "a7" * 32


@pytest_asyncio.fixture
async def srv(tmp_path):
    data_dir = str(tmp_path / "data")
    accts = _accts() + [
        _account("testserv-watch", SECRET_WATCH, grants={"#ops": "read"}),
    ]
    write_accounts(data_dir, accts)
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


async def _close(*clients):
    for c in clients:
        await c.close()


def _events(lines):
    """Decoded ``EVENT`` envelopes (dicts) from raw lines."""
    out = []
    for line in lines:
        if " EVENT " not in line:
            continue
        try:
            out.append(json.loads(base64.b64decode(line.rsplit(" :", 1)[-1], validate=True)))
        except ValueError:
            continue
    return out


def _b64(data: dict) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


async def _subscriber(srv, nick, secret=None, *, sub="EVENTSUB s1"):
    c = await _login(srv, nick, secret) if secret else await _plain(srv, nick)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.send(sub)
    await c.recv_all(0.3)
    return c


async def _quitter(srv, nick, channels):
    q = await _plain(srv, nick)
    for ch in channels:
        await q.send(f"JOIN {ch}")
    await q.recv_all(0.3)
    await q.send("QUIT :bye")
    await q.recv_all(0.3)


# ---------------------------------------------------------------------------
# 1. channel-less events are scoped to readable channels
# ---------------------------------------------------------------------------


async def test_guest_does_not_see_quit_from_unreadable_channels(srv):
    guest = await _subscriber(srv, "testserv-guest", SECRET_GUEST, sub="EVENTSUB s1 type=user.quit")
    await _quitter(srv, "testserv-q1", ["#ops", "#incident-42"])
    events = _events(await guest.recv_all(0.6))
    assert events == [], events
    await guest.close()


async def test_guest_quit_channels_filtered_to_readable(srv):
    guest = await _subscriber(srv, "testserv-guest", SECRET_GUEST, sub="EVENTSUB s1 type=user.quit")
    await _quitter(srv, "testserv-q2", ["#ops", "#sbx-1", "#incident-42"])
    events = _events(await guest.recv_all(0.6))
    assert [e["data"]["channels"] for e in events] == [["#sbx-1"]], events
    await guest.close()


async def test_plain_and_operator_see_full_quit_channel_list(srv):
    plain = await _subscriber(srv, "testserv-bot", sub="EVENTSUB s1 type=user.quit")
    op = await _subscriber(srv, "testserv-op", SECRET_BOB, sub="EVENTSUB s1 type=user.quit")
    await _quitter(srv, "testserv-q3", ["#ops", "#incident-42"])
    for c in (plain, op):
        events = _events(await c.recv_all(0.6))
        assert [sorted(e["data"]["channels"]) for e in events] == [["#incident-42", "#ops"]]
    await _close(plain, op)


@pytest.mark.parametrize(
    "payload",
    [{"channel": "#ops"}, {"target": "#ops"}, {"channels": ["#ops"]}],
)
async def test_channelless_eventpub_naming_unreadable_channel_hidden(srv, payload):
    guest = await _subscriber(srv, "testserv-guest", SECRET_GUEST, sub="EVENTSUB s1 type=custom.*")
    watch = await _subscriber(srv, "testserv-watch", SECRET_WATCH, sub="EVENTSUB s1 type=custom.*")
    pub = await _subscriber(srv, "testserv-pub", sub="EVENTSUB s0 type=none.*")
    await pub.send(f"EVENTPUB custom.thing * {_b64(payload)}")
    await pub.recv_all(0.3)
    assert _events(await guest.recv_all(0.5)) == []
    # A session that can read #ops still gets the event, untouched.
    events = _events(await watch.recv_all(0.5))
    assert [e["data"] for e in events] == [payload], events
    await _close(guest, watch, pub)


async def test_channelless_room_id_hidden_when_unreadable(srv):
    op = await _login(srv, "testserv-op", SECRET_BOB)
    await op.send("ROOMCREATE #ops :purpose=ops")
    await op.recv_all(0.3)
    room_id = srv.channels["#ops"].room_id
    guest = await _subscriber(srv, "testserv-guest", SECRET_GUEST, sub="EVENTSUB s1 type=custom.*")
    watch = await _subscriber(srv, "testserv-watch", SECRET_WATCH, sub="EVENTSUB s1 type=custom.*")
    pub = await _subscriber(srv, "testserv-pub", sub="EVENTSUB s0 type=none.*")
    await pub.send(f"EVENTPUB custom.room * {_b64({'room_id': room_id})}")
    await pub.recv_all(0.3)
    assert _events(await guest.recv_all(0.5)) == []
    assert len(_events(await watch.recv_all(0.5))) == 1
    await _close(op, guest, watch, pub)


async def test_guest_still_sees_channelless_event_without_channel_names(srv):
    guest = await _subscriber(srv, "testserv-guest", SECRET_GUEST, sub="EVENTSUB s1 type=custom.*")
    pub = await _subscriber(srv, "testserv-pub", sub="EVENTSUB s0 type=none.*")
    await pub.send(f"EVENTPUB custom.thing * {_b64({'text': 'hello'})}")
    events = _events(await guest.recv_all(0.6))
    assert [e["data"] for e in events] == [{"text": "hello"}], events
    await _close(guest, pub)


# ---------------------------------------------------------------------------
# 2. channel-less EVENTPUB policy
# ---------------------------------------------------------------------------


async def test_guest_channelless_eventpub_refused(srv):
    sub = await _subscriber(srv, "testserv-bot", sub="EVENTSUB s1 type=custom.*")
    guest = await _subscriber(srv, "testserv-guest", SECRET_GUEST, sub="EVENTSUB s0 type=none.*")
    await guest.send(f"EVENTPUB custom.thing * {_b64({'text': 'from-guest'})}")
    g_lines = await guest.recv_all(0.4)
    assert any("EVENTERR custom.thing :permission-denied" in ln for ln in g_lines), g_lines
    assert _events(await sub.recv_all(0.5)) == []
    await _close(sub, guest)


@pytest.mark.parametrize(
    ("nick", "secret"),
    [
        ("testserv-res", SECRET_RES),
        ("testserv-op", SECRET_BOB),
        ("testserv-plain2", None),
    ],
)
async def test_channelless_eventpub_allowed_for_resident_operator_plain(srv, nick, secret):
    sub = await _subscriber(srv, "testserv-bot", sub="EVENTSUB s1 type=custom.*")
    pub = await _subscriber(srv, nick, secret, sub="EVENTSUB s0 type=none.*")
    await pub.send(f"EVENTPUB custom.thing * {_b64({'text': 'ok'})}")
    assert not any("EVENTERR" in ln for ln in await pub.recv_all(0.4))
    assert len(_events(await sub.recv_all(0.5))) == 1
    await _close(sub, pub)


async def test_channelless_eventpub_refused_when_account_gone(srv):
    sub = await _subscriber(srv, "testserv-bot", sub="EVENTSUB s1 type=custom.*")
    pub = await _subscriber(srv, "testserv-res", SECRET_RES, sub="EVENTSUB s0 type=none.*")
    # The account vanishes from the table but the session is not yet evicted
    # (the window between a table change and live revoke).
    srv.accounts._table = {n: a for n, a in srv.accounts._table.items() if n != "testserv-res"}
    await pub.send(f"EVENTPUB custom.thing * {_b64({'text': 'stale'})}")
    p_lines = await pub.recv_all(0.4)
    assert any("EVENTERR custom.thing :permission-denied" in ln for ln in p_lines), p_lines
    assert _events(await sub.recv_all(0.5)) == []
    await _close(sub, pub)


# ---------------------------------------------------------------------------
# 3. ROOMINVITE needs read access, and does not leak existence
# ---------------------------------------------------------------------------


async def _invite_reply(c, channel):
    await c.send(f"ROOMINVITE {channel} testserv-target")
    lines = await c.recv_all(0.4)
    return [ln.replace(channel, "<CH>") for ln in lines]


async def test_guest_roominvite_unreadable_channel_looks_nonexistent(srv):
    op = await _login(srv, "testserv-op", SECRET_BOB)
    await op.send("ROOMCREATE #ops :purpose=secret-purpose;instructions=secret-steps")
    await op.recv_all(0.3)
    target = await _plain(srv, "testserv-target")
    guest = await _login(srv, "testserv-guest", SECRET_GUEST)
    await guest.recv_all(0.3)
    existing = await _invite_reply(guest, "#ops")
    missing = await _invite_reply(guest, "#no-such-room")
    assert existing == missing, (existing, missing)
    assert any(" 403 " in ln for ln in existing), existing
    t_lines = await target.recv_all(0.4)
    assert not any("ROOMINVITE" in ln or "secret-purpose" in ln for ln in t_lines), t_lines
    await _close(op, target, guest)


@pytest.mark.parametrize(
    ("nick", "secret"),
    [
        ("testserv-watch", SECRET_WATCH),
        ("testserv-op", SECRET_BOB),
        ("testserv-plain2", None),
    ],
)
async def test_roominvite_allowed_with_read_access(srv, nick, secret):
    op = await _login(srv, "testserv-op", SECRET_BOB)
    await op.send("ROOMCREATE #ops :purpose=ops-purpose")
    await op.recv_all(0.3)
    target = await _plain(srv, "testserv-target")
    if nick == "testserv-op":
        inviter = op
    elif secret:
        inviter = await _login(srv, nick, secret)
    else:
        inviter = await _plain(srv, nick)
    await inviter.send("ROOMINVITE #ops testserv-target")
    await inviter.recv_all(0.3)
    t_lines = await target.recv_all(0.4)
    assert any("ROOMINVITE" in ln and "ops-purpose" in ln for ln in t_lines), t_lines
    await _close(*{op, target, inviter})


# ---------------------------------------------------------------------------
# 4. bounded LRU failure limiter
# ---------------------------------------------------------------------------


def test_limiter_bounded_under_authcid_churn():
    now = [1000.0]
    lim = AuthFailureLimiter(limit=3, window=60.0, clock=lambda: now[0])
    worst = 0.0
    for i in range(20_000):
        t0 = time.perf_counter()
        lim.record(("127.0.0.1", f"attacker-{i}"))
        worst = max(worst, time.perf_counter() - t0)
    assert len(lim._events) <= 4096
    # O(1) amortised: no record() walks the whole table.
    assert worst < 0.005, worst


def test_limiter_lru_keeps_window_semantics():
    now = [1000.0]
    lim = AuthFailureLimiter(limit=3, window=60.0, clock=lambda: now[0], max_keys=4)
    for _ in range(3):
        lim.record("victim")
    assert lim.blocked("victim")
    for i in range(3):  # fills the table without evicting the recently-used victim
        lim.record(f"k{i}")
    assert lim.blocked("victim")
    now[0] += 61
    assert not lim.blocked("victim")
    assert "victim" not in lim._events  # pruned lazily on touch


def test_limiter_evicts_least_recently_used():
    lim = AuthFailureLimiter(limit=2, window=60.0, clock=lambda: 0.0, max_keys=3)
    for key in ("a", "b", "c"):
        lim.record(key)
    lim.blocked("a")  # touch: a becomes most recent
    lim.record("d")
    assert list(lim._events) == ["c", "a", "d"]


# ---------------------------------------------------------------------------
# 5. lookups read the cache; stat() only from the watcher / SIGHUP / registration
# ---------------------------------------------------------------------------


def _count_stats(store):
    calls = [0]
    orig = store._stat_signature

    def counting():
        calls[0] += 1
        return orig()

    store._stat_signature = counting
    return calls


def test_lookups_do_not_stat(tmp_path):
    write_accounts(str(tmp_path), default_accounts())
    store = AccountStore(str(tmp_path))
    calls = _count_stats(store)
    for _ in range(1000):
        store.get("testserv-alice")
        store.is_reserved("testserv-alice")
        store.lookalike_of("TESTSERV-alice")
        store.channel_access("testserv-alice", "#general")
        store.role_of("testserv-alice")
    assert calls[0] == 0


async def test_registration_costs_at_most_three_stats(srv):
    srv._accounts_task.cancel()  # no watcher ticks in the count
    calls = _count_stats(srv.accounts)
    c = await _login(srv, "testserv-res", SECRET_RES)
    for i in range(20):
        await c.send(f"NICK testserv-res{i}")  # refused, cached lookups only
    await c.recv_all(0.3)
    # first pre-registration NICK + SASL credential check + registration gate
    assert calls[0] <= 3, calls[0]
    await c.close()


async def test_hot_paths_do_not_stat(srv):
    calls = _count_stats(srv.accounts)
    bot = await _subscriber(srv, "testserv-watch", SECRET_WATCH)
    res = await _login(srv, "testserv-res", SECRET_RES)
    await res.send("JOIN #speakroom")
    await res.recv_all(0.2)
    calls[0] = 0
    start = time.monotonic()
    for i in range(100):
        await res.send(f"PRIVMSG #speakroom :msg-{i}")
    await res.recv_all(0.3)
    await bot.recv_all(0.3)
    elapsed = time.monotonic() - start
    # Only the 1 s watcher may stat in this window.
    assert calls[0] <= int(elapsed) + 1, calls[0]
    await _close(bot, res)


async def test_fresh_account_enforced_at_next_registration(srv):
    """Reload-on-registration survives the lookup cache (no watcher tick needed)."""
    srv._accounts_task.cancel()  # prove it is the registration path, not the watcher
    data_dir = srv.config.data_dir
    write_accounts(data_dir, _accts() + [_account("testserv-new", SECRET_WATCH)])
    c = await _connect(srv)
    await c.send("NICK testserv-new")
    await c.send("USER u 0 * :u")
    lines = await c.recv_all(0.6)
    assert not any(" 001 " in ln for ln in lines), lines
    await c.close()
