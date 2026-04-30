"""agentirc CLI entry point.

The major version started at 9 to leapfrog culture's earlier squat-publish
of `agentirc-cli==8.7.x.devN` on TestPyPI so future dev releases sort as
the actual "latest". Only `version` is wired up; the IRCd lifecycle verbs
(`serve`, `start`, `stop`, `restart`, `status`, `link`, `logs`) are stubs
that exit non-zero with a clear "not yet implemented" message. The real
dispatch logic lands in PR-B2, extracted from `../culture/culture/cli/
server.py`.

Public surface (semver-tracked, see docs/api-stability.md once written):
- `main()` — console-script entry point.
- `dispatch(argv)` — the function culture's `culture server` shim calls.
  Returns an `int` exit code on successful command dispatch. Per Python
  convention, argparse raises `SystemExit` for `--help`, `--version`, and
  parse errors; we let that propagate (do not silently swallow). In-process
  callers that want a return value rather than process termination must
  catch `SystemExit` themselves — typically `try: rc = dispatch(argv);
  except SystemExit as e: rc = e.code or 0`.
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
    # argparse raises SystemExit on --help, --version, and parse errors.
    # We let that propagate per Python convention; in-process callers must
    # catch SystemExit themselves.
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
            f"agentirc: '{args.verb}' is not yet implemented; "
            "the IRCd extraction lands in a follow-up release.",
            file=sys.stderr,
        )
        return 1

    # Defensive: argparse rejects unknown verbs before reaching here.
    raise AssertionError(f"unhandled verb after argparse: {args.verb!r}")


def main() -> int:
    return dispatch(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
