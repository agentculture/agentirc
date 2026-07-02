"""Path-lock test: agentirc's default on-disk paths must stay byte-identical
to the 9.7.0/culture-era defaults.

CLAUDE.md's "Defaults preserve culture continuity" rule says these paths
must not drift without a deliberate, coordinated decision (renaming
``~/.culture`` to something agentirc-specific is explicitly out of scope for
the bootstrap). This test hard-codes the expected literal strings — that
hard-coding *is* the lock: a future refactor that silently renames a path
fails this test loudly instead of drifting unnoticed. Per-name derivations
(log/PID/port file names) are asserted against a concrete sample name
(``"spark"``) rather than left as untested string-formatting logic.

Authoritative sources for each constant:

- ``agentirc/_internal/cli_shared/constants.py`` — ``DEFAULT_CONFIG``,
  ``LOG_DIR``.
- ``agentirc/_internal/pidfile.py`` — ``PID_DIR``, ``_safe_name`` (the
  path-traversal sanitizer used when building PID/port/log filenames).
- ``agentirc/cli.py`` — the ``server-<name>.{log,pid,port}`` naming
  convention (``_safe_log_name``, ``_daemonize_server``, ``_server_logs``)
  and the ``_resolve_config`` fallback defaults (host/port/webhook-port/
  data-dir) applied when neither ``--flag`` nor YAML supply a value.
- ``agentirc/config.py`` — ``ServerConfig``/``TelemetryConfig`` dataclass
  defaults.
"""

from __future__ import annotations

import os

from agentirc._internal.cli_shared.constants import DEFAULT_CONFIG, LOG_DIR
from agentirc._internal.pidfile import PID_DIR, _safe_name
from agentirc.cli import _build_parser, _resolve_config
from agentirc.config import ServerConfig, TelemetryConfig

SAMPLE_NAME = "spark"


# ---------------------------------------------------------------------------
# Bare directory / file constants
# ---------------------------------------------------------------------------


def test_default_config_path_is_culture_server_yaml():
    assert DEFAULT_CONFIG == os.path.expanduser("~/.culture/server.yaml")


def test_log_dir_is_culture_logs():
    assert LOG_DIR == os.path.expanduser("~/.culture/logs")


def test_pid_dir_is_culture_pids():
    assert PID_DIR == os.path.expanduser("~/.culture/pids")


# ---------------------------------------------------------------------------
# Per-name derivations (log file, PID file, port file)
# ---------------------------------------------------------------------------


def test_log_file_pattern_for_sample_name():
    """``agentirc logs``/the daemon's log fd both derive this same path."""
    log_path = os.path.join(LOG_DIR, f"server-{_safe_name(SAMPLE_NAME)}.log")
    assert log_path == os.path.expanduser(f"~/.culture/logs/server-{SAMPLE_NAME}.log")


def test_pid_file_pattern_for_sample_name():
    pid_name = f"server-{SAMPLE_NAME}"
    pid_path = os.path.join(PID_DIR, f"{_safe_name(pid_name)}.pid")
    assert pid_path == os.path.expanduser(f"~/.culture/pids/server-{SAMPLE_NAME}.pid")


def test_port_file_pattern_for_sample_name():
    pid_name = f"server-{SAMPLE_NAME}"
    port_path = os.path.join(PID_DIR, f"{_safe_name(pid_name)}.port")
    assert port_path == os.path.expanduser(f"~/.culture/pids/server-{SAMPLE_NAME}.port")


def test_safe_name_sanitizes_path_traversal():
    """The sanitizer that guards every derived filename above must still
    neutralize path-traversal input — otherwise the patterns asserted above
    don't actually hold for adversarial names.
    """
    assert _safe_name("../../etc/passwd") == "passwd"
    assert "/" not in _safe_name("a/b/c")
    assert ".." not in _safe_name("../x")


# ---------------------------------------------------------------------------
# ServerConfig / TelemetryConfig dataclass defaults
# ---------------------------------------------------------------------------


def test_server_config_socket_defaults():
    cfg = ServerConfig()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 6667
    assert cfg.webhook_port == 7680


def test_telemetry_audit_dir_default_matches_culture_continuity():
    tcfg = TelemetryConfig()
    assert tcfg.audit_dir == "~/.culture/audit"
    assert os.path.expanduser(tcfg.audit_dir) == os.path.expanduser("~/.culture/audit")


# ---------------------------------------------------------------------------
# CLI-resolved defaults (argparse sentinel -> _resolve_config fallback)
# ---------------------------------------------------------------------------


def test_argparse_default_config_flag_is_culture_server_yaml():
    """The ``--config`` flag's own default (before any YAML is read)."""
    parser = _build_parser()
    args = parser.parse_args(["serve"])
    assert args.config == os.path.expanduser("~/.culture/server.yaml")


def test_resolve_config_data_dir_falls_back_to_culture_data(tmp_path):
    """``_resolve_config`` reads ``--config`` YAML — point it at a path that
    is guaranteed not to exist so this test's outcome only reflects
    agentirc's *built-in* defaults, never a real ``~/.culture/server.yaml``
    that may happen to exist on the machine running the test (a real
    culture install commonly has one).
    """
    parser = _build_parser()
    missing_config = str(tmp_path / "does-not-exist.yaml")
    args = parser.parse_args(["serve", "--config", missing_config])

    cfg = _resolve_config(args)

    assert cfg.host == "0.0.0.0"
    assert cfg.port == 6667
    assert cfg.webhook_port == 7680
    assert cfg.data_dir == os.path.expanduser("~/.culture/data")
