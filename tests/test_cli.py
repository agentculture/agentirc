"""Smoke tests for the agentirc CLI public contract.

These exercise the public `dispatch()` contract — it returns an `int`
exit code on successful command dispatch, and lets argparse's
`SystemExit` propagate on `--help`/`--version`/parse-errors per Python
convention. They also lock in the dual-script + version-source
invariants from CLAUDE.md.

In PR-B2 (9.2.0) the lifecycle verbs (`serve`/`start`/`stop`/...)
became functional — calling `dispatch(['serve'])` now boots a real
IRCd. The earlier `test_lifecycle_verbs_are_stubs` Shape A smoke is
gone; the proper subprocess-driven lifecycle tests land in PR-B3.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.metadata import entry_points, version as pkg_version

import pytest

from agentirc import __version__
from agentirc.cli import dispatch


def test_version_subcommand_returns_zero(capsys):
    assert dispatch(["version"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == f"agentirc {__version__}"


def test_help_raises_systemexit_zero(capsys):
    """argparse raises SystemExit(0) on --help; dispatch lets it propagate."""
    with pytest.raises(SystemExit) as exc_info:
        dispatch(["--help"])
    assert exc_info.value.code == 0


def test_version_flag_raises_systemexit_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        dispatch(["--version"])
    assert exc_info.value.code == 0


def test_unknown_verb_raises_systemexit_nonzero(capsys):
    """Parse errors (unknown subcommand) raise SystemExit with non-zero code."""
    with pytest.raises(SystemExit) as exc_info:
        dispatch(["nonsense-verb"])
    assert exc_info.value.code != 0


def test_no_argv_prints_help_and_returns_one():
    assert dispatch([]) == 1


@pytest.mark.parametrize(
    "verb", ["serve", "start", "stop", "restart", "status", "link", "logs"]
)
def test_lifecycle_verbs_have_help(verb, capsys):
    """Each lifecycle verb registers a help-printing subparser.

    PR-B2 made these functional, so we no longer assert they are
    stubs. The strongest contract we can lock in here without
    spawning real IRCds is that `--help` produces non-empty output
    and exits zero — that proves the verb is wired and won't be
    quietly removed by a future refactor.
    """
    with pytest.raises(SystemExit) as exc_info:
        dispatch([verb, "--help"])
    assert exc_info.value.code == 0
    assert verb in capsys.readouterr().out


def test_link_verb_validates_spec(capsys):
    """`agentirc link <spec>` parses the peer spec and prints it."""
    rc = dispatch(["link", "peer1:127.0.0.1:6667:secret"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "peer1" in captured.out
    assert "127.0.0.1" in captured.out


def test_link_verb_rejects_bad_spec(capsys):
    """Invalid link spec returns 1 and writes an error to stderr."""
    rc = dispatch(["link", "totally-not-a-link-spec"])
    assert rc == 1
    assert "Link must be" in capsys.readouterr().err


def test_version_matches_package_metadata():
    """__version__ must come from installed dist metadata, not a literal."""
    assert __version__ == pkg_version("agentirc-cli")


@pytest.mark.parametrize("script", ["agentirc", "agentirc-cli"])
def test_console_script_entry_point_declared(script):
    """Both console scripts must be declared in package metadata."""
    scripts = {ep.name: ep for ep in entry_points(group="console_scripts")}
    assert script in scripts, f"{script} entry point missing"
    assert scripts[script].value == "agentirc.cli:main"


@pytest.mark.parametrize("script", ["agentirc", "agentirc-cli"])
def test_console_script_runs(script):
    """Both console scripts run end-to-end when installed on PATH."""
    path = shutil.which(script)
    if path is None:
        pytest.skip(f"{script} not on PATH (run after `pip install -e .`)")
    result = subprocess.run(
        [path, "version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == f"agentirc {__version__}"


def test_python_m_agentirc():
    result = subprocess.run(
        [sys.executable, "-m", "agentirc", "version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"agentirc {__version__}"
