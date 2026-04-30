# AgentIRC alignment onboarding

> Hand-written onboarding manual. Derived from steward's
> `docs/perfect-patient.md` and `docs/sibling-pattern.md` at the time
> this file was placed (2026-04-30). Re-read those docs in `../steward`
> when you start a new wave of work — they evolve.

This file tells the agent working in this repo what shape `agentirc`
needs to wear once the bootstrap is done, so it can plan its skills and
tasks against a known target. It does **not** override the bootstrap
spec — that comes first.

## Order of operations

1. **Bootstrap first.** Follow `docs/superpowers/specs/2026-04-30-bootstrap-design.md` and `CLAUDE.md` until `pip install agentirc-cli==0.1.0` produces a working `agentirc` binary and `agentirc serve` is byte-indistinguishable from `culture server start`. Nothing in this onboarding doc supersedes that.
2. **Sibling-pattern compliance.** Once the package, tests, and CI exist, work through the artifact checklist below until `steward doctor ../agentirc` passes.
3. **Vendor skills.** Copy in the recommended skills (next section), one PR per skill, smallest blast radius first (`version-bump`, `run-tests`, `pr-review`).
4. **Self-audit.** Run `steward doctor` (single-repo mode) after each landed PR. Treat findings as a queue.

## Skill manifest

`agentirc` has no agent backends, so the all-backends rule does not
apply here. It still needs the steward-curated skill set so the repo
hosts the same agent affordances every other sibling does.

Each skill is **vendored** (cite-don't-import). Copy the directory from
the upstream repo into `.claude/skills/<name>/` here, and only adjust
what the local repo actually requires (project name, coverage source,
etc.). When upstream changes, re-sync explicitly — there is no auto-sync.

### Recommended (build these)

Land these in roughly this order. The first three unblock the rest of
the workflow (you can't open clean PRs without `version-bump` and
`pr-review`, and you shouldn't claim "tests pass" without `run-tests`).

| Skill | Upstream | Purpose | Notes for agentirc |
|---|---|---|---|
| `version-bump` | `../steward/.claude/skills/version-bump/` | Bump semver in `pyproject.toml`, prepend Keep-a-Changelog entry. Required on every PR. | Pure Python, no per-repo customization needed. Start the changelog at `0.1.0` (per CLAUDE.md). |
| `run-tests` | `../steward/.claude/skills/run-tests/` | `pytest -n auto` with coverage. | Coverage source resolves from `[tool.coverage.run]` in `pyproject.toml`, so the script works once that section exists. |
| `pr-review` | `../steward/.claude/skills/pr-review/` | Branch → commit → push → PR → wait for automated reviewers → triage / fix / reply / resolve threads. | Steward owns the canonical workflow. Includes the portability lint that `steward doctor` also runs. |
| `gh-issues` | `../steward/.claude/skills/gh-issues/` | Fetch GitHub issues with full body + comments via `gh`. | Auto-detects the repo. |
| `notebooklm` | `../steward/.claude/skills/notebooklm/` | Generate GitHub blob URLs for repo docs (NotebookLM ingestion). | Auto-detects branch + remote. |
| `sonarclaude` | `../steward/.claude/skills/sonarclaude/` | Query SonarCloud — quality gate, issues, hotspots, metrics. Supports `accept` flow with mandatory rationale. | Set `$SONAR_PROJECT` once the project is registered, or pass `--project KEY`. |
| `pypi-maintainer` | `../steward/.claude/skills/pypi-maintainer/` | Switch a PyPI install between production / TestPyPI dev builds / local editable checkout. | Required because this repo publishes a package and PR builds go to TestPyPI as `.dev<run>`. |

### Optional (vendor if/when needed)

- `discord-notify` — Discord webhook embed (info / status / completion / error). Useful if long-running tasks here ever need to page the user. Requires `DISCORD_WEBHOOK_URL`. Upstream: `../steward/.claude/skills/discord-notify/`.

### Conditional (do **not** vendor)

- `jekyll-test` — Conditional on the repo containing a `_config.yml`. **`agentirc` does not ship a Jekyll site, so skip this.** If that ever changes, vendor from `../steward/.claude/skills/jekyll-test/`.

### Steward-specific (do **not** vendor)

- `agent-config` — Resolves Culture agent suffixes; coupled to steward's own layout.
- `doc-test-alignment` — Stub upstream; not portable yet.

## Artifact checklist (sibling-pattern)

The full source of truth is `../steward/docs/sibling-pattern.md`. The
table below is a snapshot — when in doubt, defer to that file.

| # | Artifact | Path | Status here |
|---|---|---|---|
| 1 | Toolchain | `pyproject.toml` (hatchling, Python ≥3.12, minimal runtime deps) | **TODO** (bootstrap creates it) |
| 2 | Top-level package | `agentirc/__init__.py`, `agentirc/__main__.py`; `__version__` via `importlib.metadata("agentirc-cli")` | **TODO** (bootstrap) |
| 3 | CLI scaffolding | `agentirc/cli/__init__.py`, `cli/_errors.py`, `cli/_output.py`, `cli/_commands/` (afi-cli pattern) | **TODO** (bootstrap) |
| 4 | Agent-first verbs | `cli/_commands/{learn,explain,whoami}.py` | Optional for now — bootstrap only mandates the `culture server …` verb set; treat agent verbs as a follow-up. |
| 5 | Mutation safety | Any write verb defaults to dry-run; `--apply` to commit | Apply when adding mutating verbs. |
| 6 | Tests | `tests/test_*.py`, `pytest-xdist`, coverage | **TODO** (bootstrap) |
| 7 | CI | `.github/workflows/tests.yml` + `publish.yml` (Trusted Publishing) | **TODO** (bootstrap) |
| 8 | Changelog | `CHANGELOG.md`, Keep-a-Changelog format | **TODO** — start at `0.1.0`. |
| 9 | Skills | `.claude/skills/<name>/SKILL.md` + `scripts/` per skill | **TODO** — see Skill manifest above. |
| 10 | Per-machine config | `.claude/skills.local.yaml.example` (committed) + `.claude/skills.local.yaml` (gitignored) | **TODO** — minimal template once a skill needs it. |
| 11 | Lint configs | `.flake8`, `.markdownlint-cli2.yaml` (repo-local) | **TODO** — copy from `../steward` / `../culture`. |
| 12 | `CLAUDE.md` | Project shape, build/test/publish, conventions | **Present** — extend as conventions land. |

## Self-audit

Once the package builds and tests run:

```bash
# Single-repo invariants (portability + skills-convention)
steward doctor ../agentirc

# JSON for piping into a TODO list
steward doctor ../agentirc --json
```

Treat the findings list as a queue. `--apply` is not yet implemented in
steward, so each repair is hand-driven for now (see steward's roadmap
in its `CLAUDE.md`).

When the per-target generator (`steward doctor --scope siblings`) starts
writing into `docs/steward/steward-suggestions.md` here, that file will
be auto-generated below a marker line — anything **above** the marker is
hand-written and preserved. This onboarding doc is separate and is not
touched by the generator.

## What this doc is **not**

- Not a substitute for the bootstrap spec.
- Not a backend coordination contract — agentirc has no backends.
- Not a list of features to invent. Skills here are vendored from steward, not authored from scratch. New skills land **upstream first** (in steward, when generic) and then propagate here.

— Claude
