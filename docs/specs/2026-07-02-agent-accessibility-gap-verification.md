# Before-state gap re-verification (task t1)

Re-checks every file:line citation from the before-state of
[`docs/specs/2026-07-01-agentirc-ships-an-agent-accessibility-release-ai-a.md`](2026-07-01-agentirc-ships-an-agent-accessibility-release-ai-a.md)
against the current tree (branch `agent/t1`, off `main`), per that spec's
honesty condition: "each stated gap is re-verified against source at the
cited file:line at plan time before any task is cut from it." One bullet per
citation: current file:line, verdict, and a one-line note on what's there
now.

- **`agentirc/client.py:246-248`** — silent inbound truncation. **Holds**, same lines. `if len(buffer) > 8192: buffer = buffer[-4096:]` inside the read loop of `Client.handle()` — the buffer is silently sliced to its trailing 4096 bytes with no error reply to the client.
- **`agentirc/client.py:314-315`** — `_handle_pong` is a no-op. **Holds**, same lines. `def _handle_pong(self, msg: Message) -> None: pass  # Client responding to our ping` — nothing records the reply or resets a liveness timer; there is no periodic server-to-client PING loop anywhere in `client.py` or `ircd.py`.
- **`agentirc/client.py:914-916`** — DM to absent nick → `ERR_NOSUCHNICK`; DMs never stored. **Holds**, same lines. `_handle_privmsg`'s DM branch: `found = await self._send_to_client(...); if not found: await self.send_numeric(replies.ERR_NOSUCHNICK, ...)`. Confirmed by reading `_send_to_client` (lines 843-877): on a hit it calls `recipient.send(relay)` (or relays S2S) and emits a `MESSAGE` event with `channel=None`; there is no `history_store`/`HistorySkill` call in that path, and `HistorySkill.on_event` only persists events with `channel is not None`. Independently verified by the existing `tests/test_history.py::test_history_does_not_record_dms`, which still passes.
- **`agentirc/client.py:320`** — negotiable caps are exactly `{message-tags, agentirc.io/bot}`; no msgid/server-time tags anywhere. **Holds**, same line. `_SUPPORTED_CAPS: frozenset[str] = frozenset({"message-tags", BOT_CAP})`. `git grep -n "msgid\|server-time" agentirc/ --include='*.py'` (excluding tests) returns nothing — no production code references either tag name.
- **`agentirc/cli.py:742-751`** — verb dispatch table has no client verbs (only server lifecycle + bot). **Holds**, same lines. `_HANDLERS = {"serve": ..., "start": ..., "stop": ..., "restart": ..., "status": ..., "link": ..., "logs": ..., "bot": _bot_dispatch}` — eight entries, all server lifecycle or bot; no `send`/`read`/`watch`/`join`.
- **`agentirc/server_link.py:973-1013`** — BACKFILL implemented server-to-server only; no client handler; `docs/extension-api.md:159-160` promises it to bots. **Holds**, same lines. `_send_backfill_request`, `_should_replay_event`, `_handle_backfill`, `_handle_backfillend` all live on `ServerLink` (S2S federation) and are wired only in `server_link.py`. `git grep -n "BACKFILL" agentirc/` shows zero hits in `client.py` — no client-facing `_handle_backfill`. `docs/extension-api.md` lines 159-160 (inside the EVENTSUB overflow-recovery walkthrough) still read: "3. The client connection itself stays open. 4. To recover: re-subscribe with the same or a fresh `<sub-id>`, then issue `BACKFILL` to catch up on missed history." — the doc promises a client-issuable verb the server does not implement.
- **`agentirc/skills/history.py:117-124`** and **`agentirc/history_store.py:42-60`** — history is last-N only, no since/cursor. **Holds**, same lines both files. `HistorySkill.get_recent(self, channel: str, count: int)` (117-124) takes a plain count and slices `entries[-count:]`. `HistoryStore.get_recent`/`search` (42-60) take `count`/`term`, `ORDER BY timestamp DESC, id DESC LIMIT ?` — no `since`/`cursor`/`after_id` parameter on any public method. The `(channel, timestamp, id)` index the spec cites as available-but-unused is confirmed present at `history_store.py:31`.
- **`agentirc/skills/rooms.py:48`, `skills/history.py:150,169`, `skills/threads.py:165,274,285`** — error replies mix bare NOTICE prose with ad-hoc numerics. **Holds**, same lines all five files/locations.
  - `rooms.py:48` — bare `NOTICE` prose `"Channel name must start with #"` (ROOMCREATE validation).
  - `history.py:150` — bare `NOTICE` prose `f"Unknown HISTORY subcommand: {subcmd}"`.
  - `history.py:169` — bare `NOTICE` prose `"Invalid count"` (HISTORY RECENT with a non-integer count).
  - `threads.py:165` — ad-hoc `command="400"` (not `send_numeric`, not a real IRC numeric) for an invalid THREAD CREATE name.
  - `threads.py:274` — ad-hoc `command="404"` for THREAD REPLY to a nonexistent thread.
  - `threads.py:285` — ad-hoc `command="405"` for THREAD REPLY to an archived thread.
  - None of the six carry a stable named reason token (contrast with `EVENTERR <sub-id> :backpressure-overflow`, which does).

## Summary

All 8 citations (spanning 6 files) still hold verbatim at the exact line numbers the spec cites — nothing has drifted or been fixed since the spec was written. Every wire-level claim above (DM `ERR_NOSUCHNICK`, the six mixed error replies, the HISTORY RECENT/HISTORYEND shape) is now additionally locked as an exact-string characterization test in `tests/test_wire_format_envelope.py` (see the "Client wire-shape characterization" section added by task t1), so later tasks implementing the spec's fixes can diff their new behavior against a byte-exact baseline of the old behavior instead of re-deriving it from source reading.
