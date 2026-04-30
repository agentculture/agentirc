# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state: skeleton package, pre-extraction

This repo is being bootstrapped from a server-core extraction out of the sibling project [`culture`](https://github.com/OriNachum/culture). The package skeleton is in place — `pyproject.toml`, `agentirc/__init__.py`, `agentirc/__main__.py`, `agentirc/cli.py` (stub) — and `pip install -e .` produces working `agentirc` and `agentirc-cli` console scripts. The IRCd server core has **not** been copied from culture yet; all lifecycle verbs (`serve`, `start`, `stop`, `restart`, `status`, `link`, `logs`) currently exit non-zero with a "not yet implemented" message. **Read the bootstrap spec before doing anything else**; it is the operative source of truth and is intentionally self-contained.

The culture-side counterpart spec is at `../culture/docs/superpowers/specs/2026-04-30-agentirc-extraction-design.md`. You should not need to open it to act, but it explains the *why* if a decision in the agentirc spec looks arbitrary.

## Three names, one project

There are three different names in play. Don't conflate them:

| Role | Name |
|---|---|
| PyPI distribution | `agentirc-cli` on real PyPI; on TestPyPI, also `agentirc` (we claim the TestPyPI squat — not the real-PyPI one) |
| Python import package | `agentirc` |
| CLI binary | both `agentirc` and `agentirc-cli` (same entry point: `agentirc.cli:main`) |

`pyproject.toml` declares `name = "agentirc-cli"` and `[project.scripts]` with **both** `agentirc = "agentirc.cli:main"` and `agentirc-cli = "agentirc.cli:main"`. For TestPyPI dev releases, `publish.yml` will additionally build with the dist name temporarily rewritten to `agentirc` and upload that wheel too — real PyPI on push to `main` stays `agentirc-cli` only.

## What lives here vs. in culture

- **Server-core (here):** `ircd.py`, `server_link.py`, `channel.py`, `config.py`, `events.py`, the stores (`room_store`, `thread_store`, `history_store`), `rooms_util.py`, `skill.py`, and the `skills/` directory (`rooms`, `threads`, `history`, `icon`).
- **Stays in culture:** `client.py`, `remote_client.py`, and any test that exercises the IRC *client transport* rather than the server.
- **Newly created here:** `agentirc/cli.py` (extracted from `../culture/culture/cli/server.py`), `agentirc/__main__.py`, and `agentirc/protocol.py` (consolidates verb names, numerics, and extension tag names that today are inlined as string literals in `ircd.py` / `client.py`).

When migrating tests, the rule is: pure server tests come here, transport tests stay in culture, mixed tests stay in culture and get rewritten to drive `agentirc serve` as a subprocess fixture rather than importing `IRCd` directly. When unsure, **prefer copying the test here** — this repo owns the IRCd.

## Public API contract (semver-tracked)

Only three modules are public. Everything else is internal and may be refactored without a major bump.

| Module | Members |
|---|---|
| `agentirc.config` | `ServerConfig`, `LinkConfig`, `PeerSpec` |
| `agentirc.cli` | `main()`, `dispatch(argv) -> int` |
| `agentirc.protocol` | verb name constants, numeric reply codes, extension tag names |

`agentirc.cli.dispatch(argv)` is the function `culture`'s `culture server` shim calls — it must accept the exact same flag set, exit codes, and stderr formatting that `culture server` produces today. Do not "improve" CLI ergonomics during the bootstrap; that breaks the transparency contract culture relies on.

## Defaults preserve culture continuity

- Default `--config` path: `~/.culture/server.yaml` (yes, `.culture/`, not `.agentirc/`).
- Socket paths, log paths, systemd unit names: unchanged from culture.
- Standalone (non-culture) users override via `--config`.

Do not rename on-disk artifacts during the bootstrap. That is explicitly out of scope.

## Hard invariants

- **No imports back into culture.** After the bootstrap, `git grep -E '^(from|import) culture' agentirc/ tests/` must return nothing. CI should enforce this.
- **No file rewrites in the bootstrap.** Files copy from `../culture/culture/agentirc/` as-is, with import paths rewritten (`from culture.agentirc.X` → `from agentirc.X`) and nothing else. Improvements ship in follow-up PRs.
- **Single synthetic first commit.** Message format: `Initial import from culture@<SHA>` where `<SHA>` is the culture commit ID the caller provides. No cherry-picked history.
- **No backend SDKs, no `culture` console script.** agentirc must not depend on `claude-agent-sdk`, `anthropic`, `agex-cli`, `afi-cli`, `github-copilot-sdk`, or any other agent/backend SDK, and must not declare a `culture` console script. Those are culture concerns — agent backends and the `culture` command live in `../culture` and stay there.

## Common commands

```bash
# Dev setup
uv venv && uv pip install -e ".[dev]"

# Tests (the spec mandates parallel; no tests yet — collected 0)
pytest -n auto

# Run a single test
pytest tests/path/to/test_file.py::test_name -v

# CLI smoke (works today against the skeleton)
agentirc --help
agentirc-cli --help          # alias of agentirc
agentirc version             # prints "agentirc 9.0.0"
python -m agentirc version   # equivalent

# Lifecycle verbs (stubs in 9.0.0; real impls land with the IRCd extraction)
agentirc serve --config ~/.culture/server.yaml
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

- `pip install agentirc-cli==9.0.0` on a clean venv produces working `agentirc` *and* `agentirc-cli` binaries (both pointing at `agentirc.cli:main`).
- `agentirc serve` is byte-indistinguishable from `culture server start` (same socket, same logs, same systemd integration).
- `agentirc.config.LinkConfig`, `agentirc.config.PeerSpec`, `agentirc.cli.dispatch`, `agentirc.protocol.*` all import from a clean Python session.
- `docs/api-stability.md` names the three public modules.
