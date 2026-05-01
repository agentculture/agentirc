# AgentIRC Bootstrap Design

**Status:** Proposed
**Date:** 2026-04-30
**Owner:** Ori Nachum
**Receiving agent:** the agent working in this repo

## Summary

This repo (`agentirc`) is being bootstrapped from a server-core extraction out of the sibling project [`culture`](https://github.com/agentculture/culture). When this design is implemented, this repo will:

- Be a publishable Python package on PyPI under the **distribution name `agentirc-cli`** (with TestPyPI dev releases also published as `agentirc`).
- Expose a Python **import package named `agentirc`**.
- Ship **two CLI binaries**, `agentirc` and `agentirc-cli`, both backed by `agentirc.cli:main`.
- Carry the IRCd server core (RFC 2812 base + server-to-server linking + persistence + server-side skill plugins) that today lives at `../culture/culture/agentirc/`.

The first release is **`v9.0.0`** (not 0.1.0 — see "Versioning" below for why). Culture will pin against it as `agentirc-cli>=9.0,<10` and call into `agentirc.cli.dispatch(argv)` from its own `culture server` shim, so existing `culture server …` UX is preserved without culture re-implementing anything.

### Versioning

`agentirc-cli` starts at `9.0.0` rather than `0.1.0`. Culture previously squat-published `agentirc-cli==8.7.X.devN` to TestPyPI to hold the name during the bootstrap window; that squat has been disabled, but the 8.7.X versions remain on TestPyPI forever. PyPI's "Latest" pointer is semver-sorted, so anything below `8.7.1.dev410` would be permanently masked. Starting at `9.0.0` leapfrogs the squat so dev releases (`9.0.X.dev<run>`) actually show as the latest version. Real PyPI has no equivalent squat — culture only squatted on TestPyPI — but the 9.x.x line continues there for consistency.

## Naming (called out, because three names appear)

| Role | Name |
|---|---|
| PyPI distribution name | `agentirc-cli` on real PyPI; on TestPyPI, also `agentirc` (we claim the TestPyPI squat — not the real-PyPI one, which is not ours) |
| Python import package | `agentirc` |
| CLI binary | both `agentirc` and `agentirc-cli` (same entry point: `agentirc.cli:main`) |
| Repo path | `../agentirc` (i.e. this repo) |

## Goals

- A `pip install agentirc-cli` produces working `agentirc` and `agentirc-cli` binaries that can run an IRCd indistinguishable from today's `culture server start`.
- Public API surface is small, documented, and semver-stable: `agentirc.config`, `agentirc.cli`, `agentirc.protocol`. Everything else is internal.
- Zero on-disk migration for existing culture deployments — defaults match what culture uses today.
- Tests run under `pytest -n auto` in CI and pass on first release.
- `git grep -E '^(from|import) culture' agentirc/ tests/` returns nothing — no leaked imports back into culture.

## Non-Goals

- **Rewriting** any of the moved code. Files are copied as-is (with import-path adjustments only). Any improvements to ircd/server-link/stores happen in follow-up changes, not this bootstrap.
- **Editing culture.** This work touches only this repo. Culture's own cutover (deleting `culture/agentirc/`, adding the dependency, installing the passthrough shim) is a separate PR by the culture-side agent and is **not** part of this work.
- **Renaming on-disk artifacts.** Default config path stays `~/.culture/server.yaml`; sockets, log paths, and `culture-*` systemd unit names stay culture-named. agentirc-the-standalone-product carries that legacy in its defaults; non-culture users override via `--config`.
- **Preserving git history from culture.** First commit is a single synthetic "Initial import from culture@\<SHA>" with no cherry-picked history. Anyone wanting the historical context for a line uses `git log` in culture before the deletion SHA.
- **Publishing protocol/extensions docs externally.** They live in this repo; serving them is future work.

## Inputs (sources in culture, at the SHA the caller provides)

The culture-side agent will provide a specific culture commit SHA at copy time. Sources are read from `../culture/`. Do **not** modify culture; read-only access.

| Source | Destination |
|---|---|
| `../culture/culture/agentirc/ircd.py` | `agentirc/ircd.py` |
| `../culture/culture/agentirc/server_link.py` | `agentirc/server_link.py` |
| `../culture/culture/agentirc/channel.py` | `agentirc/channel.py` |
| `../culture/culture/agentirc/config.py` | `agentirc/config.py` |
| `../culture/culture/agentirc/events.py` | `agentirc/events.py` |
| `../culture/culture/agentirc/room_store.py` | `agentirc/room_store.py` |
| `../culture/culture/agentirc/thread_store.py` | `agentirc/thread_store.py` |
| `../culture/culture/agentirc/history_store.py` | `agentirc/history_store.py` |
| `../culture/culture/agentirc/rooms_util.py` | `agentirc/rooms_util.py` |
| `../culture/culture/agentirc/skill.py` | `agentirc/skill.py` |
| `../culture/culture/agentirc/skills/` | `agentirc/skills/` |
| `../culture/protocol/extensions/` | `protocol/extensions/` |
| Tests in `../culture/tests/` that import `culture.agentirc.*` and are not transport-focused | `tests/` (sort per "Test-suite migration" below) |

**Do NOT copy:**

- ~~`../culture/culture/agentirc/client.py` (stays in culture)~~ — reversed in PR-B2 (9.2.0). Vendored as `agentirc/client.py` because it only imports already-vendored support modules and the IRCd needs it at runtime to accept TCP clients.
- ~~`../culture/culture/agentirc/remote_client.py` (stays in culture)~~ — reversed in PR-B1 (9.1.0). Server-side ghost-client stub used by `server_link.py`; vendored as `agentirc/remote_client.py`. See PR-B1 commit history.
- `../culture/culture/agentirc/__main__.py` (replaced; see Tasks).
- Tests that are bot-fixture-coupled or genuinely cross-cutting (bot+IRCd in one process) stay in culture and get rewritten there to drive `agentirc serve` as a subprocess fixture.

## Repo layout (target)

```
agentirc/                          (repo root)
├── pyproject.toml                 # name = "agentirc-cli"; scripts: agentirc + agentirc-cli → agentirc.cli:main
├── CHANGELOG.md                   # starts at 9.0.0 (see Versioning)
├── README.md
├── LICENSE                        # already present
├── .pre-commit-config.yaml
├── agentirc/                      (Python import package)
│   ├── __init__.py
│   ├── __main__.py                # python -m agentirc → agentirc.cli:main
│   ├── cli.py                     # main(), dispatch(argv) — NEW
│   ├── protocol.py                # verb names, numerics, extension tags — NEW
│   ├── config.py                  # ServerConfig, LinkConfig, TelemetryConfig (public)
│   ├── ircd.py
│   ├── server_link.py
│   ├── channel.py
│   ├── events.py
│   ├── room_store.py
│   ├── thread_store.py
│   ├── history_store.py
│   ├── rooms_util.py
│   ├── skill.py
│   └── skills/
│       ├── __init__.py
│       ├── rooms.py
│       ├── threads.py
│       ├── history.py
│       └── icon.py
├── protocol/
│   └── extensions/                # protocol docs — agentirc owns its protocol now
├── tests/
└── docs/
    ├── api-stability.md           # public surface culture pins on
    ├── cli.md
    └── deployment.md
```

## CLI surface

`agentirc.cli` exposes:

- `main()` — entrypoint backing both the `agentirc` and `agentirc-cli` console scripts.
- `dispatch(argv: list[str]) -> int` — the exact function culture's shim will call. Same flag set, same exit codes, same output as the binary. (Culture's `culture server <verb> <args>` becomes `dispatch([<verb>, *<args>])`. It is a pure passthrough; culture does not parse, validate, or rename any flag.) Returns `int` on successful command dispatch; raises `SystemExit` on `--help`/`--version`/parse-errors per argparse convention. Culture's shim must catch `SystemExit` (or use `subprocess.run(["agentirc", ...])` instead of an in-process call).

Subcommands cover the full server lifecycle agentirc exposes. Some verbs match `culture server …` today (`start`, `stop`, `status`); the rest are agentirc-only additions. Culture's pure-passthrough shim only ever emits the verbs culture itself uses, so adding new verbs here doesn't break it — culture sees its existing verbs forwarded unchanged, while standalone agentirc users get the broader surface.

| Verb | Behavior | Culture analogue |
|---|---|---|
| `agentirc serve [--config PATH]` | Starts the IRCd in foreground. `--config` defaults to `~/.culture/server.yaml`. | None — agentirc-only. |
| `agentirc start [--name NAME]` | Starts as a managed background service (systemd / supervisor handoff). | `culture server start`. |
| `agentirc stop [--name NAME]` | Stops the managed service. | `culture server stop`. |
| `agentirc restart [--name NAME]` | Restart shortcut. | None — agentirc-only. |
| `agentirc status [--name NAME]` | Reports running state. | `culture server status`. |
| `agentirc link <peer> [...]` | Registers a mesh server-to-server link. | None — culture exposes linking as flags on `start` (`--link`, `--mesh-links`). |
| `agentirc logs [--name NAME] [-f]` | Tails service logs. | None — agentirc-only. |
| `agentirc version` | Prints agentirc version. | None — agentirc-only. |

Implementation source: extract from `../culture/culture/cli/server.py`. For verbs with a culture analogue, copy the dispatch logic and per-verb handlers verbatim (rewriting `from culture.agentirc.X` imports to `from agentirc.X`); preserve flags, defaults, exit codes, and output. For agentirc-only verbs, build minimal handlers consistent with the existing patterns in that file. The original file may have culture-specific glue (e.g., reading `~/.culture/server.yaml`) — keep that glue; it's part of the transparency contract.

Culture-side server-management verbs that exist today but are *not* migrated here (`default`, `rename`, `archive`, `unarchive`) stay in culture's own CLI; they manage culture's per-machine server registry, which is a culture concern and not part of the IRCd extraction.

### Default behaviors that preserve transparency

- `--config` defaults to `~/.culture/server.yaml`.
- Socket paths, log paths, systemd unit names: unchanged from current culture defaults.
- Exit codes and stderr formatting: inherited from the moved code (no rewrites in this bootstrap).

## Public API contract

The **only** modules culture (and any third-party consumer) is allowed to import:

| Module | Members | Stability |
|---|---|---|
| `agentirc.config` | `ServerConfig`, `LinkConfig`, `TelemetryConfig`, plus dataclass fields | Public, semver-tracked. Breaking changes require a major bump. |
| `agentirc.cli` | `main()`, `dispatch(argv) -> int` | Public, semver-tracked. |
| `agentirc.protocol` | Verb name constants, numeric reply codes, extension tag names | Public, semver-tracked. |

Everything else (`agentirc.ircd`, `agentirc.server_link`, `agentirc.channel`, the stores, the skills) is internal. Document this contract in `docs/api-stability.md`. Future refactors of internals must not change `agentirc.config`, `agentirc.cli`, or `agentirc.protocol` without a semver-major bump.

### `agentirc.protocol` — what to extract

`protocol.py` did not exist in culture; the protocol vocabulary was inlined as string literals across `ircd.py`, `server_link.py`, the skills, and `client.py`. PR-B2 (9.2.0) lands `agentirc/protocol.py` consolidating:

- IRC verb names (standard `PRIVMSG`/`JOIN`/`PART`/..., agentirc skill verbs `ROOMCREATE`/`ROOMMETA`/`THREAD`/..., S2S verbs `SJOIN`/`SMSG`/`STHREAD`/...).
- Numeric reply codes (re-exported from `agentirc._internal.protocol.replies`).
- IRCv3 / extension tag names (`TRACEPARENT_TAG`, `TRACESTATE_TAG`, `EVENT_TAG_TYPE`, `EVENT_TAG_DATA`).

Existing call sites in `ircd.py`, `server_link.py`, and the skills still use inline string literals — migrating them to `protocol.<NAME>` is intentionally out of scope. The goal of `agentirc.protocol` is a stable public surface for downstream consumers (notably culture once it pins `agentirc-cli`); a future PR may sweep call sites if the diff is worth it.

Wire-format quirks (`ROOMETAEND`, `ROOMETASET` typos; `ERR_NOSUCHCHANNEL` semantic misuse for "channel exists already"; `STHREAD` collapse of THREAD_CREATE/THREAD_MESSAGE) are preserved verbatim as constants with explanatory comments — fixing them in agentirc alone would silently break culture's clients/harnesses. They need coordinated cross-repo bumps.

## Tasks (ordered)

> **Status note (2026-05-01):**
>
> - **Shape A — package skeleton** ✅ (PR #2, `9.0.0`): Tasks 1, 6, and a stub form of 9.
> - **Shape B-1 — server-core extraction** ✅ (PR #3, `9.1.0`): Tasks 2, 4, 9 (runtime deps), partial 10. See the "Cite-don't-copy" subsection below.
> - **Shape B-2 — real CLI + `protocol.py` + `client.py`** ✅ (PR-B2, `9.2.0`): Tasks 5, 7, plus vendoring `culture.pidfile`, `culture.cli.shared` (subset), and `culture/agentirc/client.py`. The bootstrap spec previously said `client.py` "stays in culture"; that decision was reversed in PR-B2 because (a) `agentirc/ircd.py:580`'s runtime `from agentirc.client import Client` was a guaranteed `ImportError` without it, and (b) `client.py` only imports already-vendored support modules — no backend-SDK pull-through.
> - **Shape B-3 — test suite migration** ✅ (PR-B3, `9.3.0`): Task 8 + Task 13. 36 tests vendored from `culture@df50942` (~6.5kloc), 315 tests run under `pytest -n auto` in ~29s. `tests/conftest.py` adapted (paraphrase) to drop bot-loader sandboxing and bot-fixture definitions. Three telemetry/test_bot_*.py files plus `test_welcome_bot.py` stay in culture (BotManager-coupled). Bucket-C tests stay in culture indefinitely.
> - **Shape B-4 — bootstrap docs + YAML config loading** ✅ (PR-B4, `9.4.0`): Tasks 11–12. `docs/api-stability.md` (3 public modules + semver contract), `docs/cli.md` (verb table + flag reference + exit codes + YAML/CLI precedence + agentirc-vs-culture diff), `docs/deployment.md` (on-disk footprint, systemd `Type=simple`, container, standalone, federation, log rotation, coexistence, backup). Plus, beyond pure-docs scope: `ServerConfig.from_yaml(path)` classmethod and CLI handlers wired to overlay flags on YAML (precedence: CLI > YAML > built-in default), closing the acceptance-criterion gap *"`agentirc start --config <path>` behaves indistinguishably from `culture server start`"*. New runtime dep: `pyyaml>=6.0`. 13 new agentirc-native tests in `tests/test_config_loader.py`; total suite 331 tests in ~28s.
> - **Acceptance audit** ✅ (Task 14, post-PR-B4): walked all 11 §"Acceptance criteria" bullets against `main@5590256`. 9 of 11 pass directly; criteria 1–2 (real-PyPI install, TestPyPI dual-name install) closed by Task 17. Audit record: [`2026-05-01-task14-audit.md`](2026-05-01-task14-audit.md).
> - **Released** ✅ (Tasks 16–18, 2026-05-01): [`v9.4.0`](https://github.com/agentculture/agentirc/releases/tag/v9.4.0) tagged at `5590256` and pushed to origin. `agentirc-cli==9.4.0` is on real PyPI, verified end-to-end by `pip install agentirc-cli==9.4.0` in a clean venv plus a TCP `NICK`/`USER` handshake against `agentirc serve` returning `001 RPL_WELCOME` from the PyPI-installed binary. Culture-side cutover unblocked via [agentculture/culture#308](https://github.com/agentculture/culture/issues/308) (version + SHA + cutover-PR shape + coexistence guarantees). **Bootstrap complete.**
>
> Task 3 (`Copy protocol/extensions/ wholesale`) is dropped: that path doesn't exist in culture. Re-add only if/when culture creates it.

### Cite-don't-copy

The "no `culture` imports remain" invariant and the "files copy as-is, only import-path rewrites" rule were originally stated as if both could hold simultaneously. They cannot — `ircd.py`, `server_link.py`, `events.py`, and the skills also import from `culture.aio`, `culture.constants`, `culture.protocol`, `culture.telemetry`, and (lazily, inside `IRCd.start()`) from `culture.bots.{bot_manager, http_listener}`. Shape B-1 resolves this with the workspace's [`citation-cli`](https://github.com/OriNachum/citation-cli) tool:

- Each vendored culture file is copied into `agentirc/_internal/` (private namespace) or `agentirc/` (public, server-core), with `culture.X` import paths rewritten to the corresponding `agentirc._internal.X` or `agentirc.X`.
- Each file gets a `[tool.citation.packages.<name>.files."<path>"]` entry in `pyproject.toml` recording its source URL, sha256, and a `quote` / `paraphrase` / `synthesize` status reflecting how much it diverges from the original.
- `culture.bots.{bot_manager, http_listener}` are too coupled to backend SDKs to vendor as-is (would violate the dependency-boundary rule). They are `synthesize`-status: API-compatible no-op stubs in `agentirc/_internal/bots/` that culture replaces at runtime when wrapping an `IRCd`.
- `cite check` validates the manifest in CI.

1. **Create the package skeleton.** Add `agentirc/__init__.py`, `agentirc/__main__.py`, empty `agentirc/skills/__init__.py`, the `tests/` directory, the `docs/` directory.
2. **Copy server-core files** per the Inputs table. Do not modify the contents yet.
3. **Copy `protocol/extensions/`** wholesale.
4. **Rewrite imports** inside the new tree: `from culture.agentirc.X` → `from agentirc.X`. Run `git grep -E '^(from|import) culture' agentirc/ tests/` and confirm no matches before proceeding.
5. **Create `agentirc/cli.py`** by extracting server-lifecycle dispatch from `../culture/culture/cli/server.py`. Expose `main()` and `dispatch(argv) -> int`. For verbs with a culture analogue (`start`, `stop`, `status`), preserve the existing verb name, flags, defaults, exit codes, and output exactly. For agentirc-only verbs (`serve`, `restart`, `link`, `logs`, `version`), add minimal handlers in the same style. The default `--config` path is `~/.culture/server.yaml`.
6. **Create `agentirc/__main__.py`** so `python -m agentirc` works (delegates to `agentirc.cli:main`).
7. **Create `agentirc/protocol.py`** per "What to extract" above. Update `ircd.py` and other server files to import from it.
8. **Sort the migrated tests** per "Test-suite migration" below. Drop tests that genuinely belong in culture (transport-focused).
9. **Write `pyproject.toml`:**
   - `name = "agentirc-cli"`, `version = "9.0.0"`
   - `[project.scripts]` declares **both** `agentirc = "agentirc.cli:main"` and `agentirc-cli = "agentirc.cli:main"`.
   - Mirror culture's **dev** dep set only: pytest, pytest-asyncio, pytest-xdist, black, isort, flake8, pylint, bandit. (Take culture's `pyproject.toml` as a reference; copy the dev-dep group verbatim where applicable.)
   - **Do not** copy culture's runtime deps (`claude-agent-sdk`, `anthropic`, `mistune`, `aiohttp`, `textual`, `agex-cli`, `afi-cli`, opentelemetry, etc.) — those belong to culture's clients/UI, not the IRCd. Add only what the moved server-core files actually `import` (likely `pyyaml` for config, plus opentelemetry-api/sdk if `ircd.py` / `events.py` use it). **Never** depend on a backend SDK; **never** declare a `culture` console script.
10. **Mirror culture's pre-commit, CI, and version workflow.** Copy `.pre-commit-config.yaml`, GitHub Actions, and `/version-bump`/CHANGELOG conventions from culture. Start `CHANGELOG.md` at `9.0.0`.
11. **Write `docs/api-stability.md`** documenting the three public modules. Mark everything else internal.
12. **Write minimal `docs/cli.md` and `docs/deployment.md`** that describe the CLI verbs and on-disk footprint (point users at `~/.culture/server.yaml` for defaults, with override via `--config`).
13. **Run the test suite.** `pytest -n auto` must pass.
14. **Verify acceptance criteria** (see below).
15. **First commit.** Single synthetic commit, message: `Initial import from culture@<SHA>` (where `<SHA>` is the source culture commit ID provided by the caller).
16. **Tag `v9.0.0`** and push.
17. **Publish to PyPI.** Real PyPI on push to `main` is `agentirc-cli` only. TestPyPI on PRs publishes **both** `agentirc-cli` and `agentirc`: a follow-up edit to `.github/workflows/publish.yml` adds a second `uv build` round that rewrites `name = "agentirc-cli"` → `name = "agentirc"` in a copied `pyproject.toml` before building, and uploads that wheel alongside the `agentirc-cli` one. Trusted Publishing must be configured for both project names on TestPyPI.
18. **Report back** the published version and source SHA so the culture-side cutover PR can pin against it.

## Test-suite migration

Tests from culture are sorted into three buckets:

1. **Imports `culture.agentirc.{ircd,server_link,channel,...}` (server core)** → moves to `tests/` here.
2. **Imports `culture.agentirc.client` / `remote_client`** → moves to `tests/` here as of 9.2.0. Both files now live in agentirc.
3. **Imports `culture.bots.*` or other backend-coupled fixtures** → stays in culture and is rewritten there to drive `agentirc serve` as a subprocess fixture rather than importing `IRCd` directly. Do not copy here.

When in doubt, prefer moving tests *here* over leaving them in culture: this repo owns the IRCd, the client transport, and IRCd-internal tests should run in this repo's CI.

**Realised in PR-B3 (9.3.0):**

- 21 root server-core tests + 15 telemetry tests = 36 files (~6.5kloc) vendored verbatim with mechanical import rewrites (`culture.agentirc.X` → `agentirc.X`, `culture.protocol.message` → `agentirc._internal.protocol.message`, `culture.telemetry.X` → `agentirc._internal.telemetry.X`, `culture.agentirc.client` → `agentirc.client`).
- `tests/conftest.py` adapted as paraphrase: dropped the `_BOTS_DIR_*` `unittest.mock.patch` calls (no-op against agentirc's no-op `_internal/bots/` stubs) and the `server_with_bot` / `server_with_bots` fixtures (`culture.bots.*` is forbidden).
- `tests/telemetry/test_tracing.py` paraphrase: one `unittest.mock.patch` target rewritten from `"culture.telemetry.tracing.OTLPSpanExporter"` to `"agentirc._internal.telemetry.tracing.OTLPSpanExporter"`. OTEL service-name strings (`"culture.agentirc"`) and OTEL attribute keys (`"culture.s2s.*"`, `"culture.federation.peer"`, `"culture.dev/traceparent"`) preserved verbatim — they are public observability identifiers downstream consumers grep for.
- `tests/test_events_basic.py` paraphrase: `with patch("culture.bots.bot_manager.BOTS_DIR", …)` block removed (same dead-weight rationale as the conftest patches).
- `agentirc/server_link.py:_replay_event` parameter renamed from `_seq` (PR-B1's unused-arg compliance variant) back to `seq` to match the upstream signature culture's tests assume; signature carries a `# noqa: ARG002` to keep the linter quiet.
- **Stayed in culture:** `test_bot_event_dispatch_span.py`, `test_bot_run_span.py`, `test_metrics_bots.py` (need real `BotManager` for the bot-event path), `test_welcome_bot.py` (inspects `bot_manager.bots`), and the 57-file bucket-C surface (cli, console, daemon, clients, credentials, mesh_config). `tests/telemetry/conftest.py` not migrated — its only consumer was the deferred bot tests above.

## Acceptance criteria

- `pip install agentirc-cli==9.0.0` from PyPI produces working `agentirc` and `agentirc-cli` binaries on a clean venv (both pointing at `agentirc.cli:main`).
- `pip install agentirc==9.0.0.devN` from TestPyPI produces the same two binaries (the TestPyPI dual-name flow).
- The installed package has no runtime dependency on `claude-agent-sdk`, `anthropic`, or any backend SDK, and does not install a `culture` console script.
- `agentirc start --config ~/.culture/server.yaml` behaves indistinguishably from today's `culture server start` (same accepting socket, same log output, same systemd integration).
- `agentirc serve --config ~/.culture/server.yaml` runs the same IRCd in the foreground (no daemonization). No culture analogue — this is the new standalone-friendly entry point.
- `agentirc.config.LinkConfig`, `agentirc.config.TelemetryConfig`, `agentirc.cli.dispatch`, and `agentirc.protocol.*` are importable from a clean Python session.
- All tests in `tests/` pass under `pytest -n auto`.
- `git grep -E '^(from|import) culture' agentirc/ tests/` returns nothing.
- `agentirc --help` lists every verb in the CLI surface table above.
- `docs/api-stability.md` exists and names the three public modules.
- `pyproject.toml` declares `name = "agentirc-cli"` and **both** `agentirc` and `agentirc-cli` console scripts.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Hidden coupling: a server-core file imports `culture.X` (not `culture.agentirc.X`) and we miss it. | Step 4 of Tasks runs `git grep` for any `culture` import. CI must enforce this with a lint step on every PR. |
| `agentirc.protocol` extraction misses a string literal. | After extraction, grep server files for IRC verb regexes (e.g. `\b(JOIN|PART|PRIVMSG|MODE|...)\b` in string contexts) to spot remaining literals. |
| Default config path `~/.culture/server.yaml` confuses standalone (non-culture) users. | Document `--config` override clearly in `docs/cli.md` and `docs/deployment.md`. The default is a transparency choice for culture continuity, not a permanent constraint. |
| First PyPI release is broken; culture's cutover PR can't merge. | Patches go out as `0.1.1`, `0.1.2`. Culture's PR can pin to a specific known-good `==0.1.X` until `0.1` is stable. |
| Test bucket sorting (server vs transport) is wrong. | When unsure, put tests here. Culture's reviewer will notice if a transport test landed in the wrong repo and the fix is a follow-up move. |

## Out of scope (deferred to follow-ups, not this bootstrap)

- Refactoring the IRCd, stores, or skills beyond import-path edits.
- Renaming on-disk artifacts to non-culture names.
- Adding new protocol extensions.
- Splitting `client.py` into its own distribution (e.g., `agentirc-client`).
- A public docs site for `protocol/extensions/`.

## Coordination with culture

- This bootstrap is **Track B** in culture's spec. **Track A** (culture-side cutover PR) is owned by the culture-side agent and depends on this Track B completing first.
- After this work merges and `agentirc-cli==9.0.0` is on PyPI, report back the version + source SHA. The culture-side agent then opens its own PR to add the dependency, delete `culture/agentirc/`, install the `culture server` passthrough, and `/version-bump major`.
- All-backends rule (a culture convention): changes that cross the agentirc / culture-transport boundary must be reflected across all four backends in culture (`claude`, `codex`, `copilot`, `acp`). Not your concern for this bootstrap; flagged here so future cross-repo changes account for it.

## Reference

The culture-side spec for this split lives at `../culture/docs/superpowers/specs/2026-04-30-agentirc-extraction-design.md`. This document (the agentirc bootstrap spec) is intentionally self-contained — you should not need to read the culture-side spec to act on this work. The culture spec is provided only for context if you want to understand why a decision was made.
