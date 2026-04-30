"""agentirc CLI entry point.

Real lifecycle implementation. PR-B1 left this as a stub that printed
"not yet implemented"; PR-B2 lands the real verb dispatch extracted
out of culture@df50942 (`culture/cli/server.py`).

The major version started at 9 to leapfrog culture's earlier
squat-publish of `agentirc-cli==8.7.x.devN` on TestPyPI so future dev
releases sort as the actual "latest".

Public surface (semver-tracked, see ``docs/api-stability.md``):

- ``main()`` — console-script entry point.
- ``dispatch(argv) -> int`` — the function culture's ``culture server``
  shim calls. Returns an integer exit code on successful command
  dispatch. Per Python convention, argparse raises ``SystemExit`` for
  ``--help``, ``--version``, and parse errors; we let that propagate
  (do not silently swallow). In-process callers that want a return
  value rather than process termination must catch ``SystemExit``
  themselves: ``try: rc = dispatch(argv); except SystemExit as e:
  rc = e.code or 0``.

Verbs:

- ``serve`` — agentirc-only: foreground, no daemonize, no PID file.
  For systemd ``Type=simple`` units, containers, and dev.
- ``start`` — daemonize and write PID/port files. Accepts
  ``--name``, ``--host``, ``--port``, ``--link``, ``--webhook-port``,
  ``--foreground``, ``--data-dir``, ``--config``.
- ``stop`` — read PID, SIGTERM, wait, force-kill if needed.
- ``restart`` — stop then start with the same args.
- ``status`` — read PID/port and report alive/dead.
- ``link`` — parse and validate a ``name:host:port:password[:trust]``
  link spec; runtime mesh-mutation lands later.
- ``logs`` — print or tail ``~/.culture/logs/server-<name>.log``.
- ``version`` — print ``agentirc <version>``.

Culture-specific verbs from ``culture/cli/server.py``
(``default``/``rename``/``archive``/``unarchive``) are deliberately
not surfaced here; they manage culture's agent manifest, which is out
of agentirc's scope.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Sequence

from agentirc import __version__
from agentirc._internal.cli_shared.constants import (
    DEFAULT_CONFIG,
    LOG_DIR,
    _CONFIG_HELP,
    _SERVER_NAME_HELP,
)
from agentirc._internal.cli_shared.mesh import parse_link
from agentirc._internal.pidfile import (
    is_culture_process,
    is_process_alive,
    read_default_server,
    read_pid,
    read_port,
    remove_pid,
    write_default_server,
    write_pid,
    write_port,
)

logger = logging.getLogger("agentirc")

_DEFAULT_SERVER = "agentirc"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _add_start_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the lifecycle flag set used by ``serve``/``start``/``restart``."""
    parser.add_argument("--name", default=None, help=_SERVER_NAME_HELP)
    parser.add_argument("--host", default="0.0.0.0", help="Listen address")
    parser.add_argument("--port", type=int, default=6667, help="Listen port")
    parser.add_argument(
        "--link",
        type=parse_link,
        action="append",
        default=[],
        help="Link to peer: name:host:port:password[:trust]",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=7680,
        help=(
            "HTTP port for bot webhooks (default: 7680; "
            "inert in agentirc until a bot harness is wired in)"
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=os.path.expanduser("~/.culture/data"),
        help="Data directory for persistent storage (default: ~/.culture/data)",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=_CONFIG_HELP)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentirc",
        description="Agent-friendly IRCd: server core for AI agent meshes.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agentirc {__version__}",
    )
    sub = parser.add_subparsers(dest="verb", metavar="<verb>")

    p_serve = sub.add_parser("serve", help="Run the IRCd in the foreground")
    _add_start_flags(p_serve)

    p_start = sub.add_parser("start", help="Start the IRCd as a managed daemon")
    _add_start_flags(p_start)
    p_start.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground (for service managers)",
    )

    p_stop = sub.add_parser("stop", help="Stop the managed IRCd daemon")
    p_stop.add_argument("--name", default=None, help=_SERVER_NAME_HELP)

    p_restart = sub.add_parser("restart", help="Restart the managed IRCd daemon")
    _add_start_flags(p_restart)
    p_restart.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground after restart",
    )

    p_status = sub.add_parser("status", help="Report IRCd daemon state")
    p_status.add_argument("--name", default=None, help=_SERVER_NAME_HELP)

    p_link = sub.add_parser("link", help="Validate a server-to-server mesh link spec")
    p_link.add_argument(
        "peer",
        help="Link spec: name:host:port:password[:trust]",
    )

    p_logs = sub.add_parser("logs", help="Print or tail IRCd daemon logs")
    p_logs.add_argument("--name", default=None, help=_SERVER_NAME_HELP)
    p_logs.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Tail the log file (like tail -f)",
    )

    sub.add_parser("version", help="Print agentirc version")

    return parser


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


def _resolve_server_name(args: argparse.Namespace) -> str:
    """Resolve the server name from ``--name``, the default-server file, or fallback."""
    if getattr(args, "name", None) is not None:
        return args.name
    return read_default_server() or _DEFAULT_SERVER


def _maybe_set_default_server(name: str) -> None:
    """Set this server as default if none is configured."""
    if read_default_server() is None:
        write_default_server(name)


def _wait_for_port(
    host: str,
    port: int,
    pid: int,
    timeout: float = 30,
) -> tuple[bool, str]:
    """Poll *host*:*port* until a TCP connect succeeds or *timeout* expires."""
    check_host = "127.0.0.1" if host == "0.0.0.0" else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return False, "failed to start"
        try:
            s = socket.create_connection((check_host, port), timeout=0.5)
            s.close()
        except OSError:
            time.sleep(0.2)
            continue
        time.sleep(0.1)
        if not is_process_alive(pid):
            return False, "failed to start"
        return True, ""
    return False, "started but not yet accepting connections"


def _check_already_running(pid_name: str, name: str) -> None:
    """Exit if the server is already running."""
    existing = read_pid(pid_name)
    if existing and is_process_alive(existing):
        print(f"Server '{name}' is already running (PID {existing})")
        sys.exit(1)


def _verify_daemon_started(args: argparse.Namespace, pid: int) -> None:
    """Wait for the daemon child to be ready, exit on failure."""
    log_hint = f"{LOG_DIR}/server-{args.name}.log"
    if args.port == 0:
        time.sleep(0.5)
        if not is_process_alive(pid):
            print(f"Server '{args.name}' failed to start", file=sys.stderr)
            print(f"  Check logs: {log_hint}", file=sys.stderr)
            sys.exit(1)
    else:
        ok, err = _wait_for_port(args.host, args.port, pid, timeout=30)
        if not ok:
            print(f"Server '{args.name}' {err}", file=sys.stderr)
            print(f"  Check logs: {log_hint}", file=sys.stderr)
            sys.exit(1)
    print(f"Server '{args.name}' started (PID {pid})")
    print(f"  Listening on {args.host}:{args.port}")
    print(f"  Logs: {log_hint}")
    if args.port:
        write_port(f"server-{args.name}", args.port)
    _maybe_set_default_server(args.name)


def _wait_for_graceful_stop(pid: int, timeout_ticks: int = 50) -> bool:
    """Wait for a process to exit gracefully. Return True if it stopped."""
    for _ in range(timeout_ticks):
        if not is_process_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _force_kill(pid: int, name: str) -> None:
    """Force-kill a process that didn't stop gracefully."""
    if sys.platform == "win32":
        print(f"Server '{name}' did not stop gracefully, terminating")
        sig = signal.SIGTERM
    else:
        print(f"Server '{name}' did not stop gracefully, sending SIGKILL")
        sig = signal.SIGKILL
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# IRCd run loop (called inside the daemon child or foreground process)
# ---------------------------------------------------------------------------


async def _run_server(
    name: str,
    host: str,
    port: int,
    links: list | None = None,
    webhook_port: int = 7680,
    data_dir: str = "",
) -> None:
    """Run the IRC server (called in the daemon child process)."""
    from agentirc.config import ServerConfig
    from agentirc.ircd import IRCd

    config = ServerConfig(
        name=name,
        host=host,
        port=port,
        webhook_port=webhook_port,
        links=links or [],
        data_dir=data_dir,
    )
    ircd = IRCd(config)
    await ircd.start()
    logger.info("Server '%s' listening on %s:%d", name, host, port)

    for lc in config.links:
        try:
            await ircd.connect_to_peer(lc.host, lc.port, lc.password, lc.trust)
            logger.info("Linking to %s at %s:%d", lc.name, lc.host, lc.port)
        except Exception as e:  # noqa: BLE001 — link failures must not abort startup
            logger.error("Failed to link to %s: %s — will retry", lc.name, e)
            ircd.maybe_retry_link(lc.name)

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: stop_event.set())

    await stop_event.wait()
    logger.info("Server '%s' shutting down", name)
    await ircd.stop()


def _run_foreground(args: argparse.Namespace, pid_name: str, links: list) -> None:
    """Run the server in the foreground (blocking).

    A PID file is written when *pid_name* is non-empty (``start
    --foreground``). The agentirc-only ``serve`` verb passes ``""`` to
    skip PID writes — useful for systemd ``Type=simple`` and containers
    that own process supervision.
    """
    if pid_name:
        write_pid(pid_name, os.getpid())
    os.makedirs(LOG_DIR, exist_ok=True)
    print(f"Server '{args.name}' starting in foreground (PID {os.getpid()})")
    print(f"  Listening on {args.host}:{args.port}")
    print(f"  Webhook port: {args.webhook_port}")
    if pid_name:
        _maybe_set_default_server(args.name)
    try:
        asyncio.run(
            _run_server(args.name, args.host, args.port, links, args.webhook_port, args.data_dir)
        )
    finally:
        if pid_name:
            remove_pid(pid_name)


def _daemonize_server(args: argparse.Namespace, pid_name: str, links: list) -> None:
    """Fork and set up the daemon child process for the server."""
    if sys.platform == "win32":
        print("Daemon mode not supported on Windows. Use --foreground.", file=sys.stderr)
        sys.exit(1)

    pid = os.fork()
    if pid > 0:
        _verify_daemon_started(args, pid)
        return

    os.setsid()

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"server-{args.name}.log")
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    # Use an explicit FileHandler. logging.StreamHandler on sys.stderr
    # inherits stderr's buffering from interpreter startup; after dup2'ing
    # fd 2 to a log file, those writes can buffer indefinitely and make
    # the daemon's runtime log appear frozen.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path)],
        force=True,
    )

    write_pid(pid_name, os.getpid())
    if args.port:
        write_port(pid_name, args.port)

    try:
        asyncio.run(
            _run_server(args.name, args.host, args.port, links, args.webhook_port, args.data_dir)
        )
    finally:
        remove_pid(pid_name)
        os._exit(0)


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------


def _server_serve(args: argparse.Namespace) -> int:
    """``agentirc serve`` — run the IRCd in the foreground without writing a PID file."""
    args.name = _resolve_server_name(args)
    links = list(getattr(args, "link", []) or [])
    _run_foreground(args, pid_name="", links=links)
    return 0


def _server_start(args: argparse.Namespace) -> int:
    """``agentirc start`` — daemonize (or run foreground if ``--foreground``)."""
    args.name = _resolve_server_name(args)
    pid_name = f"server-{args.name}"
    _check_already_running(pid_name, args.name)

    links = list(getattr(args, "link", []) or [])

    if getattr(args, "foreground", False):
        _run_foreground(args, pid_name, links)
        return 0

    _daemonize_server(args, pid_name, links)
    return 0


def _server_stop(args: argparse.Namespace) -> int:
    args.name = _resolve_server_name(args)
    pid_name = f"server-{args.name}"
    pid = read_pid(pid_name)

    if pid is None:
        print(f"No PID file for server '{args.name}'")
        return 1

    if not is_process_alive(pid):
        print(f"Server '{args.name}' is not running (stale PID {pid})")
        remove_pid(pid_name)
        return 0

    if not is_culture_process(pid):
        print(f"PID {pid} is not an agentirc/culture process — removing stale PID file")
        remove_pid(pid_name)
        return 0

    print(f"Stopping server '{args.name}' (PID {pid})...")
    os.kill(pid, signal.SIGTERM)

    if _wait_for_graceful_stop(pid):
        print(f"Server '{args.name}' stopped")
        remove_pid(pid_name)
        return 0

    _force_kill(pid, args.name)
    remove_pid(pid_name)
    print(f"Server '{args.name}' killed")
    return 0


def _server_restart(args: argparse.Namespace) -> int:
    """``agentirc restart`` — stop (best-effort) then start with the same args."""
    args.name = _resolve_server_name(args)
    pid_name = f"server-{args.name}"
    pid = read_pid(pid_name)
    if pid and is_process_alive(pid):
        rc = _server_stop(args)
        if rc != 0:
            return rc
    return _server_start(args)


def _server_status(args: argparse.Namespace) -> int:
    args.name = _resolve_server_name(args)
    pid_name = f"server-{args.name}"
    pid = read_pid(pid_name)
    port = read_port(pid_name)

    if pid is None:
        print(f"Server '{args.name}': not running (no PID file)")
        return 0

    if is_process_alive(pid):
        if port:
            print(f"Server '{args.name}': running (PID {pid}, port {port})")
        else:
            print(f"Server '{args.name}': running (PID {pid})")
        return 0

    print(f"Server '{args.name}': not running (stale PID {pid})")
    remove_pid(pid_name)
    return 0


def _server_link(args: argparse.Namespace) -> int:
    """``agentirc link`` — parse and print a peer link spec.

    Runtime mesh-mutation (adding a peer to a live IRCd, persisting it
    to mesh.yaml) needs credential storage and a running daemon to
    inject the link into; both are out of scope for this PR.
    """
    try:
        link = parse_link(args.peer)
    except argparse.ArgumentTypeError as exc:
        print(f"agentirc link: {exc}", file=sys.stderr)
        return 1
    print(
        f"Parsed link: name={link.name} host={link.host} port={link.port} "
        f"trust={link.trust}"
    )
    print(
        "Note: persisting this link to a running daemon's mesh requires "
        "credential storage. Pass --link to 'agentirc start' to bring up "
        "a daemon with this peer.",
        file=sys.stderr,
    )
    return 0


def _server_logs(args: argparse.Namespace) -> int:
    """``agentirc logs`` — print or tail the server's daemon log."""
    name = _resolve_server_name(args)
    log_path = Path(LOG_DIR) / f"server-{name}.log"

    if not log_path.exists():
        print(f"agentirc logs: no log file for server '{name}' at {log_path}", file=sys.stderr)
        return 1

    if not args.follow:
        print(log_path.read_text(errors="replace"), end="")
        return 0

    with log_path.open("r", errors="replace") as fh:
        sys.stdout.write(fh.read())
        sys.stdout.flush()
        try:
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                sys.stdout.write(line)
                sys.stdout.flush()
        except KeyboardInterrupt:
            return 0


# ---------------------------------------------------------------------------
# Public dispatch / entry point
# ---------------------------------------------------------------------------


_HANDLERS = {
    "serve": _server_serve,
    "start": _server_start,
    "stop": _server_stop,
    "restart": _server_restart,
    "status": _server_status,
    "link": _server_link,
    "logs": _server_logs,
}


def dispatch(argv: Sequence[str]) -> int:
    """Parse *argv* and run the requested verb. Return an exit code."""
    parser = _build_parser()
    args = parser.parse_args(list(argv))

    if args.verb is None:
        parser.print_help(sys.stderr)
        return 1

    if args.verb == "version":
        print(f"agentirc {__version__}")
        return 0

    handler = _HANDLERS.get(args.verb)
    if handler is None:
        # argparse rejects unknown verbs before reaching here, so this
        # only fires if a verb is registered without a handler.
        raise AssertionError(f"unhandled verb after argparse: {args.verb!r}")
    return handler(args) or 0


def main() -> int:
    return dispatch(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
