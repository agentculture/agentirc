# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state: pre-bootstrap

This repo is being bootstrapped from a server-core extraction out of the sibling project [`culture`](https://github.com/OriNachum/culture). At the moment it contains only `LICENSE`, `README.md`, `.gitignore`, and `docs/superpowers/specs/2026-04-30-bootstrap-design.md` — no Python package, no tests, no `pyproject.toml` yet. **Read the bootstrap spec before doing anything else**; it is the operative source of truth and is intentionally self-contained.

The culture-side counterpart spec is at `../culture/docs/superpowers/specs/2026-04-30-agentirc-extraction-design.md`. You should not need to open it to act, but it explains the *why* if a decision in the agentirc spec looks arbitrary.

## Three names, one project

There are three different names in play. Don't conflate them:

| Role | Name |
|---|---|
| PyPI distribution | `agentirc-cli` (the squatted `agentirc` on TestPyPI is **not** ours; use `agentirc-cli` everywhere) |
| Python import package | `agentirc` |
| CLI binary | `agentirc` |

`pyproject.toml` will declare `name = "agentirc-cli"` and `[project.scripts] agentirc = "agentirc.cli:main"`.

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

## Common commands (post-bootstrap)

These will exist once the bootstrap tasks are done — they don't work yet.

```bash
# Dev setup
uv venv && uv pip install -e ".[dev]"

# Tests (the spec mandates parallel)
pytest -n auto

# Run a single test
pytest tests/path/to/test_file.py::test_name -v

# CLI smoke
agentirc --help
agentirc serve --config ~/.culture/server.yaml
python -m agentirc serve   # equivalent
```

CLI verbs: `serve`, `start`, `stop`, `restart`, `status`, `link`, `logs`, `version`. Of these, only `start`, `stop`, `status` have a `culture server …` analogue today; the rest are agentirc-only additions. Culture's pure-passthrough shim only ever emits its existing verbs, so the additions don't break it.

## Tooling conventions (mirror culture)

Copy these from culture rather than inventing fresh:

- `.pre-commit-config.yaml`
- GitHub Actions workflows
- Dev-dep set: `pytest`, `pytest-asyncio`, `pytest-xdist`, `black`, `isort`, `flake8`, `pylint`, `bandit`
- `/version-bump` workflow and `CHANGELOG.md` style. Start the changelog at `0.1.0`.

Markdown linting follows the workspace global config (`~/.markdownlint-cli2.yaml`).

## Coordination with culture

- This is **Track B** of a two-repo split. **Track A** is culture-side and is the culture agent's job — do not edit culture from this repo.
- After `agentirc-cli==0.1.0` is on PyPI, report the published version and source SHA back so culture's cutover PR can pin against it.
- Culture's *all-backends rule* (a feature added to one of `claude`/`codex`/`copilot`/`acp` must be propagated to the others) does **not** apply inside this repo — agentirc has no backends. It is mentioned in the spec only so future cross-repo changes account for it.

## Acceptance criteria for the bootstrap

The full list lives in §"Acceptance criteria" of the bootstrap spec. The non-obvious ones:

- `pip install agentirc-cli==0.1.0` on a clean venv produces a working `agentirc` binary.
- `agentirc serve` is byte-indistinguishable from `culture server start` (same socket, same logs, same systemd integration).
- `agentirc.config.LinkConfig`, `agentirc.config.PeerSpec`, `agentirc.cli.dispatch`, `agentirc.protocol.*` all import from a clean Python session.
- `docs/api-stability.md` names the three public modules.
