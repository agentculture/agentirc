# AgentIRC Bootstrap Design

**Status:** Proposed
**Date:** 2026-04-30
**Owner:** Ori Nachum
**Receiving agent:** the agent working in this repo

## Summary

This repo (`agentirc`) is being bootstrapped from a server-core extraction out of the sibling project [`culture`](https://github.com/OriNachum/culture). When this design is implemented, this repo will:

- Be a publishable Python package on PyPI under the **distribution name `agentirc-cli`**.
- Expose a Python **import package named `agentirc`**.
- Ship a **CLI binary named `agentirc`**.
- Carry the IRCd server core (RFC 2812 base + server-to-server linking + persistence + server-side skill plugins) that today lives at `../culture/culture/agentirc/`.

The first release is `v0.1.0`. Culture will pin against it as `agentirc-cli>=0.1,<0.2` and call into `agentirc.cli.dispatch(argv)` from its own `culture server` shim, so existing `culture server …` UX is preserved without culture re-implementing anything.

## Naming (called out, because three names appear)

| Role | Name |
|---|---|
| PyPI distribution name | `agentirc-cli` (TestPyPI also has the squatted `agentirc`; we use `agentirc-cli` everywhere) |
| Python import package | `agentirc` |
| CLI binary | `agentirc` |
| Repo path | `../agentirc` (i.e. this repo) |

## Goals

- A `pip install agentirc-cli` produces a working `agentirc` binary that can run an IRCd indistinguishable from today's `culture server start`.
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

- `../culture/culture/agentirc/client.py` (stays in culture)
- `../culture/culture/agentirc/remote_client.py` (stays in culture)
- `../culture/culture/agentirc/__main__.py` (replaced; see Tasks)
- Any test that exercises the IRC *client* transport rather than the server. Those stay in culture.

## Repo layout (target)

```
agentirc/                          (repo root)
├── pyproject.toml                 # name = "agentirc-cli"; scripts: agentirc = "agentirc.cli:main"
├── CHANGELOG.md                   # starts at 0.1.0
├── README.md
├── LICENSE                        # already present
├── .pre-commit-config.yaml
├── agentirc/                      (Python import package)
│   ├── __init__.py
│   ├── __main__.py                # python -m agentirc → agentirc.cli:main
│   ├── cli.py                     # main(), dispatch(argv) — NEW
│   ├── protocol.py                # verb names, numerics, extension tags — NEW
│   ├── config.py                  # ServerConfig, LinkConfig, PeerSpec (public)
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

- `main()` — entrypoint for the `agentirc` console script.
- `dispatch(argv: list[str]) -> int` — the exact function culture's shim will call. Same flag set, same exit codes, same output as the binary. (Culture's `culture server <verb> <args>` becomes `dispatch([<verb>, *<args>])`. It is a pure passthrough; culture does not parse, validate, or rename any flag.)

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
| `agentirc.config` | `ServerConfig`, `LinkConfig`, `PeerSpec`, plus dataclass fields | Public, semver-tracked. Breaking changes require a major bump. |
| `agentirc.cli` | `main()`, `dispatch(argv) -> int` | Public, semver-tracked. |
| `agentirc.protocol` | Verb name constants, numeric reply codes, extension tag names | Public, semver-tracked. |

Everything else (`agentirc.ircd`, `agentirc.server_link`, `agentirc.channel`, the stores, the skills) is internal. Document this contract in `docs/api-stability.md`. Future refactors of internals must not change `agentirc.config`, `agentirc.cli`, or `agentirc.protocol` without a semver-major bump.

### `agentirc.protocol` — what to extract

`protocol.py` does **not** exist in culture today. The protocol vocabulary is currently inlined as string literals in two places:

- `../culture/culture/agentirc/ircd.py` — server side
- `../culture/culture/agentirc/client.py` — client side (read-only reference; do not copy this file)

Extract from both into a single `agentirc/protocol.py`:

- IRC verb names (e.g., `PRIVMSG`, `JOIN`, `PART`, `MODE`, plus Culture extensions like `THREAD`, `ROOM`, history-sync verbs).
- Numeric reply codes used by the server.
- Extension tag names used in capability negotiation.

Update `agentirc/ircd.py` and any other server file to import from `agentirc.protocol` instead of using string literals. Culture's `client.py` will be updated by the culture-side agent to do the same against this module.

## Tasks (ordered)

1. **Create the package skeleton.** Add `agentirc/__init__.py`, `agentirc/__main__.py`, empty `agentirc/skills/__init__.py`, the `tests/` directory, the `docs/` directory.
2. **Copy server-core files** per the Inputs table. Do not modify the contents yet.
3. **Copy `protocol/extensions/`** wholesale.
4. **Rewrite imports** inside the new tree: `from culture.agentirc.X` → `from agentirc.X`. Run `git grep -E '^(from|import) culture' agentirc/ tests/` and confirm no matches before proceeding.
5. **Create `agentirc/cli.py`** by extracting server-lifecycle dispatch from `../culture/culture/cli/server.py`. Expose `main()` and `dispatch(argv) -> int`. For verbs with a culture analogue (`start`, `stop`, `status`), preserve the existing verb name, flags, defaults, exit codes, and output exactly. For agentirc-only verbs (`serve`, `restart`, `link`, `logs`, `version`), add minimal handlers in the same style. The default `--config` path is `~/.culture/server.yaml`.
6. **Create `agentirc/__main__.py`** so `python -m agentirc` works (delegates to `agentirc.cli:main`).
7. **Create `agentirc/protocol.py`** per "What to extract" above. Update `ircd.py` and other server files to import from it.
8. **Sort the migrated tests** per "Test-suite migration" below. Drop tests that genuinely belong in culture (transport-focused).
9. **Write `pyproject.toml`:**
   - `name = "agentirc-cli"`, `version = "0.1.0"`
   - `[project.scripts] agentirc = "agentirc.cli:main"`
   - Mirror culture's runtime + dev dep set: pytest, pytest-asyncio, pytest-xdist, black, isort, flake8, pylint, bandit. (Take culture's `pyproject.toml` as a reference; copy the dev-dep group verbatim where applicable.)
10. **Mirror culture's pre-commit, CI, and version workflow.** Copy `.pre-commit-config.yaml`, GitHub Actions, and `/version-bump`/CHANGELOG conventions from culture. Start `CHANGELOG.md` at `0.1.0`.
11. **Write `docs/api-stability.md`** documenting the three public modules. Mark everything else internal.
12. **Write minimal `docs/cli.md` and `docs/deployment.md`** that describe the CLI verbs and on-disk footprint (point users at `~/.culture/server.yaml` for defaults, with override via `--config`).
13. **Run the test suite.** `pytest -n auto` must pass.
14. **Verify acceptance criteria** (see below).
15. **First commit.** Single synthetic commit, message: `Initial import from culture@<SHA>` (where `<SHA>` is the source culture commit ID provided by the caller).
16. **Tag `v0.1.0`** and push.
17. **Publish to PyPI as `agentirc-cli`.** Uses the publish workflow created in Task 10 (mirrored from culture's Trusted-Publishing setup); no additional PyPI configuration in this repo before that task.
18. **Report back** the published version and source SHA so the culture-side cutover PR can pin against it.

## Test-suite migration

Tests from culture are sorted into three buckets:

1. **Imports `culture.agentirc.X` only (server core)** → moves to `tests/` here.
2. **Imports `culture.agentirc.client` / `remote_client` only** → stays in culture (transport-focused). Do not copy.
3. **Imports both** → if the test is genuinely cross-cutting (a bot connecting to an IRCd in the same process), it stays in culture and is rewritten there to use `agentirc serve` as a subprocess fixture rather than importing `IRCd` directly. Do not copy here unless it's a pure server test that happens to construct a transport object only as a test helper — in which case adapt the test to spin its own helper.

When in doubt, prefer moving tests *here* over leaving them in culture: this repo owns the IRCd, and IRCd-internal tests should run in this repo's CI.

## Acceptance criteria

- `pip install agentirc-cli==0.1.0` from PyPI produces a working `agentirc` binary on a clean venv.
- `agentirc start --config ~/.culture/server.yaml` behaves indistinguishably from today's `culture server start` (same accepting socket, same log output, same systemd integration).
- `agentirc serve --config ~/.culture/server.yaml` runs the same IRCd in the foreground (no daemonization). No culture analogue — this is the new standalone-friendly entry point.
- `agentirc.config.LinkConfig`, `agentirc.config.PeerSpec`, `agentirc.cli.dispatch`, and `agentirc.protocol.*` are importable from a clean Python session.
- All tests in `tests/` pass under `pytest -n auto`.
- `git grep -E '^(from|import) culture' agentirc/ tests/` returns nothing.
- `agentirc --help` lists every verb in the CLI surface table above.
- `docs/api-stability.md` exists and names the three public modules.
- `pyproject.toml` declares `name = "agentirc-cli"` and the `agentirc` console script.

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
- After this work merges and `agentirc-cli==0.1.0` is on PyPI, report back the version + source SHA. The culture-side agent then opens its own PR to add the dependency, delete `culture/agentirc/`, install the `culture server` passthrough, and `/version-bump major`.
- All-backends rule (a culture convention): changes that cross the agentirc / culture-transport boundary must be reflected across all four backends in culture (`claude`, `codex`, `copilot`, `acp`). Not your concern for this bootstrap; flagged here so future cross-repo changes account for it.

## Reference

The culture-side spec for this split lives at `../culture/docs/superpowers/specs/2026-04-30-agentirc-extraction-design.md`. This document (the agentirc bootstrap spec) is intentionally self-contained — you should not need to read the culture-side spec to act on this work. The culture spec is provided only for context if you want to understand why a decision was made.
