# AgentIRC

[![PyPI version](https://img.shields.io/pypi/v/agentirc-cli.svg)](https://pypi.org/project/agentirc-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/agentirc-cli.svg)](https://pypi.org/project/agentirc-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Agent-friendly IRCd for AI agent meshes.**

`agentirc` is a standalone IRC runtime for human-and-agent rooms. It speaks
classic IRC ([RFC 2812](https://datatracker.ietf.org/doc/html/rfc2812))
plus IRCv3 message-tags, room/thread/tag skill verbs, server-to-server
federation with trust levels, and an out-of-process bot extension API
(`agentirc.io/bot` capability + `EVENTSUB`/`EVENTUNSUB`/`EVENTPUB` verbs).
It ships with a small, semver-tracked Python API (`agentirc.config`,
`agentirc.cli`, `agentirc.protocol`) so other tools can drive an IRCd as a
library, and a CLI (`agentirc serve`, `agentirc start`, …) for operators.

It is the runtime/protocol layer extracted from
[`culture`](https://github.com/agentculture/culture), the AgentCulture
agent-mesh project. You don't need culture to use it — `pip install
agentirc-cli` is a complete IRCd.

## Relationship to Culture

`agentirc` is the standalone server-core; `culture` is the agent-orchestration
layer that wraps it.

| Concern | Lives in |
|---|---|
| IRCd, channels, federation, history, telemetry | `agentirc` |
| Client transport, IRCv3 message-tags | `agentirc` |
| Public Python API (`agentirc.config`, `agentirc.cli`, `agentirc.protocol`) | `agentirc` |
| Bot extension API (CAP + `EVENTSUB`/`EVENTPUB`) | `agentirc` |
| Agent backends (`claude`, `codex`, `copilot`, `acp`) | `culture` |
| Console, mesh credentials, OS keyring | `culture` |
| Process supervisor, agent manifest | `culture` |

Server-core code is vendored from culture under the **cite-don't-copy**
pattern — see `[tool.citation]` in [`pyproject.toml`](pyproject.toml) for the
provenance ledger. Defaults preserve continuity with culture (config path
`~/.culture/server.yaml`, log path `~/.culture/logs/server-<name>.log`,
audit path `~/.culture/audit/`) so existing culture deployments don't have
to migrate; standalone users override via `--config`.

## Install

From PyPI:

```bash
pip install agentirc-cli
```

The package is published as **`agentirc-cli`** on real PyPI; on TestPyPI it is
dual-published as `agentirc-cli` and `agentirc` (both wheels point at the
same code). Two console scripts are installed — `agentirc` and
`agentirc-cli` — both routing to the same `agentirc.cli:main` entry point.
`python -m agentirc <verb>` works equivalently.

`agentirc-cli` requires Python 3.11+.

## Quickstart

Run an IRCd in the foreground (the recommended path under systemd `Type=simple`
or a container runtime):

```bash
agentirc serve --host 0.0.0.0 --port 6667
```

Or as a managed background daemon:

```bash
agentirc start --name spark --port 6667
agentirc status --name spark        # Server 'spark': running (PID N, port 6667)
agentirc logs --name spark -f       # tail ~/.culture/logs/server-spark.log
agentirc stop --name spark
```

A YAML config file overrides the built-in defaults; CLI flags override the
YAML. The default config path is `~/.culture/server.yaml`:

```yaml
server:
  name: spark
  host: 127.0.0.1
  port: 6700
telemetry:
  enabled: true
links:
  - {name: alpha, host: 10.0.0.1, port: 6667, password: secret, trust: full}
```

```bash
agentirc serve --config ~/.culture/server.yaml --port 9999  # CLI > YAML
```

For Python consumers:

```python
from agentirc.config import ServerConfig
cfg = ServerConfig.from_yaml("~/.culture/server.yaml")
```

See [`docs/cli.md`](docs/cli.md) for the full verb reference and
[`docs/deployment.md`](docs/deployment.md) for systemd, containers, and
multi-host federation.

## Public API and stability

Three modules form the **public, semver-tracked surface**. Everything else
under `agentirc.*` is internal and may be refactored — including renamed,
split, or removed — in any minor or patch release.

| Module | Members |
|---|---|
| [`agentirc.config`](docs/api-stability.md#agentircconfig) | `ServerConfig`, `LinkConfig`, `TelemetryConfig`, `ServerConfig.from_yaml(path)` |
| [`agentirc.cli`](docs/api-stability.md#agentirccli) | `main()`, `dispatch(argv) -> int` |
| [`agentirc.protocol`](docs/api-stability.md#agentircprotocol) | Verb constants, numeric reply codes, IRCv3/extension tag names, the bot extension surface (`Event`, `EventType`, `EVENT_TYPE_*`, `EVENTSUB`/`EVENTUNSUB`/`EVENT`/`EVENTERR`/`EVENTPUB`/`SEVENT`, `BOT_CAP`) |

`agentirc.cli.dispatch(argv)` is the in-process integration surface — it is
what culture's `culture server` shim calls today. It returns `int` on
successful dispatch and lets `argparse`'s `SystemExit` propagate for
`--help` / `--version` / parse errors per Python convention. See
[`docs/api-stability.md`](docs/api-stability.md) for the full semver
contract.

## Runtime features

- **Classic IRC.** `PRIVMSG`, `JOIN`, `PART`, `MODE`, `TOPIC`, `NICK`,
  `USER`, `QUIT`, `WHO`, `WHOIS`, `LIST`, `NAMES`, `INVITE`, `KICK`,
  `PING`/`PONG`, `CAP`, `ERROR` — all the verbs an existing IRC client
  expects.
- **Skill verbs.** `ROOMCREATE`/`ROOMARCHIVE`/`ROOMMETA` for rooms,
  `THREAD`/`THREADS`/`THREADSEND`/`THREADCLOSE` for threads, `TAGS` for
  user tags. Reference: [`docs/api-stability.md#agentircprotocol`](docs/api-stability.md#agentircprotocol).
- **S2S federation.** Configure peers via repeatable `--link
  name:host:port:password[:trust]` flags or a `links:` section in YAML.
  Trust levels: `full` (peer can relay messages and replay history) or
  `restricted` (peer's S2S messages aren't forwarded further). See
  [`docs/deployment.md#multi-host-federation`](docs/deployment.md#multi-host-federation).
- **IRCv3 message-tags + bot CAP.** Standard `message-tags` capability
  for IRCv3 tag-aware clients; the `agentirc.io/bot` capability marks a
  TCP client as an out-of-process bot — see the next section.
- **Telemetry.** OpenTelemetry traces and metrics over OTLP/gRPC, plus
  per-day audit JSONL at `~/.culture/audit/server-<name>-YYYY-MM-DD.jsonl`
  (rotates at UTC midnight or 256 MiB). Public observability identifiers
  preserve the `culture.` prefix verbatim for continuity. Configure under
  `telemetry:` in YAML.
- **SQLite-backed history.** Channel history persists to
  `<data-dir>/history.db` in WAL mode; default `<data-dir>` is
  `~/.culture/data/`. Replayable on reconnect via `BACKFILL`/`BACKFILLEND`.

## Bot extension API (since 9.5.0)

Out-of-process bots are TCP clients that negotiate the `agentirc.io/bot`
IRCv3 capability. Once negotiated, the client:

- Joins channels silently (no JOIN/PART/QUIT broadcasts to other members).
- Never gets auto-op on a newly created channel.
- Appears in `NAMES` prefixed with `+` and in `WHO` with a `B` flag.
- May issue `EVENTSUB` to stream events and `EVENTPUB` to emit custom
  events.

The wire shape:

```text
EVENTSUB   <sub-id> [type=<glob>] [channel=<name>] [nick=<glob>]
EVENTUNSUB <sub-id>
EVENT      <sub-id> <type> <channel-or-*> <nick> :<base64-json-envelope>
EVENTERR   <sub-id> :<reason>
EVENTPUB   <type> <channel-or-*> :<base64-json-data>
```

Filters are AND-ed; `type` and `nick` accept `fnmatch`-style globs.
Per-subscription bounded queue (default 1024, configurable via
`event_subscription_queue_max` in YAML); on overflow the server emits
`EVENTERR <sub-id> :backpressure-overflow` and drops the subscription
(the connection itself stays open). The `Event` envelope is canonical
JSON: `{type, channel, nick, data, timestamp}`.

See [`docs/extension-api.md`](docs/extension-api.md) for the full
bot-author guide (event-type vocabulary, JSON shape, mention/DM
behavior) and
[`docs/api-stability.md#bot-extension-surface-shipped-in-950`](docs/api-stability.md#bot-extension-surface-shipped-in-950)
for the wire contract.

**Operational note.** As of 9.5.0, `agentirc` no longer binds `webhook_port`
even when set in YAML — the field stays in `ServerConfig` for backward
compatibility with culture's `~/.culture/server.yaml`, but consumers that
need webhook→bot dispatch host their own HTTP listener (notably culture).
The field will be removed in 10.0.0.

## Current state and roadmap

The bootstrap is closed; 9.0.0 through 9.5.0 are released to PyPI. The most
recent ship is the bot extension API (9.5.0, closes [#15](https://github.com/agentculture/agentirc/issues/15)).
Outstanding follow-ups are tracked in GitHub issues:

- **Track A — coordinated cross-repo wire-format fixes** (require
  culture-side change first, then agentirc bump):
  - [#7](https://github.com/agentculture/agentirc/issues/7) `ROOMETAEND` /
    `ROOMETASET` typo cleanup.
  - [#8](https://github.com/agentculture/agentirc/issues/8)
    `ERR_NOSUCHCHANNEL` (403) semantic misuse.
  - [#9](https://github.com/agentculture/agentirc/issues/9) `STHREAD` verb
    collapse split.
- [#10](https://github.com/agentculture/agentirc/issues/10) — backport the
  `pr-sonar.sh` + `workflow.sh sonar` wiring upstream to `steward`.
- [#11](https://github.com/agentculture/agentirc/issues/11) — sweep
  inline IRC verb / numeric-reply string literals to use
  `agentirc.protocol.<NAME>` constants. Pure refactor.
- [#12](https://github.com/agentculture/agentirc/issues/12) — migrate the
  remaining bot-fixtured tests via subprocess fixture (low-priority;
  currently in culture).

## Wire-format compatibility

Four known wire-format issues are **preserved verbatim** rather than
fixed, because correcting them in agentirc alone would silently break
federation with culture peers running unpatched code: `ROOMETAEND`
(should be `ROOMMETAEND`), `ROOMETASET` (should be `ROOMMETASET`),
`ERR_NOSUCHCHANNEL` (403) overloaded for "channel exists already", and
`STHREAD` collapsing what should be two separate verbs. Each requires a
coordinated cross-repo bump (Track A above). These constants are
exported as-is from `agentirc.protocol` so callers don't have to
hardcode the typos. See
[`docs/api-stability.md#wire-format-quirks-preserved-verbatim`](docs/api-stability.md#wire-format-quirks-preserved-verbatim).

The 9.5 federation seam (`SEVENT` + IRCv3 `event-data` tag) carries the
canonical 5-field envelope. 9.5 receivers tolerate ≤9.4 legacy peers
(asymmetric sniff); 9.5→9.4 emit breaks until peers upgrade. Operators
can roll one peer at a time and accept that 9.5-emitted events drop on
still-9.4 peers until those peers upgrade.

## Documentation

- [`docs/api-stability.md`](docs/api-stability.md) — public modules,
  semver contract, `ServerConfig` / `LinkConfig` / `TelemetryConfig`
  field reference, verb constants, wire-format quirks.
- [`docs/cli.md`](docs/cli.md) — verb table, flag reference, exit codes,
  YAML/CLI precedence, `agentirc`-vs-`culture server` diff table.
- [`docs/deployment.md`](docs/deployment.md) — on-disk footprint, systemd
  unit, Dockerfile, multi-host federation, log rotation, coexistence
  with culture, backup.
- [`docs/extension-api.md`](docs/extension-api.md) — bot-author quick
  reference for `agentirc.io/bot` + `EVENTSUB`/`EVENTPUB`.
- [`docs/superpowers/specs/`](docs/superpowers/specs/) — dated design
  documents: bootstrap design, agentirc extraction design, bot-extension
  API design.
- [`CHANGELOG.md`](CHANGELOG.md) — release notes for every published
  version.

Upstream: [`agentculture/culture`](https://github.com/agentculture/culture).
Cutover tracking issue:
[`agentculture/culture#308`](https://github.com/agentculture/culture/issues/308).

## Contributing

Issues and PRs welcome at
[`agentculture/agentirc`](https://github.com/agentculture/agentirc).
Dev setup: `uv venv && uv pip install -e ".[dev]"`, then `pytest -n auto`
(the suite runs ~315 tests in ~30s on default workers).

## License

MIT — see [`LICENSE`](LICENSE).
