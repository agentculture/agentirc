"""SIGHUP reloads the accounts file in a real ``agentirc serve`` (culture#506, t8).

The rewrite keeps the file's size, mtime and inode identical, so the lazy
mtime check cannot see it — only the SIGHUP handler can make the server
pick it up.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from agentirc.accounts import ACCOUNTS_FILENAME
from tests.conftest import IRCTestClient
from tests.test_accounts_sasl import (
    SECRET_ALICE,
    _account,
    _read_to_close,
    write_accounts,
)

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGHUP"), reason="SIGHUP is POSIX-only"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _try_nick(port: int, nick: str) -> tuple[list[str], bool]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    c = IRCTestClient(reader, writer)
    await c.send(f"NICK {nick}")
    await c.send("USER u 0 * :u")
    lines, closed = await _read_to_close(c, timeout=1.0)
    await c.close()
    return lines, closed


def _welcomed(lines):
    return any(" 001 " in line for line in lines)


@pytest.mark.asyncio
async def test_sighup_reloads_accounts(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    write_accounts(str(data_dir), [_account("testserv-alice", SECRET_ALICE)])
    port = _free_port()
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentirc",
            "serve",
            "--name",
            "testserv",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--webhook-port",
            "0",
            "--data-dir",
            str(data_dir),
            "--config",
            str(tmp_path / "absent.yaml"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    out = proc.stdout.read().decode() if proc.poll() is not None else ""
                    pytest.fail(f"agentirc serve did not come up: {out}")
                await asyncio.sleep(0.1)

        # alice is an account: refused without SASL.
        lines, closed = await _try_nick(port, "testserv-alice")
        assert not _welcomed(lines)
        assert closed

        # Rename the account in place, keeping size/mtime/inode identical.
        path = data_dir / ACCOUNTS_FILENAME
        st = os.stat(path)
        original = path.read_text(encoding="utf-8")
        swapped = original.replace("testserv-alice", "testserv-alicx")
        assert len(swapped) == len(original)
        with open(path, "r+", encoding="utf-8") as f:
            f.write(swapped)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert os.stat(path).st_ino == st.st_ino

        # Invisible to the lazy check: still refused.
        lines, closed = await _try_nick(port, "testserv-alice")
        assert not _welcomed(lines)

        proc.send_signal(signal.SIGHUP)
        await asyncio.sleep(0.5)
        assert proc.poll() is None, "SIGHUP must not stop the server"

        lines, _ = await _try_nick(port, "testserv-alice")
        assert _welcomed(lines), lines
        lines, closed = await _try_nick(port, "testserv-alicx")
        assert not _welcomed(lines)
        assert closed
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
