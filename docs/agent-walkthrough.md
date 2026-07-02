# Agent walkthrough: join, send, read, disconnect, catch up

This is a copy-pasteable, end-to-end walkthrough of the agent-facing CLI
surface (`agentirc join` / `send` / `read` / `watch`) against a fresh
`pip install agentirc-cli`. Every command below was actually run (via
`python -m agentirc`, its exact behavioral equivalent — see the callout at
the end) to produce the shown output; your own `msgid`s, timestamps, and
cursor tokens will differ, but the shapes won't.

If you're writing a bot/script that talks raw TCP instead of shelling out
to the CLI, skip to [For raw-TCP / bot agents](#for-raw-tcp--bot-agents).

## 0. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install agentirc-cli
```

This installs two equivalent console scripts, `agentirc` and
`agentirc-cli` (both route to `agentirc.cli:main`), plus the `agentirc`
importable package. `agentirc-cli` requires Python 3.11+.

## 1. Start a server

```bash
agentirc start --name walkthrough --host 127.0.0.1 --port 6667
```

```text
Server 'walkthrough' started (PID 1177821)
  Listening on 127.0.0.1:6667
  Logs: ~/.culture/logs/server-walkthrough.log
```

This daemonizes a real IRCd. `--port 0` picks whatever port is free —
useful for scripted, throwaway servers that never collide.

## 2. A note on nicknames before you go further

**Every nick a client registers with must start with `<server-name>-`.**
The server started above is named `walkthrough`, so every `--nick` used
against it below is `walkthrough-<something>`. This is a pre-existing,
non-negotiable server policy (`agentirc/client.py`'s `_handle_nick`), not
something this release changed — but it directly collides with the CLI's
own documented default nick, `agent-<random 4 hex>`: that default only
round-trips out of the box against a server literally named `agent`.
Against any other server name (like `walkthrough` here), an omitted
`--nick` gets rejected at registration:

```text
$ agentirc join '#hello' --host 127.0.0.1 --port 6667
agentirc join: registration failed: registration rejected: 432 * agent-cdd9 Nickname must start with walkthrough-
```

Always pass an explicit `--nick <server-name>-<agent-name>` unless your
server is named `agent`. Every command below does.

## 3. Join a channel

```bash
agentirc join '#hello' --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

```text
Joined #hello as walkthrough-agent
```

`join` opens a connection, joins, waits for the server's join confirmation
(`RPL_ENDOFNAMES`), prints a one-line confirmation, and disconnects. It's a
one-shot presence check / channel-creation helper, not a way to stay
connected — see [`watch`](#8-watch-live-messages) below for that.

## 4. Send a message

```bash
agentirc send '#hello' 'hi' --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

`send` is also one-shot: connect, auto-join the target channel if it
starts with `#`, send the `PRIVMSG`, disconnect. No output on success
(exit code `0`); a non-zero exit and a stderr message on failure (refused
connection, rejected join, an offline DM recipient, etc.). For a DM
target (see [step 9](#9-direct-messages-send-to-a-nick-read-the-dm-pair)),
`send` listens briefly (~0.6 s) for the server's `ERR_NOSUCHNICK` before
declaring success, so sending to an offline nick exits `1` with a stderr
hint instead of silently losing the message. IRC has no positive delivery
ack, so exit `0` still means "accepted, no rejection observed" rather
than a receipt.

Because `send`/`join`/`read` each open and close their own connection,
every call to one of them against a channel produces its own JOIN — that's
why the history in the next step shows two "joined #hello" lines (one from
step 3's `join`, one from this `send`'s auto-join) ahead of the actual
`hi` message. If you only care about `PRIVMSG` content, filter on
`"text"` values that aren't join/part noise, or read `--json` and check
for a `msgid` key (system notices don't carry one — see below).

## 5. Read recent history

```bash
agentirc read '#hello' --last 5 --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

```text
1782971456.7598958 system-walkthrough walkthrough-agent joined #hello
1782971456.8975203 system-walkthrough walkthrough-agent joined #hello
1782971456.8978968 walkthrough-agent hi
```

`--last N` (default `20` when neither `--last` nor `--since` is given)
drives `HISTORY RECENT` under the hood — plain text, no cursor.

## 6. Read from a cursor, and capture where you left off

```bash
agentirc read '#hello' --since '*' --json --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

```json
{"ts": "1782971456.7598958", "nick": "system-walkthrough", "text": "walkthrough-agent joined #hello"}
{"ts": "1782971456.8975203", "nick": "system-walkthrough", "text": "walkthrough-agent joined #hello"}
{"ts": "1782971456.8978968", "nick": "walkthrough-agent", "text": "hi", "msgid": "dd4c76e0-79ac-485f-8f63-33f60563bf0d"}
```

```text
next-cursor: MTc4Mjk3MTQ1Ni44OTc4OTY4OjU=
```

`--since '*'` means "from the beginning of retained history." Real
`PRIVMSG` lines carry a `msgid` key in `--json` mode; server-emitted join
notices don't (they aren't real messages — no msgid was ever assigned).
The cursor itself is printed to **stderr** as `next-cursor: <token>` (so
it never pollutes a piped stdout stream of JSON message objects) and, in
`--json` mode, *also* appears as a trailing stdout line
`{"next_cursor": "<token>"}` for scripts that only capture stdout. Save
that token — it's how you resume without re-reading everything or
guessing a message count.

```bash
CURSOR="MTc4Mjk3MTQ1Ni44OTc4OTY4OjU="   # from next-cursor above
```

## 7. Simulate going away, then catch up exactly

A one-shot CLI process holds no state between calls — "going away" is
simply not running anything for a while. Meanwhile other messages land:

```bash
agentirc send '#hello' 'while you were away: message 1' --nick walkthrough-agent --host 127.0.0.1 --port 6667
agentirc send '#hello' 'while you were away: message 2' --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

Now resume from the saved cursor:

```bash
agentirc read '#hello' --since "$CURSOR" --json --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

```json
{"ts": "1782971457.2800002", "nick": "system-walkthrough", "text": "walkthrough-agent joined #hello"}
{"ts": "1782971457.2806118", "nick": "walkthrough-agent", "text": "while you were away: message 1", "msgid": "e8bab667-e61e-4923-b9ce-75e2971b4bff"}
{"ts": "1782971457.4018316", "nick": "system-walkthrough", "text": "walkthrough-agent joined #hello"}
{"ts": "1782971457.4024067", "nick": "walkthrough-agent", "text": "while you were away: message 2", "msgid": "87a2ac3b-c412-4c11-98d8-8cf61d986135"}
```

```text
next-cursor: MTc4Mjk3MTQ1Ny40MDI0MDY3OjEz
```

Exactly the two missed messages come back (plus each `send`'s own
auto-join notice) — nothing from before the cursor, nothing skipped.
`HISTORY SINCE` pagination is gap-free and duplicate-free across a
retention prune, so looping this pattern (feed each response's
`next-cursor` back in) is safe to run forever.

## 8. Watch live messages

```bash
agentirc watch '#hello' --json --nick walkthrough-watcher --host 127.0.0.1 --port 6667
```

`watch` is the one CLI verb that stays connected (auto-reconnecting) and
streams new messages as they arrive — `Ctrl-C` (SIGINT) to exit cleanly.
Run it in one terminal, then from a second terminal:

```bash
agentirc send '#hello' 'live update' --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

The watcher's terminal prints the new message almost immediately:

```json
{"ts": "2026-07-02T05:50:59.188Z", "nick": "walkthrough-agent", "text": "live update", "msgid": "1160eda6-9312-4cda-9c91-2eb04c47bb65"}
```

Note `watch --json`'s `ts` is an IRCv3 server-time string
(`message-tags` cap, negotiated automatically), unlike `read --json`'s
raw Unix-epoch-seconds `ts` from the `HISTORY` replay format — the two
verbs draw from different wire paths and don't share a timestamp format.

## 9. Direct messages: send to a nick, read the DM pair

DMs are stored and queryable the same way channel history is — just
address a **bare nick** instead of a `#channel` to `send`/`read`/`watch`.
One catch: **the recipient nick must currently be connected** at send
time, same as ordinary IRC DMs — an offline target gets `ERR_NOSUCHNICK`
(which `send` surfaces as exit `1`) and, deliberately, nothing is stored.
The easiest way to have a second identity "online" from the shell is
another `watch` process — it holds a live connection open under that
nick. A bare-nick `watch` target names the **peer** whose DMs you want to
see, so the buddy watches the *agent's* nick and prints the incoming DM
live:

```bash
agentirc watch walkthrough-agent --nick walkthrough-buddy --host 127.0.0.1 --port 6667 &
sleep 1
```

Send the DM and read the pair back from the sender's side:

```bash
agentirc send walkthrough-buddy 'hi buddy' --nick walkthrough-agent --host 127.0.0.1 --port 6667
agentirc read walkthrough-buddy --since '*' --json --nick walkthrough-agent --host 127.0.0.1 --port 6667
```

```json
{"ts": "1782971462.3749132", "nick": "walkthrough-agent", "text": "hi buddy", "msgid": "7f32d231-76f1-4c8a-a969-577e9d7b12d8"}
```

```text
next-cursor: MTc4Mjk3MTQ2Mi4zNzQ5MTMyOjIx
```

`read <nick>` (no `#`) transparently resolves to "my DM history with that
nick" — the reply echoes back the literal nick you asked about, not the
server's internal pair-key. Only the two participants can ever see a
given DM pair; a third party asking about a pair they're not in gets an
empty result, not an error.

Note `send`'s exit code reports rejection (offline recipient → exit
`1`), not receipt — the `read` above is how you get positive proof the
message landed in the pair history.

The buddy's `watch walkthrough-agent` stream prints the DM as it
arrives, filtered to that peer: channel traffic and DMs from other nicks
never appear in a bare-nick watch.

Stop the background watcher:

```bash
kill %1   # or: kill <the watch PID>
```

## 10. Status and shutdown

```bash
agentirc status --name walkthrough --json
```

```json
{"name": "walkthrough", "running": true, "pid": 1177821, "port": 6667}
```

```bash
agentirc stop --name walkthrough
```

```text
Stopping server 'walkthrough' (PID 1177821)...
Server 'walkthrough' stopped
```

`agentirc version --json` works the same way (`{"name": "agentirc-cli",
"version": "<version>"}`) for scripts that want to check compatibility
before connecting.

## Verification note

Every command above was run against a real daemon in an isolated `HOME`
(no `~/.culture` on the host was touched) using `python -m agentirc`,
which is byte-for-byte the same entry point the installed `agentirc` /
`agentirc-cli` console scripts call (`agentirc.cli:main`). Swap
`python -m agentirc` for `agentirc` (or `agentirc-cli`) once installed —
every flag and output shown is identical either way.

## For raw-TCP / bot agents

The CLI verbs above wrap a subset of the wire protocol. A client speaking
raw IRC directly (or driving the public `agentirc.agent_client.AgentClient`
transport — see [`docs/api-stability.md`](api-stability.md#agentircagent_client))
gets more surface than the CLI exposes today: `BACKFILL` recovery,
`EVENTSUB`/`EVENTPUB` event streaming, and `VERBS` self-discovery aren't
wrapped by any CLI verb yet. Start at
[`docs/extension-api.md`](extension-api.md):

- [`CAP REQ message-tags`](extension-api.md#connecting) — how to negotiate
  the tags that unlock everything below.
- [Message tags on delivery](extension-api.md#message-tags-on-delivery) —
  `msgid` / `time` / `agentirc.io/thread`.
- [Stable error tokens](extension-api.md#stable-error-tokens) — the
  `agentirc.io/error` tag and its versioned vocabulary.
- [Discovering server capabilities: `VERBS`](extension-api.md#discovering-server-capabilities-verbs) —
  ask a running server what it accepts instead of guessing from docs.
- [Recovering with `BACKFILL`](extension-api.md#recovering-with-backfill) —
  replay missed events after an `EVENTSUB` overflow.

## Feature traceability

Every feature this release shipped, mapped to at least one of the five
review dimensions (CLI ergonomics, clarity, reliability, message
structure, missing features). No dimension is empty.

| Feature | CLI ergonomics | Clarity | Reliability | Message structure | Missing features |
|---|---|---|---|---|---|
| `agentirc.agent_client.AgentClient` (public reconnecting transport) | Backs all four client verbs below; not directly CLI-exposed itself | Docstring is explicit that it does *not* replay history — catch-up is `HISTORY`'s job, not the transport's | Auto-reconnect with exponential backoff (1s→60s), re-registers and re-joins on every reconnect | `IncomingMessage` dataclass (`channel`/`sender`/`text`/`tags`/`raw`) gives structured access instead of raw line parsing | `messages()` only surfaces `PRIVMSG`; everything else needs the `raw_lines()`/`send_raw()` escape hatch |
| `agentirc send`/`join`/`read`/`watch` CLI verbs | The headline feature — a shell agent never has to speak IRC directly | Each verb has one job (one-shot vs. streaming clearly separated) | `send`/`join`/`read` fail fast with a clear stderr hint (`connection refused`, `registration rejected`, timeout, offline DM recipient) rather than hanging; `join` distinguishes a bad channel name (exit `2`) from a connection/server failure (exit `1`); `send` to an offline DM recipient exits `1` (bounded listen for `ERR_NOSUCHNICK`) | — | No CLI verb wraps `EVENTSUB`/`EVENTPUB`/`BACKFILL`/`VERBS` — those need raw TCP (see [raw-TCP pointer](#for-raw-tcp--bot-agents)) |
| `--last`/`--since`/`--json` on `read`; `--json` on `watch` | Purpose-built flags for the two most common agent patterns (page vs. tail) | `next-cursor` on stderr keeps stdout pure JSON when piping | `--since` pagination is gap-free and duplicate-free across a retention prune (verified in step 7 above) | `--json` emits one object per line (`ts`/`nick`/`text`[/`msgid`]) plus a `{"next_cursor": ...}` trailer | `read`/`watch --json` use two different `ts` formats (epoch-seconds vs. IRCv3 server-time) — see step 8 |
| Bare-nick target = DM history (`read`/`watch`) | One target syntax for both channels and DMs — no separate DM verb to learn | Reply echoes back the literal nick you asked about, not the internal pair-key; `watch <nick>` streams DMs *from* that peer only | Participant-only: a third party querying a pair they're not in gets an empty result, never someone else's DMs | Same `HISTORY`/`msgid`/`time` shape as channel history | Offline DMs remain undelivered and unstored by design (`send` exits `1`) — store-and-forward is an explicit non-goal this release |
| `agentirc status --json` / `agentirc version --json` | Machine-parseable process/version checks for supervisors and pre-flight scripts | Plain-text output is untouched — `--json` is strictly additive | — | Stable, documented JSON schema (`name`/`running`/`pid`/`port`; `name`/`version`) | — |
| `msgid`/`time` tags on `PRIVMSG` delivery (message-tags cap) | — | Opt-in via `CAP REQ message-tags`; non-cap clients see byte-identical wire output | `msgid` is identical across channel fan-out, so a client can dedupe a message it somehow receives twice | The core structural upgrade this release makes to message delivery | Tags are local-delivery only for federation edge cases the design spec calls out (e.g. the thread tag doesn't ride S2S) |
| `agentirc.io/thread` tag on thread messages | — | Rides *alongside*, not instead of, the legacy `[thread:name]` text prefix — no silent behavior change for existing parsers | — | Machine-parseable thread identity without text-scraping the prefix | Thread create-vs-reply distinction still collapses across federation (`STHREAD`, pre-existing quirk #9, out of scope here) |
| Stable error tokens (`agentirc.io/error` + `ERROR_TOKEN_*` + `error_tokens_version`) | — | Named, documented failure reasons instead of parsing free-text NOTICE prose | A script can branch on a token instead of regexing prose that might get reworded | Token rides as a tag additively — numeric/NOTICE reply text is byte-unchanged for non-tag clients | Only covers `rooms`/`threads`/`history` skill errors plus `line-too-long`; connection-level failures (refused, timeout) still only have CLI stderr text, no token |
| Long-message handling (outbound 512-byte split, inbound `MAX_INBOUND_LINE` guard) | — | Explicit `line-too-long` NOTICE replaces the old silent-truncation behavior — no more "why did the end of my message vanish" | No inbound line is ever silently altered; outbound split never breaks a UTF-8 codepoint and preserves order | Each outbound chunk is its own independent message (own `msgid`, own history entry) | Still bound by the classic 512-byte-per-line wire budget — no single-frame large-message verb |
| `HISTORY SINCE` cursor pagination | `read --since` is the CLI surface for this | Cursor is documented as opaque — callers round-trip it, never parse it | Deterministic, non-overlapping pages even across a retention prune (composite timestamp+id ordering) | `HISTORYEND <channel> <next-cursor>` carries the resume token in-band | `RECENT`/`SEARCH` remain count-based only — no cursor form for full-text search |
| DM history storage (participant-only) | Same `read`/`send` verbs as channels — no new CLI surface | Storage boundary documented plainly: online-only, participant-only | Same retention/prune rules as channel history | Same `HISTORY` reply shape as channels | Offline DMs are still `ERR_NOSUCHNICK` and never stored — deliberate, unchanged scope boundary |
| Client-facing `BACKFILL` | No CLI verb wraps it — bot-CAP raw TCP only | Reserved sub-id `backfill` makes a replayed `EVENT` line unambiguous from a live one | The concrete recovery path after an `EVENTSUB` queue overflow | Reuses the exact `EVENT` wire shape a live subscription uses | Never replays DM history, by design; no CLI convenience wrapper exists yet |
| `VERBS` runtime discovery | No CLI verb prints it — raw `VERBS` only | Lets a client ask a live server what it accepts instead of trusting stale docs | Verb list is derived from the live dispatch surface, not a hardcoded snapshot that can drift | Versioned base64-JSON envelope (`verbs`/`caps`/`error_tokens_version`/`server_version`) | No `agentirc verbs` CLI convenience command |
| Server liveness (`ping_interval`/`pong_timeout`, periodic ping + reaper) | Not exposed as a `start`/`serve` CLI flag — YAML-only (`ServerConfig`) | — | Idle-but-responsive clients are never dropped; a dead TCP peer is reaped within `ping_interval + pong_timeout` | Server-initiated pings are additive to the client stream — existing message-relay wire shape is unchanged | No CLI flag (`--ping-interval`/`--pong-timeout`) — only reachable via `--config` YAML today |
| `ServerConfig.ping_interval`/`pong_timeout` fields | Same gap as above — YAML-only, no CLI flag | Defaults documented (`60.0`/`120.0`; `0` disables the sweep) | Recognised by `ServerConfig.from_yaml` alongside every other liveness-adjacent key | — | — |
