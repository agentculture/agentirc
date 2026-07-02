"""End-to-end smoke test: an agent driving agentirc purely through its
public, agent-facing CLI surface — over real TCP, against a real daemon
process, exactly as a shell agent would.

Unlike ``tests/test_cli_client_verbs.py`` (which boots the IRCd in-process
via the ``server`` fixture and only drives the *client* verbs as
subprocesses), this test never touches ``agentirc.ircd.IRCd`` directly. The
daemon itself is a genuine ``python -m agentirc start`` subprocess, managed
purely through the CLI: ``start`` -> ``join`` -> ``send`` -> ``read`` (both
``--last`` and ``--since '*'``) -> ``watch`` -> ``status`` -> ``stop``. This
is the walkthrough task t14's design directive calls for; a later docs task
mirrors these same steps in prose.

Isolation: every subprocess gets ``HOME`` overridden to a throwaway
``tmp_path`` directory. All of agentirc's on-disk defaults (config, PID/port
files, logs, data dir) are resolved from ``~`` at *subprocess start*, so
this fully sandboxes the daemon away from any real ``~/.culture`` on the
host — no dev-machine state is read or mutated, and nothing is left behind
(``tmp_path`` is pytest-managed).

Marked ``walkthrough`` (registered in ``pyproject.toml``) so CI's
``agent-guards`` job can also invoke it by name via
``pytest -m walkthrough``; it runs in the normal full-suite collection too.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys

import pytest

pytestmark = pytest.mark.walkthrough

CLI = [sys.executable, "-m", "agentirc"]

# Overall ceiling for the live-watch probe loop. Generous for slow CI
# runners; the happy path resolves in well under a second.
_WATCH_PROBE_DEADLINE_SECONDS = 10.0
_WATCH_PROBE_ATTEMPT_TIMEOUT_SECONDS = 1.5


def _free_port() -> int:
    """Ask the OS for a currently-unused loopback port.

    Small TOCTOU window between closing this socket and the daemon binding
    the same port — acceptable for a test (same pattern used elsewhere in
    this suite for picking ephemeral ports).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run_cli(*args: str, env: dict, timeout: float = 35.0) -> tuple[int, str, str]:
    """Run an agentirc CLI verb as a real (non-blocking) subprocess.

    Returns ``(returncode, stdout, stderr)``.
    """
    proc = await asyncio.create_subprocess_exec(
        *CLI,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await proc.communicate()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout.decode(), stderr.decode()


@pytest.mark.asyncio
async def test_agent_walkthrough(tmp_path):
    """Drive the full agent-facing CLI lifecycle against a real daemon.

    Steps (mirroring how a shell agent would use agentirc):

    1. ``start`` a daemon on a free port.
    2. ``join`` a channel.
    3. ``send`` a message to it.
    4. ``read --last`` sees the message; ``read --since '*'`` sees it too
       and — since the message came from a real PRIVMSG, which always
       stamps a msgid — the JSON output carries a ``msgid``.
    5. ``watch`` streams a live message sent after it started.
    6. ``status`` reports the daemon running (no ``--json`` flag exists in
       this tree yet — a parallel task is adding it elsewhere).
    7. ``stop`` shuts the daemon down cleanly.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}

    name = "t14walk"
    port = _free_port()
    channel = "#t14-walkthrough"
    host = "127.0.0.1"

    def _client_flags() -> list[str]:
        return ["--host", host, "--port", str(port)]

    # 1. start a daemon on a free port
    rc, out, err = await _run_cli(
        "start", "--name", name, "--host", host, "--port", str(port), env=env,
    )
    assert rc == 0, f"start failed: rc={rc} out={out!r} err={err!r}"
    assert f"Server '{name}' started" in out, out

    try:
        # 2. join a channel
        rc, out, err = await _run_cli(
            "join", channel, "--nick", f"{name}-joiner", *_client_flags(), env=env,
        )
        assert rc == 0, f"join failed: {err}"
        assert f"Joined {channel} as {name}-joiner" in out, out

        # 3. send a message
        message_text = "hello from the agent walkthrough"
        rc, out, err = await _run_cli(
            "send", channel, message_text,
            "--nick", f"{name}-sender", *_client_flags(), env=env,
        )
        assert rc == 0, f"send failed: {err}"

        # 4a. `read --last` sees it
        rc, out, err = await _run_cli(
            "read", channel, "--last", "5", "--json",
            "--nick", f"{name}-reader1", *_client_flags(), env=env,
        )
        assert rc == 0, f"read --last failed: {err}"
        last_objs = [json.loads(ln) for ln in out.splitlines() if ln]
        assert any(o["text"] == message_text for o in last_objs), out

        # 4b. `read --since '*'` also sees it, tagged with a msgid — proof
        # the CLI's real HISTORY SINCE/msgid wire contract round-trips, not
        # just RECENT's untagged replay.
        rc, out, err = await _run_cli(
            "read", channel, "--since", "*", "--json",
            "--nick", f"{name}-reader2", *_client_flags(), env=env,
        )
        assert rc == 0, f"read --since failed: {err}"
        since_lines = [json.loads(ln) for ln in out.splitlines() if ln]
        since_msgs = [o for o in since_lines if "text" in o]
        matches = [o for o in since_msgs if o["text"] == message_text]
        assert matches, out
        assert matches[0].get("msgid"), f"expected a msgid on SINCE replay, got {matches[0]!r}"

        # 5. `watch` streams a live message sent after it starts.
        watch_proc = await asyncio.create_subprocess_exec(
            *CLI, "watch", channel, "--json",
            "--nick", f"{name}-watcher", *_client_flags(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            seen = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + _WATCH_PROBE_DEADLINE_SECONDS
            attempt = 0
            # `watch` prints nothing on join, so there's no signal to poll
            # for readiness directly. Instead: repeatedly send a
            # uniquely-tagged probe message and read watch's stdout with a
            # short per-attempt timeout, until one is observed. Any probes
            # sent before the watcher finishes joining are simply not
            # delivered to it (a channel send only fans out to members
            # present at send time) and are safely ignored.
            while loop.time() < deadline and seen is None:
                attempt += 1
                probe_text = f"live probe {attempt}"
                rc, _, err = await _run_cli(
                    "send", channel, probe_text,
                    "--nick", f"{name}-prober", *_client_flags(), env=env,
                )
                assert rc == 0, f"send (watch probe) failed: {err}"
                try:
                    line = await asyncio.wait_for(
                        watch_proc.stdout.readline(),
                        timeout=_WATCH_PROBE_ATTEMPT_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
                if not line:
                    continue
                obj = json.loads(line.decode())
                if obj.get("text") == probe_text:
                    seen = obj

            assert seen is not None, "watch never observed a live message"
            assert seen.get("msgid"), f"expected a msgid on the live watch line, got {seen!r}"
        finally:
            if watch_proc.returncode is None:
                watch_proc.send_signal(signal.SIGINT)
                await asyncio.wait_for(watch_proc.wait(), timeout=5.0)

        # 6. `status` reports the daemon running. No `--json` flag exists
        # on this branch yet (see module docstring), so assert on the
        # plain-text contract only.
        rc, out, err = await _run_cli("status", "--name", name, env=env)
        assert rc == 0, f"status failed: {err}"
        assert f"Server '{name}': running" in out, out
        assert str(port) in out, out
    finally:
        # 7. stop — always attempted, even if an earlier assertion failed,
        # so a broken step doesn't leak a daemon process.
        rc, out, err = await _run_cli("stop", "--name", name, env=env)
        assert rc == 0, f"stop failed: {err}"
        assert f"Server '{name}' stopped" in out or "killed" in out, out
