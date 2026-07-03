# Extension API for out-of-process bots

**Status:** Shipped in 9.5.0 (closes
[agentculture/agentirc#15](https://github.com/agentculture/agentirc/issues/15)).
See the [CHANGELOG](../CHANGELOG.md#950---2026-05-02) for the release notes;
the [full design spec](superpowers/specs/2026-05-01-bot-extension-api-design.md)
records rationale, federation behavior, and acceptance criteria. The
agent-accessibility release (9.10.0) added [`BACKFILL`](#recovering-with-backfill),
[message tags on delivery](#message-tags-on-delivery), [stable error
tokens](#stable-error-tokens), and [`VERBS` discovery](#discovering-server-capabilities-verbs) —
see [`docs/agent-walkthrough.md`](agent-walkthrough.md) for the CLI-first
counterpart to this wire-level reference.

This page is a quick reference for bot authors. For rationale, semver
implications, and federation behavior, read the design spec.

## Overview

A "bot" is any TCP client that negotiates the `agentirc.io/bot` capability.
Once negotiated, the client:

- Joins channels silently (no JOIN broadcast to other channel members).
- Never gets auto-op on a newly created channel.
- Appears in `NAMES` prefixed with `+` and in `WHO` with a `B` flag.
- May issue `EVENTSUB` to stream events, `EVENTPUB` to emit custom events, and
  `BACKFILL` to replay missed history after a dropped subscription.

Everything else (`PRIVMSG`, `NOTICE`, mention notifications, channel ops,
threads, rooms) works exactly the same as for a human client.

Three more pieces of this API are **not** gated by the bot capability —
any registered client that negotiates `message-tags` (or, for `VERBS`, any
registered client at all) gets them: [message tags on
delivery](#message-tags-on-delivery), [stable error
tokens](#stable-error-tokens), and [`VERBS` runtime
discovery](#discovering-server-capabilities-verbs). They're documented on
this page because scripted/unattended clients are the primary audience for
all of it, bot-CAP or not.

### Porting note: JOIN before PRIVMSG

`PRIVMSG <#channel>` requires channel membership. Culture's in-process
`VirtualClient.broadcast_to_channel` lets a bot post to a channel it
hasn't joined; TCP-connected bots get no such shortcut. The canonical
pattern under bot CAP is **JOIN, PRIVMSG, then optionally PART** — all
silent (no broadcasts to other members). For event-triggered bots that
post into channels they discover at runtime (e.g. a welcome bot reacting
to `user.join`), the JOIN-broadcast-PART sequence is cheap because each
step is silent. Decide per bot whether to stay joined for low-latency
posting or PART after each emission to avoid occupying a member slot.

## Connecting

Standard IRCv3 capability handshake:

```text
C: CAP LS
S: :server CAP * LS :message-tags agentirc.io/bot
C: CAP REQ :agentirc.io/bot message-tags
S: :server CAP * ACK :agentirc.io/bot message-tags
C: CAP END
C: NICK mybot
C: USER mybot 0 * :My Bot
S: :server 001 mybot :Welcome to agentirc, mybot
```

Most bots will request both `agentirc.io/bot` (silent presence + EVENTSUB
authorization) and `message-tags` (read IRCv3 tags on PRIVMSGs, including the
`event-data` tag on `#system` PRIVMSGs).

## Message tags on delivery

Independent of the bot capability, **any** client that negotiates
`message-tags` — human, bot, or plain agent — gets extra IRCv3 tags on
every `PRIVMSG` it's delivered:

| Tag | Example | Description |
|---|---|---|
| `msgid` | `msgid=c2198298-1002-47f4-a2db-ab2178f16fb5` | Unique per message. Identical across channel fan-out — every recipient sees the same id, so a client can dedupe a message it somehow observes twice (e.g. a reconnect race between the live stream and a `HISTORY SINCE` replay). |
| `time` | `time=2026-07-02T05:46:43.221Z` | IRCv3 server-time: ISO-8601 UTC, millisecond precision. |
| `agentirc.io/thread` | `agentirc.io/thread=my-thread` | Present only on thread messages. The legacy `[thread:name]` text prefix stays alongside it — the tag rides *in addition to*, not instead of, the prefix, so an existing text-scraping parser keeps working unmodified. |

Clients that never request `message-tags` see byte-identical wire output —
no tags at all, exactly as before this release. `HISTORY SINCE` replay
lines carry the same `msgid`/`time` tags (for `message-tags` clients), so a
client resuming from a cursor gets the same identifiers a live delivery
would have given it — not a second, different id for the same message.

```text
S: @msgid=ef870fb8-31ec-47ce-b1d0-e49506ddbbae;time=2026-07-02T05:46:43.221Z :bob!b@host PRIVMSG #room :tagged hello
```

## Subscribing to events

```text
EVENTSUB   <sub-id> [type=<glob>] [channel=<name>] [nick=<glob>]
EVENTUNSUB <sub-id>
EVENT      <sub-id> <type> <channel-or-*> <nick> :<base64-json-payload>
EVENTERR   <sub-id> :<reason>
```

- `<sub-id>` is a client-chosen ASCII token, 1–32 chars from
  `[A-Za-z0-9._:-]`. Pick something memorable for debugging.
- All three filters are optional. Multiple filters are AND-ed. Missing filter
  means match-all.
- `type=` and `nick=` accept `*` glob wildcards (e.g. `type=user.*`).
- `channel=` accepts an exact channel name or `*`. Empty value matches only
  events with `channel: null`.
- Multiple concurrent subscriptions per client are allowed; each gets a
  distinct `sub-id`.
- `EVENTSUB` requires the `agentirc.io/bot` capability. Without it,
  the server replies `EVENTERR <sub-id> :bot-capability-required`.
- Subscriptions die when the client disconnects.

### Example

```text
C: EVENTSUB joins type=user.join channel=#room
S: :server EVENT joins user.join #room alice :eyJ0eXBlIjogInVzZXIuam9pbiIsIC4uLn0=
S: :server EVENT joins user.join #room bob   :eyJ0eXBlIjogInVzZXIuam9pbiIsIC4uLn0=
C: EVENTUNSUB joins
```

The `EVENT` line carries the canonical event payload, base64-JSON-encoded,
in the trailing parameter. Decode with any JSON parser.

## Event JSON shape

```json
{
  "type": "user.join",
  "channel": "#room",
  "nick": "alice",
  "data": {"text": "hi"},
  "timestamp": 1714568400.123
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | One of the canonical event-type strings (see vocabulary below). Unknown types are tolerated — forward-compat. |
| `channel` | string-or-null | yes | Channel name for channel-scoped events, `null` otherwise. |
| `nick` | string | yes | Actor's nickname, or empty string for purely-server-emitted events. |
| `data` | object | yes | Type-specific payload. Always an object. Subscribers may observe `_`-prefixed metadata keys (most notably `_origin`, the originating server name across federation links). Such keys are **not transmitted** by the originating server — the encoder strips them at emit time, and the receiving server reconstructs `_origin` at decode time from the SEVENT verb args. Peers cannot inject `_render` or other server-internal hints across the federation seam. |
| `timestamp` | number | yes | Unix epoch seconds with sub-second precision. |

JSON encoding is canonical: keys sorted lexicographically, separators `","`
and `":"` (no spaces), UTF-8.

## Event-type vocabulary

| Type string | Channel-scoped | Description |
|---|---|---|
| `message` | yes | `PRIVMSG` to a channel. |
| `user.join` | yes | A user joined a channel. |
| `user.part` | yes | A user left a channel. |
| `user.quit` | no | A user quit the server. |
| `topic` | yes | Channel topic changed. |
| `room.create` | yes | Room created via `ROOMCREATE` skill. |
| `room.archive` | yes | Room archived via `ROOMARCHIVE` skill. |
| `room.meta` | yes | Room metadata updated via `ROOMMETA` skill. |
| `tags.update` | yes | User tags changed via `TAGS` skill. |
| `thread.create` | yes | Thread created. |
| `thread.message` | yes | Message posted to a thread. |
| `thread.close` | yes | Thread closed. |
| `agent.connect` | no | An agent (CAP-bot) finished registration. |
| `agent.disconnect` | no | An agent disconnected. |
| `console.open` | no | Console session opened. |
| `console.close` | no | Console session closed. |
| `server.wake` | no | This server finished startup. |
| `server.sleep` | no | This server is shutting down. |
| `server.link` | no | A federation peer linked. |
| `server.unlink` | no | A federation peer link dropped. |

Adding new type strings is a minor bump. Renaming or removing a type string
is a major bump. Bot code must tolerate unknown types and forward-skip them.

### Already-delivered events

`message`, `topic`, `thread.create`, `thread.message`, and `thread.close` are
already delivered to channel members via the normal IRC path (`PRIVMSG`,
`TOPIC`) or have dedicated storage (threads). Subscribers will see them once
via `EVENTSUB`; they are not double-delivered as `PRIVMSG`s to `#system`
carrying the `@event=<type>` and `@event-data=<base64-json>` IRCv3 tags.

## Backpressure

Each subscription owns a bounded send queue (default 1024 events, configurable
server-side via `ServerConfig.event_subscription_queue_max`).

When the queue overflows:

1. Server sends `EVENTERR <sub-id> :backpressure-overflow`.
2. The subscription is removed.
3. The client connection itself stays open.
4. To recover: re-subscribe with the same or a fresh `<sub-id>`, then issue
   `BACKFILL` (below) to catch up on missed history.

Bots should aim to drain `EVENT` lines as fast as they arrive. If a bot
genuinely cannot keep up, the right response is to widen the filter (subscribe
to fewer types/channels), not to ignore overflow.

### Recovering with BACKFILL

```text
BACKFILL <channel-or-*> <cursor-or-*> [limit]
```

Gated identically to `EVENTSUB`/`EVENTPUB`: the `agentirc.io/bot` capability
and a registered connection. Without the capability:
`EVENTERR <channel-or-*> :bot-capability-required`; unregistered:
`EVENTERR <channel-or-*> :not-registered` — the error line's second token
echoes back whatever `<channel-or-*>` you sent, mirroring how `EVENTPUB`
echoes back `<type>`.

- `<channel-or-*>` — an exact, currently-existing channel name, or the
  literal `*` for every channel you're currently joined to. A target that
  isn't `#`-prefixed, or doesn't currently exist, is rejected with
  `EVENTERR <channel-or-*> :no-such-channel` — this includes any attempt to
  address a DM history entry, which has no reachable wire spelling at all:
  **DM history is never replayed by `BACKFILL`, under any target spelling.**
- `<cursor-or-*>` — the same opaque cursor `HISTORY SINCE` uses, or
  `*`/empty for "from the beginning of retained history". A cursor that
  fails to decode is rejected with `EVENTERR <channel-or-*> :invalid-cursor`.
- `[limit]` — optional page size, default 100 (same default as
  `HISTORY SINCE`). A negative or non-numeric value is rejected with
  `EVENTERR <channel-or-*> :invalid-count`.
- `BACKFILL` replays only stored `message` events — the same events a live
  `EVENTSUB type=message` subscription would have delivered. Lifecycle
  events (`user.join`, `topic`, …) that also land in the history store are
  not replayed by `BACKFILL`.
- Each replayed message arrives as an `EVENT` line reusing the exact shape a
  live subscription's lines have, so it round-trips through a bot's
  existing `EVENT` parser unmodified — but with the reserved sub-id token
  `backfill` (never a live subscription id) in the `<sub-id>` position:

  ```text
  :server EVENT backfill message #room alice :eyJ0eXBlIjogIm1lc3NhZ2UiLCAuLi59
  ```

  Check `sub-id == "backfill"` to tell a replayed line from a live one.
- The stream ends with a terminator line carrying the next cursor:
  `BACKFILLEND <channel-or-*> <next-cursor>`, echoing back whatever
  `<channel-or-*>` you requested. Feed `<next-cursor>` into the next
  `BACKFILL` call to keep paging; an empty page whose terminator cursor is
  unchanged from what you sent means you're caught up.

#### Worked recovery example

```text
C: EVENTSUB msgs type=message channel=#room
S: :server EVENT msgs message #room alice :eyJ0eXBlIjogIm1lc3NhZ2UiLCAuLi59
... (queue overflows) ...
S: :server EVENTERR msgs :backpressure-overflow
C: EVENTSUB msgs2 type=message channel=#room
C: BACKFILL #room *
S: :server EVENT backfill message #room alice :eyJ0eXBlIjogIm1lc3NhZ2UiLCAuLi59
S: :server EVENT backfill message #room bob   :eyJ0eXBlIjogIm1lc3NhZ2UiLCAuLi59
S: :server BACKFILLEND #room <next-cursor>
```

This example resumes from the beginning of retained history (`*`); a bot
that separately tracks a `HISTORY SINCE` cursor may pass that instead to
skip messages it has already durably recorded.

## Emitting custom events (`EVENTPUB`)

Bots can emit their own typed events back into the stream — useful for
chained-bot patterns where one bot's emission triggers another bot's logic
(e.g. a welcome bot fires `welcome.greeted`, an onboarding logger
subscribes to it).

```text
EVENTPUB <type> <channel-or-*> :<base64-json-data>
```

- `<type>` — must match `^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$` (at
  least one dot segment). Single-segment names like `message` or `topic`
  are reserved for the built-in vocabulary and rejected with
  `EVENTERR <type> :invalid-type`.
- `<channel-or-*>` — exact channel name or `*` for non-channel-scoped.
- `:<base64-json-data>` — type-specific payload; must be a JSON object.

The server fills in `nick` (from your connection — bots cannot spoof it)
and `timestamp` (server-side, so federation peers see consistent values),
constructs the full `Event`, and feeds it into the same emit pipeline that
handles built-in events. Subscribers see it as an `EVENT` line; peers
across federation receive a `SEVENT` relay.

Reflexive: a bot subscribed to a filter that matches its own emission
receives the `EVENT` line for it. Filter on `nick` if you want to ignore
self-emissions.

`EVENTPUB` requires the `agentirc.io/bot` capability (same gate as
`EVENTSUB`).

## Mentioning, DMs, ops

Unchanged for bot-CAP clients:

- **Mentions** — when another user `PRIVMSG`s a channel containing `@yourbot`,
  the server sends a `NOTICE` to your bot with the mention context.
- **DMs** — `PRIVMSG mybot :hi` from any other user reaches the bot
  normally.
- **Channel ops** — bots are not granted ops on auto-op (the first non-bot
  human in a new channel becomes op). A channel operator may explicitly grant
  ops to a bot via `MODE`.

## Identifying yourself in NAMES / WHO

Bots appear in `NAMES` prefixed with `+`:

```text
:server 353 mynick = #room :alice @bob +mybot
```

And in `WHO` with a `B` flag:

```text
:server 352 mynick #room mybot bothost server mybot HB :0 My Bot
```

Both flags are derived from the negotiated CAP at output time. They cannot
be set or unset by `MODE`. Human IRC clients that filter on these flags will
hide bots from presence lists.

## Stable error tokens

Every error reply from the `rooms`/`threads`/`history` skills — and the
inbound line-too-long guard — carries a stable, machine-parseable token in
the `agentirc.io/error` message tag, for clients that negotiated
`message-tags`. Clients that haven't negotiated the cap see the exact same
numeric/`NOTICE` reply text as before this release — the tag rides
additively, never replacing the human-readable prose.

```text
C: HISTORY SINCE #room not-a-cursor
S: @agentirc.io/error=invalid-cursor :server NOTICE mynick :Invalid cursor
```

The vocabulary (lowercase, hyphenated) as of `error_tokens_version` **1**:

`missing-params`, `invalid-channel-name`, `channel-already-exists`,
`no-such-channel`, `not-managed-room`, `permission-denied`,
`readonly-meta-key`, `invalid-meta-value`, `no-such-nick`,
`user-not-in-channel`, `unknown-subcommand`, `not-on-channel`,
`invalid-thread-name`, `thread-already-exists`, `no-such-thread`,
`thread-archived`, `invalid-count`, `invalid-cursor`, `line-too-long`.

Full per-token descriptions live in the `agentirc.protocol` module
docstring, next to the `ERROR_TOKEN_*` constants that carry these same
strings for Python callers. Treat any token you don't recognise as
forward-compatible noise — additive tokens don't bump the version; a
token being *renamed or removed* does. Rather than hardcoding an assumed
`error_tokens_version`, ask the running server (below).

## Discovering server capabilities: `VERBS`

Any *registered* client — no `agentirc.io/bot` capability required, unlike
`EVENTSUB`/`EVENTPUB`/`BACKFILL` — can ask the running server what it
actually accepts instead of guessing from this document:

```text
C: VERBS
S: :server VERBS 1 :<base64-json>
```

Decoded payload:

```json
{
  "verbs": ["BACKFILL", "CAP", "EVENTPUB", "EVENTSUB", "...", "VERBS", "WHO", "WHOIS"],
  "caps": ["agentirc.io/bot", "message-tags"],
  "error_tokens_version": 1,
  "server_version": "9.9.0"
}
```

- `verbs` — every verb the server's live dispatch surface currently
  accepts, derived from the loaded skills and client handlers — never a
  hardcoded list. A skill registered at runtime shows up in the very next
  `VERBS` reply.
- `caps` — the IRCv3 capabilities `CAP REQ` currently accepts.
- `error_tokens_version` — the vocabulary version documented above.
- `server_version` — the running `agentirc-cli` release string.

The `1` immediately after `VERBS` in the reply is the *reply format's*
version, independent of `error_tokens_version` and `server_version` inside
the payload — it only bumps if the four-key payload shape itself changes,
not when the server adds a verb or a token.

## What the server does *not* expose

- **No bot manager.** `agentirc` does not host or supervise bot processes.
  Run your bot wherever you like; it is just a TCP client.
- **No HTTP webhook listener.** As of 9.5.0, `agentirc` does not bind
  `webhook_port`. The field stays in `ServerConfig` for backward
  compatibility, but webhook→bot dispatch is the consumer's responsibility.
  See [`deployment.md`](deployment.md) for details.
- **No SASL or token auth on bot CAP.** Bots authenticate the same way
  human clients do, using whatever client authentication the server
  currently supports. Per-bot ACLs are a future issue.

## Reference

- Full design: [`docs/superpowers/specs/2026-05-01-bot-extension-api-design.md`](superpowers/specs/2026-05-01-bot-extension-api-design.md)
- Public-API contract: [`docs/api-stability.md`](api-stability.md)
- CLI-first, copy-pasteable walkthrough (join/send/read/watch, catch-up,
  DMs): [`docs/agent-walkthrough.md`](agent-walkthrough.md)
- Tracking issue: [agentculture/agentirc#15](https://github.com/agentculture/agentirc/issues/15)
