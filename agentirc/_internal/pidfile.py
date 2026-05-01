"""PID file management for agentirc daemon instances.

Vendored from culture@df50942 (`culture/pidfile.py`) and adapted for
agentirc per the cite-don't-copy convention. The on-disk layout
(`~/.culture/pids/<name>.{pid,port}`) is preserved unchanged so culture
and agentirc daemons can coexist on a host without separate state
directories.

Adaptation versus the upstream copy: `is_managed_process()` accepts
both ``culture`` and ``agentirc`` argv tokens. ``is_culture_process``
remains as a thin alias so call sites that haven't migrated keep
working.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PID_DIR = os.path.expanduser("~/.culture/pids")

_MANAGED_PROCESS_TOKENS = ("culture", "agentirc", "agentirc-cli")


def _safe_name(name: str) -> str:
    """Sanitize a daemon name to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", Path(name).name)


def write_pid(name: str, pid: int) -> Path:
    """Write a PID file for the named daemon. Creates the directory if needed."""
    pid_dir = Path(PID_DIR)
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_path = pid_dir / f"{_safe_name(name)}.pid"
    pid_path.write_text(str(pid))
    return pid_path


def read_pid(name: str) -> int | None:
    """Read the PID for the named daemon. Returns None if file is missing."""
    pid_path = Path(PID_DIR) / f"{_safe_name(name)}.pid"
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pid(name: str) -> None:
    """Remove the PID file for the named daemon if it exists."""
    pid_path = Path(PID_DIR) / f"{_safe_name(name)}.pid"
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def write_port(name: str, port: int) -> Path:
    """Write a port file for the named daemon. Creates the directory if needed."""
    pid_dir = Path(PID_DIR)
    pid_dir.mkdir(parents=True, exist_ok=True)
    port_path = pid_dir / f"{_safe_name(name)}.port"
    port_path.write_text(str(port))
    return port_path


def read_port(name: str) -> int | None:
    """Read the port for the named daemon. Returns None if file is missing."""
    port_path = Path(PID_DIR) / f"{_safe_name(name)}.port"
    if not port_path.exists():
        return None
    try:
        return int(port_path.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_port(name: str) -> None:
    """Remove the port file for the named daemon if it exists."""
    port_path = Path(PID_DIR) / f"{_safe_name(name)}.port"
    try:
        port_path.unlink()
    except FileNotFoundError:
        pass


def is_managed_process(pid: int) -> bool:
    """Check whether the given PID belongs to a culture or agentirc process.

    Reads /proc/<pid>/cmdline on Linux and checks NUL-separated argv
    tokens for an exact match against ``culture``, ``agentirc``, or
    ``agentirc-cli`` (e.g. argv[0] basename or a ``-m culture``
    argument). On platforms without /proc (macOS, Windows) or when
    /proc parsing fails, falls back to ``ps -p <pid> -o command=``
    when available; if neither identification path succeeds, returns
    False (fail closed) so a stale PID file can never SIGTERM an
    unrelated reused-PID process. Upstream culture treated the
    macOS/Windows case as "assume valid"; PR #4 review flagged that
    as exploitable when PIDs are reused.
    """
    if os.path.isdir("/proc"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            tokens = [t for t in raw.decode(errors="replace").split("\x00") if t]
            return any(
                os.path.basename(t) in _MANAGED_PROCESS_TOKENS
                or t in _MANAGED_PROCESS_TOKENS
                for t in tokens
            )
        except OSError:
            return False
    return _is_managed_via_ps(pid)


def _is_managed_via_ps(pid: int) -> bool:
    """Fallback identity check using ``ps`` for non-/proc platforms.

    Uses ``ps -p <pid> -o command=`` (POSIX) to recover the command
    line and tests for a managed-process token. Any failure (no
    ``ps``, non-zero exit, parse error) returns False so we fail
    closed rather than signalling an unknown process.
    """
    import shutil
    import subprocess

    ps = shutil.which("ps")
    if ps is None:
        return False
    try:
        # argv is a fixed token list with the pid coerced to int — no shell
        # interpretation, no path that traverses untrusted input.
        result = subprocess.run(
            [ps, "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    cmdline = result.stdout.strip()
    if not cmdline:
        return False
    tokens = cmdline.split()
    return any(
        os.path.basename(t) in _MANAGED_PROCESS_TOKENS or t in _MANAGED_PROCESS_TOKENS
        for t in tokens
    )


def is_culture_process(pid: int) -> bool:
    """Backwards-compatible alias for :func:`is_managed_process`.

    The upstream culture API used this name; preserved so existing call
    sites in vendored code keep working without churn.
    """
    return is_managed_process(pid)


def is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is alive.

    Uses signal 0 (existence test, no signal delivered) per POSIX. The
    ``# NOSONAR S4828`` marker is intentional: signal 0 cannot deliver
    a signal to the target — it only reports whether the kernel can
    locate the PID. Sonar's "sending signals is safe here" rule is a
    false positive for this idiom.
    """
    try:
        os.kill(pid, 0)  # NOSONAR S4828 — sig=0 is existence test, no delivery
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def list_servers() -> list[dict]:
    """List running culture/agentirc servers.

    Returns list of dicts with keys: name, pid, port.
    """
    pid_dir = Path(PID_DIR)
    if not pid_dir.exists():
        return []
    servers = []
    prefix = "server-"
    for pid_path in sorted(pid_dir.glob(f"{prefix}*.pid")):
        pid_name = pid_path.stem
        name = pid_name[len(prefix) :]
        pid = read_pid(pid_name)
        if pid is None or not is_process_alive(pid) or not is_managed_process(pid):
            continue
        port = read_port(pid_name) or 6667
        servers.append({"name": name, "pid": pid, "port": port})
    return servers


def read_default_server() -> str | None:
    """Read the default server name. Returns None if unset."""
    default_path = Path(PID_DIR) / "default_server"
    if not default_path.exists():
        return None
    try:
        return default_path.read_text().strip() or None
    except OSError:
        return None


def write_default_server(name: str) -> None:
    """Set the default server name."""
    pid_dir = Path(PID_DIR)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "default_server").write_text(name)


def rename_pid(old_name: str, new_name: str) -> bool:
    """Rename a PID file and its associated port file.

    Best-effort: returns True if at least one file was renamed.
    Failures are silently ignored to avoid raising during cleanup paths.
    """
    pid_dir = Path(PID_DIR)
    renamed = False
    for suffix in (".pid", ".port"):
        old_path = pid_dir / f"{_safe_name(old_name)}{suffix}"
        new_path = pid_dir / f"{_safe_name(new_name)}{suffix}"
        if old_path.exists():
            try:
                old_path.rename(new_path)
                renamed = True
            except OSError:
                pass
    return renamed
