"""Smoke tests for the agentirc CLI skeleton.

These exist to exercise the public `dispatch()` contract — culture's shim
calls it in-process and depends on it returning an `int` rather than
raising `SystemExit`. They also lock in the dual-script + version-source
invariants from CLAUDE.md.
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


def test_help_returns_int_not_systemexit(capsys):
    """argparse raises SystemExit on --help; dispatch must catch and return."""
    rc = dispatch(["--help"])
    assert isinstance(rc, int)
    assert rc == 0


def test_version_flag_returns_int_not_systemexit(capsys):
    rc = dispatch(["--version"])
    assert isinstance(rc, int)
    assert rc == 0


def test_unknown_verb_returns_int_not_systemexit():
    """Parse errors (unknown subcommand) must come back as a non-zero int."""
    rc = dispatch(["nonsense-verb"])
    assert isinstance(rc, int)
    assert rc != 0


def test_no_argv_prints_help_and_returns_one():
    assert dispatch([]) == 1


@pytest.mark.parametrize(
    "verb", ["serve", "start", "stop", "restart", "status", "link", "logs"]
)
def test_lifecycle_verbs_are_stubs(verb, capsys):
    assert dispatch([verb]) == 1
    err = capsys.readouterr().err
    assert "not yet implemented" in err
    assert verb in err


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
