# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state: bootstrap functionally + docs complete (9.4.0); release ceremony remains

This repo is the agentirc server-core extraction out of the sibling project [`culture`](https://github.com/OriNachum/culture). As of 9.4.0:

- **Server-core** (`agentirc/{ircd,server_link,channel,events,skill,remote_client,…}.py`, `agentirc/skills/{rooms,threads,history,icon}.py`) — vendored from `culture@df50942` via the `cite-don't-copy` pattern (see `[tool.citation]` in `pyproject.toml`).
- **Client transport** (`agentirc/client.py`) — vendored from `culture/agentirc/client.py` in PR-B2.
- **Public CLI** (`agentirc/cli.py`) — real verb dispatch extracted from `culture/cli/server.py`. Verbs: `serve` (foreground, no PID; for systemd `Type=simple` and containers), `start`/`stop`/`status` (lifecycle), `restart`, `link` (peer-spec validator), `logs` (cat / tail of `~/.culture/logs/server-<name>.log`), `version`. Since 9.4.0, `serve`/`start`/`restart` overlay CLI flags on `--config` YAML (precedence: CLI > YAML > built-in default).
- **Public config** (`agentirc/config.py`) — `ServerConfig`, `LinkConfig`, `TelemetryConfig` dataclasses plus the `ServerConfig.from_yaml(path)` classmethod (added 9.4.0). Recognises `server`/`telemetry`/`links`/`webhook_port`/`data_dir`/`system_bots` keys; silently ignores culture-only keys (`supervisor`, `agents`, `buffer_size`, etc.) so the same `~/.culture/server.yaml` can drive both daemons.
- **Public protocol** (`agentirc/protocol.py`) — verb name constants, numerics, IRCv3 tag names. Wire-format quirks (`ROOMETAEND`, `ROOMETASET` typos, `ERR_NOSUCHCHANNEL` semantic misuse, `STHREAD` verb collapse) preserved verbatim — they need coordinated cross-repo bumps to fix.
- **Test suite** (PR-B3, 9.3.0; +13 in 9.4.0) — 36 tests vendored from `culture@df50942` (~6.5kloc) plus 13 new agentirc-native tests in `tests/test_config_loader.py`. 328 tests run under `pytest -n auto` in ~28s on default workers. Three telemetry tests (`test_bot_event_dispatch_span`, `test_bot_run_span`, `test_metrics_bots`) and `test_welcome_bot` stay in culture because they depend on the real `BotManager`.
- **Internal support** (`agentirc/_internal/`) — `aio`, `constants`, `protocol/`, `telemetry/`, `virtual_client`, `pidfile`, `cli_shared/`, `bots/` stubs.
- **Bootstrap docs** (PR-B4, 9.4.0) — `docs/api-stability.md` (3 public modules + semver contract), `docs/cli.md` (verb table, flag reference, exit codes, YAML/CLI precedence, agentirc-vs-culture diff table), `docs/deployment.md` (on-disk footprint, systemd `Type=simple` example, container deployment, multi-host federation, log rotation, coexistence with culture, backup).

End-to-end verified: `agentirc start --port <p>` boots a real IRCd, TCP NICK/USER handshake returns `001 RPL_WELCOME`, `agentirc stop` shuts cleanly. `agentirc serve --config server.yaml --port 9999` correctly overlays CLI flag on YAML.

What is **not** done yet:
- **Acceptance-criteria spot-check** (Task 14 in the bootstrap spec) — read-only audit to confirm every bullet in §"Acceptance criteria" is ✅ before tagging.
- **Release ceremony** (Tasks 16–18) — tag `v9.4.0`, the existing `publish.yml` CI pushes to PyPI on push to `main`, then report version + source SHA back so culture's cutover PR can pin against it.
- **Cross-repo wire-format fixes (Track A)** — `ROOMETAEND`/`ROOMETASET` typos, `ERR_NOSUCHCHANNEL` overload, `STHREAD` collapse. Each requires culture-side change first then agentirc bump.
- **Steward backport** — port the 9.3.0 `pr-sonar.sh` + `workflow.sh` SonarCloud wiring back to the steward skills repo.

Read the bootstrap spec at `docs/superpowers/specs/2026-04-30-bootstrap-design.md` for the full plan; it is the operative source of truth and is intentionally self-contained. The culture-side counterpart spec is at `../culture/docs/superpowers/specs/2026-04-30-agentirc-extraction-design.md` — not normally needed, but explains *why* if a decision looks arbitrary.

### Cite-don't-copy

Vendored culture code is tracked under `[tool.citation]` in `pyproject.toml` using the workspace's [`citation-cli`](https://github.com/OriNachum/citation-cli) tool. Each vendored file has a `quote` (verbatim copy), `paraphrase` (copied with import rewrites), or `synthesize` (rewritten as agentirc-native) status. Run `cite check` to verify integrity. When pulling new culture changes, update the citation entries' source URLs and sha256s — the manifest is the provenance ledger.

## Three names, one project

There are three different names in play. Don't conflate them:

| Role | Name |
|---|---|
| PyPI distribution | `agentirc-cli` on real PyPI; on TestPyPI, also `agentirc` (we claim the TestPyPI squat — not the real-PyPI one) |
| Python import package | `agentirc` |
| CLI binary | both `agentirc` and `agentirc-cli` (same entry point: `agentirc.cli:main`) |

`pyproject.toml` declares `name = "agentirc-cli"` and `[project.scripts]` with **both** `agentirc = "agentirc.cli:main"` and `agentirc-cli = "agentirc.cli:main"`. For TestPyPI dev releases, `publish.yml` will additionally build with the dist name temporarily rewritten to `agentirc` and upload that wheel too — real PyPI on push to `main` stays `agentirc-cli` only.

## What lives here vs. in culture

- **Server-core (here):** `ircd.py`, `server_link.py`, `channel.py`, `config.py`, `events.py`, the stores (`room_store`, `thread_store`, `history_store`), `rooms_util.py`, `skill.py`, `remote_client.py`, and the `skills/` directory (`rooms`, `threads`, `history`, `icon`).
- **Client transport (here, since 9.2.0):** `client.py` — vendored in PR-B2. Pre-9.2 the bootstrap spec said "stays in culture"; that assumption broke down once we found `client.py` only imports already-vendored modules and that the IRCd needs it at runtime to accept TCP clients.
- **Internal support (here):** `agentirc/_internal/{aio, constants, protocol/, telemetry/, virtual_client, pidfile, cli_shared/}` plus `bots/` no-op stubs (the real `culture.bots.*` depends on backend SDKs forbidden by agentirc's dependency boundary; culture replaces the stubs at runtime when wrapping an IRCd).
- **Stays in culture:** `culture.bots.*` (the real bot manager), `culture.config` / `culture.bots.config` (agent-manifest concerns), `culture.cli.shared.{ipc,display,formatting,process}` (CLI ergonomics agentirc doesn't need), `culture.credentials` / `culture.mesh_config` (OS-keyring + mesh.yaml).

When migrating tests, the rule is: pure server tests come here, transport tests **also** come here now that we own `client.py`, mixed tests stay in culture and get rewritten to drive `agentirc serve` as a subprocess fixture rather than importing `IRCd` directly. When unsure, **prefer copying the test here** — this repo owns the IRCd and the client transport.

### Test layout (since 9.3.0)

- `tests/conftest.py` — paraphrase of culture's conftest. Drops the `_BOTS_DIR_*` `unittest.mock.patch` calls (no-op against agentirc's bot stubs) and the `server_with_bot` / `server_with_bots` fixtures. Keeps `IRCTestClient`, the IRCd lifecycle fixtures (`server`, `linked_servers`, `make_client*`, `server_welcome_disabled`), telemetry fixtures (`tracing_exporter`, `metrics_reader`, `audit_dir`), and the `TEST_LINK_PASSWORD` constant.
- `tests/test_*.py` — 21 server-core tests + the agentirc-native `test_cli.py`. Cover IRC lifecycle, channels, rooms, threads, history, federation, events, mentions, the icon skill.
- `tests/telemetry/test_*.py` — 15 telemetry integration tests covering audit JSONL emission, OTLP span injection on dispatch, S2S relay spans, metrics initialization, trace-context propagation. Uses two private helper modules `_fakes.py` (FakeWriter etc.) and `_metrics_helpers.py`. No `tests/telemetry/conftest.py` (the upstream one was bot-coupled).
- Tests left in culture: `test_bot_event_dispatch_span.py`, `test_bot_run_span.py`, `test_metrics_bots.py`, `test_welcome_bot.py` (bot-manager-coupled), plus the entire bucket-C surface (cli, console, daemon, clients, credentials).

## Public API contract (semver-tracked)

Only three modules are public. Everything else is internal and may be refactored without a major bump.

| Module | Members |
|---|---|
| `agentirc.config` | `ServerConfig`, `LinkConfig`, `TelemetryConfig` |
| `agentirc.cli` | `main()`, `dispatch(argv) -> int` |
| `agentirc.protocol` | verb name constants, numeric reply codes, extension tag names |

`agentirc.cli.dispatch(argv)` is the function `culture`'s `culture server` shim calls — it must accept the exact same flag set, exit codes, and stderr formatting that `culture server` produces today. Do not "improve" CLI ergonomics during the bootstrap; that breaks the transparency contract culture relies on. `dispatch()` returns `int` on successful command dispatch and lets argparse's `SystemExit` propagate on `--help`/`--version`/parse-errors per Python convention; in-process callers (i.e. culture's shim) must catch `SystemExit` themselves or use `subprocess`.

Two intentional, additive deltas vs. culture's CLI:

- `agentirc status` prints `Server 'X': running (PID N, port P)` when a port file is present — culture only prints `(PID N)`. Strictly a superset; culture's shim relies on exit codes, not output parsing.
- `agentirc start` no longer accepts `--mesh-config` (depends on `culture.credentials` and `culture.mesh_config`, out of agentirc's scope). Use `--link name:host:port:password[:trust]` flags instead.

## Defaults preserve culture continuity

- Default `--config` path: `~/.culture/server.yaml` (yes, `.culture/`, not `.agentirc/`).
- Socket paths, log paths, systemd unit names: unchanged from culture.
- Standalone (non-culture) users override via `--config`.

Do not rename on-disk artifacts during the bootstrap. That is explicitly out of scope.

## Hard invariants

- **No imports back into culture.** After the bootstrap, `git grep -E '^(from|import) culture' agentirc/ tests/` must return nothing. CI should enforce this.
- **Cite-don't-copy adaptation only.** Files copy from `../culture/` with import paths rewritten and minimal adaptation where the dependency boundary forces it (e.g. `culture.bots.{bot_manager,http_listener}` are stubbed to no-ops in `agentirc/_internal/bots/`). All adaptations are recorded in `[tool.citation]` with status `paraphrase` or `synthesize`. Improvements beyond what's needed for the dependency boundary ship in follow-up PRs.
- **Single synthetic first commit.** Message format: `Initial import from culture@<SHA>` where `<SHA>` is the culture commit ID the caller provides. No cherry-picked history.
- **No backend SDKs, no `culture` console script.** agentirc must not depend on `claude-agent-sdk`, `anthropic`, `agex-cli`, `afi-cli`, `github-copilot-sdk`, or any other agent/backend SDK, and must not declare a `culture` console script. Those are culture concerns — agent backends and the `culture` command live in `../culture` and stay there.

## Common commands

```bash
# Dev setup
uv venv && uv pip install -e ".[dev]"

# Tests (315 collected, ~29s on default workers)
pytest -n auto

# Run a single test
pytest tests/path/to/test_file.py::test_name -v

# CLI smoke
agentirc --help
agentirc-cli --help          # alias of agentirc
agentirc version             # prints "agentirc 9.3.0"
python -m agentirc version   # equivalent

# Lifecycle (functional since 9.2.0)
agentirc serve --config ~/.culture/server.yaml          # foreground, no PID
agentirc start --name spark --host 127.0.0.1 --port 6667  # daemonize
agentirc status --name spark
agentirc stop --name spark
agentirc logs --name spark -f                            # tail -f the daemon log
agentirc link 'peer1:host:6667:secret:full'              # parse + validate spec
```

CLI verbs: `serve`, `start`, `stop`, `restart`, `status`, `link`, `logs`, `version`. Of these, only `start`, `stop`, `status` have a `culture server …` analogue today; the rest are agentirc-only additions. Culture's pure-passthrough shim only ever emits its existing verbs, so the additions don't break it.

## Tooling conventions (mirror culture)

Copy these from culture rather than inventing fresh:

- `.pre-commit-config.yaml`
- Dev-dep set: `pytest`, `pytest-asyncio`, `pytest-xdist`, `black`, `isort`, `flake8`, `pylint`, `bandit`
- `/version-bump` workflow and `CHANGELOG.md` style. Start the changelog at `9.0.0` (see "Versioning" below).

GitHub Actions workflows are already in `.github/workflows/`:

- `tests.yml` — pytest with coverage on `agentirc/`, plus a version-bump check that nags via PR comment if `pyproject.toml` matches main.
- `publish.yml` — Trusted-Publishing release of `agentirc-cli` to TestPyPI on PRs (`9.0.X.dev<run>`) and to PyPI on push to main. The TestPyPI step also publishes the same wheel under the `agentirc` distribution name.

## Versioning

`agentirc-cli` starts at **`9.0.0`**, not `0.1.0`. Reason: culture previously squat-published `agentirc-cli==8.7.X.devN` to TestPyPI (now disabled). PyPI sorts by semver, so dev releases below `8.7.1.dev410` would be permanently masked under "Latest." Starting at 9.0.0 leapfrogs the squat so our dev releases (`9.0.X.dev<run>`) are the visible "Latest." Real PyPI is independent — no squat there from culture, but the 9.x.x line continues for consistency.

`__version__` is read from installed-dist metadata via `importlib.metadata.version("agentirc-cli")`; `pyproject.toml` is the single source of truth. Bump with `/version-bump patch|minor|major` once the version-bump skill is vendored (steward has it; see `docs/steward/onboarding.md`).

Both jobs are gated on `hashFiles('pyproject.toml') != ''` so they no-op cleanly during the pre-bootstrap window.

Markdown linting follows the user's global markdownlint config (no committed `.markdownlint-cli2.yaml` in this repo yet).

## Skills (vendored from steward)

Per `docs/steward/onboarding.md`, agent skills live under `.claude/skills/<name>/`, vendored cite-don't-import from `../steward/.claude/skills/<name>/`. Already vendored:

- `pr-review` — branch / commit / push / PR / wait for Qodo+Copilot / triage / fix / reply / resolve. Includes a portability lint (run via `.claude/skills/pr-review/scripts/workflow.sh lint`) and an alignment-delta check for sibling-project drift.

Per-machine paths for these skills go in `.claude/skills.local.yaml` (gitignored). The committed `.claude/skills.local.yaml.example` documents the schema. When upstream skills change, re-sync explicitly — there is no auto-sync.

## Coordination with culture

- This is **Track B** of a two-repo split. **Track A** is culture-side and is the culture agent's job — do not edit culture from this repo.
- After `agentirc-cli==9.0.0` is on PyPI, report the published version and source SHA back so culture's cutover PR can pin against it.
- Culture's *all-backends rule* (a feature added to one of `claude`/`codex`/`copilot`/`acp` must be propagated to the others) does **not** apply inside this repo — agentirc has no backends. It is mentioned in the spec only so future cross-repo changes account for it.

## Acceptance criteria for the bootstrap

The full list lives in §"Acceptance criteria" of the bootstrap spec. The non-obvious ones:

- `pip install agentirc-cli==9.3.0` on a clean venv produces working `agentirc` *and* `agentirc-cli` binaries (both pointing at `agentirc.cli:main`). ✅ since 9.0.0.
- `agentirc serve` is byte-indistinguishable from `culture server start` (same socket, same logs, same systemd integration). ✅ since 9.2.0.
- `agentirc.config.{ServerConfig, LinkConfig, TelemetryConfig}`, `agentirc.cli.{main, dispatch}`, `agentirc.protocol.*` all import from a clean Python session. ✅ since 9.2.0.
- `pytest -n auto` passes for the migrated suite (315 tests, ~29s). ✅ since 9.3.0.
- `docs/api-stability.md` names the three public modules. ⏳ pending PR-B4.
