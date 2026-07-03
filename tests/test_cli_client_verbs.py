"""Subprocess-fixture tests for the agent-facing CLI verbs: send, read, watch, join.

Boots a real IRCd via the shared ``server`` fixture (a real asyncio TCP
listener on an OS-assigned loopback port — see ``tests/conftest.py``) and
drives each verb as a genuine subprocess (``python -m agentirc ...``)
talking to it over real TCP. Per the CLAUDE.md note on
``test_console_script_runs``, ``python -m agentirc`` is used instead of the
installed console scripts to dodge a known stale-``~/.local/bin``-script
PATH artifact in worktree venvs.

Two environment quirks this file works around:

- **Nick-prefix policy.** ``agentirc/client.py``'s ``_handle_nick`` rejects
  any nick that doesn't start with ``"<server-name>-"`` (see
  ``tests/test_messaging.py``'s ``testserv-ori``/``testserv-claude`` nicks
  for precedent). The shared ``server`` fixture names its IRCd
  ``"testserv"``, so every nick used against it here — CLI ``--nick``
  flags and ``make_client`` nicks alike — is prefixed ``testserv-``. This
  is a pre-existing, out-of-scope-to-change server policy (``client.py`` is
  off limits for this task); it also means the CLI's own documented
  default nick (``agent-<random 4 hex>``) only round-trips out of the box
  against a server actually named ``"agent"`` — see
  ``test_send_uses_default_nick_when_omitted`` below, which boots a
  bespoke IRCd named ``"agent"`` specifically to exercise that default.
- **Event-loop sharing.** The ``server`` fixture's IRCd shares the *same*
  asyncio event loop as the test coroutine (pytest-asyncio runs both on one
  loop). A plain blocking ``subprocess.run()`` would freeze that loop — and
  therefore the server — for the duration of the subprocess call,
  deadlocking any verb that waits on a server reply. Every subprocess here
  is driven with ``asyncio.create_subprocess_exec`` so the loop (and the
  in-process server) keeps running while we await the child process.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import sys

import pytest

from agentirc.protocol import Event, EventType
from tests.conftest import IRCTestClient

CLI = [sys.executable, "-m", "agentirc"]

_NEXT_CURSOR_RE = re.compile(r"^next-cursor: (.+)$")
_DEFAULT_NICK_RE = re.compile(r"^agent-[0-9a-f]{4}$")

# Ceiling for _run_cli's subprocess wait — all call sites in this file use it.
_CLI_TIMEOUT_SECONDS = 20.0


async def _run_cli(*args: str) -> tuple[int, str, str]:
    """Run an agentirc CLI verb as a real (non-blocking) subprocess.

    Returns ``(returncode, stdout, stderr)``. Uses
    ``asyncio.create_subprocess_exec`` rather than ``subprocess.run`` — see
    the module docstring for why a blocking call would deadlock tests that
    share the event loop with the in-process ``server`` fixture.
    """
    proc = await asyncio.create_subprocess_exec(
        *CLI,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(_CLI_TIMEOUT_SECONDS):
            stdout, stderr = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout.decode(), stderr.decode()


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll a sync ``predicate`` until it is truthy or ``timeout`` elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _wait_for_member(server, channel: str, nick: str, timeout: float = 5.0) -> bool:
    def _has_member() -> bool:
        ch = server.channels.get(channel)
        return bool(ch and any(m.nick == nick for m in ch.members))

    return await _wait_for(_has_member, timeout=timeout)


async def _boot_named_ircd(tmp_path, name: str):
    """Boot a standalone IRCd named *name* on a random loopback port.

    Only used by ``test_send_uses_default_nick_when_omitted``, which needs
    a server literally named ``"agent"`` to exercise the CLI's undecorated
    default nick against the nick-prefix policy (see module docstring).
    """
    from agentirc.config import ServerConfig, TelemetryConfig
    from agentirc.ircd import IRCd

    config = ServerConfig(
        name=name,
        host="127.0.0.1",
        port=0,
        webhook_port=0,
        telemetry=TelemetryConfig(audit_dir=str(tmp_path / "audit")),
    )
    ircd = IRCd(config)
    await ircd.start()
    ircd.config.port = ircd._server.sockets[0].getsockname()[1]
    return ircd


async def _seed_history(
    server, channel: str, texts: list[str], *, base_ts: float, nick: str = "seed-bot",
    with_msgid: bool = False,
) -> None:
    """Inject MESSAGE events directly into history, bypassing PRIVMSG/JOIN.

    Mirrors ``tests/test_history_since.py``'s ``_emit_messages`` helper.
    When ``with_msgid`` is set, each entry gets a synthetic ``msgid`` so
    HISTORY SINCE replay tags it (RECENT/SEARCH never tag regardless).
    """
    for i, text in enumerate(texts):
        data = {"text": text}
        if with_msgid:
            data["msgid"] = f"seed-msgid-{i}"
        await server.emit_event(
            Event(
                type=EventType.MESSAGE,
                channel=channel,
                nick=nick,
                data=data,
                timestamp=base_ts + i * 0.01,
            )
        )


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_delivers_message_to_observer(server, make_client):
    observer = await make_client(nick="testserv-observer1", user="observer1")
    await observer.send("JOIN #t11send")
    await observer.recv_all(timeout=0.5)

    rc, _, err = await _run_cli(
        "send", "#t11send", "hello from cli",
        "--nick", "testserv-sender1", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err

    lines = await observer.recv_all(timeout=2.0)
    assert any("PRIVMSG #t11send :hello from cli" in ln for ln in lines)
    assert any(ln.startswith(":testserv-sender1!") for ln in lines)


@pytest.mark.asyncio
async def test_send_dm_to_nick(server, make_client):
    observer = await make_client(nick="testserv-observer2", user="observer2")

    rc, _, err = await _run_cli(
        "send", "testserv-observer2", "dm from cli",
        "--nick", "testserv-sender2", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err

    lines = await observer.recv_all(timeout=2.0)
    assert any("PRIVMSG testserv-observer2 :dm from cli" in ln for ln in lines)


@pytest.mark.asyncio
async def test_send_uses_default_nick_when_omitted(tmp_path):
    """No ``--nick`` given: the CLI falls back to ``agent-<random 4 hex>``.

    Needs a server literally named ``"agent"`` — see module docstring on
    the nick-prefix policy.
    """
    ircd = await _boot_named_ircd(tmp_path, "agent")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", ircd.config.port)
        observer = IRCTestClient(reader, writer)
        await observer.send("NICK agent-observer")
        await observer.send("USER observer 0 * :observer")
        await observer.recv_all(timeout=0.5)

        rc, _, err = await _run_cli(
            "send", "agent-observer", "dm from cli",
            "--host", "127.0.0.1", "--port", str(ircd.config.port),
        )
        assert rc == 0, err

        lines = await observer.recv_all(timeout=2.0)
        dm_lines = [ln for ln in lines if "PRIVMSG agent-observer :dm from cli" in ln]
        assert dm_lines, lines
        sender_nick = dm_lines[0].split("!", 1)[0].lstrip(":")
        assert _DEFAULT_NICK_RE.match(sender_nick), sender_nick
    finally:
        await ircd.stop()


@pytest.mark.asyncio
async def test_send_connection_refused_reports_hint():
    """A closed port produces a non-zero exit and a stderr hint (no server fixture needed)."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()  # nothing listening on this port now

    rc, _, err = await _run_cli(
        "send", "#nowhere", "hi", "--host", "127.0.0.1", "--port", str(closed_port),
    )
    assert rc != 0
    assert "refused" in err.lower()


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_creates_channel_and_confirms(server, make_client):
    assert "#t11join" not in server.channels

    rc, out, err = await _run_cli(
        "join", "#t11join",
        "--nick", "testserv-joiner1", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err
    assert "Joined #t11join as testserv-joiner1" in out

    # A JOIN + immediate QUIT round trip is a valid way to prove the channel
    # got created: query HISTORY (which needs no membership) and see the
    # join/quit lifecycle rendered into it.
    observer = await make_client(nick="testserv-observer3", user="observer3")
    await observer.send("HISTORY RECENT #t11join 10")
    reply = await observer.recv_until("HISTORYEND")
    assert "joiner1" in reply


@pytest.mark.asyncio
async def test_join_existing_channel_confirms(server, make_client):
    holder = await make_client(nick="testserv-holder1", user="holder1")
    await holder.send("JOIN #t11join2")
    await holder.recv_all(timeout=0.5)

    rc, out, err = await _run_cli(
        "join", "#t11join2",
        "--nick", "testserv-joiner2", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err
    assert "Joined #t11join2 as testserv-joiner2" in out

    lines = await holder.recv_all(timeout=2.0)
    assert any("JOIN #t11join2" in ln and "joiner2" in ln for ln in lines)


@pytest.mark.asyncio
async def test_join_rejects_channel_without_hash(server):
    rc, _, err = await _run_cli(
        "join", "not-a-channel",
        "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc != 0
    assert "must start with" in err


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_last_n_returns_last_n(server):
    await _seed_history(
        server, "#t11readlast", [f"msg{i}" for i in range(5)], base_ts=1_700_000_000.0
    )

    rc, out, err = await _run_cli(
        "read", "#t11readlast", "--last", "3",
        "--nick", "testserv-reader1", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 3
    assert "msg2" in lines[0]
    assert "msg3" in lines[1]
    assert "msg4" in lines[2]
    for ln in lines:
        assert "seed-bot" in ln


@pytest.mark.asyncio
async def test_read_since_star_returns_everything_and_prints_cursor(server):
    await _seed_history(
        server, "#t11readsince", [f"first{i}" for i in range(3)], base_ts=1_700_000_100.0
    )

    rc, out, err = await _run_cli(
        "read", "#t11readsince", "--since", "*",
        "--nick", "testserv-reader2", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 3
    assert all(f"first{i}" in lines[i] for i in range(3))

    err_lines = [ln for ln in err.splitlines() if ln]
    m = _NEXT_CURSOR_RE.match(err_lines[-1])
    assert m, err
    cursor = m.group(1)
    assert cursor  # non-empty, opaque token


@pytest.mark.asyncio
async def test_read_since_cursor_resumes_without_overlap(server):
    channel = "#t11readresume"
    await _seed_history(server, channel, ["a0", "a1", "a2"], base_ts=1_700_000_200.0)

    rc, out, err = await _run_cli(
        "read", channel, "--since", "*",
        "--nick", "testserv-reader3", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err
    first_lines = [ln for ln in out.splitlines() if ln]
    assert len(first_lines) == 3
    cursor = _NEXT_CURSOR_RE.match(err.splitlines()[-1]).group(1)

    await _seed_history(server, channel, ["b0", "b1"], base_ts=1_700_000_300.0)

    rc2, out2, err2 = await _run_cli(
        "read", channel, "--since", cursor,
        "--nick", "testserv-reader4", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc2 == 0, err2
    second_lines = [ln for ln in out2.splitlines() if ln]
    assert len(second_lines) == 2
    assert "b0" in second_lines[0]
    assert "b1" in second_lines[1]
    # No overlap / no gaps: none of the first page's texts reappear.
    assert not any("a0" in ln or "a1" in ln or "a2" in ln for ln in second_lines)


@pytest.mark.asyncio
async def test_read_since_json_output_has_msgid(server):
    channel = "#t11readjson"
    await _seed_history(
        server, channel, ["tagged0", "tagged1"], base_ts=1_700_000_400.0, with_msgid=True
    )

    rc, out, err = await _run_cli(
        "read", channel, "--since", "*", "--json",
        "--nick", "testserv-reader5", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err
    out_lines = [ln for ln in out.splitlines() if ln]
    # 2 message lines + 1 JSON next_cursor trailer.
    assert len(out_lines) == 3

    msg_objs = [json.loads(ln) for ln in out_lines[:2]]
    for obj in msg_objs:
        assert set(obj) == {"ts", "nick", "text", "msgid"}
        assert obj["msgid"].startswith("seed-msgid-")
        assert obj["nick"] == "seed-bot"

    trailer = json.loads(out_lines[-1])
    assert set(trailer) == {"next_cursor"}
    assert trailer["next_cursor"]

    err_lines = [ln for ln in err.splitlines() if ln]
    assert _NEXT_CURSOR_RE.match(err_lines[-1])


@pytest.mark.asyncio
async def test_read_empty_channel_since_star(server):
    rc, out, err = await _run_cli(
        "read", "#t11readempty", "--since", "*",
        "--nick", "testserv-reader6", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc == 0, err
    assert out.strip() == ""
    err_lines = [ln for ln in err.splitlines() if ln]
    assert err_lines[-1] == "next-cursor: *"


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_streams_message_then_sigint_exits_cleanly(server, make_client):
    channel = "#t11watch"
    proc = await asyncio.create_subprocess_exec(
        *CLI,
        "watch", channel,
        "--nick", "testserv-watcher1", "--host", "127.0.0.1", "--port", str(server.config.port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        joined = await _wait_for_member(server, channel, "testserv-watcher1", timeout=10.0)
        assert joined, "watch subprocess never joined the channel"

        sender = await make_client(nick="testserv-sender-w", user="sender-w")
        await sender.send(f"JOIN {channel}")
        await sender.recv_all(timeout=0.5)
        await sender.send(f"PRIVMSG {channel} :hello watch")

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
        text = line.decode().strip()
        assert "sender-w" in text
        assert "hello watch" in text

        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_watch_json_output_has_msgid(server, make_client):
    channel = "#t11watchjson"
    proc = await asyncio.create_subprocess_exec(
        *CLI,
        "watch", channel, "--json",
        "--nick", "testserv-watcher2", "--host", "127.0.0.1", "--port", str(server.config.port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        joined = await _wait_for_member(server, channel, "testserv-watcher2", timeout=10.0)
        assert joined, "watch subprocess never joined the channel"

        sender = await make_client(nick="testserv-sender-wj", user="sender-wj")
        await sender.send(f"JOIN {channel}")
        await sender.recv_all(timeout=0.5)
        await sender.send(f"PRIVMSG {channel} :json line")

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
        obj = json.loads(line.decode())
        assert set(obj) == {"ts", "nick", "text", "msgid"}
        assert obj["nick"] == "testserv-sender-wj"
        assert obj["text"] == "json line"
        assert obj["msgid"]

        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


# ---------------------------------------------------------------------------
# DM ergonomics (integrator fixes for the two gaps t15's doc verification found)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_dm_target_streams_direct_messages(server, make_client):
    """``watch <nick>`` (bare-nick target) surfaces DMs from that peer.

    Regression test: IncomingMessage.channel is None for DMs, so the old
    channel-equality filter silently dropped every direct message.
    """
    proc = await asyncio.create_subprocess_exec(
        *CLI,
        "watch", "testserv-dmsender", "--json",
        "--nick", "testserv-dmwatcher", "--host", "127.0.0.1", "--port", str(server.config.port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        registered = await _wait_for(
            lambda: "testserv-dmwatcher" in server.clients, timeout=10.0
        )
        assert registered, "watch subprocess never registered"

        sender = await make_client(nick="testserv-dmsender", user="dmsender")
        await sender.send("PRIVMSG testserv-dmwatcher :dm for watch")

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
        obj = json.loads(line.decode())
        assert obj["nick"] == "testserv-dmsender"
        assert obj["text"] == "dm for watch"
        assert obj["msgid"]

        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_watch_dm_target_ignores_channel_and_third_party_traffic(server, make_client):
    """``watch <nick>`` shows only that peer's DMs — not channel chatter."""
    proc = await asyncio.create_subprocess_exec(
        *CLI,
        "watch", "testserv-dmpeer", "--json",
        "--nick", "testserv-dmwatcher2", "--host", "127.0.0.1", "--port", str(server.config.port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        registered = await _wait_for(
            lambda: "testserv-dmwatcher2" in server.clients, timeout=10.0
        )
        assert registered, "watch subprocess never registered"

        other = await make_client(nick="testserv-dmother", user="dmother")
        await other.send("PRIVMSG testserv-dmwatcher2 :dm from the wrong peer")
        peer = await make_client(nick="testserv-dmpeer", user="dmpeer")
        await peer.send("PRIVMSG testserv-dmwatcher2 :dm from the right peer")

        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
        obj = json.loads(line.decode())
        assert obj["nick"] == "testserv-dmpeer"
        assert obj["text"] == "dm from the right peer"

        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_send_dm_offline_recipient_exits_nonzero_with_hint(server):
    """``send <nick>`` to an absent nick must not lie with exit 0.

    Regression test: the server replies ERR_NOSUCHNICK (401) and drops the
    DM (offline DMs are deliberately unstored); the CLI has to surface that.
    """
    rc, _, err = await _run_cli(
        "send", "testserv-ghost", "anyone home?",
        "--nick", "testserv-sender-x", "--host", "127.0.0.1", "--port", str(server.config.port),
    )
    assert rc != 0
    assert "testserv-ghost" in err
    assert "no such nick" in err.lower()
