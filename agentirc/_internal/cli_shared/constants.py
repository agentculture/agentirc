"""Shared constants for the agentirc CLI.

Vendored from culture@df50942 (`culture/cli/shared/constants.py`) and
trimmed to the subset agentirc actually consumes. Bot-related constants
(``BOT_CONFIG_FILE``, ``LEGACY_CONFIG``, ``AGENTS_YAML``, etc.) are
dropped; agentirc has no bot configuration concept.

Default paths (``~/.culture/server.yaml``, ``~/.culture/logs``) are kept
intact per the "Defaults preserve culture continuity" rule in
CLAUDE.md, so agentirc and culture daemons share state directories on a
host without separate config trees.
"""

from __future__ import annotations

import os
import stat

DEFAULT_CONFIG = os.path.expanduser("~/.culture/server.yaml")
LOG_DIR = os.path.expanduser("~/.culture/logs")

_CONFIG_HELP = "Config file path"
_SERVER_NAME_HELP = "Server name"


def culture_runtime_dir() -> str:
    """Return a user-private directory for daemon sockets.

    Resolution order:

    1. ``$XDG_RUNTIME_DIR`` when set (Linux/systemd default — already
       user-private at ``/run/user/<uid>``).
    2. ``~/.culture/run/`` otherwise (typical macOS path), created mode
       0700 if missing and re-tightened to 0700 on every call so a
       hand-created or pre-existing dir cannot leak sockets.

    Raises ``RuntimeError`` when neither ``XDG_RUNTIME_DIR`` nor a
    resolvable home directory is available — silently writing a literal
    ``~/.culture/run`` directory in CWD would surprise callers and the
    daemons (which now route through this resolver) would fail at
    socket-bind time anyway.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return xdg
    home = os.path.expanduser("~")
    if not home or home == "~" or not os.path.isabs(home):
        raise RuntimeError(
            "culture_runtime_dir(): cannot resolve a home directory "
            "(os.path.expanduser('~') returned %r). Set $HOME or "
            "$XDG_RUNTIME_DIR before running agentirc commands." % home
        )
    fallback = os.path.join(home, ".culture", "run")
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    os.chmod(fallback, stat.S_IRWXU)
    return fallback
