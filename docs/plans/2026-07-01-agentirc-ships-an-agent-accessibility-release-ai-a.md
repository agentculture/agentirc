# Build Plan — agentirc ships an agent-accessibility release: AI agents get a first-class CLI to join, send, read, and watch the mesh; messages carry machine-parseable structure; and the client transport is reliable enough that an autonomous agent can treat the chat as dependable infrastructure rather than a fragile text stream.

slug: `agentirc-ships-an-agent-accessibility-release-ai-a` · status: `exported` · from frame: `agentirc-ships-an-agent-accessibility-release-ai-a`

> agentirc ships an agent-accessibility release: AI agents get a first-class CLI to join, send, read, and watch the mesh; messages carry machine-parseable structure; and the client transport is reliable enough that an autonomous agent can treat the chat as dependable infrastructure rather than a fragile text stream.

## Tasks

### t1 — Re-verify before-state gaps and lock the wire-format baseline

- covers: c6, h17, c9, h20
- acceptance:
  - every file:line citation in the spec before-state is re-checked against current main and recorded in a short verification note under docs/specs/
  - the 9.5.0a2 golden wire-format tests run green unmodified and are extended to baseline PRIVMSG relay, NOTICE error replies, and HISTORY replay lines

### t2 — Stable error tokens across rooms, threads, and history skills

- depends on: t1
- covers: c19, h10, c23, h24
- acceptance:
  - every error path in skills/rooms.py, skills/threads.py, skills/history.py emits a reply carrying a stable named token; a parameterized test enumerates all paths and asserts each token
  - reply shapes the culture harness already matches on are unchanged: tokens ride additively, verified against the golden baseline

### t3 — msgid, server-time, and thread tags on delivered messages

- depends on: t1, t2
- covers: c15, h6
- acceptance:
  - clients that negotiated message-tags receive msgid and time tags on PRIVMSG delivery and a thread tag on thread messages; the [thread:name] text prefix remains
  - clients without message-tags see byte-identical 9.7.0 wire format (golden tests)
  - msgids are unique per message and stable across channel fan-out (every recipient sees the same msgid)

### t4 — Long-message handling: outbound split, explicit inbound limit error

- depends on: t3
- covers: c16, h7
- acceptance:
  - outbound messages beyond the 512-byte line limit split into ordered continuation lines, never mid-codepoint, preserving order
  - an over-limit inbound line yields an explicit error reply naming the limit; the silent truncation path at client.py:246-248 is gone; no input is ever silently altered

### t5 — Server liveness: periodic PING and dead-connection reaper

- depends on: t4
- covers: c14, h5
- acceptance:
  - server sends PING at a configurable interval and reaps connections missing a configurable PONG deadline; _handle_pong updates liveness state
  - an idle-but-responsive client is never dropped; a killed TCP peer is reaped within interval plus timeout; existing suite green with the loop on

### t6 — HISTORY since-cursor pagination

- depends on: t2
- covers: c17, h8
- acceptance:
  - HISTORY gains a since form taking an opaque cursor (timestamp plus id composite) returning deterministic, non-overlapping pages on both the deque and SQLite backends
  - HISTORY RECENT and SEARCH behavior is byte-unchanged; pagination tests span a retention-prune boundary

### t7 — DM history storage

- depends on: t5, t6
- covers: c24, h16
- acceptance:
  - a DM to a connected recipient lands in the history store under the same retention and prune rules as channels and is retrievable via the since-cursor form after reconnect
  - DM history is queryable only by the sender and the recipient; DMs never appear in channel history queries; online DM wire behavior is unchanged

### t8 — Client-facing BACKFILL

- depends on: t5, t6
- covers: c18, h9
- acceptance:
  - a registered client can issue BACKFILL and receive events missed in its subscription window; the EVENTSUB overflow-recovery walkthrough in extension-api.md executes as written
  - a docs-vs-implementation test extracts the verbs extension-api.md promises and asserts each has a live handler

### t9 — Runtime verb discovery

- depends on: t8
- covers: c20, h11
- acceptance:
  - a single query verb returns a versioned, machine-parseable list of exactly the verbs the running server accepts, derived from loaded skills rather than a hardcoded list
  - the reply includes the negotiable capability names and the error-token vocabulary version

### t10 — Public reconnecting client transport

- depends on: t1
- covers: c13, h4
- acceptance:
  - a new public module exposes a client that connects, registers, joins, reads, and sends over TCP with auto-reconnect (exponential backoff), nick re-registration, and channel re-join
  - a kill-and-restart server fixture proves reconnection and re-join; nothing in the module imports culture or any backend SDK

### t11 — Agent CLI client verbs

- depends on: t10, t6
- covers: c11, h2
- acceptance:
  - agent-facing verbs (join, send, read, watch — final names settled at implementation per parked v3) drive a running daemon over real TCP from a shell; subprocess-fixture tests cover each verb
  - read and watch can resume from a since-cursor so a shell agent catches up without count-guessing

### t12 — --json output for status and version

- depends on: t11
- covers: c12, h3
- acceptance:
  - agentirc status --json and agentirc version --json emit valid JSON on stdout with diagnostics on stderr; without the flag output stays byte-identical to 9.7.0

### t13 — Reconnect catch-up integration test

- depends on: t3, t6, t10
- covers: c22, h23
- acceptance:
  - an integration test disconnects a client mid-stream, delivers messages during the outage, reconnects, and asserts exact recovery — no gaps, no duplicates, msgid-verified — via the public transport plus since-cursor

### t14 — CI guards: clean-venv run, dependency grep, on-disk path lock

- depends on: t11
- covers: c7, h18, c8, h19, c10, h21
- acceptance:
  - a CI job runs the full suite plus the agent walkthrough in a venv with no culture checkout on the machine
  - CI fails on any culture import under agentirc/ or tests/ and on any agent or backend SDK appearing in pyproject.toml
  - a test asserts default config, log, and socket paths are byte-identical to the 9.7.0 defaults

### t15 — Agent-facing docs: walkthrough, extension-api, traceability

- depends on: t8, t9, t11, t12
- covers: c2, h13, c3, h14, c5, h1, c21, h22
- acceptance:
  - docs include an end-to-end agent walkthrough — join, send, read, disconnect, catch up — runnable as written against a fresh pip install of agentirc-cli
  - api-stability.md and extension-api.md document every new public surface from the agent point of view, and extension-api.md promises only implemented verbs
  - a traceability table maps every shipped feature to at least one of the five dimensions (CLI, clarity, reliability, message structure, missing features) with no dimension empty

### t16 — Release audit and version bumps

- depends on: t13, t14, t15
- covers: c1, h12, c4, h15
- acceptance:
  - each headline capability from the announcement is exercised by at least one test that drives the system the way an agent would (shell or TCP); the audit note lands under docs/specs/
  - changes ship as semver-minor bumps on the 9.x line via /version-bump with changelog entries; acceptance is judged on the agent path only

## Risks

- [unknown_nonblocking] error-token rewrites must stay additive to reply shapes the culture harness parses; verify against the actual culture-side parsers before merging t2 (task t2)
- [unknown_nonblocking] thread-tag design may brush against federation quirk #9 (STHREAD verb collapse); keep the tag local-optional so no Track A coordination is required (task t3)
- [unknown_nonblocking] cursor stability under concurrent writes (timestamp collisions) — settle the timestamp-plus-id composite cursor semantics at t6 implementation time (task t6)
