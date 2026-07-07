# agentirc now speaks PRESENCE: the IRCd parses resident presence heartbeats, tracks per-client presence state, propagates it across server links, flags presumed-hung residents via a stale-busy watchdog, and serves the aggregate to culture via PRESENCE LIST — unblocking culture resident presence v1 (issue #53)

> agentirc now speaks PRESENCE: the IRCd parses resident presence heartbeats, tracks per-client presence state, propagates it across server links, flags presumed-hung residents via a stale-busy watchdog, and serves the aggregate to culture via PRESENCE LIST — unblocking culture resident presence v1 (issue #53)

## Audience

- Primary consumer is culture's front door (the 'culture residents' CLI verb t5 and the JSON endpoint t7 for the irc-lens console); secondary audience is mesh operators and future balancing policies, plus any embedder using agentirc.ircd.IRCd

## Before → After

- Before: The mesh has no visibility into which residents are busy: no presence verb exists, culture's residents CLI degrades to 'server does not support PRESENCE' (421), and a hung agent is indistinguishable from a busy one
- After: Any server in the mesh can render a live aggregate of every connected resident: {nick, server, state, since, task, tokens_in, tokens_out, presumed_hung, last_refresh} — including residents connected to OTHER servers via S2S propagation

## Why it matters

- Operators and balancing policies can act on active-resident counts and token spend; a kill -9'd resident that last reported busy is surfaced as presumed-hung with zero cooperation from the dead process

## Requirements

- PRESENCE verb: parse per the wire contract (state/since/optional task/optional tokens_in+tokens_out); it is a NEW verb — no RFC 2812 command is redefined, and unknown-verb behavior for vanilla clients (e.g. weechat in the same channel) is unchanged
  - honesty: Payload parsing matches protocol/extensions/presence.md field-for-field (names, types, optionality), and a test proves a vanilla client without the feature still gets stock 421 unknown-command semantics for any other unknown verb
- Per-client presence state: the server keeps latest presence per connected client — state (idle|listening|thinking|working|draining|offline), since, task, token counters, last_refresh
  - honesty: A test drives two heartbeats and asserts the stored state reflects the LATEST one (state, since, task, counters) with last_refresh updated server-side
- S2S propagation: presence propagates across server links (same pattern as existing S2S relay) so residents on other servers appear in any server's aggregate view
  - honesty: A linked_servers test asserts a resident connected to server A appears in server B's PRESENCE LIST with the correct server attribution, and presence state survives the case where the update predates the link (burst or re-sync on link)
- Stale-busy watchdog: a client whose last state is busy (non-idle, non-offline) and whose last_refresh is older than configurable stale_after is flagged presumed_hung in the aggregate; a slow-but-alive resident heartbeating through a long LLM call is NOT flagged
  - honesty: Watchdog test: freeze/patch time or use short stale_after — busy client with old last_refresh is presumed_hung=true; same client after one more heartbeat is presumed_hung=false; idle and offline states are never flagged
- Query surface: adopt culture's anticipated shape — client sends PRESENCE LIST; server replies one PRESENCELIST :<json> line per resident (JSON object per wire contract) terminated by PRESENCEEND; servers without the feature answer 421 so culture degrades gracefully
  - honesty: Wire shape is byte-compatible with culture's adapter expectation: 'PRESENCE LIST' in, one 'PRESENCELIST :<json>' per resident out, 'PRESENCEEND' terminator; the JSON object carries exactly {nick, server, state, since, task, tokens_in, tokens_out, presumed_hung, last_refresh}
- Wire syntax (authoritative, presence.md@b69705e): publish is 'PRESENCE :<json>' — JSON object as single trailing param; fields state (required, enum), since (required, ISO-8601 UTC), task (optional, capped 128 chars), tokens_in/tokens_out (optional cumulative ints, OMITTED not null when unknown); whole line <=512 bytes; publish is fire-and-forget, no ack. Server transitions a resident to offline implicitly on disconnect/QUIT — no final PRESENCE line required
  - honesty: Parser tests cover: valid full payload, state-only payload (no task/tokens), task truncation/rejection at 128 chars, invalid state enum, malformed JSON, and missing required fields — with defined non-crashing behavior for each

## Honesty conditions

- End-to-end proof on a linked pair: resident publishes PRESENCE on server A; culture-shaped 'PRESENCE LIST' on server B returns that resident's row with correct server attribution; the full pytest suite stays green; culture's adapter shape is consumed unmodified
- Culture's t5/t7 transport adapter consumes the reply unmodified — proven by matching their anticipated shape (PRESENCE LIST / PRESENCELIST json / PRESENCEEND / 421 degrade) byte-for-byte in tests
- Verified against 9.11.0: PRESENCE currently answers 421 via the stock unknown-verb path (regression-asserted so the degrade contract holds for older servers)
- A linked-pair end-to-end test shows one PRESENCE LIST containing BOTH a local and a remote resident, every row carrying all nine keys with correct server attribution
- The aggregate alone is sufficient for culture to compute active-resident counts and per-resident token spend — no additional server round-trips or side channels needed
- The diff contains no code path where presence state or token counters gate/defer/reject any command, message, or connection; git grep culture_core over the diff is empty
- Each acceptance item maps to a named pytest (watchdog flag, heartbeat non-flag, byte-shape) or a PR-description checklist item (suite green, #53 reply posted)
- pyproject.toml lands at 9.12.0 in the PR (version-bump CI check green) and the merged release publishes agentirc-cli 9.12.0 to PyPI
- ServerConfig gains the presence keys with defaults; from_yaml on an existing culture server.yaml WITHOUT the keys still loads (backward compat test)
- ServerConfig defaults are 30/90; a config with stale_after <= heartbeat_interval fails at load with a clear error; culture's shipped docs/resident-presence.md values match ours exactly
- A byte-level test asserts the exact reply lines including the ':End of presence list' trailing and that an agentirc WITHOUT the feature (i.e. current 9.11.0 behavior) already produces 421 for PRESENCE
- The skill registers via IRCd.register_skill with commands={'PRESENCE'}; an unmodified 9.11.0 server (skill absent) answers PRESENCE with stock 421 — proven by the existing dispatch path, no gating code added
- linked_servers tests prove: (1) a presence update on A appears in B's LIST; (2) no relay loop (count_until_idle style, mirroring test_federated_event_does_not_loop); (3) a client connected BEFORE the link is established still appears on the peer after link-up (snapshot re-emit); (4) SERVER_UNLINK flips the lost server's rows to offline
- With stale_after configured small in a test: a client that publishes a busy state then goes silent (connection open) reads presumed_hung=true after stale_after; one more heartbeat flips it back to false; idle/offline rows never flag
- from_yaml on culture's shipped presence: section yields 30/90; absent section yields the same defaults; stale<=heartbeat raises at load with a clear message; unknown keys inside presence: are ignored like other culture-only keys
- A byte-level test parses each PRESENCELIST trailing param as JSON and asserts exactly the nine keys, null task/tokens for a state-only publisher, and ISO-8601 UTC since/last_refresh
- Code-read of 9.11.0 _handle_sevent + emit_event confirms unknown type strings flow as custom events without exception (or a mixed-version test proves it); finding otherwise forces a typed-relay redesign before merge
- The watchdog acceptance test drives a real TCP client that publishes working, then stalls silently; it is flagged within stale_after with zero further writes from the client

## Success signals

- Acceptance: (1) a kill -9'd resident that last reported busy is flagged presumed-hung within stale_after with zero cooperation from the dead process; (2) a slow-but-alive resident heartbeating through a long LLM call is NOT flagged; (3) culture's adapter consumes PRESENCE LIST -> PRESENCELIST/PRESENCEEND unmodified; (4) full pytest suite passes; (5) reply posted on #53 with surface shape + release version

## Scope / boundaries

- Observe-only v1: the server never declines, defers, or blocks work based on presence or spend; budget warnings are computed culture-side. The emitter (client harness) is cultureagent's parallel task t4 — agentirc implements only the IRCd side. Wire/identity strings stay culture.* (config filename, telemetry names), never culture_core.*

## Non-goals

- No enforcement, no budget math, no client-side emitter, no irc-lens page — those are culture-side tasks t4/t5/t7/t8

## Assumptions

- A 9.11 peer receiving SEVENT presence.update tolerates the unknown event type (the 9.5.0 EVENTPUB feature already federates custom-typed events), so a mixed-version mesh degrades gracefully — to be code-verified during implementation
- kill -9 that produces a clean TCP FIN yields an accurate 'offline' row (the contract mandates implicit offline on disconnect); presumed_hung covers silent deaths — partitions, lost FINs, stalled-but-connected processes — so the acceptance test simulates a client that stops heartbeating WITHOUT disconnecting

## Decisions

- Release: lands as agentirc-cli 9.12.0 (minor bump over merged 9.11.0) — this is the version floor culture pins (plan risk r4)
- Config: stale_after and heartbeat interval land in ServerConfig / server.yaml (culture-only keys remain ignored-if-unknown for older agentirc); defaults are ours to pick and document (plan risk r1)
- Adopt culture's shipped config defaults verbatim (resolves plan risk r1 for agentirc): heartbeat_interval_seconds=30, stale_after_seconds=90, validated positive ints with stale_after strictly greater than heartbeat_interval, fail-fast at config load
- No CAP gating in v1: PRESENCE / PRESENCE LIST are plain new verbs (contract says a 'culture/presence' capability notification stream is a possible v2, out of scope); v1 never relays PRESENCE to clients — the server consumes it solely for aggregation
- Terminator lines match the anticipated contract byte-for-byte: 'PRESENCELIST :<json>' per resident, 'PRESENCEEND :End of presence list' terminator, and unsupported servers answer '421 <nick> PRESENCE :Unknown command'
- Implementation shape: a PresenceSkill (agentirc/skills/presence.py) following the rooms/threads/history/icon precedent — owns the PRESENCE verb (publish + LIST subcommand via skill.commands), the per-resident registry, and offline transitions via event hooks; PRESENCELIST/PRESENCEEND replies mirror the THREADS/THREADSEND template byte-shape
- S2S via the existing event bus: presence updates emit Event(type='presence.update') through IRCd.emit_event, free-riding the generic SEVENT relay with its origin-tag loop prevention (no new typed S2S verb, no hop counts); burst-on-link = on SERVER_LINK the skill re-emits the local presence snapshot (idempotent overwrites); on SERVER_UNLINK rows attributed to the lost server flip to offline; 'presence.update' joins NO_SURFACE_EVENT_TYPES so 30s heartbeats never spam #system
- presumed_hung is computed at read time (state is busy AND now - last_refresh > stale_after) rather than by a background sweep task — observably identical for the aggregate view ('flagged within stale-T' holds for any read after stale-T), zero new task lifecycle, and tests need only a small configured stale_after, no time-freezing
- Config: a PresenceConfig dataclass (heartbeat_interval_seconds=30, stale_after_seconds=90) on ServerConfig, parsed from the nested 'presence:' YAML section exactly as culture ships it (TelemetryConfig precedent), so one ~/.culture/server.yaml keeps driving both daemons; fail-fast at load when stale_after <= heartbeat_interval
- PRESENCELIST rows always emit all nine keys ({nick, server, state, since, task, tokens_in, tokens_out, presumed_hung, last_refresh}) with JSON null for unknown task/token values — stable shape for culture's parser; since/last_refresh are ISO-8601 UTC
- Offline-row retention (q1, user-decided): on disconnect a resident's row flips to state=offline and STAYS in the aggregate — keyed by nick, overwritten on reconnect, cleared on server restart; no TTL/prune in v1

## Open / follow-up

- Whether presence transitions should also emit into the existing EVENTSUB event stream (EVENT_TYPE_PRESENCE_*) so bots can react — contract is silent; keep v1 scope tight, revisit as v2 alongside the culture/presence CAP notification stream
