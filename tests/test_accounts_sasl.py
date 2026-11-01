"""Agent accounts + SASL PLAIN authentication (culture#506, plan task t8).

Real servers on random ports, real TCP clients. Covers:

- accounts load from ``<data_dir>/accounts.json`` and reload on change
  (lazy mtime check) without a restart (SIGHUP lives in
  ``test_accounts_sighup.py`` because it drives a real ``agentirc serve``);
- an account nick never gets 001 without SASL PLAIN — on the CAP LS path,
  the bare ``CAP REQ`` path and the no-CAP path;
- wrong / revoked / suspended secrets get 904 and a closed connection; the
  right secret gets 001 whether or not the real agent is already online;
- SASL is accepted only from loopback or a configured trusted CIDR;
- nicks without an account (including ``<agent>__peek…`` observer nicks)
  register exactly as before;
- no account data crosses a server link (two linked servers).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket

import pytest
import pytest_asyncio

from agentirc.accounts import ACCOUNTS_FILENAME, AccountStore, is_trusted_peer
from agentirc.config import AccountsConfig, ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd
from tests.conftest import IRCTestClient

SECRET_ALICE = "a1" * 32  # 64-char hex, the shape culture mints
SECRET_BOB = "b2" * 32
SECRET_REVOKED = "c3" * 32
SECRET_SUSPENDED = "d4" * 32


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _account(nick, secret, *, role="resident", grants=None, state="active"):
    return {
        "nick": nick,
        "secret_sha256": _sha(secret),
        "role": role,
        "grants": grants if grants is not None else {"#general": "speak"},
        "state": state,
    }


def write_accounts(data_dir, accounts, *, version=1) -> None:
    """Atomically (tmp + rename) write an accounts file, like culture does."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, ACCOUNTS_FILENAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": version, "accounts": accounts}, f)
    os.replace(tmp, path)


def default_accounts(prefix="testserv"):
    return [
        _account(
            f"{prefix}-alice",
            SECRET_ALICE,
            grants={"#general": "speak", "#ops": "read"},
        ),
        _account(f"{prefix}-bob", SECRET_BOB, role="operator"),
        _account(f"{prefix}-rev", SECRET_REVOKED, state="revoked"),
        _account(f"{prefix}-sus", SECRET_SUSPENDED, state="suspended"),
    ]


def plain_payload(authcid: str, secret: str, authzid: str = "") -> str:
    raw = f"{authzid}\0{authcid}\0{secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


async def _boot(tmp_path, *, host="127.0.0.1", trusted_cidrs=None, accounts=None):
    data_dir = str(tmp_path / "data")
    write_accounts(data_dir, default_accounts() if accounts is None else accounts)
    config = ServerConfig(
        name="testserv",
        host=host,
        port=0,
        webhook_port=0,
        data_dir=data_dir,
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
        accounts=AccountsConfig(trusted_cidrs=list(trusted_cidrs or [])),
    )
    ircd = IRCd(config)
    await ircd.start()
    ircd.config.port = ircd._server.sockets[0].getsockname()[1]
    return ircd


@pytest_asyncio.fixture
async def acct_server(tmp_path):
    ircd = await _boot(tmp_path)
    yield ircd
    await ircd.stop()


async def _connect(server, host="127.0.0.1") -> IRCTestClient:
    reader, writer = await asyncio.open_connection(host, server.config.port)
    return IRCTestClient(reader, writer)


def _welcomed(lines) -> bool:
    return any(" 001 " in line for line in lines)


def _has_numeric(lines, code: str) -> bool:
    return any(f" {code} " in line for line in lines)


async def _read_to_close(
    c: IRCTestClient, timeout: float = 2.0
) -> tuple[list[str], bool]:
    """Read lines until the server closes the socket (or *timeout*).

    Returns ``(lines, closed)``.
    """
    lines: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                lines.append(await c.recv(timeout=timeout))
    except ConnectionError:
        return lines, True
    except (asyncio.TimeoutError, TimeoutError):
        return lines, False


async def sasl_ls_flow(c: IRCTestClient, nick: str, authcid: str, secret: str) -> None:
    """The standard IRCv3 flow: CAP LS, NICK/USER, REQ sasl, AUTHENTICATE, END."""
    await c.send("CAP LS 302")
    await c.send(f"NICK {nick}")
    await c.send("USER u 0 * :u")
    await c.send("CAP REQ :sasl")
    await c.recv_until(" ACK ")
    await c.send("AUTHENTICATE PLAIN")
    await c.recv_until("AUTHENTICATE +")
    await c.send(f"AUTHENTICATE {plain_payload(authcid, secret)}")


# ---------------------------------------------------------------------------
# Accounts file: load + reload
# ---------------------------------------------------------------------------


def test_store_loads_all_fields(tmp_path):
    write_accounts(str(tmp_path), default_accounts())
    store = AccountStore(str(tmp_path))
    alice = store.get("testserv-alice")
    assert alice is not None
    assert alice.nick == "testserv-alice"
    assert alice.secret_sha256 == _sha(SECRET_ALICE)
    assert alice.role == "resident"
    assert alice.grants == {"#general": "speak", "#ops": "read"}
    assert alice.state == "active"
    assert store.get("testserv-bob").role == "operator"
    assert store.get("testserv-rev").state == "revoked"
    assert store.get("testserv-sus").state == "suspended"
    assert store.get("testserv-nobody") is None
    # Exact match only: observer nicks are not covered by the account.
    assert store.get("testserv-alice__peekab12") is None


def test_store_without_data_dir_is_empty():
    store = AccountStore("")
    assert store.get("testserv-alice") is None
    assert store.is_account("testserv-alice") is False


def test_store_missing_file_is_empty(tmp_path):
    store = AccountStore(str(tmp_path))
    assert store.get("testserv-alice") is None


def test_store_verify_uses_hash_and_state(tmp_path):
    write_accounts(str(tmp_path), default_accounts())
    store = AccountStore(str(tmp_path))
    assert store.verify("testserv-alice", SECRET_ALICE) is not None
    assert store.verify("testserv-alice", SECRET_BOB) is None
    assert store.verify("testserv-rev", SECRET_REVOKED) is None
    assert store.verify("testserv-sus", SECRET_SUSPENDED) is None
    assert store.verify("testserv-nobody", SECRET_ALICE) is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"version": 2, "accounts": []}),
        json.dumps({"accounts": []}),
        json.dumps({"version": 1, "accounts": [{"nick": "testserv-x"}]}),
        json.dumps(
            {
                "version": 1,
                "accounts": [
                    {
                        "nick": "testserv-x",
                        "secret_sha256": "nothex",
                        "role": "resident",
                        "grants": {},
                        "state": "active",
                    }
                ],
            }
        ),
        json.dumps(
            {
                "version": 1,
                "accounts": [_account("testserv-x", SECRET_ALICE, role="king")],
            }
        ),
        json.dumps(
            {
                "version": 1,
                "accounts": [_account("testserv-x", SECRET_ALICE, state="zombie")],
            }
        ),
        json.dumps(
            {
                "version": 1,
                "accounts": [
                    _account("testserv-x", SECRET_ALICE, grants={"#a": "write"})
                ],
            }
        ),
        json.dumps(
            {
                "version": 1,
                "accounts": [
                    _account("testserv-x", SECRET_ALICE),
                    _account("testserv-x", SECRET_BOB),
                ],
            }
        ),
        json.dumps([1, 2, 3]),
    ],
)
def test_malformed_file_keeps_last_good_table(tmp_path, caplog, content):
    write_accounts(str(tmp_path), default_accounts())
    store = AccountStore(str(tmp_path))
    assert store.get("testserv-alice") is not None
    path = tmp_path / ACCOUNTS_FILENAME
    path.write_text(content, encoding="utf-8")
    os.utime(path, ns=(1, 1))  # force a signature change regardless of fs granularity
    with caplog.at_level("ERROR", logger="agentirc.accounts"):
        assert store.get("testserv-alice") is not None  # last good table kept
        assert store.get("testserv-x") is None
    assert any("accounts" in r.getMessage() for r in caplog.records)
    # The secret hash never reaches the log.
    assert all(_sha(SECRET_ALICE) not in r.getMessage() for r in caplog.records)


def test_malformed_file_at_startup_is_empty_not_crash(tmp_path):
    (tmp_path / ACCOUNTS_FILENAME).write_text("{nope", encoding="utf-8")
    store = AccountStore(str(tmp_path))
    assert store.get("testserv-alice") is None


@pytest.mark.asyncio
async def test_accounts_reload_on_change_without_restart(acct_server, tmp_path):
    data_dir = acct_server.config.data_dir
    # carol has no account yet: registers exactly as today.
    c = await _connect(acct_server)
    await c.send("NICK testserv-carol")
    await c.send("USER carol 0 * :carol")
    assert " 001 " in await c.recv_until(" 001 ")
    await c.send("QUIT :bye")
    await c.close()
    await asyncio.sleep(0.1)

    # culture mints carol a credential: same running server now refuses her
    # without SASL, and accepts her with it.
    write_accounts(
        data_dir, default_accounts() + [_account("testserv-carol", SECRET_BOB)]
    )
    c = await _connect(acct_server)
    await c.send("NICK testserv-carol")
    await c.send("USER carol 0 * :carol")
    lines, closed = await _read_to_close(c)
    assert not _welcomed(lines)
    assert _has_numeric(lines, "904")
    assert closed

    c = await _connect(acct_server)
    await sasl_ls_flow(c, "testserv-carol", "testserv-carol", SECRET_BOB)
    await c.recv_until(" 903 ")
    await c.send("CAP END")
    assert " 001 " in await c.recv_until(" 001 ")
    await c.close()

    # Account removed again: back to no-account behaviour.
    write_accounts(data_dir, default_accounts())
    await asyncio.sleep(0.1)
    c = await _connect(acct_server)
    await c.send("NICK testserv-carol")
    await c.send("USER carol 0 * :carol")
    assert " 001 " in await c.recv_until(" 001 ")
    await c.close()


@pytest.mark.asyncio
async def test_ircd_reload_accounts_forces_reload(acct_server):
    """``IRCd.reload_accounts()`` (the SIGHUP handler) re-reads even when the
    file signature is unchanged."""
    path = os.path.join(acct_server.config.data_dir, ACCOUNTS_FILENAME)
    st = os.stat(path)
    with open(path, encoding="utf-8") as f:
        original = f.read()
    # Same length, same mtime, same inode: invisible to the lazy check.
    swapped = original.replace("testserv-alice", "testserv-alicx")
    assert len(swapped) == len(original)
    with open(path, "w", encoding="utf-8") as f:
        f.write(swapped)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert acct_server.accounts.get("testserv-alice") is not None  # stale, as expected
    acct_server.reload_accounts()
    assert acct_server.accounts.get("testserv-alice") is None
    assert acct_server.accounts.get("testserv-alicx") is not None


# ---------------------------------------------------------------------------
# CAP advertisement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_ls_advertises_sasl(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    line = await c.recv_until(" LS ")
    assert "sasl=PLAIN" in line
    await c.close()
    c = await _connect(acct_server)
    await c.send("CAP LS")
    line = await c.recv_until(" LS ")
    assert " sasl" in line or ":sasl" in line
    await c.close()


# ---------------------------------------------------------------------------
# Account nick refused without SASL — three registration paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_nick_refused_cap_ls_path_without_sasl(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    await c.send("NICK testserv-alice")
    await c.send("USER alice 0 * :alice")
    await c.send("CAP END")
    lines, closed = await _read_to_close(c)
    assert not _welcomed(lines)
    assert _has_numeric(lines, "904")
    assert closed
    assert "testserv-alice" not in acct_server.clients


@pytest.mark.asyncio
async def test_account_nick_refused_bare_cap_req_path_without_sasl(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP REQ :sasl")
    await c.send("NICK testserv-alice")
    await c.send("USER alice 0 * :alice")
    lines, closed = await _read_to_close(c)
    assert not _welcomed(lines)
    assert _has_numeric(lines, "904")
    assert closed


@pytest.mark.asyncio
async def test_account_nick_refused_no_cap_path(linked_servers_with_accounts):
    server_a, server_b = linked_servers_with_accounts
    c = await _connect(server_a)
    await c.send("NICK alpha-alice")
    await c.send("USER alice 0 * :alice")
    lines, closed = await _read_to_close(c)
    assert not _welcomed(lines)
    assert _has_numeric(lines, "904")
    assert closed
    await asyncio.sleep(0.2)
    # No SNICK reached the peer.
    assert "alpha-alice" not in server_b.remote_clients


@pytest.mark.asyncio
async def test_authenticate_without_sasl_cap_fails(acct_server):
    c = await _connect(acct_server)
    await c.send("AUTHENTICATE PLAIN")
    lines = await c.recv_all(timeout=0.5)
    assert _has_numeric(lines, "904")
    assert not any("AUTHENTICATE +" in line for line in lines)
    await c.close()


# ---------------------------------------------------------------------------
# Wrong / revoked / suspended secrets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nick,secret",
    [
        ("testserv-alice", SECRET_BOB),  # wrong secret
        ("testserv-alice", ""),  # empty secret
        ("testserv-rev", SECRET_REVOKED),  # right secret, revoked
        ("testserv-sus", SECRET_SUSPENDED),  # right secret, suspended
        ("testserv-nobody", SECRET_ALICE),  # no such account
    ],
)
async def test_bad_credentials_get_904_and_close(acct_server, nick, secret):
    c = await _connect(acct_server)
    await sasl_ls_flow(c, nick, nick, secret)
    lines, closed = await _read_to_close(c)
    assert _has_numeric(lines, "904")
    assert not _has_numeric(lines, "903")
    assert not _welcomed(lines)
    assert closed
    assert nick not in acct_server.clients


@pytest.mark.asyncio
async def test_secret_for_account_a_cannot_register_nick_b(acct_server):
    """Authenticating as alice, then registering as bob, is refused."""
    c = await _connect(acct_server)
    await sasl_ls_flow(c, "testserv-bob", "testserv-alice", SECRET_ALICE)
    lines = await c.recv_all(timeout=0.5)
    await c.send("CAP END")
    more, closed = await _read_to_close(c)
    lines += more
    assert not _welcomed(lines)
    assert _has_numeric(lines, "904")
    assert closed
    assert "testserv-bob" not in acct_server.clients


@pytest.mark.asyncio
async def test_authzid_must_match_authcid(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    await c.send("NICK testserv-alice")
    await c.send("USER u 0 * :u")
    await c.send("CAP REQ :sasl")
    await c.send("AUTHENTICATE PLAIN")
    await c.recv_until("AUTHENTICATE +")
    await c.send(
        f"AUTHENTICATE {plain_payload('testserv-alice', SECRET_ALICE, authzid='testserv-bob')}"
    )
    lines, closed = await _read_to_close(c)
    assert _has_numeric(lines, "904")
    assert not _welcomed(lines)
    assert closed


# ---------------------------------------------------------------------------
# Right secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_right_secret_cap_ls_path_registers(acct_server):
    c = await _connect(acct_server)
    await sasl_ls_flow(c, "testserv-alice", "testserv-alice", SECRET_ALICE)
    out = await c.recv_until(" 903 ")
    assert " 900 " in out
    assert " 903 " in out
    await c.send("CAP END")
    assert " 001 " in await c.recv_until(" 001 ")
    client = acct_server.clients["testserv-alice"]
    # Account carried on the client for later enforcement (t9).
    assert client.account is not None
    assert client.account.role == "resident"
    assert client.account.grants == {"#general": "speak", "#ops": "read"}
    await c.close()


@pytest.mark.asyncio
async def test_right_secret_bare_cap_req_path_registers(acct_server):
    """A client that REQs sasl without LS and authenticates before NICK/USER."""
    c = await _connect(acct_server)
    await c.send("CAP REQ :sasl")
    await c.recv_until(" ACK ")
    await c.send("AUTHENTICATE PLAIN")
    await c.recv_until("AUTHENTICATE +")
    await c.send(f"AUTHENTICATE {plain_payload('testserv-bob', SECRET_BOB)}")
    await c.recv_until(" 903 ")
    await c.send("NICK testserv-bob")
    await c.send("USER bob 0 * :bob")
    assert " 001 " in await c.recv_until(" 001 ")
    assert acct_server.clients["testserv-bob"].account.role == "operator"
    await c.close()


@pytest.mark.asyncio
async def test_right_secret_registers_while_real_agent_online(acct_server):
    """The credential always wins: a second authenticated session for the same
    account registers and the old session is closed."""
    old = await _connect(acct_server)
    await sasl_ls_flow(old, "testserv-alice", "testserv-alice", SECRET_ALICE)
    await old.recv_until(" 903 ")
    await old.send("CAP END")
    assert " 001 " in await old.recv_until(" 001 ")
    await old.send("JOIN #general")
    await old.recv_until(" 366 ")

    # An impostor without the credential gets nowhere while alice is online.
    imp = await _connect(acct_server)
    await imp.send("NICK testserv-alice")
    await imp.send("USER x 0 * :x")
    lines, closed = await _read_to_close(imp)
    assert not _welcomed(lines)
    assert closed
    assert acct_server.clients["testserv-alice"].account is not None

    new = await _connect(acct_server)
    await sasl_ls_flow(new, "testserv-alice", "testserv-alice", SECRET_ALICE)
    await new.recv_until(" 903 ")
    await new.send("CAP END")
    assert " 001 " in await new.recv_until(" 001 ")

    old_lines, old_closed = await _read_to_close(old)
    assert old_closed
    assert any(line.startswith("ERROR") for line in old_lines)
    await asyncio.sleep(0.1)
    holder = acct_server.clients["testserv-alice"]
    # The surviving entry is the new session, and the old one left #general.
    assert holder.account is not None
    general = acct_server.channels.get("#general")
    assert general is None or holder not in general.members and not general.members
    await new.close()


@pytest.mark.asyncio
async def test_right_secret_registers_when_unauthenticated_squatter_holds_nick(
    tmp_path,
):
    """A nick taken before its credential was minted (no flag day) is reclaimed
    by the authenticated agent."""
    ircd = await _boot(tmp_path, accounts=[])
    try:
        squat = await _connect(ircd)
        await squat.send("NICK testserv-alice")
        await squat.send("USER s 0 * :s")
        assert " 001 " in await squat.recv_until(" 001 ")
        write_accounts(ircd.config.data_dir, default_accounts())
        c = await _connect(ircd)
        await sasl_ls_flow(c, "testserv-alice", "testserv-alice", SECRET_ALICE)
        await c.recv_until(" 903 ")
        await c.send("CAP END")
        assert " 001 " in await c.recv_until(" 001 ")
        _, closed = await _read_to_close(squat)
        assert closed
        assert ircd.clients["testserv-alice"].account is not None
        await c.close()
    finally:
        await ircd.stop()


# ---------------------------------------------------------------------------
# Post-registration NICK change, observer nicks, no-account nicks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nick_change_to_account_nick_refused(acct_server):
    c = await _connect(acct_server)
    await c.send("NICK testserv-dave")
    await c.send("USER dave 0 * :dave")
    assert " 001 " in await c.recv_until(" 001 ")
    await c.send("NICK testserv-alice")
    lines = await c.recv_all(timeout=0.5)
    assert _has_numeric(lines, "432")
    assert "testserv-alice" not in acct_server.clients
    assert "testserv-dave" in acct_server.clients
    await c.close()


@pytest.mark.asyncio
async def test_peek_observer_nick_unaffected(acct_server):
    c = await _connect(acct_server)
    await c.send("NICK testserv-alice__peekab12")
    await c.send("USER peek 0 * :peek")
    assert " 001 " in await c.recv_until(" 001 ")
    assert acct_server.clients["testserv-alice__peekab12"].account is None
    await c.close()


@pytest.mark.asyncio
async def test_no_account_nick_registers_as_today(acct_server):
    c = await _connect(acct_server)
    await c.send("NICK testserv-erin")
    await c.send("USER erin 0 * :erin")
    out = await c.recv_until(" 376 ")
    assert " 001 " in out
    assert " 904 " not in out and " 900 " not in out
    # Prefix check still applies.
    d = await _connect(acct_server)
    await d.send("NICK otherserv-erin")
    assert " 432 " in await d.recv_until(" 432 ")
    # Collision check still applies.
    await d.send("NICK testserv-erin")
    assert " 433 " in await d.recv_until(" 433 ")
    await c.close()
    await d.close()


# ---------------------------------------------------------------------------
# AUTHENTICATE wire details
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_mechanism(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    await c.send("CAP REQ :sasl")
    await c.send("AUTHENTICATE EXTERNAL")
    lines = await c.recv_all(timeout=0.5)
    assert _has_numeric(lines, "908")
    assert _has_numeric(lines, "904")
    await c.close()


@pytest.mark.asyncio
async def test_abort_with_star(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    await c.send("CAP REQ :sasl")
    await c.send("AUTHENTICATE PLAIN")
    await c.recv_until("AUTHENTICATE +")
    await c.send("AUTHENTICATE *")
    lines = await c.recv_all(timeout=0.5)
    assert _has_numeric(lines, "906")
    await c.close()


@pytest.mark.asyncio
async def test_payload_exactly_400_chars_then_plus(acct_server):
    """A 400-byte chunk means "more follows"; a lone ``+`` terminates it."""
    authcid = "testserv-alice"
    # Pad the authzid-less payload to exactly 300 raw bytes -> 400 base64 chars
    # by using a long (wrong-length but valid-shaped) secret, so the server
    # must wait for the terminator before deciding. The secret is wrong, so
    # the outcome is 904 — what matters is that it is decided only after "+".
    pad = 300 - len(authcid) - 2
    payload = plain_payload(authcid, "x" * pad)
    assert len(payload) == 400
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    await c.send("CAP REQ :sasl")
    await c.send("AUTHENTICATE PLAIN")
    await c.recv_until("AUTHENTICATE +")
    await c.send(f"AUTHENTICATE {payload}")
    lines = await c.recv_all(timeout=0.5)
    assert not _has_numeric(lines, "904")  # still waiting for more
    await c.send("AUTHENTICATE +")
    lines, closed = await _read_to_close(c)
    assert _has_numeric(lines, "904")
    assert closed


@pytest.mark.asyncio
async def test_oversized_payload_rejected(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    await c.send("CAP REQ :sasl")
    await c.send("AUTHENTICATE PLAIN")
    await c.recv_until("AUTHENTICATE +")
    chunk = "A" * 400
    lines: list[str] = []
    closed = False
    for _ in range(5):
        try:
            await c.send(f"AUTHENTICATE {chunk}")
        except ConnectionError:
            closed = True
            break
    more, closed2 = await _read_to_close(c)
    lines += more
    assert _has_numeric(lines, "905") or _has_numeric(lines, "904")
    assert closed or closed2


@pytest.mark.asyncio
async def test_garbage_base64_fails(acct_server):
    c = await _connect(acct_server)
    await c.send("CAP LS 302")
    await c.send("CAP REQ :sasl")
    await c.send("AUTHENTICATE PLAIN")
    await c.recv_until("AUTHENTICATE +")
    await c.send("AUTHENTICATE !!!notbase64!!!")
    lines, closed = await _read_to_close(c)
    assert _has_numeric(lines, "904")
    assert closed


@pytest.mark.asyncio
async def test_authenticate_after_registration_rejected(acct_server):
    c = await _connect(acct_server)
    await sasl_ls_flow(c, "testserv-alice", "testserv-alice", SECRET_ALICE)
    await c.recv_until(" 903 ")
    await c.send("CAP END")
    await c.recv_until(" 001 ")
    await c.send("AUTHENTICATE PLAIN")
    lines = await c.recv_all(timeout=0.5)
    assert _has_numeric(lines, "907")
    await c.close()


# ---------------------------------------------------------------------------
# Trusted peers
# ---------------------------------------------------------------------------


def test_is_trusted_peer_rules():
    assert is_trusted_peer("127.0.0.1", [])
    assert is_trusted_peer("127.4.5.6", [])
    assert is_trusted_peer("::1", [])
    assert is_trusted_peer("::ffff:127.0.0.1", [])
    assert not is_trusted_peer("10.1.2.3", [])
    assert is_trusted_peer("10.1.2.3", ["10.0.0.0/8"])
    assert not is_trusted_peer("10.1.2.3", ["192.168.0.0/16"])
    assert is_trusted_peer("100.64.1.1", ["100.64.0.0/10", "10.0.0.0/8"])
    assert is_trusted_peer("fd00::5", ["fd00::/8"])
    assert not is_trusted_peer("unknown", ["0.0.0.0/0"])
    assert not is_trusted_peer("", [])


def _non_loopback_ipv4() -> str | None:
    """A local non-loopback IPv4 this host can bind and connect to, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    if ip.startswith("127.") or ip == "0.0.0.0":
        return None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind((ip, 0))
        probe.close()
    except OSError:
        return None
    return ip


@pytest.mark.asyncio
async def test_sasl_from_untrusted_peer_fails_904(tmp_path):
    ip = _non_loopback_ipv4()
    if ip is None:
        pytest.skip("no bindable non-loopback IPv4 on this host")
    ircd = await _boot(tmp_path, host=ip)
    try:
        c = await _connect(ircd, host=ip)
        await sasl_ls_flow(c, "testserv-alice", "testserv-alice", SECRET_ALICE)
        lines, closed = await _read_to_close(c)
        assert _has_numeric(lines, "904")
        assert not _has_numeric(lines, "903")
        assert not _welcomed(lines)
        assert closed
    finally:
        await ircd.stop()


@pytest.mark.asyncio
async def test_sasl_from_trusted_cidr_succeeds(tmp_path):
    ip = _non_loopback_ipv4()
    if ip is None:
        pytest.skip("no bindable non-loopback IPv4 on this host")
    ircd = await _boot(tmp_path, host=ip, trusted_cidrs=[f"{ip}/32"])
    try:
        c = await _connect(ircd, host=ip)
        await sasl_ls_flow(c, "testserv-alice", "testserv-alice", SECRET_ALICE)
        await c.recv_until(" 903 ")
        await c.send("CAP END")
        assert " 001 " in await c.recv_until(" 001 ")
        await c.close()
    finally:
        await ircd.stop()


@pytest.mark.asyncio
async def test_sasl_untrusted_peer_address_fails_deterministic(
    acct_server, monkeypatch
):
    """Host-independent variant: the trust decision is made on the client's
    peer address, so a client whose recorded peer address is non-loopback is
    refused even though the TCP socket is local."""
    from agentirc import client as client_mod

    orig_init = client_mod.Client.__init__

    def fake_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.host = "203.0.113.7"  # TEST-NET-3, never loopback

    monkeypatch.setattr(client_mod.Client, "__init__", fake_init)
    c = await _connect(acct_server)
    await sasl_ls_flow(c, "testserv-alice", "testserv-alice", SECRET_ALICE)
    lines, closed = await _read_to_close(c)
    assert _has_numeric(lines, "904")
    assert not _welcomed(lines)
    assert closed


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_yaml_accounts_trusted_cidrs(tmp_path):
    p = tmp_path / "server.yaml"
    p.write_text(
        "server:\n  name: spark\naccounts:\n  trusted_cidrs:\n    - 100.64.0.0/10\n"
        "    - fd7a:115c:a1e0::/48\n  unknown_key: 1\n",
        encoding="utf-8",
    )
    cfg = ServerConfig.from_yaml(p)
    assert cfg.accounts.trusted_cidrs == ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
    assert ServerConfig().accounts.trusted_cidrs == []


def test_config_rejects_bad_cidr():
    with pytest.raises(ValueError):
        AccountsConfig(trusted_cidrs=["not-a-cidr"])


def test_cli_resolve_config_carries_accounts(tmp_path):
    import argparse

    from agentirc.cli import _resolve_config

    p = tmp_path / "server.yaml"
    p.write_text("accounts:\n  trusted_cidrs: [10.0.0.0/8]\n", encoding="utf-8")
    args = argparse.Namespace(
        config=str(p),
        name=None,
        host=None,
        port=None,
        webhook_port=None,
        data_dir=str(tmp_path),
        link=None,
    )
    cfg = _resolve_config(args)
    assert cfg.accounts.trusted_cidrs == ["10.0.0.0/8"]


# ---------------------------------------------------------------------------
# S2S: no account data crosses a link
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def linked_servers_with_accounts(tmp_path):
    from tests._helpers import boot_linked_pair, link_pair

    data_a = str(tmp_path / "data_alpha")
    write_accounts(
        data_a,
        [
            _account(
                "alpha-alice",
                SECRET_ALICE,
                role="operator",
                grants={"#general": "speak", "#hidden-grant-room": "read"},
            )
        ],
    )
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


@pytest.mark.asyncio
async def test_no_account_data_in_s2s_traffic(
    linked_servers_with_accounts, monkeypatch
):
    from agentirc.server_link import ServerLink

    server_a, server_b = linked_servers_with_accounts
    sent: list[str] = []
    orig_send_raw = ServerLink.send_raw

    async def recording_send_raw(self, line, *a, **kw):
        sent.append(line)
        return await orig_send_raw(self, line, *a, **kw)

    monkeypatch.setattr(ServerLink, "send_raw", recording_send_raw)

    c = await _connect(server_a)
    await sasl_ls_flow(c, "alpha-alice", "alpha-alice", SECRET_ALICE)
    await c.recv_until(" 903 ")
    await c.send("CAP END")
    await c.recv_until(" 001 ")
    await c.send("JOIN #general")
    await c.recv_until(" 366 ")
    await c.send("PRIVMSG #general :hello mesh")
    await asyncio.sleep(0.5)

    assert "alpha-alice" in server_b.remote_clients
    assert any(line.split(" ")[0] == "SNICK" or " SNICK " in line for line in sent)
    assert any("SEVENT" in line or "SMSG" in line for line in sent)

    forbidden = [
        _sha(SECRET_ALICE),
        SECRET_ALICE,
        "secret_sha256",
        "operator",
        "#hidden-grant-room",
        "grants",
        base64.b64encode(_sha(SECRET_ALICE).encode()).decode()[:24],
    ]
    for line in sent:
        decoded_parts = [line]
        # SEVENT payloads are base64 JSON: decode any base64-looking token.
        for tok in line.split(" "):
            tok = tok.lstrip(":")
            try:
                decoded_parts.append(
                    base64.b64decode(tok, validate=True).decode("utf-8", "ignore")
                )
            except Exception:  # noqa: BLE001
                pass
        for blob in decoded_parts:
            for needle in forbidden:
                assert (
                    needle not in blob
                ), f"account data {needle!r} leaked in S2S line {line!r}"

    # Peer B holds no account table and its RemoteClient carries no account.
    assert server_b.accounts.get("alpha-alice") is None
    assert getattr(server_b.remote_clients["alpha-alice"], "account", None) is None
    await c.close()


@pytest.mark.asyncio
async def test_stop_closes_pending_account_nick_connection(tmp_path):
    """A connection holding an account nick pre-registration is not in
    ``clients``; ``IRCd.stop()`` must still close it rather than hang."""
    ircd = await _boot(tmp_path)
    c = await _connect(ircd)
    await c.send("CAP LS 302")
    await c.send("NICK testserv-alice")
    await c.send("USER alice 0 * :alice")
    await c.recv_all(timeout=0.3)
    assert "testserv-alice" not in ircd.clients
    async with asyncio.timeout(5):
        await ircd.stop()
    _, closed = await _read_to_close(c, timeout=1.0)
    assert closed
