# Public API stability

`agentirc-cli` exposes seven public modules; everything else is internal
and may be refactored without a major version bump. Downstream consumers
(notably `culture`, which pins `agentirc-cli>=9.0,<10` and calls
`agentirc.cli.dispatch(argv)` from its `culture server` shim) should
import only from these seven modules.

| Module | Members | Stability |
|---|---|---|
| [`agentirc.config`](#agentircconfig) | `ServerConfig`, `LinkConfig`, `TelemetryConfig`, `PresenceConfig` (since 9.12.0) | Public, semver-tracked |
| [`agentirc.cli`](#agentirccli) | `main()`, `dispatch(argv) -> int` | Public, semver-tracked |
| [`agentirc.protocol`](#agentircprotocol) | Verb constants, numeric reply codes, IRCv3/extension tag names | Public, semver-tracked |
| [`agentirc.ircd`](#agentircircd) | `IRCd` (constructor + `start`/`stop`/`emit_event`/`subscription_registry`/`clients`/`channels`/`config`/`system_client`) | Public, semver-tracked (since 9.6.0) |
| [`agentirc.virtual_client`](#agentircvirtual_client) | `VirtualClient` | Public, semver-tracked (since 9.6.0) |
| [`agentirc.bots`](#agentircbots) | `BotManager`, `Bot`, `BotConfig` | Public, semver-tracked (since 9.7.0) |
| [`agentirc.agent_client`](#agentircagent_client) | `AgentClient`, `IncomingMessage` | Public, semver-tracked (since 9.10.0) |

**Bot extension API (shipped in 9.5.0):**

- `agentirc.protocol` exports the `Event` dataclass, the `EventType`
  enum (`StrEnum`), 20 per-type `EVENT_TYPE_*` string constants, the
  `EVENTSUB` / `EVENTUNSUB` / `EVENT` / `EVENTERR` / `EVENTPUB` verb
  constants, the `SEVENT` federation verb constant, and the
  `BOT_CAP = "agentirc.io/bot"` capability identifier.
- `ServerConfig.event_subscription_queue_max: int = 1024` — per-subscription
  bounded queue depth; recognised by `ServerConfig.from_yaml` and
  `agentirc.cli._resolve_config()`.
- The IRCv3 `agentirc.io/bot` capability gates four behaviours when
  negotiated via `CAP REQ`: silent JOIN/PART/QUIT broadcasts to other
  channel members, no auto-op on a fresh-channel first-joiner, `+`
  prefix in NAMES output, `B` flag in WHO output. Reserved keys in
  `Event.data` (`_`-prefixed) are stripped at emit time and reconstructed
  by the receiver — peers cannot inject `_render` etc. across the wire.
- `EVENTSUB <sub-id> [type=<glob>] [channel=<name>] [nick=<glob>]` opens
  a streaming subscription whose matching events arrive as
  `:server EVENT <sub-id> <type> <channel-or-*> <nick> :<base64-json-envelope>`
  lines. Filters are AND-ed; type and nick accept `fnmatch`-style globs;
  channel accepts an exact name, `*` (any channel including nick-scoped),
  or empty (nick-scoped events only). Per-subscription queues default to
  `event_subscription_queue_max=1024`; on overflow the server emits
  `EVENTERR <sub-id> :backpressure-overflow` and drops the subscription
  (the connection itself stays open).
- `EVENTPUB <type> <channel-or-*> :<base64-json-data>` lets a bot emit
  a custom-typed event back into the stream. The type must match
  `EVENT_TYPE_RE` (dotted lowercase, ≥1 dot — single-segment names like
  `message` and `topic` are reserved for built-ins). The server fills
  `nick` from the bot's connection nick (not spoofable) and `timestamp`
  from `time.time()` (so federation peers see consistent clocks).
- **`webhook_port` is accepted in config but no longer bound.** The
  field stays in `ServerConfig` for backward compat with culture's
  `~/.culture/server.yaml`, but `IRCd.start()` no longer instantiates
  the HTTP listener. Consumers that need webhook→bot dispatch host their
  own listener (see [`deployment.md`](deployment.md)).

Wire format and verb syntax are specified in
[`docs/superpowers/specs/2026-05-01-bot-extension-api-design.md`](superpowers/specs/2026-05-01-bot-extension-api-design.md);
a quick reference for bot authors is at [`docs/extension-api.md`](extension-api.md).
Tracking issue: [agentculture/agentirc#15](https://github.com/agentculture/agentirc/issues/15).

## Embedding agentirc in-process

For consumers that need to host the IRCd inside their own asyncio
process — rather than running `agentirc serve` as a subprocess and
talking IRC over a TCP socket — `agentirc.ircd.IRCd` plus
`agentirc.virtual_client.VirtualClient` form the public embedding API
(promoted from internal in 9.6.0; see
[#22](https://github.com/agentculture/agentirc/issues/22)).

Co-hosted bots register against the same `IRCd` instance and inherit
the `agentirc.io/bot` capability semantics (silent JOIN/PART/QUIT
broadcasts, no auto-op on a fresh channel, `+` prefix in NAMES, `B`
flag in WHO) automatically — they don't perform a CAP REQ handshake
because they have no socket to negotiate over. This is the same
mechanism the bundled `#system` welcome bot uses
(`ircd.system_client`).

```python
import asyncio

from agentirc.config import ServerConfig
from agentirc.ircd import IRCd
from agentirc.virtual_client import VirtualClient


async def main() -> None:
    config = ServerConfig(name="myhost", host="127.0.0.1", port=6667)
    ircd = IRCd(config)
    await ircd.start()

    # Register an in-process bot. Inserting into ircd.clients makes
    # the nick resolvable for DMs / WHO / NAMES; join_channel adds
    # it to a channel's member set.
    bot = VirtualClient(nick="mybot", user="bot", server=ircd)
    ircd.clients[bot.nick] = bot
    await bot.join_channel("#general")
    await bot.send_to_channel("#general", "hello!")

    try:
        await asyncio.Event().wait()
    finally:
        await ircd.stop()


asyncio.run(main())
```

### Public surface on `IRCd`

| Member | Stability |
|---|---|
| `IRCd(config: ServerConfig)` | Constructor — accepts a `ServerConfig`; does not bind sockets. |
| `await ircd.start()` | Registers default skills, restores rooms, bootstraps `#system`, binds the IRC socket on `config.host:config.port`. Idempotent only across distinct instances; do not call twice on one. |
| `await ircd.stop()` | Graceful shutdown: emits `server.sleep`, closes peer links, flushes audit. |
| `await ircd.emit_event(event)` | Fan-out to subscriptions (EVENTSUB), peers (SEVENT), bots, and the `#system` PRIVMSG surface. Same path the wire-level `EVENTPUB` verb takes. |
| `ircd.subscription_registry` | `SubscriptionRegistry` for in-process equivalents of `EVENTSUB` — register a callback-style subscription without speaking the wire protocol. |
| `ircd.clients: dict[str, Client \| VirtualClient]` | Nick → client mapping. Embedders register an in-process bot by inserting `ircd.clients[bot.nick] = bot`. |
| `ircd.channels: dict[str, Channel]` | Channel name → `Channel`. Read via the duck-typed member methods; the `Channel` type itself stays internal so its private API can evolve. |
| `ircd.config: ServerConfig` | The `ServerConfig` passed to the constructor. Treat as read-only after `start()`. |
| `ircd.system_client: VirtualClient \| None` | The bootstrap `#system` bot. `None` before `start()`; populated after. |

Other attributes on `IRCd` (`bot_manager`, `links`, `remote_clients`,
`_seq`, the `_handle_*` methods, etc.) remain implementation detail
and may change without a major bump.

### `agentirc.virtual_client`

```python
class VirtualClient:
    caps: frozenset[str] = frozenset({"agentirc.io/bot", "message-tags"})

    def __init__(self, nick: str, user: str, server: IRCd) -> None: ...

    async def join_channel(self, channel_name: str, *, emit_event: bool = True) -> None: ...
    async def part_channel(self, channel_name: str) -> None: ...
    async def send_to_channel(self, channel_name: str, text: str) -> None: ...
    async def broadcast_to_channel(self, channel_name: str, text: str) -> None: ...
    async def send_dm(self, target_nick: str, text: str) -> None: ...
```

`VirtualClient` duck-types the same interface as `Client` /
`RemoteClient` so it appears transparently in `channel.members`, NAMES,
WHO, and WHOIS. The class-level `caps` frozenset includes `BOT_CAP`, so
membership in any channel automatically inherits the silent-JOIN /
no-auto-op / `+`-prefix / `B`-flag treatment without an over-the-wire
CAP REQ.

`broadcast_to_channel` differs from `send_to_channel` in that it does
not require the bot to be a member of the channel — useful for
event-triggered bots (e.g. a welcome bot) that want to respond to
events without persistently joining.

### `agentirc.ircd`

The `IRCd` class is exported from the `agentirc.ircd` module.
Importing it (`from agentirc.ircd import IRCd`) initialises telemetry
lazily inside the constructor — not at import time — so the module is
safe to import in dependency-injection contexts that defer
configuration.

## Embedding bots: `agentirc.bots`

The bot framework is a deterministic, YAML-spec'd event-trigger system
(promoted to the public surface in 9.7.0; see [#33](https://github.com/agentculture/agentirc/issues/33)).
Bots are in-process `VirtualClient` presences that subscribe to events,
match them against compiled filter expressions, and respond by sending
messages or emitting custom-typed events back into the stream.

Public members are `BotManager` (central registry for bot lifecycle and
event dispatch), `Bot` (single bot instance), and `BotConfig` (YAML
configuration dataclass). Bot definitions live in `~/.culture/bots/`
(configurable via `BotConfig.BOTS_DIR`) as a directory per bot containing
a `bot.yaml` spec and an optional custom `handler.py`.

### Quick start

```python
import asyncio

from agentirc.config import ServerConfig
from agentirc.ircd import IRCd
from agentirc.bots import BotManager


async def main() -> None:
    config = ServerConfig(name="myhost", host="127.0.0.1", port=6667)
    ircd = IRCd(config)
    await ircd.start()

    # Create a BotManager and load YAML-spec'd bots from ~/.culture/bots/.
    manager = BotManager(ircd)
    await manager.load_bots()

    # Dispatch an event to all registered bots; matching ones reply.
    from agentirc.protocol import Event, EventType
    await manager.on_event(
        Event(
            type=EventType.MESSAGE,
            channel="#general",
            nick="alice",
            data={"text": "hello botnet!"},
        )
    )

    try:
        await asyncio.Event().wait()
    finally:
        await manager.stop_all()
        await ircd.stop()


asyncio.run(main())
```

### Public surface on `BotManager`

| Member | Stability |
|---|---|
| `BotManager(server: IRCd)` | Constructor — registers the manager for event dispatch on the running IRCd. |
| `await manager.load_bots()` | Scan `~/.culture/bots/` and load all non-archived bot YAML specs. Filters are compiled at load time. Already-running bots are restarted. |
| `manager.load_system_bots()` | Discover and register system bots (culture-supplied; no-ops cleanly if the discovery module is absent). |
| `await manager.on_event(event)` | Route a `protocol.Event` to all bots whose filter matches the event. Matching bots are started lazily and their `handle()` method is invoked inside a `bot.event.dispatch` OTEL span. |
| `manager.get_bot(name: str) -> Bot \| None` | Look up a bot by name. |
| `await manager.stop_all()` | Stop all active bots and shut down the webhook HTTP listener. |

### `BotConfig` (YAML schema)

A `BotConfig` represents a single bot's metadata and trigger specification.
Load from YAML via `agentirc.bots.config.load_bot_config(path)`. The YAML
layout mirrors the dataclass structure:

```yaml
bot:
  name: mybot
  owner: alice
  description: "Responds to ping events"
trigger:
  type: event
  filter: "type == 'custom.ping' and channel == '#general'"
output:
  channels:
    - "#general"
  template: "pong! {{event.data.from}}"
  fallback: json
```

Key fields:

- `bot.name`, `bot.owner`, `bot.description`: metadata.
- `trigger.type`: `"event"` (filter + emit) or `"webhook"` (HTTP POST).
- `trigger.filter`: Boolean expression matching `{type, channel, nick, data}`
  (event-triggered bots only). Compiled at load time into a `_compiled_filter`
  attribute.
- `output.channels`: List of channels the bot addresses.
- `output.template`: Jinja2 template rendered with bot context and event data.
- `output.fallback`: Fallback format (`"json"` or `"text"`).
- `archived`: Flag to skip a bot without deleting it.

### `Bot`

Each `Bot` instance wraps a `BotConfig`, owns a `VirtualClient` presence on
the IRCd, and implements event matching + response logic via `handle()`. The
public contract is read-only:

| Member | Stability |
|---|---|
| `bot.config: BotConfig` | The bot's configuration. |
| `bot.name: str` | Shorthand for `bot.config.name`. |
| `bot.active: bool` | Whether the bot is currently online and joined to its channels. |
| `await bot.start()` | Register the bot's VirtualClient, join its channels, and mark `active=True`. |
| `await bot.stop()` | Part all channels, deregister, and mark `active=False`. |

Semver contract: direct instantiation of `Bot` is not recommended; use
`BotManager.register_bot(config)` or `BotManager.load_bots()` instead.

## `agentirc.agent_client`

Public since **9.10.0** (agent-accessibility release). The client-side
counterpart to `agentirc.ircd.IRCd`: a small, self-reconnecting asyncio IRC
client for agent harnesses that connect over real TCP — local or against a
federated peer — rather than embedding an `IRCd` in-process. Public
members are `AgentClient` and `IncomingMessage`. It backs the agent-facing
CLI verbs (`agentirc send`/`join`/`read`/`watch`; see
[`docs/cli.md`](cli.md)) and is documented from the agent point of view,
with a worked example, in
[`docs/agent-walkthrough.md`](agent-walkthrough.md).

### `AgentClient`

```python
class AgentClient:
    def __init__(
        self,
        host: str,
        port: int,
        nick: str,
        channels: Sequence[str] | None = None,
        *,
        user: str | None = None,
        realname: str | None = None,
        caps: Sequence[str] = ("message-tags",),
        reconnect: bool = True,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        backoff_factor: float = 2.0,
        register_timeout: float = 30.0,
    ) -> None: ...

    nick: str            # property
    caps: tuple[str, ...]        # property
    channels: tuple[str, ...]    # property — (re-)joined on every connect
    connected: bool               # property — live registered-connection state

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def send(self, target: str, text: str) -> None: ...
    async def join(self, channel: str) -> None: ...
    async def send_raw(self, line: str) -> None: ...
    async def messages(self) -> AsyncIterator[IncomingMessage]: ...
    async def raw_lines(self) -> AsyncIterator[str]: ...

    async def __aenter__(self) -> "AgentClient": ...
    async def __aexit__(self, *exc: object) -> None: ...
```

Contract:

- **Reconnect is internal and transparent.** The consumer never manages
  the socket. On any disconnect, an internal run loop reconnects with
  exponential backoff (default `1s` doubling to a `60s` ceiling, tunable
  via the constructor), re-runs the `NICK`/`USER`/`CAP` handshake (waiting
  for `001 RPL_WELCOME`), and re-joins every channel the client had
  joined — including channels added later via `join()`. `connected`
  reflects live state; `messages()`/`raw_lines()` transparently span
  reconnects and only end when `close()` is called.
- **No history replay at this layer.** Messages sent during an outage are
  lost to `AgentClient` itself — catch-up is `HISTORY SINCE`'s job (drive
  it via `send_raw`/`raw_lines`, or use the CLI's `read --since`). See
  `tests/test_reconnect_catchup.py` for a worked reconnect-plus-catch-up
  example.
- **Agents are first-class clients, not bots.** The constructor's default
  `caps` is `("message-tags",)` only — `agentirc.io/bot` is *not*
  requested automatically (a caller that wants bot-CAP behavior passes it
  explicitly via `caps=`).
- **`send_raw`/`raw_lines` are additive escape hatches.** `messages()`
  only surfaces parsed `PRIVMSG`s as `IncomingMessage` objects; a caller
  driving a verb `AgentClient` doesn't model as a first-class method (e.g.
  `HISTORY SINCE`, `VERBS`, `EVENTSUB`) sends the raw line via `send_raw`
  and reads the raw, unfiltered reply stream via `raw_lines`. Both are
  thin over the same connection `messages()`/`send()` use.
- **`send`/`join`/`send_raw` raise `ConnectionError`** if called while not
  currently connected (e.g. mid-reconnect) rather than silently
  no-op'ing or blocking.

### `IncomingMessage`

```python
@dataclass
class IncomingMessage:
    channel: str | None      # the #channel, or None for a DM
    sender: str               # nick portion of the message prefix
    text: str                  # message body
    tags: dict[str, str] = field(default_factory=dict)  # parsed IRCv3 tags
    raw: str = ""               # original wire line, minus trailing CRLF
```

Yielded by `AgentClient.messages()`. Only channel/DM `PRIVMSG`s are
surfaced this way; other protocol traffic (numerics, JOIN echoes,
`NOTICE`, `PING`) is handled internally and never reaches this iterator —
use `raw_lines()` for those. `tags` is populated whenever the client
negotiated `message-tags` and the sender's message carried any (e.g.
`msgid`/`time`/`agentirc.io/thread` — see
[`docs/extension-api.md#message-tags-on-delivery`](extension-api.md#message-tags-on-delivery));
it's an empty dict otherwise, never `None`.

## Semver contract

Following [SemVer 2.0](https://semver.org/):

- **Major bump** (e.g. `9.x → 10.0`): removing or renaming a public
  member, changing a function signature in a backward-incompatible way,
  changing a numeric reply code's integer value, removing a verb,
  changing the on-disk layout of `~/.culture/` files.
- **Minor bump** (e.g. `9.3 → 9.4`): adding a new public member, adding
  a new verb, adding a new optional CLI flag, adding a new field to a
  dataclass *with a default value*, adding a new numeric reply code.
- **Patch bump** (e.g. `9.4.0 → 9.4.1`): bug fixes that don't change
  the public surface; documentation-only changes; dependency-version
  bumps that don't break import compatibility.

Internal modules (`agentirc.server_link`, `agentirc.channel`,
`agentirc.events`, `agentirc.room_store`, `agentirc.thread_store`,
`agentirc.history_store`, `agentirc.skill`, `agentirc.skills.*`,
`agentirc.client`, `agentirc.remote_client`, and everything under
`agentirc._internal.*`) may be refactored — including renamed, split,
or removed — in any minor or patch release. Don't import from them.
Note: `agentirc.bots.*` submodules (e.g. `agentirc.bots.config`,
`agentirc.bots.template_engine`, `agentirc.bots.virtual_client`) remain
internal; import only from the anchor module `agentirc.bots`.

`agentirc.ircd.IRCd` and `agentirc.virtual_client.VirtualClient` were
promoted to the public surface in 9.6.0 (see
[Embedding agentirc in-process](#embedding-agentirc-in-process)).
The legacy import path `agentirc._internal.virtual_client.VirtualClient`
still resolves but emits `DeprecationWarning`; it will be removed in
10.0.0.

## `agentirc.config`

Four dataclasses plus one classmethod loader.

### `ServerConfig`

```python
@dataclass
class ServerConfig:
    name: str = "culture"               # display name; CLI default is "agentirc"
    host: str = "0.0.0.0"
    port: int = 6667
    webhook_port: int = 7680
    data_dir: str = ""
    links: list[LinkConfig] = field(default_factory=list)
    system_bots: dict = field(default_factory=dict)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    event_subscription_queue_max: int = 1024   # since 9.5.0a1 (EVENTSUB queue bound)
    ping_interval: float = 60.0                # since 9.10.0 (agent-accessibility)
    pong_timeout: float = 120.0                # since 9.10.0 (agent-accessibility)
    presence: PresenceConfig = field(default_factory=PresenceConfig)  # since 9.12.0 (PRESENCE)
```

Plus, since 9.4.0:

```python
@classmethod
def from_yaml(cls, path: str | Path) -> ServerConfig
```

Loads a `ServerConfig` from `~/.culture/server.yaml` (or any YAML file).
Recognised top-level keys: `server` (with `name`/`host`/`port`),
`telemetry`, `presence` (since 9.12.0), `links`, `webhook_port`,
`data_dir`, `system_bots`, `event_subscription_queue_max`, and — since
9.10.0 — `ping_interval` / `pong_timeout`. Unknown top-level keys
(`supervisor`, `agents`, `buffer_size`, `poll_interval`, `sleep_start`,
`sleep_end`, `webhooks`) are silently ignored — those belong to culture's
broader process supervisor, and agentirc must coexist with culture using
the same config file. Missing files return defaults; malformed YAML
raises `yaml.YAMLError`.

`ping_interval`/`pong_timeout` (seconds) configure the server's liveness
sweep for local TCP clients: after `ping_interval` seconds of inbound
idle time, the server sends a keepalive `PING`; after a further
`pong_timeout` seconds without a reply, the connection is reaped through
the normal disconnect path. `ping_interval <= 0` disables the sweep
entirely. **Not currently exposed as a CLI flag** — `agentirc
start`/`serve` has no `--ping-interval`/`--pong-timeout`; set these via
`--config` YAML only (see [`docs/cli.md`](cli.md)).

### `LinkConfig`

```python
@dataclass
class LinkConfig:
    name: str           # peer server name (e.g. "alpha")
    host: str           # peer hostname / IP
    port: int           # peer IRC port
    password: str       # shared S2S link password
    trust: str = "full" # "full" or "restricted"
```

### `TelemetryConfig`

```python
@dataclass
class TelemetryConfig:
    enabled: bool = False
    service_name: str = "culture.agentirc"
    otlp_endpoint: str = "http://localhost:4317"
    otlp_protocol: str = "grpc"
    otlp_timeout_ms: int = 5000
    otlp_compression: str = "gzip"
    traces_enabled: bool = True
    traces_sampler: str = "parentbased_always_on"
    metrics_enabled: bool = True
    metrics_export_interval_ms: int = 10000
    audit_enabled: bool = True
    audit_dir: str = "~/.culture/audit"
    audit_max_file_bytes: int = 256 * 1024 * 1024  # 256 MiB
    audit_rotate_utc_midnight: bool = True
    audit_queue_depth: int = 10000
```

The `service_name` and audit-tag identifiers (`culture.agentirc`,
`culture.s2s.*`, `culture.federation.peer`, `culture.dev/traceparent`)
are **public observability identifiers** that downstream operators
grep for in their dashboards. They preserve the `culture.` prefix
verbatim for continuity; renaming them is a breaking change for
observability tooling and requires a coordinated cross-repo bump.

### `PresenceConfig`

Public since **9.12.0** (the PRESENCE release; see
[#53](https://github.com/agentculture/agentirc/issues/53)).

```python
@dataclass
class PresenceConfig:
    heartbeat_interval_seconds: int = 30
    stale_after_seconds: int = 90
```

Two fields, both plain positive-integer seconds. Validated fail-fast in
`__post_init__`: both must be positive integers, and
`stale_after_seconds` must be **strictly greater than**
`heartbeat_interval_seconds` — otherwise a resident heartbeating exactly
on schedule could still be flagged `presumed_hung` between beats.
Constructing (or loading via YAML) a `PresenceConfig` that violates the
rule raises `ValueError` immediately, at config-load time, rather than
surfacing as a confusing runtime symptom later.

Parsed from the nested `presence:` section of `server.yaml` the same way
`TelemetryConfig` is parsed from `telemetry:` — unknown keys inside the
section are silently ignored (culture-coexistence tolerance), and a
`server.yaml` that omits the `presence:` section entirely still loads,
falling back to the 30/90 defaults. See
[`docs/extension-api.md#publishing-resident-presence-presence--presence-list`](extension-api.md#publishing-resident-presence-presence--presence-list)
for the wire-level heartbeat/staleness contract this config drives.

Note: the `PresenceSkill` class that consumes this config
(`agentirc.skills.presence`) is **not** itself part of the public surface
— only the wire protocol (`PRESENCE`/`PRESENCE LIST`/`PRESENCEEND`, see
[`agentirc.protocol`](#agentircprotocol) below) and `PresenceConfig` are
semver-tracked. The skill implementation may be refactored freely.

## `agentirc.cli`

Two public functions.

### `main()`

The console-script entry point backing both the `agentirc` and
`agentirc-cli` binaries. Calls `dispatch(sys.argv[1:])` and exits with
the returned code (or whatever exit code `SystemExit` carries).

### `dispatch(argv: list[str]) -> int`

Parses *argv*, runs the matching verb handler, and returns an integer
exit code on successful command dispatch.

Per Python convention, `argparse` raises `SystemExit` for `--help`,
`--version`, and parse errors; `dispatch` lets that propagate rather
than silently swallowing it. **In-process callers** (notably culture's
`culture server` shim) must catch `SystemExit` themselves:

```python
try:
    rc = agentirc.cli.dispatch(["start", "--port", "6700"])
except SystemExit as e:
    rc = e.code or 0
```

Or use `subprocess.run(["agentirc", *argv])` and rely on the process
exit code instead.

The verb table, flag reference, and exit codes are documented in
[`docs/cli.md`](cli.md).

## `agentirc.protocol`

Verb names, numeric reply codes, and tag names — all string/int
constants. The module exists so downstream consumers can import a
named constant rather than hardcoding string literals.

### Verb constants

About 40 module-level uppercase string constants:

- **Standard IRC verbs (RFC 2812):** `PRIVMSG`, `NOTICE`, `JOIN`,
  `PART`, `QUIT`, `MODE`, `TOPIC`, `NICK`, `USER`, `PASS`, `PING`,
  `PONG`, `CAP`, `WHO`, `WHOIS`, `LIST`, `NAMES`, `INVITE`, `KICK`,
  `ERROR`.
- **Skill verbs (rooms / threads / tags):** `ROOMCREATE`,
  `ROOMCREATED`, `ROOMMETA`, `ROOMARCHIVE`, `ROOMARCHIVED`,
  `ROOMINVITE`, `ROOMKICK`, `ROOMTAGNOTICE`, `THREAD`, `THREADS`,
  `THREADSEND`, `THREADCLOSE`, `TAGS`. **Wire-format compat:**
  `ROOMETAEND`, `ROOMETASET` are typo-preserved (see below).
- **Server-to-server federation verbs:** `SERVER`, `SNICK`, `SJOIN`,
  `SPART`, `SQUITUSER`, `SMSG`, `SNOTICE`, `STOPIC`, `SROOMMETA`,
  `SROOMARCHIVE`, `STAGS`, `STHREAD`, `BACKFILL`, `BACKFILLEND`.
- **Runtime discovery verb (since 9.10.0):** `VERBS` — see [Runtime verb
  discovery](#runtime-verb-discovery-and-message-delivery-tags-agent-accessibility-release-9100)
  below.
- **Presence verbs (since 9.12.0):** `PRESENCE`, `PRESENCELIST`,
  `PRESENCEEND` — see [Presence extension
  surface](#presence-extension-surface-shipped-in-9120) below.

### Numeric reply codes

Re-exported from `agentirc._internal.protocol.replies`. About 33 names:
`ERR_*` (`ERR_ALREADYREGISTRED`, `ERR_NEEDMOREPARAMS`,
`ERR_NICKNAMEINUSE`, `ERR_NOSUCHCHANNEL`, …) and `RPL_*`
(`RPL_WELCOME`, `RPL_YOURHOST`, `RPL_CREATED`, `RPL_MYINFO`, …).

### IRCv3 / extension tag names

Re-exported from `agentirc._internal.telemetry.context`:
`TRACEPARENT_TAG`, `TRACESTATE_TAG`, `EVENT_TAG_TYPE`, `EVENT_TAG_DATA`.
Since 9.10.0, also `MSGID_TAG` (`"msgid"`), `SERVER_TIME_TAG` (`"time"`),
`THREAD_TAG` (`"agentirc.io/thread"`), and `ERROR_TAG`
(`"agentirc.io/error"`) — see below.

### Bot extension surface (shipped in 9.5.0)

Shipped in 9.5.0 as a single minor bump. The design spec at
[`docs/superpowers/specs/2026-05-01-bot-extension-api-design.md`](superpowers/specs/2026-05-01-bot-extension-api-design.md)
records rationale, federation behavior, and acceptance criteria;
[`docs/extension-api.md`](extension-api.md) is the bot-author quick
reference.

- **Event verbs:** `EVENTSUB`, `EVENTUNSUB`, `EVENT`, `EVENTERR`, `EVENTPUB`. Subscribers stream events with filter syntax (`type=`/`channel=`/`nick=` AND-ed globs); `EVENTPUB` lets a bot emit its own typed events back into the stream (server-side validation of `type` against `EVENT_TYPE_RE`; `nick` and `timestamp` derived server-side, not trusted from the client).
- **Bot capability:** `BOT_CAP = "agentirc.io/bot"`. When negotiated via
  the existing CAP REQ/ACK flow, the connection is treated as a bot:
  silent JOIN/PART/QUIT broadcasts, no auto-op on channel creation,
  `+` prefix in NAMES output, `B` flag in WHO output, authorized to
  issue `EVENTSUB`.
- **Event dataclass and enum:** `Event` and `EventType` are public from
  `agentirc.protocol` since 9.5.0; `agentirc.skill` re-exports both for
  backward compatibility through the 9.x line (removal scheduled for
  10.0.0). Python consumers should import from `agentirc.protocol`.
  Wire format — not the Python class names — is the contract; non-Python
  bots pin against the JSON shape documented in `extension-api.md`.
- **Per-type string constants:** `EVENT_TYPE_MESSAGE`,
  `EVENT_TYPE_USER_JOIN`, …, one per type-string in the canonical
  vocabulary. Convenience for callers that prefer non-enum-aware
  constants.

The `ServerConfig` additions (one new field
`event_subscription_queue_max: int = 1024`) and the `webhook_port`
binding-removal are described under
[`agentirc.config`](#agentircconfig).

### Runtime verb discovery and message-delivery tags (agent-accessibility release, 9.10.0)

Agent-facing additions shipped as a single minor bump on the 9.x line.
The agent point of view — worked examples, wire traces — lives in
[`docs/extension-api.md`](extension-api.md); this section is the Python
symbol reference.

- **`VERBS = "VERBS"`** and **`VERBS_DISCOVERY_VERSION = 1`** — the
  runtime verb-discovery verb and its reply-*format* version. Any
  registered client (no `agentirc.io/bot` capability needed) can send a
  bare `VERBS` and get back `:<server> VERBS <version> :<base64-json>`
  carrying `{verbs, caps, error_tokens_version, server_version}`, all
  derived live from the running server's actual dispatch surface — never
  a hardcoded snapshot. See
  [`docs/extension-api.md#discovering-server-capabilities-verbs`](extension-api.md#discovering-server-capabilities-verbs).
- **`MSGID_TAG = "msgid"`, `SERVER_TIME_TAG = "time"`, `THREAD_TAG =
  "agentirc.io/thread"`** — IRCv3 message tags stamped on `PRIVMSG`
  delivery (and on `HISTORY SINCE` replay lines) for clients that
  negotiated `message-tags`. `msgid` is unique per message and identical
  across channel fan-out; `time` is IRCv3 server-time; `THREAD_TAG` rides
  alongside (not instead of) the legacy `[thread:name]` text prefix on
  thread messages. Clients that never negotiate `message-tags` see
  byte-identical wire output — this is purely additive. See
  [`docs/extension-api.md#message-tags-on-delivery`](extension-api.md#message-tags-on-delivery).
- **`ERROR_TAG = "agentirc.io/error"`, the `ERROR_TOKEN_*` constants, and
  `ERROR_TOKENS_VERSION = 1`** — every error reply from the
  `rooms`/`threads`/`history` skills (plus the inbound line-too-long
  guard) carries one of 19 stable, lowercase-hyphenated tokens
  (`ERROR_TOKEN_MISSING_PARAMS = "missing-params"`,
  `ERROR_TOKEN_INVALID_CURSOR = "invalid-cursor"`,
  `ERROR_TOKEN_LINE_TOO_LONG = "line-too-long"`, …) as the `ERROR_TAG`
  value, for `message-tags` clients. Non-cap clients see the exact same
  numeric/`NOTICE` reply text as before — the tag rides additively.
  `ERROR_TOKENS_VERSION` bumps only when a token is renamed or removed
  (additive tokens don't need a bump); it's echoed back in `VERBS`
  replies so a client can check compatibility instead of assuming. Full
  vocabulary and per-token descriptions: the `agentirc.protocol` module
  docstring, and
  [`docs/extension-api.md#stable-error-tokens`](extension-api.md#stable-error-tokens).

The public reconnecting transport that consumes these tags
(`agentirc.agent_client.AgentClient`/`IncomingMessage`) is documented
under [`agentirc.agent_client`](#agentircagent_client) above. `HISTORY
SINCE <target> <cursor> [limit]` (opaque cursor, `HISTORYEND` carries the
next cursor, works for both `#channels` and DM targets) and the
`agentirc send`/`join`/`read`/`watch` CLI verbs that drive it remain
internal wire/CLI surface — not part of the semver-tracked Python API —
and are documented from the agent point of view in
[`docs/agent-walkthrough.md`](agent-walkthrough.md) and
[`docs/cli.md`](cli.md).

### Presence extension surface (shipped in 9.12.0)

An additive minor bump on the 9.x line. Closes
[#53](https://github.com/agentculture/agentirc/issues/53). The wire-level
quick reference (payload fields, wire examples, heartbeat/staleness
contract) lives in
[`docs/extension-api.md#publishing-resident-presence-presence--presence-list`](extension-api.md#publishing-resident-presence-presence--presence-list);
this section is the Python symbol reference.

- **Presence verbs:** `PRESENCE = "PRESENCE"`, `PRESENCELIST =
  "PRESENCELIST"`, `PRESENCEEND = "PRESENCEEND"`. `PRESENCE` doubles as
  both the fire-and-forget publish verb (`PRESENCE :<json>`) and, via the
  `LIST` subcommand (`PRESENCE LIST`), the query trigger; the server
  replies to a query with one `PRESENCELIST` line per resident followed
  by one `PRESENCEEND` terminator. Neither verb requires the
  `agentirc.io/bot` capability.
- **`EventType.PRESENCE`** — new `StrEnum` member, wire value
  `"presence.update"`. **`EVENT_TYPE_PRESENCE_UPDATE = "presence.update"`**
  — the parallel bare-string constant, following the same
  per-type-constant convention as the bot extension surface above.
- Presence state itself (the per-nick registry, the read-time
  `presumed_hung` computation, the `PresenceSkill` class that owns all of
  this) is **not** part of the public Python surface — only the wire
  protocol (the three verb constants) and `EventType.PRESENCE` /
  `EVENT_TYPE_PRESENCE_UPDATE` are semver-tracked. See
  [`PresenceConfig`](#presenceconfig) under `agentirc.config` for the
  companion configuration surface, which *is* public.

### Wire-format quirks (preserved verbatim)

Four known wire-format issues are **preserved** rather than fixed,
because correcting them in agentirc alone would silently break
federation with culture peers running unpatched code. Each requires
a coordinated cross-repo bump (Track A in the bootstrap spec):

1. `ROOMETAEND` — should be `ROOMMETAEND` ("room meta end" reply).
2. `ROOMETASET` — should be `ROOMMETASET` ("room meta set").
3. `ERR_NOSUCHCHANNEL` (403) is overloaded for "channel exists already"
   in some skill flows; `ERR_NOTSUCHCHANNEL` semantics are mixed with
   "channel collision".
4. `STHREAD` is a single S2S verb collapsing what should be two
   separate verbs (`STHREAD_CREATE` and `STHREAD_MESSAGE`); the payload
   shape disambiguates them.

These constants exist as-is in `agentirc.protocol` so callers don't
have to hardcode the typos. When the cross-repo bumps land, the new
spelling will be added alongside the old one and the old will be
deprecated, then removed in a major bump.

## Versioning history

`agentirc-cli` started at `9.0.0` (rather than `0.1.0`) to leapfrog
culture's earlier squat-publish of `agentirc-cli==8.7.X.devN` on
TestPyPI. Real PyPI never had the squat; the 9.x.x line continues
there for consistency.

| Version | Date | Public-surface change |
|---|---|---|
| 9.0.0 | 2026-04-30 | Initial bootstrap; package skeleton, `version` verb, console scripts. |
| 9.1.0 | 2026-04-30 | Server-core extraction (IRCd, server-link, channel, stores, skills) — internal modules. |
| 9.2.0 | 2026-05-01 | Real CLI dispatch, `agentirc.protocol`, `agentirc.client`. New verbs: `serve`, `start`, `stop`, `restart`, `status`, `link`, `logs`. |
| 9.3.0 | 2026-05-01 | Test suite (315 tests, `pytest -n auto` in ~29s) — internal. |
| 9.4.0 | 2026-05-01 | `ServerConfig.from_yaml(path)` classmethod. CLI flags now overlay YAML config (precedence: CLI > YAML > built-in defaults). Three docs published: `api-stability.md`, `cli.md`, `deployment.md`. New runtime dep: `pyyaml>=6.0`. |
| 9.4.1 | 2026-05-01 | Docs-only follow-up; functionally identical to 9.4.0. |
| 9.5.0a1 | 2026-05-02 | **Bot extension API — declarations slice.** Public `agentirc.protocol` exports: `Event`, `EventType` (now `StrEnum`), 20 `EVENT_TYPE_*` constants, `EVENTSUB` / `EVENTUNSUB` / `EVENT` / `EVENTERR` / `EVENTPUB` verb constants, `BOT_CAP`. New `ServerConfig.event_subscription_queue_max: int = 1024`. Symbols importable but inert. |
| 9.5.0a2 | 2026-05-02 | **Bot extension API — wire-format slice.** SEVENT and IRCv3 `event-data` tag now carry the canonical 5-field envelope `{type, channel, nick, data, timestamp}`. `_handle_sevent` sniffs the shape so 9.5 receivers tolerate ≤9.4 legacy peers (asymmetric: 9.5→9.4 emit breaks until peers upgrade). Added `agentirc.protocol.SEVENT`. Internal-only changes; no new public-API symbols beyond `SEVENT`. |
| 9.5.0 | 2026-05-02 | **Bot extension API — final.** `agentirc.io/bot` IRCv3 capability gates silent JOIN/PART/QUIT broadcasts, no auto-op on fresh channels, `+` prefix in NAMES, `B` flag in WHO. New IRC verbs: `EVENTSUB` / `EVENTUNSUB` / `EVENTPUB` (handlers + per-subscription bounded queues; `EVENT` / `EVENTERR` server→client). `webhook_port` no longer bound by `IRCd.start()` (field stays for backward compat). Closes [#15](https://github.com/agentculture/agentirc/issues/15). |
| 9.6.0 | 2026-05-02 | **Embedding API.** Promote `agentirc.ircd.IRCd` and `agentirc.virtual_client.VirtualClient` to the public surface so consumers can host an IRCd in-process and register in-process bots against it. Documents `IRCd.{start, stop, emit_event, subscription_registry, clients, channels, config, system_client}` as the in-process embedding contract. `agentirc._internal.virtual_client.VirtualClient` continues to resolve via a transitional re-export that emits `DeprecationWarning`; removal scheduled for 10.0.0. Closes [#22](https://github.com/agentculture/agentirc/issues/22). |
| 9.7.0 | 2026-06-12 | **Bot framework absorption.** Promote `agentirc.bots` (BotManager, Bot, BotConfig) to the public surface. Deterministic, YAML-spec'd event-trigger bots run as in-process VirtualClient presences. BotManager loads bot specs from `~/.culture/bots/`, dispatches Event objects to matching bots (via compiled filter expressions), and manages bot lifecycle. Worked example and full API reference in api-stability.md. Closes [#33](https://github.com/agentculture/agentirc/issues/33). |

## Distribution

- **Real PyPI:** `agentirc-cli` only.
- **TestPyPI:** dual-published as `agentirc-cli` (release name) and
  `agentirc` (squat we hold to prevent confusion). Both wheels point at
  the same code; `pip install agentirc==X.Y.Z.devN -i …testpypi…` and
  `pip install agentirc-cli==X.Y.Z.devN -i …testpypi…` install the
  same files.
- **Console scripts:** both `agentirc` and `agentirc-cli` map to
  `agentirc.cli:main`, regardless of which distribution name was used
  to install.

See [`docs/superpowers/specs/2026-04-30-bootstrap-design.md`](superpowers/specs/2026-04-30-bootstrap-design.md)
for the full bootstrap rationale and the cite-don't-copy provenance
ledger.
