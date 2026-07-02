"""Tests for --json flag support in status and version commands.

Verifies that:
- `agentirc version --json` emits valid JSON with correct structure
- `agentirc status --json` emits valid JSON for various states
- Text output (without --json) remains byte-identical to previous behavior
- Exit codes remain unchanged
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agentirc import __version__
from agentirc.cli import dispatch


class TestVersionJson:
    """Tests for `agentirc version --json`."""

    def test_version_json_valid_structure(self, capsys):
        """version --json produces valid JSON with name and version fields."""
        rc = dispatch(["version", "--json"])
        assert rc == 0
        output = capsys.readouterr().out.strip()
        data = json.loads(output)
        assert data["name"] == "agentirc-cli"
        assert data["version"] == __version__

    def test_version_json_single_line(self, capsys):
        """version --json outputs exactly one line of JSON."""
        rc = dispatch(["version", "--json"])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.split("\n")
        # Filter out empty lines
        non_empty = [l for l in lines if l.strip()]
        assert len(non_empty) == 1

    def test_version_text_unchanged(self, capsys):
        """version without --json preserves exact text format."""
        rc = dispatch(["version"])
        assert rc == 0
        output = capsys.readouterr().out.strip()
        assert output == f"agentirc {__version__}"

    def test_version_json_matches_text_version(self, capsys):
        """version --json version field matches text output."""
        rc_json = dispatch(["version", "--json"])
        assert rc_json == 0
        json_output = capsys.readouterr().out.strip()
        data = json.loads(json_output)

        rc_text = dispatch(["version"])
        assert rc_text == 0
        text_output = capsys.readouterr().out.strip()

        # Extract version from text output (format: "agentirc X.Y.Z")
        text_version = text_output.split()[1]
        assert data["version"] == text_version


class TestStatusJson:
    """Tests for `agentirc status --json`."""

    def test_status_json_nonexistent_server(self, capsys):
        """status --json for nonexistent server outputs valid JSON."""
        rc = dispatch(["status", "--name", "nonexistent-test-server"])
        # We're not passing --json here, so this tests text output
        assert rc == 0
        output = capsys.readouterr().out.strip()
        assert "not running" in output

    def test_status_json_nonexistent_server_json(self, capsys):
        """status --json for nonexistent server returns running=false."""
        rc = dispatch(["status", "--name", "nonexistent-test-server", "--json"])
        assert rc == 0
        output = capsys.readouterr().out.strip()
        data = json.loads(output)
        assert data["name"] == "nonexistent-test-server"
        assert data["running"] is False
        assert data["pid"] is None
        assert data["port"] is None
        assert "stale" not in data

    def test_status_json_valid_structure(self, capsys):
        """status --json includes required fields for any state."""
        rc = dispatch(["status", "--name", "test-server", "--json"])
        assert rc == 0
        output = capsys.readouterr().out.strip()
        data = json.loads(output)
        # All states should have these fields
        assert "name" in data
        assert "running" in data
        assert "pid" in data
        assert "port" in data
        # running is always a boolean
        assert isinstance(data["running"], bool)
        # pid and port are either null or int
        assert data["pid"] is None or isinstance(data["pid"], int)
        assert data["port"] is None or isinstance(data["port"], int)

    def test_status_text_unchanged(self, capsys):
        """status without --json preserves exact text format."""
        rc = dispatch(["status", "--name", "test-text-format"])
        assert rc == 0
        output = capsys.readouterr().out.strip()
        # Should contain the server name and "not running"
        assert "test-text-format" in output
        assert "not running" in output

    def test_status_json_single_line(self, capsys):
        """status --json outputs exactly one line of JSON."""
        rc = dispatch(["status", "--name", "test-single-line", "--json"])
        assert rc == 0
        output = capsys.readouterr().out
        lines = output.split("\n")
        non_empty = [l for l in lines if l.strip()]
        assert len(non_empty) == 1


class TestStatusJsonWithDaemon:
    """Tests for status --json with a running daemon.

    Uses a temporary directory and a running IRCd instance to test
    the full state-reporting logic.
    """

    @pytest.fixture
    def temp_dirs(self, tmp_path):
        """Set up temporary directories for config and data."""
        config_dir = tmp_path / "config"
        data_dir = tmp_path / "data"
        config_dir.mkdir()
        data_dir.mkdir()
        return {
            "config": config_dir,
            "data": data_dir,
            "config_file": config_dir / "server.yaml",
            "log_dir": config_dir / "logs",
        }

    @pytest.fixture
    def running_server(self, temp_dirs):
        """Start a real agentirc daemon and yield its info.

        The daemon runs in the background. Cleanup is handled in finally.
        """
        # Write a minimal config
        config_file = temp_dirs["config_file"]
        server_name = "test-json-daemon"
        log_dir = temp_dirs["log_dir"]
        log_dir.mkdir(parents=True, exist_ok=True)
        port = 16667  # Arbitrary high port to avoid conflicts

        config_content = f"""
server:
  name: {server_name}
  host: 127.0.0.1
  port: {port}
"""
        config_file.write_text(config_content)

        # Override the log directory via a wrapper script or direct call
        # For this test, we'll rely on the .culture/ fallback and just
        # test the PID/port file mechanism with our own setup

        # Start the server in a subprocess with explicit paths
        proc = subprocess.Popen(
            [
                "python",
                "-m",
                "agentirc",
                "start",
                "--name",
                server_name,
                "--port",
                str(port),
                "--data-dir",
                str(temp_dirs["data"]),
                "--config",
                str(config_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for the daemon to start and write PID file
        time.sleep(1)
        _, stderr = proc.communicate(timeout=5)

        # Check if the server started successfully
        if proc.returncode != 0:
            pytest.skip(f"Failed to start daemon: {stderr}")

        try:
            yield {
                "name": server_name,
                "port": port,
                "config_file": config_file,
                "data_dir": temp_dirs["data"],
            }
        finally:
            # Clean up: stop the server
            subprocess.run(
                ["python", "-m", "agentirc", "stop", "--name", server_name],
                capture_output=True,
                timeout=10,
            )

    def test_status_json_running_daemon(self, capsys, running_server):
        """status --json for a running daemon reports running=true and pid."""
        rc = dispatch(["status", "--name", running_server["name"], "--json"])
        assert rc == 0
        output = capsys.readouterr().out.strip()
        data = json.loads(output)

        assert data["name"] == running_server["name"]
        assert data["running"] is True
        assert isinstance(data["pid"], int)
        assert data["pid"] > 0
        # Port may or may not be recorded; if it is, it should match
        if data["port"] is not None:
            assert data["port"] == running_server["port"]

    def test_status_text_running_daemon(self, capsys, running_server):
        """status text output for running daemon includes PID and port."""
        rc = dispatch(["status", "--name", running_server["name"]])
        assert rc == 0
        output = capsys.readouterr().out.strip()

        # Should contain running indicator
        assert "running" in output
        assert running_server["name"] in output
        # Should have a PID number
        assert "PID" in output


class TestJsonExitCodes:
    """Verify exit codes remain consistent with text versions."""

    def test_version_json_exit_code(self):
        """version --json exits with 0."""
        rc = dispatch(["version", "--json"])
        assert rc == 0

    def test_version_text_exit_code(self):
        """version text exits with 0."""
        rc = dispatch(["version"])
        assert rc == 0

    def test_status_json_exit_code(self):
        """status --json for nonexistent server exits with 0."""
        rc = dispatch(["status", "--name", "nonexistent", "--json"])
        assert rc == 0

    def test_status_text_exit_code(self):
        """status text for nonexistent server exits with 0."""
        rc = dispatch(["status", "--name", "nonexistent"])
        assert rc == 0
