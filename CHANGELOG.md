# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

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
