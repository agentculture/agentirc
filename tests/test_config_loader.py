"""Tests for ServerConfig.from_yaml and the cli._resolve_config merge.

Agentirc-native; not vendored. Covers the YAML-loading + CLI-overrides
behavior introduced in 9.4.0 (PR-B4) when ``agentirc/cli.py:108``'s
"--config not yet wired" warning was retired.
"""

from __future__ import annotations

import argparse

import pytest
import yaml

from agentirc.cli import _resolve_config
from agentirc.config import (LinkConfig, PresenceConfig, ServerConfig,
                             TelemetryConfig)


def _ns(**overrides) -> argparse.Namespace:
    """argparse.Namespace pre-populated with sentinel-None lifecycle flags."""
    base = {
        "name": None,
        "host": None,
        "port": None,
        "webhook_port": None,
        "data_dir": None,
        "link": None,
        "config": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# ServerConfig.from_yaml
# ---------------------------------------------------------------------------


def test_from_yaml_missing_path_returns_defaults(tmp_path):
    cfg = ServerConfig.from_yaml(tmp_path / "does-not-exist.yaml")
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 6667
    assert cfg.name == "culture"  # dataclass default; CLI uses "agentirc"
    assert cfg.links == []
    assert cfg.telemetry.enabled is False


def test_from_yaml_empty_file_returns_defaults(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    cfg = ServerConfig.from_yaml(p)
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 6667


def test_from_yaml_server_section_overrides_defaults(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("server:\n  name: spark\n  host: 127.0.0.1\n  port: 6700\n")
    cfg = ServerConfig.from_yaml(p)
    assert cfg.name == "spark"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 6700
    # untouched
    assert cfg.webhook_port == 7680


def test_from_yaml_telemetry_section_populates_dataclass(tmp_path):
    p = tmp_path / "t.yaml"
    audit_dir = str(tmp_path / "audit")
    p.write_text(
        "telemetry:\n"
        "  enabled: true\n"
        "  service_name: test-service\n"
        f"  audit_dir: {audit_dir}\n"
    )
    cfg = ServerConfig.from_yaml(p)
    assert cfg.telemetry.enabled is True
    assert cfg.telemetry.service_name == "test-service"
    assert cfg.telemetry.audit_dir == audit_dir
    # other telemetry fields stay default
    assert (
        cfg.telemetry.otlp_endpoint == "http://localhost:4317"
    )  # NOSONAR S5332 — test-only OTLP endpoint


def test_from_yaml_links_list_builds_linkconfigs(tmp_path):
    p = tmp_path / "l.yaml"
    p.write_text(
        "links:\n"
        "  - {name: alpha, host: 127.0.0.1, port: 6601, password: x}\n"
        "  - {name: beta, host: 127.0.0.1, port: 6602, password: y, trust: restricted}\n"
    )
    cfg = ServerConfig.from_yaml(p)
    assert len(cfg.links) == 2
    assert isinstance(cfg.links[0], LinkConfig)
    assert cfg.links[0].name == "alpha"
    assert cfg.links[1].trust == "restricted"


def test_from_yaml_ignores_culture_only_keys(tmp_path):
    """The full culture server.yaml has supervisor / agents / buffer_size /
    poll_interval / sleep_start / sleep_end and ``server.archived*`` —
    none belong to agentirc, all should be silently ignored.
    """
    p = tmp_path / "full.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "server": {
                    "name": "spark",
                    "host": "localhost",
                    "port": 6667,
                    "archived": False,
                    "archived_at": "",
                    "archived_reason": "",
                },
                "supervisor": {"model": "claude-sonnet-4-6", "thinking": "medium"},
                "webhooks": {"url": None, "irc_channel": "#alerts"},
                "buffer_size": 500,
                "poll_interval": 60,
                "sleep_start": "23:00",
                "sleep_end": "08:00",
                "agents": {"daria": "/path/to/daria"},
            }
        )
    )
    # Should not raise
    cfg = ServerConfig.from_yaml(p)
    assert cfg.name == "spark"
    assert cfg.host == "localhost"
    assert cfg.port == 6667


def test_from_yaml_liveness_defaults_when_absent(tmp_path):
    """t5: ping_interval/pong_timeout default to 60.0/120.0 when unset."""
    p = tmp_path / "s.yaml"
    p.write_text("server:\n  name: spark\n")
    cfg = ServerConfig.from_yaml(p)
    assert cfg.ping_interval == 60.0
    assert cfg.pong_timeout == 120.0


def test_from_yaml_liveness_keys_override_defaults(tmp_path):
    """t5: top-level ping_interval/pong_timeout keys are recognised."""
    p = tmp_path / "s.yaml"
    p.write_text("ping_interval: 5\npong_timeout: 10\n")
    cfg = ServerConfig.from_yaml(p)
    assert cfg.ping_interval == 5
    assert cfg.pong_timeout == 10


def test_from_yaml_liveness_disabled_via_zero(tmp_path):
    """t5: 0 (or negative) is a valid, meaningful value — disables the loop."""
    p = tmp_path / "s.yaml"
    p.write_text("ping_interval: 0\npong_timeout: 0\n")
    cfg = ServerConfig.from_yaml(p)
    assert cfg.ping_interval == 0
    assert cfg.pong_timeout == 0


def test_from_yaml_malformed_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("server:\n  name: [unclosed\n")
    with pytest.raises(yaml.YAMLError):
        ServerConfig.from_yaml(p)


def test_from_yaml_unknown_telemetry_key_silently_dropped(tmp_path):
    """If culture extends TelemetryConfig in the future, agentirc shouldn't
    crash on unknown keys — silent-drop matches culture-coexistence rule.
    """
    p = tmp_path / "t.yaml"
    p.write_text(
        "telemetry:\n  enabled: true\n  future_field_not_yet_in_agentirc: 42\n"
    )
    cfg = ServerConfig.from_yaml(p)
    assert cfg.telemetry.enabled is True


# ---------------------------------------------------------------------------
# PresenceConfig (t2) — heartbeat_interval_seconds / stale_after_seconds
# ---------------------------------------------------------------------------


def test_server_config_default_presence_is_30_90():
    """ServerConfig() default construction works and .presence carries the
    30/90 defaults with no YAML involved."""
    cfg = ServerConfig()
    assert isinstance(cfg.presence, PresenceConfig)
    assert cfg.presence.heartbeat_interval_seconds == 30
    assert cfg.presence.stale_after_seconds == 90


def test_from_yaml_presence_section_populates_dataclass(tmp_path):
    """Culture-shaped presence: section (30/90) round-trips through from_yaml."""
    p = tmp_path / "p.yaml"
    p.write_text(
        "presence:\n" "  heartbeat_interval_seconds: 30\n" "  stale_after_seconds: 90\n"
    )
    cfg = ServerConfig.from_yaml(p)
    assert isinstance(cfg.presence, PresenceConfig)
    assert cfg.presence.heartbeat_interval_seconds == 30
    assert cfg.presence.stale_after_seconds == 90


def test_from_yaml_presence_section_non_default_values_round_trip(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(
        "presence:\n" "  heartbeat_interval_seconds: 10\n" "  stale_after_seconds: 25\n"
    )
    cfg = ServerConfig.from_yaml(p)
    assert cfg.presence.heartbeat_interval_seconds == 10
    assert cfg.presence.stale_after_seconds == 25


def test_from_yaml_no_presence_section_uses_defaults(tmp_path):
    """Backward compat: an existing culture server.yaml without a presence:
    section still loads, defaulting to 30/90."""
    p = tmp_path / "s.yaml"
    p.write_text("server:\n  name: spark\n")
    cfg = ServerConfig.from_yaml(p)
    assert cfg.presence.heartbeat_interval_seconds == 30
    assert cfg.presence.stale_after_seconds == 90


def test_from_yaml_presence_unknown_keys_silently_dropped(tmp_path):
    """Unknown keys inside presence: are ignored, same tolerance as the
    telemetry: block and top-level culture-only keys."""
    p = tmp_path / "p.yaml"
    p.write_text(
        "presence:\n"
        "  heartbeat_interval_seconds: 30\n"
        "  stale_after_seconds: 90\n"
        "  future_field_not_yet_in_agentirc: 42\n"
    )
    cfg = ServerConfig.from_yaml(p)
    assert cfg.presence.heartbeat_interval_seconds == 30
    assert cfg.presence.stale_after_seconds == 90


def test_presence_config_stale_not_greater_than_heartbeat_raises():
    with pytest.raises(ValueError, match="stale_after_seconds"):
        PresenceConfig(heartbeat_interval_seconds=90, stale_after_seconds=30)


def test_presence_config_stale_equal_to_heartbeat_raises():
    """Strictly greater — equal values are also invalid."""
    with pytest.raises(ValueError, match="stale_after_seconds"):
        PresenceConfig(heartbeat_interval_seconds=30, stale_after_seconds=30)


def test_presence_config_zero_heartbeat_raises():
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        PresenceConfig(heartbeat_interval_seconds=0, stale_after_seconds=90)


def test_presence_config_negative_heartbeat_raises():
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        PresenceConfig(heartbeat_interval_seconds=-5, stale_after_seconds=90)


def test_presence_config_zero_stale_after_raises():
    with pytest.raises(ValueError, match="stale_after_seconds"):
        PresenceConfig(heartbeat_interval_seconds=30, stale_after_seconds=0)


def test_presence_config_negative_stale_after_raises():
    with pytest.raises(ValueError, match="stale_after_seconds"):
        PresenceConfig(heartbeat_interval_seconds=30, stale_after_seconds=-10)


def test_from_yaml_invalid_presence_section_raises_at_load(tmp_path):
    """The same validation fires when the bad values arrive via YAML, not
    just direct construction — from_yaml propagates the ValueError."""
    p = tmp_path / "p.yaml"
    p.write_text(
        "presence:\n" "  heartbeat_interval_seconds: 90\n" "  stale_after_seconds: 30\n"
    )
    with pytest.raises(ValueError, match="stale_after_seconds"):
        ServerConfig.from_yaml(p)


def test_presence_config_non_int_value_raises_clear_valueerror():
    """A wrong-typed value (e.g. a quoted YAML string) fails with a clear
    ValueError, not a raw TypeError from the `<=` comparison."""
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        PresenceConfig(heartbeat_interval_seconds="thirty", stale_after_seconds=90)


def test_presence_config_bool_value_raises_valueerror():
    """bool is an int subclass but not a valid interval — rejected explicitly."""
    with pytest.raises(ValueError, match="stale_after_seconds"):
        PresenceConfig(heartbeat_interval_seconds=30, stale_after_seconds=True)


def test_from_yaml_wrong_typed_presence_value_raises_valueerror(tmp_path):
    """A quoted (string) YAML value propagates a ValueError through from_yaml,
    not a TypeError."""
    p = tmp_path / "p.yaml"
    p.write_text(
        "presence:\n"
        '  heartbeat_interval_seconds: "thirty"\n'
        "  stale_after_seconds: 90\n"
    )
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        ServerConfig.from_yaml(p)


# ---------------------------------------------------------------------------
# cli._resolve_config — CLI > YAML > built-in default
# ---------------------------------------------------------------------------


def test_resolve_config_cli_overrides_yaml(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("server:\n  name: yaml-name\n  host: 127.0.0.1\n  port: 6700\n")
    args = _ns(config=str(p), port=9999, name="cli-name")
    cfg = _resolve_config(args)
    assert cfg.name == "cli-name"  # CLI wins
    assert cfg.port == 9999  # CLI wins
    assert cfg.host == "127.0.0.1"  # YAML wins (CLI absent)


def test_resolve_config_yaml_used_when_cli_absent(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("server:\n  name: spark\n  port: 6700\n")
    args = _ns(config=str(p))
    cfg = _resolve_config(args)
    assert cfg.name == "spark"
    assert cfg.port == 6700
    assert args.name == "spark"  # mutated for downstream code
    assert args.port == 6700


def test_resolve_config_falls_back_to_builtin_defaults(tmp_path):
    """Both CLI and YAML absent → built-in defaults."""
    args = _ns(config=str(tmp_path / "missing.yaml"))
    cfg = _resolve_config(args)
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 6667
    assert cfg.webhook_port == 7680
    # name is special: stays None on args (so _resolve_server_name handles it)
    # but cfg.name falls back to "agentirc"
    assert args.name is None
    assert cfg.name == "agentirc"


def test_resolve_config_links_cli_replaces_yaml(tmp_path):
    p = tmp_path / "l.yaml"
    p.write_text(
        "links:\n" "  - {name: alpha, host: 127.0.0.1, port: 6601, password: x}\n"
    )
    cli_link = LinkConfig(
        name="cli-peer",
        host="127.0.0.1",
        port=7777,
        password="cli",  # noqa: S106  # NOSONAR S2068 — test fixture, not a real credential
        trust="full",
    )
    args = _ns(config=str(p), link=[cli_link])
    cfg = _resolve_config(args)
    assert len(cfg.links) == 1
    assert cfg.links[0].name == "cli-peer"  # CLI replaces YAML wholesale


def test_resolve_config_links_yaml_used_when_cli_empty(tmp_path):
    p = tmp_path / "l.yaml"
    p.write_text(
        "links:\n" "  - {name: alpha, host: 127.0.0.1, port: 6601, password: x}\n"
    )
    args = _ns(config=str(p))  # link defaults to None
    cfg = _resolve_config(args)
    assert len(cfg.links) == 1
    assert cfg.links[0].name == "alpha"


# ---------------------------------------------------------------------------
# Reliability / edge cases (Qodo + Copilot review on PR-B4)
# ---------------------------------------------------------------------------


def test_non_mapping_yaml_raises_clearly(tmp_path):
    """Top-level list/scalar yaml is valid YAML but wrong shape — should raise
    a clean ``yaml.YAMLError`` rather than ``AttributeError`` from
    ``raw.get(...)``.
    """
    p = tmp_path / "list.yaml"
    p.write_text("- one\n- two\n")
    with pytest.raises(yaml.YAMLError):
        _resolve_config(_ns(config=str(p)))


def test_non_mapping_yaml_via_from_yaml_raises_yaml_error(tmp_path):
    """Same shape problem via the public ``ServerConfig.from_yaml`` —
    raises ``yaml.YAMLError`` with a clear message rather than letting
    ``AttributeError`` from ``.get(...)`` leak through.
    """
    p = tmp_path / "scalar.yaml"
    p.write_text("just-a-string\n")
    with pytest.raises(yaml.YAMLError):
        ServerConfig.from_yaml(p)


def test_resolve_config_yaml_data_dir_tilde_expanded(tmp_path, monkeypatch):
    """A ``data_dir: ~/foo`` in YAML must be expanded so persistence code
    doesn't write to a literal ``~`` directory in the working dir.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "d.yaml"
    p.write_text("data_dir: ~/agentirc-data\n")
    cfg = _resolve_config(_ns(config=str(p)))
    assert cfg.data_dir == str(tmp_path / "agentirc-data")
    assert "~" not in cfg.data_dir
