# Bot extension API design

**Status:** Proposed
**Date:** 2026-05-01
**Owner:** Ori Nachum
**Receiving agent:** the agent working in this repo
**Tracking issue:** [agentculture/agentirc#15](https://github.com/agentculture/agentirc/issues/15)
**Target version:** 9.5.0
**Blocks:** [agentculture/culture#308](https://github.com/agentculture/culture/issues/308) phase A2

## Summary

`agentirc-cli==9.4.x` exposes three public modules (`config`, `cli`, `protocol`) and an in-process bot manager that runs inside `IRCd` via the `agentirc._internal.bots.*` no-op stubs. Culture replaces those stubs at runtime with the real `culture.bots.*` and gets full bot integration for free. That continues to work today.

Culture's [#308](https://github.com/agentculture/culture/issues/308) deletes culture's vendored `culture/agentirc/*` and shims `culture server start` to `subprocess.run(["agentirc", *argv])`. After that shim lands, the IRCd lives in another process; culture's bots have no documented way to attach. The runtime-stub-replacement trick stops working because there is no in-process IRCd to inject into.

This spec defines the four-piece public extension API that lets out-of-process bots (in any language) attach to a running `agentirc` daemon over the same TCP socket every other client uses:

1. **Public event wire format** — `Event` / `EventType` promoted from internal `agentirc.skill` to public `agentirc.protocol`, with the JSON shape and type-string vocabulary semver-tracked.
2. **Event subscription verbs** — `EVENTSUB`, `EVENTUNSUB`, `EVENT`, `EVENTERR` for streaming events to a connected bot, with filter and backpressure semantics.
3. **Bot capability** — IRCv3 capability `agentirc.io/bot` that, when negotiated, gives a TCP-connected bot the same silent-presence properties as today's in-process `VirtualClient` (silent JOIN, no auto-op, distinguished WHO/NAMES output).
4. **Webhook port ownership** — `agentirc` stops binding `config.webhook_port`. The field stays in `ServerConfig` for backward compatibility and YAML coexistence with culture; binding the port becomes the consumer's responsibility.

All four ship as a single coordinated minor bump (`9.5.0`). Once published, culture's [#308](https://github.com/agentculture/culture/issues/308) Phase A2 (bot rewrite) is unblocked.

## Non-goals

- **Importing `culture.bots.*` inside `agentirc`.** Forbidden by the bootstrap dependency boundary. The `agentirc._internal/bots/` synthesize stubs continue to exist as no-ops for one cycle so any vendored test that imports them keeps working; they are scheduled for deletion in 9.6.0.
- **Backend SDKs (`claude-agent-sdk`, `anthropic`, `agex-cli`, etc.) as runtime deps of `agentirc`.** Unchanged.
- **Wire-format quirk fixes ([#7](https://github.com/agentculture/agentirc/issues/7), [#8](https://github.com/agentculture/agentirc/issues/8), [#9](https://github.com/agentculture/agentirc/issues/9)).** Tracked separately; each requires its own cross-repo coordination. None of those quirks are in the new surface defined here.
- **A binary or richer-typed event payload.** JSON is sufficient and matches what `IRCd._encode_event_data` already produces for the existing `event-data` IRCv3 tag.
- **Authentication beyond shared-secret S2S linking.** A real `SASL`/auth story for bot connections is a separate issue. For now, bots connect on the same listener as humans and identify via CAP.

## Decision A — public event wire format

### Wire format (canonical, JSON)

The event wire format is the contract; Python classes are a convenience for in-process consumers. Bots written in any language pin against this JSON shape.

```json
{
  "type": "user.join",
  "channel": "#room",
  "nick": "alice",
  "data": {"text": "hi", "_origin": "alpha"},
  "timestamp": 1714568400.123
}
```

Field semantics (all five fields are always present in the encoded payload):

- `type` (string, required) — one of the canonical event-type strings (see table below). Unknown strings are tolerated by the parser and forwarded to subscribers; subscribers must be prepared for type strings their version does not recognise.
- `channel` (string-or-null, required) — channel name (e.g. `"#room"`) for channel-scoped events; `null` for nick-scoped or server-scoped events.
- `nick` (string, required) — the actor's nickname; empty string for purely-server-emitted events with no actor.
- `data` (object, required) — type-specific payload. Always an object (possibly empty `{}`). Keys whose name starts with `_` are reserved metadata (e.g. `_origin` carries the originating server's name across federation links). Subscribers receive `_`-prefixed keys verbatim.
- `timestamp` (number, required) — Unix epoch seconds with sub-second precision (Python `time.time()` shape). Stable across the lifetime of a single event; not regenerated when the event is relayed across links.

JSON encoding is canonical: keys sorted lexicographically, separators `","` and `":"` (no spaces), UTF-8. The full five-field object above is encoded then base64-wrapped on the wire.

This is intentionally **not** the same shape that `IRCd._encode_event_data` encodes today. Current `_encode_event_data` (and federated `SEVENT` traffic) serializes the legacy *data-only* payload object produced by `_build_event_payload` — that is, the type-specific `data` dict with `nick`/`channel` merged in and `_`-prefixed metadata stripped. It carries no outer envelope: no `type`, no `timestamp`. The new `EVENT` wire payload defined here is a strict superset and a different shape; existing `SEVENT` / `event-data`-tag decoders will need a small update to handle the five-field envelope. PR-EXT-2 updates `IRCd._encode_event_data` (and the federated `SEVENT` path) to emit the new envelope, so both subscribers and peers see byte-identical canonical JSON; on the federation seam, peers running ≤9.4.x continue to receive the legacy data-only shape until the cross-repo bump lands. (Tracked alongside the wire-format quirk fixes — see [#7](https://github.com/agentculture/agentirc/issues/7), [#8](https://github.com/agentculture/agentirc/issues/8), [#9](https://github.com/agentculture/agentirc/issues/9).)

### Event-type vocabulary

Twenty type strings, all using lowercase segment names. Most are dotted (e.g. `user.join`, `thread.create`); two are single-segment (`message`, `topic`) — these predate `EVENT_TYPE_RE` and are grandfathered into the public vocabulary.

Note that `EVENT_TYPE_RE` in `agentirc._internal.constants` (`^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$`) requires at least one dot segment and therefore does **not** match `message` or `topic`. The regex exists to validate **custom-emitted** type strings (see [`EVENTPUB`](#decision-e--bot-side-event-emission-eventpub) below), not the built-in vocabulary. Type strings the IRCd emits internally bypass the regex; type strings that bots emit via `EVENTPUB` must pass it.

| Type string | Channel-scoped? | Description |
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
| `console.open` | no | A console session opened. |
| `console.close` | no | A console session closed. |
| `server.wake` | no | This server finished startup. |
| `server.sleep` | no | This server is shutting down. |
| `server.link` | no | A federation peer linked successfully. |
| `server.unlink` | no | A federation peer link dropped. |

Adding new type strings is a **minor bump**. Renaming or removing a type string is a **major bump**. Subscribers must tolerate unknown types (forward-compat).

### Python public surface

`agentirc.protocol` (currently constants-only) gains two dataclasses and one enum:

```python
class EventType(StrEnum):
    MESSAGE = "message"
    JOIN = "user.join"
    PART = "user.part"
    QUIT = "user.quit"
    TOPIC = "topic"
    ROOM_CREATE = "room.create"
    ROOM_ARCHIVE = "room.archive"
    ROOMMETA = "room.meta"
    TAGS = "tags.update"
    THREAD_CREATE = "thread.create"
    THREAD_MESSAGE = "thread.message"
    THREAD_CLOSE = "thread.close"
    AGENT_CONNECT = "agent.connect"
    AGENT_DISCONNECT = "agent.disconnect"
    CONSOLE_OPEN = "console.open"
    CONSOLE_CLOSE = "console.close"
    SERVER_WAKE = "server.wake"
    SERVER_SLEEP = "server.sleep"
    SERVER_LINK = "server.link"
    SERVER_UNLINK = "server.unlink"

@dataclass
class Event:
    type: EventType | str
    channel: str | None
    nick: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
```

`StrEnum` (Python 3.11+) lets `EventType.JOIN == "user.join"` evaluate true at JSON boundaries, so both `EventType.JOIN` and the bare string `"user.join"` work in filter specs. The dataclass tolerates a bare string in `type` because federated events whose type is unknown to this version still need to round-trip.

Per-type string constants are **also** exported for callers that prefer non-enum-aware constants (parallel to the existing all-caps verb constants in `protocol.py`):

```python
EVENT_TYPE_MESSAGE = "message"
EVENT_TYPE_USER_JOIN = "user.join"
EVENT_TYPE_USER_PART = "user.part"
# ... one constant per row of the table above ...
```

`agentirc.skill.{Event, EventType}` continues to import-and-re-export from `agentirc.protocol` for one minor cycle so any internal consumer keeps working. `agentirc.skill` remains internal — the re-export is a transitional convenience, not a contract.

`agentirc.events` (render templates and `NO_SURFACE_EVENT_TYPES`) stays internal. The `NO_SURFACE_EVENT_TYPES` set is documented in `extension-api.md` as informational ("these types are already delivered via PRIVMSG / TOPIC / dedicated storage; subscribers will see them via `EVENTSUB` once") but the Python set itself is not part of the public API.

## Decision B — event subscription verbs

### Verb syntax

```text
EVENTSUB   <sub-id> [type=<glob>] [channel=<name>] [nick=<glob>]
EVENTUNSUB <sub-id>
EVENT      <sub-id> <type> <channel-or-*> <nick> :<base64-json-payload>
EVENTERR   <sub-id> :<reason>
```

Verb names are added to `agentirc.protocol`:

```python
EVENTSUB = "EVENTSUB"
EVENTUNSUB = "EVENTUNSUB"
EVENT = "EVENT"
EVENTERR = "EVENTERR"
```

`<sub-id>` is a client-chosen ASCII token, 1–32 characters, `[A-Za-z0-9._:-]`. Multiple concurrent subscriptions per client are allowed; each gets a distinct `sub-id`. Re-using an active `sub-id` results in `EVENTERR <sub-id> :sub-id-in-use` and the existing subscription is unchanged.

### Filter semantics

All three filters are optional and **AND**ed together; missing filter means match-all. At least one filter is recommended to avoid receiving every event on the server.

- `type=<glob>` — matches against the event's `type` field. Glob supports `*` wildcard at any position (e.g. `type=user.*` matches `user.join`/`user.part`/`user.quit`; `type=*` matches everything; `type=thread.create` matches exactly).
- `channel=<name>` — matches against `channel` field. Either an exact channel name (`#room`) or `*` (any channel including `null`). To subscribe only to non-channel-scoped events, use `channel=` with empty value (no channel).
- `nick=<glob>` — matches against `nick` field. Same glob rules as `type`.

Filter parameters are space-separated trailing tokens after `<sub-id>`, in any order. Each parameter appears at most once. Unknown parameters cause `EVENTERR <sub-id> :unknown-filter <name>` and the subscription is rejected.

### Privilege gate

`EVENTSUB` requires the client to have negotiated the `agentirc.io/bot` capability (Decision C). Without it: `EVENTERR <sub-id> :bot-capability-required`; the subscription is not registered.

This is deliberate. Today, any user can read the contents of `#system` (where most events are surfaced as PRIVMSGs), so non-bot clients are not data-deprived by this gate; they just have to consume events the human-readable way. Cap-gating makes the privilege boundary explicit so future tightening (per-bot ACLs, payload redaction) has a place to hook in.

### Streaming format

Each matching event is sent to the subscriber as a single `EVENT` line:

```text
:<server-name> EVENT <sub-id> <type> <channel-or-*> <nick> :<base64-json-payload>
```

The `<channel-or-*>` token is the literal channel name (e.g. `#room`) for channel-scoped events or the literal `*` for events with `channel=null`. The base64-JSON payload is the same canonical encoding produced by `IRCd._encode_event_data` and used for federated `SEVENT` and the existing `event-data` IRCv3 tag — bots can reuse any decoder that already handles those.

Events flow to subscribers in the same order `IRCd.emit_event` produces them. The order across multiple subscribers is consistent (one subscriber's stream is a temporal subsequence of every other subscriber's stream of the same events).

Federated events (events emitted via `SEVENT` from a peer and re-emitted locally with `_origin` set in `data`) are delivered to subscribers verbatim — `_origin` is preserved in the JSON `data` object. Subscribers that want to filter out federation echoes can inspect `data._origin`.

### Backpressure

Each subscription owns a bounded send queue. Default size: `ServerConfig.event_subscription_queue_max = 1024`. A new field is added to `ServerConfig` in 9.5.0, defaulting to 1024 (minor bump per the dataclass-field-with-default rule).

When the queue would exceed its bound:

1. The IRCd sends `EVENTERR <sub-id> :backpressure-overflow` as the next message on the wire.
2. The subscription is removed from the registry (no further events arrive on `<sub-id>`).
3. The client connection itself stays open. The client may re-subscribe with a fresh (or the same) `<sub-id>` and use `BACKFILL` (already in `agentirc.protocol`) to recover missed history, then resume.
4. No automatic disconnect. No partial delivery. The bot confronts overload explicitly.

Drop-the-subscription is chosen over drop-individual-events because the former is unambiguous: after an overflow, the bot knows it has missed an unspecified number of events and must reconcile. Dropping individual events with a counter requires the bot to handle a "you have missed N events" tag on every subsequent event, which is more complex and easier to ignore.

### Federation interaction

`EVENTSUB` is a client-server verb only. Subscribers do not participate in the S2S protocol. A bot subscribed to server A receives events that happened on linked server B because B relays them to A via `SEVENT`, A re-emits via `IRCd.emit_event`, and `_dispatch_to_subscribers` (a new step in `emit_event`) routes them to all matching subscribers regardless of origin.

A bot that wants to subscribe across the whole mesh must connect to one server and let federation deliver. Connecting to multiple servers and de-duplicating is also valid (the `_origin` field is the dedup key) but unnecessary in the common case.

### Wire-up

Implementation touches:

- `agentirc/protocol.py` — add four verb constants, two filter helpers (optional).
- `agentirc/client.py` — `_handle_eventsub`, `_handle_eventunsub` methods.
- `agentirc/ircd.py` `emit_event` — add `await self._dispatch_to_subscribers(event)` step alongside the existing `_dispatch_to_bots`/`_surface_event_privmsg`/`_relay_to_peers` calls.
- New `agentirc/_internal/event_subscriptions.py` — `SubscriptionRegistry` class. Holds `dict[Client, dict[sub_id, Subscription]]`. Subscription owns the filter, the bounded `asyncio.Queue`, and a drain task that pulls from the queue and writes to the client. Cleanup hook on client disconnect.
- `agentirc/_internal/event_subscriptions.py` is internal — the public API is the wire verbs.

## Decision C — bot connection conventions

Bots identify via the existing IRCv3 capability negotiation. One new capability:

```text
agentirc.io/bot
```

The vendored namespace (`agentirc.io/`) follows IRCv3 conventions and prevents collision if `agentirc` ever federates with non-`agentirc` IRC servers that might define a bare `bot` cap with different semantics.

### Negotiation flow

Standard IRCv3 CAP flow, same as today's `message-tags` capability:

```text
C: CAP LS
S: :server CAP * LS :message-tags agentirc.io/bot
C: CAP REQ :agentirc.io/bot message-tags
S: :server CAP * ACK :agentirc.io/bot message-tags
C: CAP END
C: NICK mybot
C: USER mybot 0 * :My Bot
S: :server 001 mybot :Welcome ...
C: EVENTSUB sub1 type=user.* channel=#system
```

`agentirc/client.py` `_handle_cap` extends its `supported` set from `{"message-tags"}` to `{"message-tags", "agentirc.io/bot"}`. When `agentirc.io/bot` is `ACK`'d, the client's `caps` set contains it and four behaviors change for the lifetime of the connection:

### 1. Silent JOIN

Today, `Client._handle_join` adds the client to the channel's member set and broadcasts a JOIN line to every other channel member so they update their local presence. For bot-CAP clients, the broadcast loop is skipped: the bot is added to `Channel.members`, the `Event.JOIN` event is still emitted (subscribers see it via `EVENTSUB`), but no human or non-bot client receives a JOIN line.

This mirrors the in-process `VirtualClient.join_channel`, which today emits the JOIN event but only writes the JOIN line to `member is not self` peers. For TCP bots: skip the per-member broadcast entirely.

PART, QUIT, NICK changes follow the same rule: the IRCd's normal broadcast loop excludes bot-CAP clients from emitting these notifications to other clients. Channel state (membership, op set) is updated normally.

### 2. No auto-op

`Channel.add()` grants ops to the first local non-`RemoteClient` member of a newly created channel. `_local_members()` is the predicate that decides who counts. It is extended:

```python
def _local_members(self) -> set[Client]:
    return {
        m for m in self.members
        if not isinstance(m, (RemoteClient, VirtualClient))
        and "agentirc.io/bot" not in getattr(m, "caps", set())
    }
```

A bot that joins an empty channel does not become op. A human joining the same channel a moment later does become op (the bot is invisible to the auto-op predicate).

### 3. WHO / NAMES distinction

Bots appear in `NAMES #room` output prefixed with `+` (the IRC voice flag — closest standard for "non-disruptive participant"):

```text
:server 353 mynick = #room :alice @bob +mybot +otherbot
:server 366 mynick #room :End of NAMES list
```

`WHO #room` returns each bot with a `B` flag in the user-modes column:

```text
:server 352 mynick #room mybot bothost server mybot HB :0 My Bot
```

The `H` flag (Here, IRC standard) and `B` flag (Bot, agentirc extension) compose. Human IRC clients can filter on `B` to hide bots from presence panels; agentirc-aware clients can use `+` in NAMES as the same signal.

`+` and `B` are documented under "User modes" in `extension-api.md`. The flags are display-only — they do not grant or revoke any privileges and they cannot be set or unset by `MODE`. They are derived from the bot CAP at NAMES/WHO output time.

### 4. PRIVMSG / NOTICE / DM / mention behavior

Unchanged. Bot-CAP clients use `PRIVMSG` and `NOTICE` exactly like any other client. The existing `_notify_mentions` regex scan in `agentirc/client.py` (and the equivalent in `_internal/virtual_client.py`) handles `@nick` mentions transparently.

### CAP composition

`agentirc.io/bot` and `message-tags` compose freely. A bot will typically request both: `message-tags` so it can read IRCv3 tags on PRIVMSGs (including the existing `event-data` tag on `#system` PRIVMSGs), and `agentirc.io/bot` so it can `EVENTSUB` and get the silent-presence treatment.

### In-process VirtualClient

The in-process `VirtualClient` (used by skills and any culture-runtime-injected code) gains a class-level attribute:

```python
class VirtualClient:
    caps = frozenset({"agentirc.io/bot", "message-tags"})
```

This makes bot-CAP-aware code paths treat `VirtualClient` identically to a real CAP-bot. The change is internal; `VirtualClient` itself remains internal.

## Decision D — webhook port ownership

`ServerConfig.webhook_port` stays in the dataclass with its current default `7680`, accepted in YAML and on the CLI as `--webhook-port`, and ignored at runtime by `agentirc`.

### What changes

- `agentirc/ircd.py` `start()` no longer instantiates `HttpListener`. The fields `self._http_listener` and the corresponding `start()`/`stop()` plumbing are removed.
- `agentirc/_internal/bots/http_listener.py` (the synthesize stub) stays for one cycle in case any vendored test imports it; scheduled for removal in 9.6.0.
- `docs/cli.md` `--webhook-port` help text changes to "Accepted for backward compatibility; not bound by `agentirc`. Consumers (e.g. culture) host their own listener if needed."
- `docs/deployment.md` adds a note: "As of 9.5.0, `agentirc` does not bind `webhook_port`. Culture's bot harness binds it itself when running."

### What does not change

- `ServerConfig.webhook_port: int = 7680` field — preserved. Removing it would break culture's YAML loaders (which pass the same `~/.culture/server.yaml` to both daemons).
- `--webhook-port PORT` CLI flag — accepted, parsed, stored on the config. No-op at the IRCd layer.
- No runtime warning logged when `webhook_port` is set. Logging on every startup that an unused config key is unused is noise; the docs change is sufficient.

### Why no deprecation

Culture's existing systemd units, mesh.yaml, and `~/.culture/server.yaml` all pass `webhook_port` (or rely on its default). A deprecation warning would fire on every culture deployment until culture's [#308](https://github.com/agentculture/culture/issues/308) cleanup — that is needless noise. The field is being repurposed, not removed: in 10.0.0 (a future major bump), it may be removed; this minor bump only changes who binds it.

## Decision E — bot-side event emission (`EVENTPUB`)

Symmetric to `EVENTSUB`. Without it, TCP-connected bots can subscribe to events but cannot **emit** custom-typed events back into the stream — losing culture's chained-bot pattern, in which one bot's `welcome.greeted` emission triggers a downstream bot's onboarding logger. Today this works because `BotManager` calls `IRCd.emit_event(Event(type='welcome.greeted', ...))` in-process; after culture's [#308](https://github.com/agentculture/culture/issues/308) cutover, that path is gone unless agentirc publishes a verb for it.

### Verb syntax

```text
EVENTPUB <type> <channel-or-*> :<base64-json-data>
```

Verb name added to `agentirc.protocol`:

```python
EVENTPUB = "EVENTPUB"
```

- `<type>` — must validate against `EVENT_TYPE_RE` (`^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$`). At least one dot segment is required; this prevents collision with the grandfathered single-segment built-in types (`message`, `topic`) and reserves a clean namespace for custom emissions. On regex failure: `EVENTERR <type> :invalid-type`. The connection stays open; nothing is emitted.
- `<channel-or-*>` — exact channel name (e.g. `#room`) for channel-scoped emissions, or the literal `*` for non-channel-scoped emissions. Channel must exist; if not: `EVENTERR <type> :no-such-channel`.
- `:<base64-json-data>` — the type-specific `data` payload as base64-JSON. The IRCd does not introspect or validate the contents beyond requiring it to be a JSON object (not a list, scalar, or null). On parse failure: `EVENTERR <type> :invalid-payload`.

The IRCd constructs the resulting `Event` with:

- `type` from the verb argument (validated against `EVENT_TYPE_RE`).
- `channel` from the verb argument (`null` if `*`).
- `nick` set server-side from the bot's connection — clients **cannot** spoof `nick`. This matches how `IRCd._handle_privmsg` derives the source from the connection rather than trusting client-supplied values.
- `data` from the decoded JSON object.
- `timestamp` set server-side via `time.time()` so peers across federation see consistent timestamps.

The constructed `Event` is then fed through the same `IRCd.emit_event` pipeline that handles built-in events: skill hooks run, peers receive `SEVENT` relays, subscribers see `EVENT` lines, and (for non-`NO_SURFACE_EVENT_TYPES` types) the human-visible `#system` PRIVMSG surfaces. There is no separate path for bot-emitted events — they are first-class.

### Privilege gate

`EVENTPUB` requires the `agentirc.io/bot` capability. Without it: `EVENTERR <type> :bot-capability-required` and the emission is dropped. Same gate as `EVENTSUB`. Cap-gating is deliberate: it makes "bot can emit on the network" an explicit, opt-in privilege boundary.

### Reflexive subscription

A bot that has emitted via `EVENTPUB` and is also subscribed to a matching filter receives its own emission via `EVENT` like any other subscriber. This matches the in-process `BotManager` behavior today (a bot's own emission does fire its own `on_event` hook). Bots that want to filter out self-emissions inspect `nick` against their own connection nick.

### Federation

`EVENTPUB`-emitted events are relayed to linked peers via `SEVENT` exactly like built-in events. The originating server's name is preserved in `data._origin` on the receiving side. Peers running ≤9.4.x will not understand custom type strings the receiver hasn't seen before, but the forward-compat clause ("subscribers must tolerate unknown types") covers this — they relay-but-ignore.

### Wire-up

- `agentirc/protocol.py` — `EVENTPUB` constant.
- `agentirc/client.py` — `_handle_eventpub` method. Validates type/channel/payload, constructs `Event`, calls `IRCd.emit_event`.
- No new internal module — `EVENTPUB` reuses the existing `IRCd.emit_event` pipeline directly.

### Acceptance test (added to PR-EXT-2)

A bot connects with `agentirc.io/bot`, emits `EVENTPUB welcome.greeted #room :<b64-json>`, a second bot subscribed via `EVENTSUB sub1 type=welcome.greeted` receives one `EVENT sub1 welcome.greeted #room nick :<payload>` line. The first bot also receives one `EVENT` line for the same emission via its own subscription (reflexive). The IRCd's `#system` PRIVMSG surfacing fires once (since `welcome.greeted` is not in `NO_SURFACE_EVENT_TYPES`), reaching any human client in `#system`.

## Versioning

This spec defines **9.5.0**, a minor bump per the contract in `docs/api-stability.md`:

- New public members in `agentirc.protocol`: `Event`, `EventType`, `EVENT_TYPE_*` per-type constants, `EVENTSUB`/`EVENTUNSUB`/`EVENT`/`EVENTERR`/`EVENTPUB` verbs, `BOT_CAP`. **Minor.**
- New `ServerConfig` field with default value: `event_subscription_queue_max`. **Minor.**
- New IRC verbs handled by the daemon: `EVENTSUB`, `EVENTUNSUB`, `EVENTPUB`. **Minor.**
- New IRCv3 capability advertised: `agentirc.io/bot`. **Minor** (additive to CAP LS output).
- `webhook_port` no longer bound. Dataclass field preserved, CLI flag preserved, documentation updated. Not a removal — culture's systemd unit and YAML keep working unchanged. **Minor.** (If we ever remove the field, that's major.)

No API removal, no signature change, no numeric reply-code value change, no on-disk layout change. Major bump not required.

## Phasing

Two PRs on a feature branch:

- **PR-EXT-1** (this spec is its core deliverable). Lands:
  - `docs/superpowers/specs/2026-05-01-bot-extension-api-design.md` (this file).
  - `docs/extension-api.md` — short consumer-facing reference; links here for the rationale.
  - `docs/api-stability.md` — reserves the new public surface area, labels it `9.5.0-pending`, links here.

  No code changes. No version bump. Comment the spec on [#15](https://github.com/agentculture/agentirc/issues/15) to invite culture and any future bot consumer to review the wire format **before** it gets baked into a minor bump that is forever pinnable.

- **PR-EXT-2.** Lands the implementation:
  - `agentirc/protocol.py` — `EventType`, `Event`, per-type constants, new verb constants, `BOT_CAP`.
  - `agentirc/skill.py` — make `EventType`/`Event` re-exports from `agentirc.protocol`.
  - `agentirc/ircd.py` — drop `HttpListener` wiring; add `_dispatch_to_subscribers` step in `emit_event`; extend NAMES/WHO output for bot CAP; suppress JOIN broadcasts for bot CAP.
  - `agentirc/client.py` — extend `_handle_cap`'s supported set; add `_handle_eventsub`/`_handle_eventunsub`/`_handle_eventpub`.
  - `agentirc/channel.py` — extend `_local_members` to exclude bot CAP.
  - `agentirc/_internal/event_subscriptions.py` — new file: `SubscriptionRegistry`.
  - `agentirc/_internal/virtual_client.py` — add `caps` class attribute.
  - `agentirc/config.py` — add `event_subscription_queue_max: int = 1024`.
  - `docs/api-stability.md` — flip `9.5.0-pending` to `9.5.0`; add the public surface to the version-history table.
  - `docs/cli.md` — update `--webhook-port` help text.
  - `docs/deployment.md` — webhook_port note.
  - `pyproject.toml` `[tool.citation]` — note bot-CAP additions on `events.py` and `virtual_client.py` paraphrase entries.
  - `CHANGELOG.md` — 9.5.0 entry.
  - Tests: `tests/test_event_subscriptions.py`, `tests/test_bot_capability.py`, an extension to `tests/test_server_link_federation.py` for cross-server subscription, a wire-format golden-file test.

The two PRs target the same feature branch. PR-EXT-2 merges into PR-EXT-1's branch, then the combined branch merges to main.

## Acceptance criteria

For PR-EXT-1 (this spec):

1. This file exists and links from `docs/api-stability.md` and `docs/extension-api.md`.
2. The wire format JSON shape is fully specified (field semantics, encoding rules, type-string vocabulary, federation behavior).
3. The five verbs (`EVENTSUB`, `EVENTUNSUB`, `EVENT`, `EVENTERR`, `EVENTPUB`) are fully specified (syntax, filter rules, privilege gate, backpressure policy, type-validation rule for emitted types, server-side `nick`/`timestamp` derivation).
4. The bot CAP behavior is fully specified (silent JOIN/PART/QUIT, no auto-op, NAMES `+` flag, WHO `B` flag, mention behavior, CAP composition).
5. The `webhook_port` change is fully specified (what changes, what does not, why no deprecation).
6. The phasing and versioning are fully specified.

For PR-EXT-2:

1. `pip install agentirc-cli==9.5.0.devN` from TestPyPI in a clean venv. `python -c "from agentirc.protocol import EventType, Event, EVENTSUB, EVENTUNSUB, EVENT, EVENTERR, BOT_CAP, EVENT_TYPE_USER_JOIN; print('ok')"` succeeds.
2. `agentirc serve --port 9999` boots cleanly with no `HttpListener` startup line in the log. `ss -tlnp | grep 7680` returns nothing. `ss -tlnp | grep 9999` shows the bound IRC port.
3. A test bot script: connects, `CAP REQ :agentirc.io/bot message-tags`, `EVENTSUB sub1 type=user.join channel=#room`, observes a third client joining `#room` and receives one `EVENT sub1 user.join #room nick :<payload>` line. `EVENTUNSUB sub1` and the stream stops. The bot's JOIN to `#room` does not produce a JOIN broadcast on a separate human client also in `#room`. The bot is not op.
4. A federation test: bot on server A, client joins server B (linked to A), bot receives the `user.join` event with `data._origin == "B"`.
5. A backpressure test: queue limit set to 4, 100 matching events emitted in a tight loop, the subscription receives ≤ 4 events plus exactly one `EVENTERR sub-id :backpressure-overflow`, then no more events on that sub-id.
6. An `EVENTPUB` test: bot A emits `EVENTPUB welcome.greeted #room :<b64-json>`, bot B subscribed to `type=welcome.greeted` receives one matching `EVENT` line. The IRCd-set `nick` matches bot A's connection nick (not a value bot A could spoof). A peer linked via federation receives a `SEVENT welcome.greeted` relay. An emission with a non-regex-matching type returns `EVENTERR welcome :invalid-type` and produces no `EVENT` lines.
7. Wire-format golden-file test passes byte-for-byte.
8. `pytest -n auto` passes for the full suite (existing 328 + new tests).
9. `docs/api-stability.md` table lists `agentirc.protocol` members as adding `Event`, `EventType`, `EVENTSUB`/`EVENTUNSUB`/`EVENT`/`EVENTERR`/`EVENTPUB` verbs, `BOT_CAP`, plus per-type `EVENT_TYPE_*` constants. The version-history table includes a 9.5.0 row.

When all nine pass, [#15](https://github.com/agentculture/agentirc/issues/15) closes, and culture's [#308](https://github.com/agentculture/culture/issues/308) Phase A2 is unblocked.

## Open structural questions (resolved)

- **Spec PR vs. one PR.** Two PRs (spec → impl). Lower lock-in risk for the wire format.
- **CAP token name.** `agentirc.io/bot` (vendored namespace). Future-proof against federation with non-agentirc IRC servers.
- **Backpressure policy.** Drop-the-subscription on overflow. Forces explicit reconciliation via re-subscribe + `BACKFILL`.
- **Public home for `Event`/`EventType`.** `agentirc.protocol` (per the issue's stated preference; keeps the existing three-public-modules contract intact).
