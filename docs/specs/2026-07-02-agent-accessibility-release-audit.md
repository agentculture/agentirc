# Agent-accessibility release — acceptance audit (t16)

Closes the plan's t16 (`docs/plans/2026-07-01-agentirc-ships-an-agent-accessibility-release-ai-a.md`).
Audit rule (honesty conditions h12/h15 of the spec): every headline
capability from the announcement must be exercised by at least one test
that drives the system **the way an agent would** — a shell subprocess or
a raw/public TCP client — never only via internal fixtures.

## Headline capabilities → agent-path evidence

| Announcement claim | Agent-path test(s) | Path |
|---|---|---|
| First-class CLI to join, send, read, watch | `tests/test_cli_client_verbs.py` (17 tests) — every verb driven as a `python -m agentirc` subprocess against a live daemon over TCP | shell |
| …usable end-to-end with zero culture presence | `tests/test_agent_walkthrough.py::test_agent_walkthrough` (start→join→send→read→watch→stop, isolated `HOME`); CI `agent-guards` job repeats it in a fresh venv and asserts no `../culture` checkout | shell |
| Machine-parseable structure: msgid/time/thread tags | `tests/test_message_tags.py` (10) — raw TCP clients with/without `message-tags`; fan-out msgid stability | TCP |
| Machine-parseable structure: stable error tokens | `tests/test_error_tokens.py` (56 cases) — tagged vs untagged clients on every skills error path | TCP |
| Machine-parseable structure: VERBS discovery | `tests/test_verb_discovery.py` (13) — incl. the not-hardcoded lock (runtime-registered skill appears live) | TCP |
| Reliable transport: reconnect + re-join | `tests/test_agent_client.py` (9) — kill-and-restart fixture over real sockets | TCP (public API) |
| Reliable transport: deterministic catch-up | `tests/test_reconnect_catchup.py` — two forced disconnects, exact recovery, msgid-verified vs a stationary witness, cursor chaining; `tests/test_history_since.py` (24) for the cursor itself | TCP (public API) |
| Reliable transport: server liveness | `tests/test_liveness.py` (10) — killed peer reaped, responsive/busy clients never dropped | TCP |
| Reliable transport: no silent loss on long lines | `tests/test_long_messages.py` (21) — explicit `line-too-long` error, codepoint-safe split, chunks in history | TCP |
| DM history (user decision q1: store-in-history) | `tests/test_dm_history.py` (11) — participant-only, prune, EVENTSUB shape unchanged; DM watch/offline-send regressions in `test_cli_client_verbs.py` | TCP + shell |
| Docs promise only what exists (BACKFILL made real) | `tests/test_client_backfill.py` (12, incl. the extension-api walkthrough executed as written); `tests/test_docs_promises.py` (2) | TCP |
| Wire compat: 9.7.0 clients unaffected | `tests/test_wire_format_envelope.py` (24) — 9.5.0a2 golden tests unmodified + t1 characterization baselines | TCP |
| On-disk continuity (`~/.culture/` unchanged) | `tests/test_disk_paths.py` (11) — literal path locks | n/a |

Suite at audit time: **761 passed** (`pytest -n auto`), up from 531 on the
pre-release baseline (+230, all additive; zero existing tests modified
except one integrator deflake of a t5 timing test, documented in its
commit).

## Deviations from the plan (all recorded in merge commits)

- t8 gained a dependency on t7 (shared history-store surface) — folded from
  the colleague review before the plan was exported; waves ran 10 deep.
- t11's first run died on a spend limit; a resumed agent salvaged its WIP,
  adopted t13's `send_raw`/`raw_lines` helpers, and fixed an
  unretrieved-task race found during the salvage.
- t15's command-by-command doc verification exposed two DM bugs in the t11
  surface (`watch <nick>` filter mismatch; `send <nick>` exit 0 on
  `ERR_NOSUCHNICK`); fixed by the integrator with three regression tests
  before this audit (commit `f52910a`).
- The `PASS` verb is deliberately absent from `VERBS` output — it is
  consumed by the connection sniffer before a `Client` exists, so listing
  it would violate the "every listed verb is dispatchable" guarantee.

## Still open (recorded plan risks, pre-release decisions for the operator)

- **r4 delivery acks:** `send` now surfaces DM rejection (`ERR_NOSUCHNICK`)
  but there is still no positive ack (`echo-message`/`labeled-response`
  parked as follow-up v2). Release wording says "accepted, no rejection
  observed" accordingly.
- **r5 rate limiting:** no stance shipped — add or explicitly exclude in a
  follow-up before promoting the release as abuse-safe.
