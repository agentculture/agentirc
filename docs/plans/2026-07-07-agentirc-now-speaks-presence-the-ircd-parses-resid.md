# Build Plan — agentirc now speaks PRESENCE: the IRCd parses resident presence heartbeats, tracks per-client presence state, propagates it across server links, flags presumed-hung residents via a stale-busy watchdog, and serves the aggregate to culture via PRESENCE LIST — unblocking culture resident presence v1 (issue #53)

slug: `agentirc-now-speaks-presence-the-ircd-parses-resid` · status: `exported` · from frame: `agentirc-now-speaks-presence-the-ircd-parses-resid`

> agentirc now speaks PRESENCE: the IRCd parses resident presence heartbeats, tracks per-client presence state, propagates it across server links, flags presumed-hung residents via a stale-busy watchdog, and serves the aggregate to culture via PRESENCE LIST — unblocking culture resident presence v1 (issue #53)

## Tasks

### t1 — Protocol surface: add PRESENCE / PRESENCELIST / PRESENCEEND verb constants, EventType.PRESENCE = 'presence.update', and EVENT_TYPE_PRESENCE_UPDATE to agentirc/protocol.py + __all__ (files: agentirc/protocol.py only)

- covers: c11
- acceptance:
  - from agentirc.protocol import PRESENCE, PRESENCELIST, PRESENCEEND, EVENT_TYPE_PRESENCE_UPDATE succeeds and EventType.PRESENCE == 'presence.update'
  - all five names appear in __all__; the diff to protocol.py is additive-only (no existing constant renamed, removed, or re-valued)

### t2 — Config: PresenceConfig dataclass (heartbeat_interval_seconds=30, stale_after_seconds=90) on ServerConfig, parsed from the nested 'presence:' YAML section (TelemetryConfig precedent), fail-fast validation; tests in tests/test_config_loader.py (files: agentirc/config.py, tests/test_config_loader.py)

- covers: c10
- acceptance:
  - from_yaml on a culture-shaped presence: section yields 30/90; a YAML with no presence: section yields the same defaults (backward compat)
  - stale_after_seconds <= heartbeat_interval_seconds (or non-positive values) raises at load with a clear message; unknown keys inside presence: are silently ignored

### t3 — PresenceSkill core: new agentirc/skills/presence.py (registry keyed by nick, PRESENCE publish parsing per presence.md@b69705e, latest-wins updates, server-side last_refresh stamping, offline flip on disconnect/QUIT with row retention, no CAP gating) + registration in ircd.py mirroring rooms/threads/history/icon; tests in tests/test_presence.py (files: agentirc/skills/presence.py [new], agentirc/ircd.py, tests/test_presence.py [new])

- depends on: t1, t2
- covers: c7, c8, c16, c3, h1, h2, h9, h21
- acceptance:
  - parser tests: valid full payload, state-only payload (task/tokens omitted -> stored as None), task capped at 128 chars, invalid state enum / malformed JSON / missing required fields each get defined non-crashing behavior
  - two heartbeats from one client: stored record reflects the LATEST (state, since, task, counters) with last_refresh updated server-side on each
  - a client that disconnects (QUIT or socket close) has its row flipped to state=offline and RETAINED; reconnect + publish overwrites it
  - PRESENCE works without any CAP REQ; an IRCd booted without the skill answers PRESENCE with stock 421 (degrade regression); another unknown verb still 421s on a full server

### t4 — Query surface: PRESENCE LIST handling in agentirc/skills/presence.py — one PRESENCELIST :<json> per resident (all nine keys, null for unknown task/tokens, ISO-8601 UTC since/last_refresh), PRESENCEEND :End of presence list terminator (THREADS/THREADSEND template); presumed_hung computed at read time (busy AND now-last_refresh > stale_after); byte-level tests appended to tests/test_presence.py (files: agentirc/skills/presence.py, tests/test_presence.py)

- depends on: t3
- covers: c11, c2, c5, c10, h5, h20, h23
- acceptance:
  - byte-level test: 'PRESENCE LIST' yields exactly one 'PRESENCELIST :<json>' line per resident then 'PRESENCEEND :End of presence list'; each trailing param parses as JSON with exactly the nine keys {nick, server, state, since, task, tokens_in, tokens_out, presumed_hung, last_refresh}
  - a state-only publisher's row shows task/tokens_in/tokens_out as JSON null; since/last_refresh are ISO-8601 UTC strings
  - presumed_hung is true iff state is busy (listening/thinking/working/draining) AND now - last_refresh > stale_after — computed at read time, no background task added

### t5 — Federation: presence updates emit Event('presence.update') through IRCd.emit_event free-riding the generic SEVENT relay (origin-tag loop prevention); on SERVER_LINK re-emit the local presence snapshot (idempotent); on SERVER_UNLINK flip that server's rows to offline; add 'presence.update' to NO_SURFACE_EVENT_TYPES in agentirc/events.py; code-verify 9.11.0 _handle_sevent tolerates unknown event types; tests in tests/test_presence_federation.py (files: agentirc/skills/presence.py, agentirc/events.py, tests/test_presence_federation.py [new])

- depends on: t4
- covers: c9, c4, c1, h3, h22, h19
- acceptance:
  - linked_servers test: a resident publishing on A appears in B's PRESENCE LIST with server attributed to A; a LIST on A shows both a local and a remote resident, every row carrying all nine keys
  - no relay loop: federated presence updates are never re-relayed (count_until_idle style, mirroring test_federated_event_does_not_loop)
  - a resident that published BEFORE the link was established appears on the peer after link-up (snapshot re-emit); after unlink, the peer's rows for the lost server read state=offline
  - no #system PRIVMSG surfaces from presence.update events; code-read verdict on 9.11.0 unknown-type SEVENT tolerance recorded in the PR description (redesign escalated if it fails)

### t6 — Stale-busy acceptance tests: tests/test_presence_stale.py with a small configured stale_after — the plan's watchdog acceptance pair as real TCP tests (files: tests/test_presence_stale.py [new])

- depends on: t4
- covers: c10, h4
- acceptance:
  - a client publishes 'working' then goes silent WITHOUT disconnecting: PRESENCE LIST reads presumed_hung=true after stale_after, with zero further writes from the client
  - a client heartbeating through a long 'thinking' span (re-publishing within stale_after) is never flagged; one heartbeat after being flagged flips presumed_hung back to false
  - idle and offline rows are never flagged regardless of last_refresh age

### t7 — Docs + release: PRESENCE section in docs/extension-api.md (publish payload, heartbeat expectation, LIST reply shape, 421 degrade), protocol-surface note in docs/api-stability.md ('extended 9.12.0'), CHANGELOG.md 9.12.0 entry, pyproject.toml bump to 9.12.0 + uv.lock restage (files: docs/extension-api.md, docs/api-stability.md, CHANGELOG.md, pyproject.toml, uv.lock)

- depends on: t4
- covers: c12
- acceptance:
  - pyproject.toml version == 9.12.0 and uv.lock staged alongside it; CHANGELOG has a 9.12.0 heading in Keep-a-Changelog style with bold-leading bullets
  - extension-api.md documents publish syntax, the six-state enum, 30/90 defaults, LIST reply byte-shape, and the 421 degrade for older servers; api-stability.md notes the additive protocol.py extension

### t8 — Final audit: full suite + observe-only + acceptance mapping (files: none — verification only)

- depends on: t5, t6, t7
- covers: c6, c12, h6, h24
- acceptance:
  - pytest -n auto green on the full suite; git grep culture_core over the diff is empty; no code path where presence state or token counters gate/defer/reject any command, message, or connection
  - every spec acceptance item maps to a named test or PR-description checklist item (including the #53 reply carrying surface shape + 9.12.0)

## Risks

- [unknown_nonblocking] Skill event-hook surface: exact mechanism for the presence skill to observe SERVER_LINK / SERVER_UNLINK / QUIT (Skill.on_event signature vs IRCd-level hook) — t5 worker adapts from skill.py; fallback is a small IRCd hook, still additive (task t5)
- [unknown_nonblocking] 30/90 heartbeat/stale defaults are open tuning values (inherited plan risk r1) — expected to move once the live mesh gathers heartbeat data; both are config knobs so retuning needs no code change
- [follow_up] presence.update events are incidentally visible to EVENTSUB subscribers (a natural consequence of the event-bus S2S design) — harmless observe-only exposure; the CAP-gated client notification stream stays a v2 follow-up
