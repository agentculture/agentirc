---
name: pr-review
description: >
  PR workflow for agentirc: branch, commit, push, PR, wait for Qodo/Copilot,
  triage, fix, reply, resolve. Includes a portability lint (no absolute /home
  paths, no per-user dotfile refs in committed docs) and an alignment-delta
  check when CLAUDE.md or anything under .claude/skills/ changes. Use when:
  creating PRs, handling review feedback, or the user says "create PR",
  "review comments", "address feedback", "resolve threads".
---

# PR Review — agentirc edition

Vendored from `../steward/.claude/skills/pr-review/` per
`docs/steward/onboarding.md`. Re-sync explicitly when steward updates upstream
— there is no auto-sync.

agentirc's PRs touch the bootstrap spec, `CLAUDE.md`, vendored skills, and
(post-bootstrap) the IRCd server core. Two recurring bug classes the generic
`pr-review` skills miss:

- **Path leaks** — committing absolute home-directory paths that work only on
  the author's machine.
- **Per-user config dependencies** — referencing a dotfile under the user's
  home directory in repo guidance, breaking reproducibility for other
  contributors and CI.

Both are caught by `scripts/portability-lint.sh`. The full workflow is
encapsulated in `scripts/workflow.sh` — follow that, not a manual checklist.

## Prerequisites

Hard requirements: `gh` (GitHub CLI), `jq`, `bash`, `python3` (stdlib only),
`curl` (used by `pr-status.sh`).

Per-machine paths (sibling-project layout) live in
`.claude/skills.local.yaml`; see the committed `.example` for the schema.

## How to run

`scripts/workflow.sh` is the entry point. Subcommands:

| Command | Purpose |
|---------|---------|
| `workflow.sh lint` | Portability lint on the current diff (staged + unstaged). |
| `workflow.sh poll <PR>` | Fetch and display all review comments. |
| `workflow.sh delta` | Dump each sibling project's `CLAUDE.md` head + `culture.yaml`. |
| `workflow.sh reply <PR>` | Batch reply (JSONL on stdin) and resolve threads. |
| `workflow.sh help` | Print this list. |

The single-comment helpers — `pr-reply.sh`, `pr-status.sh` — live next to
`workflow.sh` and are usable directly when batching isn't appropriate.

## End-to-end flow

```text
git checkout -b <type>/<desc>
# ... edit ...
.claude/skills/pr-review/scripts/workflow.sh lint
git commit -am "..." && git push -u origin <branch>
gh pr create --title "..." --body "..."   # title <70 chars, body signed "- Claude"
sleep 300                                  # wait for Qodo + Copilot
.claude/skills/pr-review/scripts/workflow.sh poll <PR>
# triage; if CLAUDE.md / .claude/skills changed:
.claude/skills/pr-review/scripts/workflow.sh delta
# fix, re-lint, push
.claude/skills/pr-review/scripts/workflow.sh reply <PR> < replies.jsonl
gh pr checks <PR>
# Wait for human merge — never merge yourself.
```

Branch naming: `fix/<desc>`, `feat/<desc>`, `docs/<desc>`, `skill/<name>`.
Commit/PR signature: `- Claude` (workspace convention). The reply script
auto-appends `- Claude` only if the body isn't already signed, so JSONL
entries can include or omit it.

## Triage rules

For every comment, decide **FIX** or **PUSHBACK** with reasoning.

Default to **FIX** for: portability complaints (always valid — recurring bug
class), test or doc requests, style nits aligned with workspace conventions,
and inconsistencies the reviewer catches between spec sections.

Default to **PUSHBACK** for: architecture opinions that conflict with
`CLAUDE.md` or the bootstrap spec; greenfield false-positives (e.g. "add
tests" before there's any source — defer to a later PR with a clear note,
don't refuse).

### Alignment-delta rule

If the PR touches `CLAUDE.md` or anything under `.claude/skills/`, run
`workflow.sh delta` **before** declaring FIX or PUSHBACK on each comment.
The script dumps the head of every sibling project's `CLAUDE.md` plus the
full `culture.yaml`, using `sibling_projects` from `skills.local.yaml`.
agentirc's siblings are `culture` and `steward` — note any sibling that
needs a follow-up PR and mention it in your reply.

## Greenfield-aware steps

The lint and the workflow script are always-on. Stack-specific steps are
conditional and currently no-op (greenfield repo until the bootstrap lands):

```bash
[ -d tests ] && [ -f pyproject.toml ] && uv run pytest tests/ -x -q
[ -f pyproject.toml ] && bump_version_per_project_convention
[ -f .markdownlint-cli2.yaml ] && markdownlint-cli2 "$(git diff --name-only --cached '*.md')"
```

Revisit each line as the corresponding stack element actually lands during
or after the bootstrap.

## Reply etiquette

Every comment must get a reply — no silent fixes. Always pass `--resolve`
when batch-replying so threads close automatically. Reference the
review-comment IDs in the fix-up commit message. agentirc currently has no
SonarCloud project and isn't a registered mesh agent, so skip the
sonarclaude check and the post-merge IRC ping that culture's `pr-review`
includes — those will return when agentirc joins those systems (the
`sonarclaude` skill is on the steward-onboarding vendoring queue).

## All-backends rule

agentirc has no agent backends, so the all-backends rule that culture
enforces does not apply here. If a comment cites it, push back.
