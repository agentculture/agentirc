"""Tests for agentirc.bots.config — vendored from culture/bots/config.py."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agentirc.bots.config import (
    BOTS_DIR,
    BotConfig,
    EmitEventSpec,
    load_bot_config,
    reset_fires_event_warning_state,
    save_bot_config,
)


# ---------------------------------------------------------------------------
# BOTS_DIR default resolves under ~/.culture
# ---------------------------------------------------------------------------

def test_bots_dir_under_culture_dotdir():
    """BOTS_DIR default must be inside ~/.culture (culture continuity invariant)."""
    culture_root = Path.home() / ".culture"
    # BOTS_DIR must be a sub-path of ~/.culture
    assert str(BOTS_DIR).startswith(str(culture_root)), (
        f"BOTS_DIR {BOTS_DIR!r} does not resolve under {culture_root!r}"
    )


# ---------------------------------------------------------------------------
# load_bot_config: happy path
# ---------------------------------------------------------------------------

VALID_YAML = textwrap.dedent("""\
    bot:
      name: mybot
      owner: ori
      description: A test bot
      created: "2026-01-01"
    trigger:
      type: webhook
    output:
      channels:
        - "#general"
      dm_owner: true
      mention: "@mybot"
      template: null
      fallback: json
""")


def test_load_valid_bot_config(tmp_path):
    """Loading a well-formed bot.yaml produces a fully-populated BotConfig."""
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(VALID_YAML)

    config = load_bot_config(cfg_path)

    assert isinstance(config, BotConfig)
    assert config.name == "mybot"
    assert config.owner == "ori"
    assert config.description == "A test bot"
    assert config.created == "2026-01-01"
    assert config.trigger_type == "webhook"
    assert config.channels == ["#general"]
    assert config.dm_owner is True
    assert config.mention == "@mybot"
    assert config.template is None
    assert config.fallback == "json"
    assert config.archived is False
    assert config.fires_event is None


def test_load_bot_config_with_fires_event_under_output(tmp_path):
    """fires_event under output: section is parsed into EmitEventSpec."""
    yaml_text = textwrap.dedent("""\
        bot:
          name: eventbot
          owner: ori
        trigger:
          type: webhook
        output:
          channels: []
          fires_event:
            type: my.event
            data:
              key: value
    """)
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(yaml_text)

    config = load_bot_config(cfg_path)

    assert config.fires_event is not None
    assert isinstance(config.fires_event, EmitEventSpec)
    assert config.fires_event.type == "my.event"
    assert config.fires_event.data == {"key": "value"}


def test_load_bot_config_fires_event_top_level_accepted(tmp_path):
    """fires_event at top-level is accepted (backward-compat, issue #260)."""
    reset_fires_event_warning_state()
    yaml_text = textwrap.dedent("""\
        bot:
          name: legacybot
          owner: ori
        trigger:
          type: webhook
        output:
          channels: []
        fires_event:
          type: legacy.event
          data: {}
    """)
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(yaml_text)

    config = load_bot_config(cfg_path)

    assert config.fires_event is not None
    assert config.fires_event.type == "legacy.event"


def test_load_bot_config_defaults(tmp_path):
    """Missing optional fields fall back to documented defaults."""
    minimal_yaml = textwrap.dedent("""\
        bot:
          name: minimalbot
        trigger: {}
        output: {}
    """)
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(minimal_yaml)

    config = load_bot_config(cfg_path)

    assert config.name == "minimalbot"
    assert config.trigger_type == "webhook"  # default
    assert config.fallback == "json"          # default
    assert config.channels == []
    assert config.dm_owner is False
    assert config.fires_event is None


# ---------------------------------------------------------------------------
# Malformed / invalid YAML is rejected without partial state
# ---------------------------------------------------------------------------

def test_malformed_yaml_raises(tmp_path):
    """A syntactically broken YAML file raises an exception; no partial state."""
    bad_yaml = tmp_path / "bot.yaml"
    bad_yaml.write_text("bot: [\nunclosed bracket\n  bad: yaml")

    with pytest.raises(Exception):
        load_bot_config(bad_yaml)


def test_missing_file_raises(tmp_path):
    """A non-existent path raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_bot_config(tmp_path / "nonexistent.yaml")


def test_empty_yaml_returns_defaults(tmp_path):
    """An empty (null) YAML file yields a default BotConfig, no exception."""
    empty = tmp_path / "bot.yaml"
    empty.write_text("")

    # yaml.safe_load("") returns None; load_bot_config does `or {}` so this is valid
    config = load_bot_config(empty)
    assert isinstance(config, BotConfig)
    assert config.name == ""


# ---------------------------------------------------------------------------
# save_bot_config round-trip
# ---------------------------------------------------------------------------

def test_save_and_reload_roundtrip(tmp_path):
    """save_bot_config writes atomically; reloading produces an equivalent config."""
    original = BotConfig(
        name="roundtrip",
        owner="ori",
        description="RT bot",
        trigger_type="webhook",
        channels=["#test"],
        dm_owner=False,
        fallback="json",
        fires_event=EmitEventSpec(type="rt.event", data={"x": 1}),
    )
    out_path = tmp_path / "bot.yaml"
    save_bot_config(out_path, original)

    reloaded = load_bot_config(out_path)

    assert reloaded.name == original.name
    assert reloaded.owner == original.owner
    assert reloaded.channels == original.channels
    assert reloaded.fires_event is not None
    assert reloaded.fires_event.type == "rt.event"
    assert reloaded.fires_event.data == {"x": 1}
