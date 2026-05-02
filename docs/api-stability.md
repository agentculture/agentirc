# Public API stability

`agentirc-cli` exposes three public modules; everything else is internal
and may be refactored without a major version bump. Downstream consumers
(notably `culture`, which pins `agentirc-cli>=9.0,<10` and calls
`agentirc.cli.dispatch(argv)` from its `culture server` shim) should
import only from these three modules.

| Module | Members | Stability |
|---|---|---|
| [`agentirc.config`](#agentircconfig) | `ServerConfig`, `LinkConfig`, `TelemetryConfig` | Public, semver-tracked |
| [`agentirc.cli`](#agentirccli) | `main()`, `dispatch(argv) -> int` | Public, semver-tracked |
| [`agentirc.protocol`](#agentircprotocol) | Verb constants, numeric reply codes, IRCv3/extension tag names | Public, semver-tracked |
| [`agentirc.ircd`](#agentircircd) | `IRCd` (constructor + `start`/`stop`/`emit_event`/`subscription_registry`/`clients`/`channels`/`config`/`system_client`) | Public, semver-tracked (since 9.6.0) |
| [`agentirc.virtual_client`](#agentircvirtual_client) | `VirtualClient` | Public, semver-tracked (since 9.6.0) |

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

`agentirc.ircd.IRCd` and `agentirc.virtual_client.VirtualClient` were
promoted to the public surface in 9.6.0 (see
[Embedding agentirc in-process](#embedding-agentirc-in-process)).
The legacy import path `agentirc._internal.virtual_client.VirtualClient`
still resolves but emits `DeprecationWarning`; it will be removed in
10.0.0.

## `agentirc.config`

Three dataclasses plus one classmethod loader.

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
```

Plus, since 9.4.0:

```python
@classmethod
def from_yaml(cls, path: str | Path) -> ServerConfig
```

Loads a `ServerConfig` from `~/.culture/server.yaml` (or any YAML file).
Recognised top-level keys: `server` (with `name`/`host`/`port`),
`telemetry`, `links`, `webhook_port`, `data_dir`, `system_bots`.
Unknown top-level keys (`supervisor`, `agents`, `buffer_size`,
`poll_interval`, `sleep_start`, `sleep_end`, `webhooks`) are silently
ignored — those belong to culture's broader process supervisor, and
agentirc must coexist with culture using the same config file. Missing
files return defaults; malformed YAML raises `yaml.YAMLError`.

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

### Numeric reply codes

Re-exported from `agentirc._internal.protocol.replies`. About 33 names:
`ERR_*` (`ERR_ALREADYREGISTRED`, `ERR_NEEDMOREPARAMS`,
`ERR_NICKNAMEINUSE`, `ERR_NOSUCHCHANNEL`, …) and `RPL_*`
(`RPL_WELCOME`, `RPL_YOURHOST`, `RPL_CREATED`, `RPL_MYINFO`, …).

### IRCv3 / extension tag names

Re-exported from `agentirc._internal.telemetry.context`:
`TRACEPARENT_TAG`, `TRACESTATE_TAG`, `EVENT_TAG_TYPE`, `EVENT_TAG_DATA`.

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
