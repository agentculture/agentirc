# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [9.2.0] - 2026-05-01

### Added

- `agentirc/protocol.py` — public, semver-tracked module consolidating
  IRC verb names, numeric reply codes (re-exported from
  `_internal.protocol.replies`), and IRCv3 / agentirc tag names. Wire-
  format quirks (`ROOMETAEND`, `ROOMETASET` typos; `ERR_NOSUCHCHANNEL`
  semantic misuse; `STHREAD` verb collapse) are preserved verbatim with
  explanatory comments — they require coordinated cross-repo bumps to
  fix.
- `agentirc/client.py` — IRC client transport vendored from
  `culture/agentirc/client.py` at SHA `df50942`. Body unchanged; only
  imports rewritten. The bootstrap spec originally said this would
  "stay in culture" but its dependency surface (already-vendored
  `_internal` support modules + opentelemetry) made vendoring the
  cleaner path. Without it, `agentirc/ircd.py:580`'s runtime
  `from agentirc.client import Client` raised `ImportError` on the
  first TCP IRC connection.
- Real `agentirc/cli.py` — verb dispatch extracted from
  `culture/cli/server.py`. New verbs: `serve` (foreground, no PID;
  for systemd `Type=simple` and containers), `restart`, `link`
  (peer-spec validator), `logs` (cat / tail of `~/.culture/logs/server-
  <name>.log`). Existing verbs `start`/`stop`/`status` reuse culture's
  proven daemonize / `_wait_for_port` / `_wait_for_graceful_stop` /
  `_force_kill` helpers.
- Internal support modules:
  - `agentirc/_internal/pidfile.py` — PID/port file management.
    `is_managed_process()` recognizes both `culture` and
    `agentirc`/`agentirc-cli` argv tokens; `is_culture_process` is
    preserved as a thin alias.
  - `agentirc/_internal/cli_shared/{constants,mesh}.py` — minimal
    subset of `culture/cli/shared`. Keeps `DEFAULT_CONFIG`, `LOG_DIR`,
    `culture_runtime_dir()`, `parse_link()`. Drops everything that
    touched `culture.bots.config`, `culture.credentials`,
    `culture.mesh_config`.
- Citations recorded in `[tool.citation]`: `culture-pidfile`,
  `culture-cli-shared`, `culture-client`, `culture-cli-server`.

### Changed

- Default server name changed from `culture` to `agentirc` in
  `agentirc.cli`. PID/port files at `~/.culture/pids/server-<name>.{pid,
  port}` keep their existing layout per the "Defaults preserve culture
  continuity" rule, but the default fallback name no longer collides
  with culture's daemon when both run on the same host.
- Dropped culture-only verbs (`default`, `rename`, `archive`,
  `unarchive`) from agentirc's CLI surface — they manage culture's
  agent manifest, which agentirc does not own.
- Dropped `--mesh-config` from `agentirc start` — depends on
  `culture.credentials` / `culture.mesh_config` (out of scope).

### Notes

- End-to-end smoke verified: `agentirc start --port 16667` boots,
  TCP NICK/USER handshake returns `001 RPL_WELCOME` from a real
  `IRCd`, `agentirc stop` shuts cleanly. `agentirc serve` is now
  byte-indistinguishable from `culture server start` for the lifecycle
  contract culture's shim relies on.
- Test suite migration (PR-B3) is the only remaining bootstrap slice.

## [9.1.0] - 2026-04-30

### Added

- Server-core vendored from `culture` at SHA `df50942`. The `agentirc`
  package now contains the IRCd (`ircd.py`), server-to-server linking
  (`server_link.py`), channel/event/store/skill modules, `remote_client.py`
  (peer-server ghost client), and the four built-in skills
  (`skills/{rooms,threads,history,icon}.py`).
- Internal vendored support modules under `agentirc/_internal/`:
  - `aio` (`maybe_await`)
  - `constants` (system user/channel constants)
  - `protocol/` (IRC `Message` and numeric `replies`)
  - `telemetry/` (OpenTelemetry audit/tracing/metrics — full subpackage)
  - `virtual_client` (`VirtualClient` for in-process bot integration)
  - `bots/{bot_manager,http_listener}` (no-op stubs; culture replaces
    these at runtime when wrapping an `IRCd`)
- `[tool.citation]` block in `pyproject.toml` enumerating every vendored
  file with a quote/paraphrase/synthesize status, source URL, and
  sha256, validated by `cite check`.
- Runtime dependencies: `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-grpc` (all `>=1.22`).
- Dev dependency: `citation-cli` (provides the `cite` console script).

### Changed

- Bootstrap spec deviation: `remote_client.py` was originally listed as
  "do not copy" but turned out to be server-side (used by `server_link`
  and `virtual_client` for peer-server users in channel member lists).
  Vendored as public `agentirc/remote_client.py`. See commit `8b4a6d8`.

### Notes

- `agentirc/cli.py` still ships only `version`; the `serve|start|stop|
  restart|status|link|logs` lifecycle verbs remain stubs. The real CLI
  is the next slice (PR-B2).
- Tests are not migrated yet (PR-B3).

## [9.0.0] - 2026-04-30

### Added

- Initial bootstrap of `agentirc-cli` as an installable Python package.
- Skeleton `agentirc/{__init__,__main__,cli}.py` with `version` verb
  wired up and lifecycle verbs (`serve|start|stop|restart|status|link|
  logs`) as stubs.
- Console scripts: both `agentirc` and `agentirc-cli` map to
  `agentirc.cli:main`.
- Major version starts at `9.0.0` to leapfrog the
  `agentirc-cli==8.7.X.devN` squat that culture previously published to
  TestPyPI, so dev releases sort as the actual "Latest".
