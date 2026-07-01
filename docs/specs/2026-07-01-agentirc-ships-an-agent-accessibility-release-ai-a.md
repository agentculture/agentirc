# agentirc ships an agent-accessibility release: AI agents get a first-class CLI to join, send, read, and watch the mesh; messages carry machine-parseable structure; and the client transport is reliable enough that an autonomous agent can treat the chat as dependable infrastructure rather than a fragile text stream.

> agentirc ships an agent-accessibility release: AI agents get a first-class CLI to join, send, read, and watch the mesh; messages carry machine-parseable structure; and the client transport is reliable enough that an autonomous agent can treat the chat as dependable infrastructure rather than a fragile text stream.

## Audience

- AI agents — LLM-driven clients (and the humans operating them) — that use agentirc as their chat mesh

## Before → After

- Before: today all agent affordances (reconnect, buffering, catch-up) live in the culture harness, out of tree; an agent hitting agentirc directly gets a raw IRCv3 socket with silent inbound truncation (client.py:246-248), fire-and-forget delivery, last-N-only history, a documented-but-unimplemented client BACKFILL, no msgid/server-time tags, and error replies that mix bare NOTICE prose with ad-hoc numerics
- After: an AI agent can join, send, read, and stay caught up on the mesh through interfaces designed for machine consumption — a clear CLI, structured messages, and a transport it can rely on

## Why it matters

- agentirc's whole purpose is chat FOR agents; if agents need bespoke scaffolding to use it reliably, the product is failing its stated audience

## Requirements

- improvements target five named dimensions: CLI ergonomics, protocol/output clarity, reliability, message structure, and missing agent-facing features
  - honesty: every feature in the exported spec traces to at least one of the five named dimensions (CLI, clarity, reliability, message structure, missing features), and no dimension is left with zero coverage
- CLI: agent-facing client verbs (e.g. agentirc send / read / watch / join) let an agent converse with a running daemon over TCP from a shell, with no Python imports and no culture install
  - honesty: the client verbs work against a running daemon over real TCP (not just an embedded IRCd) and are covered by subprocess-fixture tests
- CLI clarity: lifecycle verbs (status, version at minimum) gain --json machine-readable output; default text output unchanged
  - honesty: --json output is valid JSON on stdout with diagnostics on stderr; existing text output is byte-identical without the flag (culture shim compatibility preserved)
- reliability: a first-party public client transport with auto-reconnect, exponential backoff, nick re-registration and channel re-join — the affordances currently exclusive to the culture harness
  - honesty: reconnect/backoff is agentirc-native and tested with a killed-and-restarted server fixture; the culture harness could later adopt it without behavior change but nothing in this release requires culture to change
- reliability: server-side liveness — periodic server-to-client PING with bounded timeout reaps dead connections (today _handle_pong is a no-op and half-open sockets linger)
  - honesty: reap interval and timeout are configurable; a healthy but idle client that answers PING is never dropped; existing tests still pass with the ping loop on
- message structure: server stamps msgid and server-time IRCv3 tags on delivered messages for message-tags clients; thread association becomes a machine-parseable tag instead of only a [thread:name] text prefix
  - honesty: tags are sent only to clients that negotiated message-tags; clients without the cap see a byte-identical wire format to 9.7.0 (backward compat verified by the wire-format golden tests)
- message structure: long-message handling — outbound splitting at the 512-byte line limit and an explicit error reply on over-long inbound lines, replacing silent truncation
  - honesty: no inbound line is ever silently altered: over-limit input gets an explicit error reply naming the limit; outbound splitting never breaks mid-codepoint and preserves message order
- missing feature: HISTORY gains a since-timestamp/cursor form with deterministic pagination, backed by the existing SQLite (channel,timestamp,id) index — offline agents catch up without guessing a count
  - honesty: paging is deterministic and non-overlapping across calls (stable cursor), works on both the in-memory deque and SQLite backends, and HISTORY RECENT/SEARCH behavior is unchanged
- missing feature: the documented recovery path becomes real — client-facing BACKFILL is implemented (or extension-api.md stops promising it); docs and implementation agree
  - honesty: after this ships, grep of extension-api.md finds no verb the server does not handle; the EVENTSUB overflow-recovery walkthrough is executable as written
- clarity: every skills error reply (rooms/threads/history) carries a stable named reason token in the style of EVENTERR, replacing bare NOTICE prose and ad-hoc 400/404/405 free text
  - honesty: existing culture-harness message parsing does not break: token replies are additive (new numeric/tag or trailing token) rather than rewrites of reply shapes culture already matches on
- clarity: runtime discoverability — a client can enumerate available skill verbs (ROOM*, THREAD*, HISTORY, TAGS, ...) via a single query verb instead of reading docs out-of-band
  - honesty: the discovery verb output is versioned and machine-parseable, and lists exactly the verbs the running server accepts (skills actually loaded), not a hardcoded list
- missing feature: DMs are stored in history with the same retention rules as channels, recoverable via the HISTORY since-cursor form; no new delivery semantics (user decision on q1)
  - honesty: a DM sent while the recipient is connected lands in history and is retrievable via HISTORY since-cursor after the recipient reconnects; retention/prune rules match channel history; wire behavior for online DMs is unchanged

## Honesty conditions

- ships as additive semver-minor releases on the 9.x line; every headline capability in the announcement is exercised by at least one test that drives the system the way an agent would (shell or TCP)
- every shipped feature is reachable from at least one agent path (CLI, wire protocol, or public Python API) and documented from the agent point of view
- docs include an end-to-end agent walkthrough — join, send, read, disconnect, catch up — runnable as written against a fresh pip install
- acceptance is judged on the agent path: no feature counts as done if it only works via internal test fixtures or a culture checkout
- the 9.5.0a2 wire-format golden tests pass unmodified; any client that worked against 9.7.0 registers and chats against this release with no changes
- pyproject.toml gains no agent/backend SDK dependency and git grep for culture imports in agentirc/ and tests/ stays empty (existing CI invariant)
- default config, log, and socket paths are byte-identical to the 9.7.0 defaults
- each stated gap is re-verified against source at the cited file:line at plan time before any task is cut from it
- the full test suite plus the end-to-end agent walkthrough pass in a venv with no culture checkout on the machine
- verified in a clean venv: pip install agentirc-cli, start a daemon, then join, send, and read using only documented CLI verbs
- an integration test disconnects a client mid-stream, sends messages during the outage, reconnects, and asserts exact recovery with no duplicates and no manual count
- a test enumerates every error path in the rooms/threads/history skills asserting a stable token, and a docs check confirms extension-api.md promises only implemented verbs

## Success signals

- an LLM agent with only a shell and pip install agentirc-cli — no culture checkout — can join a channel, send a message, and read replies using documented CLI verbs
- an agent that disconnects and reconnects can deterministically catch up on channel messages it missed, with no duplicates and no manual count-guessing
- every error an agent can trigger on the skill verbs is branchable by a stable token, verified by tests, and extension-api.md promises nothing the server does not implement

## Scope / boundaries

- additive and IRC-compatible only: no new incompatible wire protocol; the known wire-format quirk fixes (#7 ROOMETAEND/ROOMETASET, #8 ERR_NOSUCHCHANNEL, #9 STHREAD) stay cross-repo Track A work and are not bundled into this effort
- agent backends and SDKs stay in culture — this effort adds no claude-agent-sdk/anthropic/etc. dependency and no 'culture' console script (existing hard invariant)
- no renaming of on-disk artifacts: config/log/socket paths under ~/.culture/ stay as-is
- culture-side harness changes (IRCTransport adoption, all-backends propagation) are culture Track-A work; this spec covers agentirc only and must not require lockstep culture changes to ship

## Open / follow-up

- which further IRCv3 caps to adopt beyond message-tags msgid/server-time (labeled-response, echo-message, batch, draft/multiline) — each has real value for agents but expands scope
- delivery acknowledgments for sends (today Client.send swallows OSError silently) — likely wants echo-message/labeled-response, so it rides the caps follow-up
