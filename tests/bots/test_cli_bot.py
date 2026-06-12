"""Tests for agentirc.cli bot verb (agentirc bot …).

Coverage:
1. ``agentirc.cli.dispatch(["bot", "list"])`` runs and returns 0 in a hermetic
   tmp bots dir (BOTS_DIR patched in every namespace that imports it by-name).
2. Create → list → inspect → archive → unarchive round-trip via the real
   ``agentirc.cli.dispatch`` entry point.
3. The ``bot`` subparser registers all seven subcommands so flag parity with
   ``culture bot`` is provable.
"""

from __future__ import annotations

import pytest

from agentirc import cli as cli_mod
from agentirc.bots import cli as bot_cli_mod
from agentirc.bots import config as bots_config_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bots_dir(tmp_path, monkeypatch):
    """Hermetic tmp bots dir wired into every module that reads BOTS_DIR.

    Follows the same pattern as ``tests/bots/test_bot_manager.py``:
    ``BOTS_DIR`` is imported by-name into ``bots.config`` and ``bots.cli``,
    so both namespaces must be patched.
    """
    d = tmp_path / "bots"
    d.mkdir()
    monkeypatch.setattr(bots_config_mod, "BOTS_DIR", d)
    monkeypatch.setattr(bot_cli_mod, "BOTS_DIR", d)
    return d


def _dispatch(*args: str) -> int:
    """Thin wrapper so tests read cleanly."""
    return cli_mod.dispatch(list(args))


# ---------------------------------------------------------------------------
# Test 1 — bot list returns 0 with empty bots dir
# ---------------------------------------------------------------------------


def test_bot_list_empty_dir_returns_0(bots_dir, capsys):
    """``agentirc bot list`` exits 0 and says "No bots configured."."""
    rc = _dispatch("bot", "list")
    assert rc == 0
    out = capsys.readouterr().out
    assert "No bots configured" in out


# ---------------------------------------------------------------------------
# Test 2 — create → list → inspect → archive → unarchive round-trip
# ---------------------------------------------------------------------------


def test_bot_create_creates_on_disk(bots_dir, capsys):
    """``agentirc bot create`` writes a bot.yaml under BOTS_DIR."""
    rc = _dispatch("bot", "create", "mybot", "--owner", "ori", "--channels", "#test")
    assert rc == 0
    out = capsys.readouterr().out
    # auto-prefixed name: "ori-mybot" (owner + name, no server prefix in agentirc)
    assert "ori-mybot" in out or "mybot" in out
    # the bot directory must now exist
    found = list(bots_dir.iterdir())
    assert len(found) == 1, f"expected 1 bot dir, got: {found}"
    bot_dir = found[0]
    assert (bot_dir / "bot.yaml").is_file()


def test_bot_list_shows_created_bot(bots_dir, capsys):
    """``agentirc bot list`` shows the bot created in the previous step."""
    _dispatch("bot", "create", "mybot", "--owner", "ori")
    capsys.readouterr()  # flush create output

    rc = _dispatch("bot", "list")
    assert rc == 0
    out = capsys.readouterr().out
    # Some form of "mybot" must appear in the listing
    assert "mybot" in out


def test_bot_inspect_shows_details(bots_dir, capsys):
    """``agentirc bot inspect <name>`` prints bot details."""
    _dispatch("bot", "create", "mybot", "--owner", "ori", "--description", "A test bot")
    capsys.readouterr()

    # Discover the actual bot name from disk (may be prefixed)
    bot_dirs = list(bots_dir.iterdir())
    assert bot_dirs, "bot dir should exist after create"
    bot_name = bot_dirs[0].name

    rc = _dispatch("bot", "inspect", bot_name)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Bot:" in out
    assert "ori" in out  # owner shown


def test_bot_archive_and_unarchive(bots_dir, capsys):
    """archive flips the archived flag; unarchive restores it."""
    _dispatch("bot", "create", "mybot", "--owner", "ori")
    capsys.readouterr()

    bot_dirs = list(bots_dir.iterdir())
    bot_name = bot_dirs[0].name

    # Archive
    rc = _dispatch("bot", "archive", bot_name, "--reason", "testing")
    assert rc == 0
    capsys.readouterr()

    # After archive, plain list should hide it
    rc = _dispatch("bot", "list")
    assert rc == 0
    out = capsys.readouterr().out
    assert bot_name not in out or "No bots configured" in out

    # With --all it should appear with [archived] marker
    rc = _dispatch("bot", "list", "--all")
    assert rc == 0
    out = capsys.readouterr().out
    assert bot_name in out
    assert "[archived]" in out

    # Unarchive
    rc = _dispatch("bot", "unarchive", bot_name)
    assert rc == 0
    capsys.readouterr()

    # Now plain list should show it again
    rc = _dispatch("bot", "list")
    assert rc == 0
    out = capsys.readouterr().out
    assert bot_name in out


# ---------------------------------------------------------------------------
# Test 3 — all seven subcommands are registered in the parser
# ---------------------------------------------------------------------------

_EXPECTED_BOT_SUBCOMMANDS = {
    "create",
    "start",
    "stop",
    "list",
    "inspect",
    "archive",
    "unarchive",
}


def test_bot_subparser_registers_all_seven_subcommands():
    """The ``bot`` subparser exposes all seven subcommands for flag parity with culture."""
    import argparse

    # Build a standalone parser with just the bot verb registered, so we can
    # introspect the subcommand choices without depending on full cli internals.
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="verb")
    bot_cli_mod.register(sub)

    # Walk the subparser registry to find the "bot" subparser and its choices
    # argparse stores subparsers choices in _subparsers action groups
    bot_subparser = None
    for action in parser._subparsers._group_actions:
        if "bot" in action.choices:
            bot_subparser = action.choices["bot"]
            break

    assert bot_subparser is not None, "bot subparser not found"

    # Find the nested subparser (bot_command choices)
    bot_command_choices: set[str] = set()
    for action in bot_subparser._subparsers._group_actions:
        bot_command_choices.update(action.choices.keys())

    assert bot_command_choices == _EXPECTED_BOT_SUBCOMMANDS, (
        f"subcommand mismatch: got {bot_command_choices!r}, "
        f"expected {_EXPECTED_BOT_SUBCOMMANDS!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — dispatch with no bot_command returns 1 (usage error)
# ---------------------------------------------------------------------------


def test_bot_no_subcommand_returns_1(bots_dir, capsys):
    """``agentirc bot`` with no subcommand prints usage and returns 1."""
    rc = _dispatch("bot")
    assert rc == 1
    err = capsys.readouterr().err
    assert "Usage" in err or "usage" in err


# ---------------------------------------------------------------------------
# Test 5 — bot start / stop on existing bot return 0
# ---------------------------------------------------------------------------


def test_bot_start_existing_bot_returns_0(bots_dir, capsys):
    """``agentirc bot start <name>`` returns 0 for an existing bot."""
    _dispatch("bot", "create", "mybot", "--owner", "ori")
    capsys.readouterr()
    bot_name = next(iter(bots_dir.iterdir())).name

    rc = _dispatch("bot", "start", bot_name)
    assert rc == 0


def test_bot_stop_existing_bot_returns_0(bots_dir, capsys):
    """``agentirc bot stop <name>`` returns 0 for an existing bot."""
    _dispatch("bot", "create", "mybot", "--owner", "ori")
    capsys.readouterr()
    bot_name = next(iter(bots_dir.iterdir())).name

    rc = _dispatch("bot", "stop", bot_name)
    assert rc == 0


# ---------------------------------------------------------------------------
# Test 6 — bot start / stop on missing bot return 1
# ---------------------------------------------------------------------------


def test_bot_start_missing_bot_returns_1(bots_dir, capsys):
    """``agentirc bot start <name>`` returns 1 when the bot doesn't exist."""
    rc = _dispatch("bot", "start", "ghost-bot")
    assert rc == 1


def test_bot_stop_missing_bot_returns_1(bots_dir, capsys):
    """``agentirc bot stop <name>`` returns 1 when the bot doesn't exist."""
    rc = _dispatch("bot", "stop", "ghost-bot")
    assert rc == 1


# ---------------------------------------------------------------------------
# Security — bot-name path traversal is rejected before any path is built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    ["../evil", "../../etc/passwd", "a/b", "..", ".", "", "foo/../bar", "x\\y"],
)
def test_bot_create_rejects_path_traversal_names(bots_dir, evil, capsys):
    """A name that is not a single safe path segment is refused with exit 1.

    Regression for the path-traversal finding: bot names become ``BOTS_DIR /
    name``, so ``../`` or separators must never reach the filesystem layer.
    """
    rc = _dispatch("bot", "create", evil, "--owner", "ori")
    assert rc == 1
    assert "Invalid bot name" in capsys.readouterr().err
    # Nothing was written outside (or inside) the bots dir.
    assert list(bots_dir.iterdir()) == []


def test_bot_inspect_rejects_path_traversal_names(bots_dir, capsys):
    """Read verbs reject traversal names too (no arbitrary file reads)."""
    rc = _dispatch("bot", "inspect", "../../secret")
    assert rc == 1
    assert "Invalid bot name" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Event-triggered bot creation (since 9.8.0)
# ---------------------------------------------------------------------------


def _read_yaml(bots_dir, bot_name):
    import yaml

    return yaml.safe_load(
        (bots_dir / bot_name / "bot.yaml").read_text()
    )


def test_bot_create_event_writes_filter(bots_dir, capsys):
    """``--trigger event --event-filter ...`` writes an event bot spec."""
    rc = _dispatch(
        "bot", "create", "pinger", "--owner", "ori",
        "--trigger", "event",
        "--event-filter", "type == 'user.message' and channel == '#general'",
        "--channels", "#general", "--template", "pong {{event.nick}}",
    )
    assert rc == 0
    spec = _read_yaml(bots_dir, "ori-pinger")
    assert spec["trigger"]["type"] == "event"
    assert spec["trigger"]["filter"] == "type == 'user.message' and channel == '#general'"


def test_bot_create_event_requires_filter(bots_dir, capsys):
    """``--trigger event`` without a filter is rejected."""
    rc = _dispatch("bot", "create", "nofilter", "--owner", "ori", "--trigger", "event")
    assert rc == 1
    assert "requires --event-filter" in capsys.readouterr().err
    assert list(bots_dir.iterdir()) == []


def test_bot_create_event_rejects_bad_filter(bots_dir, capsys):
    """An --event-filter that doesn't compile is rejected at create time."""
    rc = _dispatch(
        "bot", "create", "badfilter", "--owner", "ori",
        "--trigger", "event", "--event-filter", "type == =",
    )
    assert rc == 1
    assert "invalid --event-filter" in capsys.readouterr().err
    assert list(bots_dir.iterdir()) == []


def test_bot_create_webhook_rejects_event_filter(bots_dir, capsys):
    """--event-filter only applies to event triggers."""
    rc = _dispatch(
        "bot", "create", "wh", "--owner", "ori",
        "--trigger", "webhook", "--event-filter", "type == 'x.y'",
    )
    assert rc == 1
    assert "only applies to --trigger event" in capsys.readouterr().err
