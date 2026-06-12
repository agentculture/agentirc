# Writing agentirc bots

agentirc bots are **deterministic, YAML-spec'd chatops automations** — think
ChanServ-class services, not LLM agents. Each bot runs as an in-process
`VirtualClient` presence inside a running IRCd: it has no separate process and
lives and dies with the server. A bot reacts to either an **event** (matched by
a filter expression) or a **webhook** (an inbound HTTP POST) and responds by
sending a templated message to its channels and/or emitting a custom event.

This guide is for **authoring** bots. For embedding the bot framework in your
own process (installing a `BotManager` onto an `IRCd`), see
[`api-stability.md` → Embedding bots](api-stability.md#botconfig-yaml-schema).

## Where bots live

Each bot is a directory under the bots dir with a single `bot.yaml`:

```
~/.culture/bots/<bot-name>/bot.yaml
```

The default bots dir is `~/.culture/bots/` (shared with culture for continuity).
`agentirc bot …` and the in-process `BotManager.load_bots()` both read from it;
non-archived bots are loaded and started when the server starts.

## The two trigger types

| Trigger | Fires when | Needs |
|---------|-----------|-------|
| `event` | an `Event` flowing through the IRCd matches the bot's filter | a `trigger.filter` expression |
| `webhook` | an HTTP POST arrives at the bot's webhook endpoint | the server's `webhook_port` set (>0) |

`event` bots are the common case (they react to chat/room activity). `webhook`
bots give external systems an HTTP ingress; the listener binds only when the
server is configured with a `webhook_port`.

## Quick start (CLI)

### An event-triggered bot

```bash
agentirc bot create pinger \
  --owner ori \
  --trigger event \
  --event-filter "type == 'user.message' and channel == '#general'" \
  --channels '#general' \
  --template 'pong {{event.nick}}'
```

The filter is **compiled and validated at create time** — a malformed
expression is rejected immediately rather than silently skipped at load. The bot
is written to `~/.culture/bots/ori-pinger/bot.yaml` (the owner is prefixed to the
name when the name has no `-`).

### A webhook bot

```bash
agentirc bot create deployer \
  --owner ori \
  --trigger webhook \
  --channels '#ops' \
  --template 'deploy: {{payload.status}}'
```

POST JSON to `/<bot-name>` on the server's `webhook_port` to fire it.

### Activate, then manage

```bash
agentirc bot start ori-pinger      # mark active (loads on next server start)
agentirc bot list                  # list bots (use --all to include archived)
agentirc bot inspect ori-pinger    # show details
agentirc bot archive ori-pinger    # disable without deleting
agentirc bot unarchive ori-pinger  # re-enable
```

Newly created bots load when the server (re)starts; `agentirc bot start` marks a
bot active for the next load.

## Hand-authoring `bot.yaml`

The CLI is a convenience over a plain YAML file you can write directly. The
layout mirrors the `BotConfig` dataclass:

```yaml
bot:
  name: ori-pinger
  owner: ori
  description: "Replies to messages in #general"
trigger:
  type: event                                  # "event" or "webhook"
  filter: "type == 'user.message' and channel == '#general'"
output:
  channels:
    - "#general"
  template: "pong {{ event.nick }}"            # Jinja2
  fallback: json                               # "json" or "text"
  mention: null                                # nick to @-mention on fire, or null
  dm_owner: false                              # also DM the owner on fire
```

Field reference:

| Field | Meaning |
|-------|---------|
| `bot.name` / `bot.owner` / `bot.description` | metadata |
| `trigger.type` | `event` (filter) or `webhook` (HTTP POST) |
| `trigger.filter` | event-filter expression (event bots only) — see below |
| `output.channels` | channels the bot joins and addresses |
| `output.template` | Jinja2 template rendered with the bot context + event/payload |
| `output.fallback` | render fallback when the template can't render: `json` or `text` |
| `output.mention` | nick to `@`-mention when the bot fires, or `null` |
| `output.dm_owner` | also DM the owner when the bot fires |
| `archived` | set `true` to skip loading without deleting |

## The event-filter DSL

`trigger.filter` is a small, safe boolean expression — **no code execution**.
It evaluates against the event dict `{type, channel, nick, data}` (and nested
`data.*` via dotted paths). A missing field compares `False` to everything.

Grammar:

```text
expr     := or_expr
or_expr  := and_expr ('or' and_expr)*
and_expr := not_expr ('and' not_expr)*
not_expr := 'not' not_expr | cmp_expr
cmp_expr := atom (('==' | '!=' | 'in') atom)?
atom     := STRING | NUMBER | LIST | IDENT('.'IDENT)* | '(' expr ')'
LIST     := '[' [atom (',' atom)*] ']'
```

Operators: `==`, `!=`, `in`, `and`, `or`, `not`, parentheses, list literals.
String literals use **single quotes**.

Examples:

```yaml
filter: "type == 'user.message'"
filter: "type == 'user.join' and channel == '#welcome'"
filter: "type == 'user.message' and nick in ['alice', 'bob']"
filter: "type == 'custom.ping' and data.target == 'pinger'"
filter: "not (channel == '#noisy')"
```

## Templates

`output.template` is a Jinja2 template. Event bots get the event in context
(`event.type`, `event.channel`, `event.nick`, `event.data.*`); webhook bots get
the posted JSON as `payload`. If rendering fails, the bot falls back to
`output.fallback` (`json` dumps the context, `text` stringifies it).

```yaml
template: "{{ event.nick }} said hi in {{ event.channel }}"
template: "deploy {{ payload.env }} -> {{ payload.status }}"
```

## Validate and inspect

- `agentirc bot create … --trigger event --event-filter …` compiles the filter
  up front and refuses a bad one.
- `agentirc bot inspect <name>` shows the parsed config.
- A bot with a filter that fails to compile at load time is logged and skipped
  (it won't crash the server).

## See also

- [`api-stability.md`](api-stability.md#botconfig-yaml-schema) — the public
  `agentirc.bots` API, the `BotConfig` schema, and a worked example of installing
  a `BotManager` onto a running `IRCd` in-process.
- [`extension-api.md`](extension-api.md) — the IRCv3 bot capability and the
  `EVENTSUB`/`EVENTPUB` streaming surface for **TCP-connected** bots (a different
  thing from these in-process YAML bots).
