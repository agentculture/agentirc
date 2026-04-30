"""agentirc CLI entry point.

This is the 0.1.0 skeleton. Only `version` is wired up; the IRCd lifecycle
verbs (`serve`, `start`, `stop`, `restart`, `status`, `link`, `logs`) are
stubs that exit non-zero with a clear "not yet implemented" message. The
real dispatch logic lands when the server core is copied from culture in
the IRCd extraction PR.

Public surface (semver-tracked, see docs/api-stability.md once written):
- `main()` — console-script entry point.
- `dispatch(argv)` — the function culture's `culture server` shim calls.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from agentirc import __version__

_NOT_IMPLEMENTED_VERBS = ("serve", "start", "stop", "restart", "status", "link", "logs")


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
    p_serve.add_argument("--config", default="~/.culture/server.yaml")

    p_start = sub.add_parser("start", help="Start the IRCd as a managed service")
    p_start.add_argument("--name")

    p_stop = sub.add_parser("stop", help="Stop the managed IRCd service")
    p_stop.add_argument("--name")

    p_restart = sub.add_parser("restart", help="Restart the managed IRCd service")
    p_restart.add_argument("--name")

    p_status = sub.add_parser("status", help="Report IRCd service state")
    p_status.add_argument("--name")

    p_link = sub.add_parser("link", help="Register a server-to-server mesh link")
    p_link.add_argument("peer", nargs="?")

    p_logs = sub.add_parser("logs", help="Tail IRCd service logs")
    p_logs.add_argument("--name")
    p_logs.add_argument("-f", "--follow", action="store_true")

    sub.add_parser("version", help="Print agentirc version")

    return parser


def dispatch(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))

    if args.verb is None:
        parser.print_help(sys.stderr)
        return 1

    if args.verb == "version":
        print(f"agentirc {__version__}")
        return 0

    if args.verb in _NOT_IMPLEMENTED_VERBS:
        print(
            f"agentirc: '{args.verb}' is not yet implemented in {__version__}; "
            "the IRCd extraction lands in a follow-up release.",
            file=sys.stderr,
        )
        return 1

    parser.error(f"unknown verb: {args.verb}")
    return 2


def main() -> int:
    return dispatch(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
