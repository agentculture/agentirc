# `agentirc` CLI reference

```text
agentirc <verb> [options]
```

Both binaries (`agentirc`, `agentirc-cli`) point at `agentirc.cli:main`.
`python -m agentirc <verb>` works equivalently.

## Verbs

| Verb | Mode | Culture analogue | Description |
|---|---|---|---|
| [`serve`](#serve) | foreground | — | Run the IRCd in the foreground. No PID file. For systemd `Type=simple` and containers. |
| [`start`](#start) | daemon (or `--foreground`) | `culture server start` | Start the IRCd as a managed background daemon. |
| [`stop`](#stop) | one-shot | `culture server stop` | Stop the managed daemon (SIGTERM → 5s grace → SIGKILL). |
| [`restart`](#restart) | daemon | — | Stop (best-effort) then start. |
| [`status`](#status) | one-shot | `culture server status` | Report PID and listen port. |
| [`link`](#link) | one-shot | — | Validate a peer link spec (parse-only today). |
| [`logs`](#logs) | one-shot or `-f` | — | Cat or tail `~/.culture/logs/server-<name>.log`. |
| [`version`](#version) | one-shot | — | Print `agentirc <version>` (`--json` for machine-parseable output). |
| [`join`](#join) | one-shot | — | Join (creating if needed) a channel and confirm. |
| [`send`](#send) | one-shot | — | Send a one-shot message to a channel or nick (DM). |
| [`read`](#read) | one-shot | — | Catch up on channel or DM history (`--last`/`--since`/`--json`). |
| [`watch`](#watch) | streaming | — | Stream live channel messages until `SIGINT`/EOF. |

The verbs `serve`, `restart`, `link`, `logs`, `version` — and, since
9.10.0 (the agent-accessibility release), `join`, `send`, `read`, `watch`
— are agentirc-only additions; culture's `culture server` shim only ever
forwards verbs culture itself uses, so the additions don't break
passthrough. Unlike the lifecycle verbs above, `join`/`send`/`read`/`watch`
don't manage a daemon — they're agent-facing client verbs that drive an
*already-running* server over real TCP, documented end-to-end (with
copy-pasteable examples) in
[`docs/agent-walkthrough.md`](agent-walkthrough.md).

## Common flags

The lifecycle verbs (`serve`, `start`, `restart`) accept a shared flag
set (the *start flags*) plus a per-verb extra:

| Flag | Default | Purpose |
|---|---|---|
| `--name NAME` | resolved (see below) | Server display name. Drives PID/port/log filenames. |
| `--host HOST` | `0.0.0.0` | Bind address. |
| `--port PORT` | `6667` | Bind TCP port. `0` requests an OS-assigned port; in `start` mode the daemon won't write a `.port` file when `--port 0` is used, so `agentirc status` reports only the PID. |
| `--link SPEC` | none | S2S link, format `name:host:port:password[:trust]`. Repeatable. |
| `--webhook-port PORT` | `7680` | Accepted for backward compatibility; **not bound by `agentirc`** as of 9.5.0. The field is preserved so culture's `~/.culture/server.yaml` keeps loading unchanged; consumers (e.g. culture) host their own webhook listener if they need one. |
| `--data-dir PATH` | `~/.culture/data` | Directory for persistent storage (history.db, etc.). |
| `--config PATH` | `~/.culture/server.yaml` | YAML file to load defaults from. |

`stop`, `status`, `restart` (the stop half), `logs`: `--name NAME` only.

`--name` resolution when not supplied: read `~/.culture/pids/default_server`
(set by the first successful `agentirc start`); fall back to `agentirc`
if no default-server file exists.

## Config precedence

Since 9.4.0, `serve`/`start`/`restart` build their `ServerConfig` from
three sources, in decreasing priority:

1. **Explicit CLI flag** (e.g. `--port 9999`). Detected via sentinel
   `None` defaults — argparse only sets a non-`None` value when the
   user typed the flag.
2. **YAML key in `--config`**. Recognised top-level keys: `server`
   (with `name`/`host`/`port`), `telemetry`, `links`, `webhook_port`,
   `data_dir`, `system_bots`. Unknown keys (`supervisor`, `agents`,
   `buffer_size`, `poll_interval`, `sleep_start`, `sleep_end`,
   `webhooks`) are silently ignored — those belong to culture's
   broader process supervisor, and agentirc must coexist with culture
   on the same `~/.culture/server.yaml`.
3. **Built-in defaults** (the values in the table above).

Example: `~/.culture/server.yaml` contains

```yaml
server:
  name: spark
  host: 127.0.0.1
  port: 6700
telemetry:
  enabled: true
  audit_dir: /var/log/agentirc/audit
links:
  - {name: alpha, host: 10.0.0.1, port: 6667, password: secret}
```

Then `agentirc serve --port 9999` listens on `127.0.0.1:9999` (host
from YAML, port from CLI), with telemetry enabled, audit going to
`/var/log/agentirc/audit`, and one S2S link to `alpha`. The unknown
`supervisor:`/`agents:` sections that culture might add to the same
file don't trigger any warning or error.

A missing `--config` path is **not** an error; agentirc falls back to
the built-in defaults silently. Malformed YAML, however, raises a
`yaml.YAMLError` and aborts startup — fix or remove the file.

## Per-verb reference

### `serve`

```text
agentirc serve [--name NAME] [--host HOST] [--port PORT]
               [--link SPEC ...] [--webhook-port PORT]
               [--data-dir PATH] [--config PATH]
```

Run the IRCd in the foreground. No PID file is written; process
supervision is the caller's responsibility (systemd, container runtime,
`tmux`, etc.).

- **Stdout/stderr:** the daemon's log goes to the foreground streams.
- **SIGTERM/SIGINT:** triggers a clean shutdown (closes listening
  socket, terminates active connections, flushes audit, exits 0).
- **No PID/port files:** `serve` deliberately writes neither, so
  `agentirc status` reports `not running` for it. Use `start
  --foreground` if you need PID/port-file integration with status
  *and* a foreground process.
- **Exit code:** propagates the asyncio event loop's exit status; `0`
  on clean shutdown; non-zero if startup fails.

### `start`

```text
agentirc start [--name NAME] [--host HOST] [--port PORT]
               [--link SPEC ...] [--webhook-port PORT] [--foreground]
               [--data-dir PATH] [--config PATH]
```

Start the IRCd as a managed daemon. By default, forks into the
background; `--foreground` keeps the process attached for service
managers that want to own supervision themselves but still want PID/port
files.

- **PID file:** written to `~/.culture/pids/server-<name>.pid`.
- **Port file:** written to `~/.culture/pids/server-<name>.port` after
  the listener binds.
- **Default-server file:** the first successful `start` writes
  `~/.culture/pids/default_server` with the chosen name; subsequent
  verbs that omit `--name` read it.
- **Log file:** the daemon redirects stdout/stderr to
  `~/.culture/logs/server-<name>.log` (append).
- **Exit codes:**
  - `0` — daemon spawned and bound to its port.
  - `1` — already running (PID file present, process alive); or daemon
    failed to bind within 30s; or daemon child exited non-zero.

### `stop`

```text
agentirc stop [--name NAME]
```

Stop the managed daemon. Sequence: `SIGTERM` → poll up to 5s for
graceful exit (50 × 0.1s) → `SIGKILL` if still alive. The `is_managed_process`
gate prevents killing PIDs that have been recycled by an unrelated
process.

- **Exit codes:**
  - `0` — server stopped cleanly, *or* PID file was present but stale
    (cleaned up).
  - `1` — no PID file for the named server, *or* the PID belongs to a
    process that's not an agentirc/culture daemon.

### `restart`

```text
agentirc restart [start flags...]
```

If a PID file exists and the process is alive, `stop` it; then `start`
with the same arguments. Propagates non-zero exit codes from either
half.

### `status`

```text
agentirc status [--name NAME] [--json]
```

Read `~/.culture/pids/server-<name>.pid` and `.port` and report state:

- `Server '<name>': running (PID N, port P)` — both files present, PID alive.
- `Server '<name>': running (PID N)` — PID file present, no port file
  (older daemon, or `serve` started without a port).
- `Server '<name>': not running (no PID file)` — never started, or
  `stop`ped cleanly.
- `Server '<name>': not running (stale PID N)` — PID file present but
  the process is gone; agentirc cleans up the PID file.

Exit code is always `0` (status is a query, not a check). Scripts that
need a true alive/dead boolean should grep the output or rely on
`agentirc start`'s "already running" detection.

**Note:** `agentirc status` extends `culture server status` by also
printing the port. Strict superset; culture's shim relies on exit
codes, not output parsing.

**`--json`** (since 9.10.0): emits `{"name": ..., "running": bool, "pid":
int|null, "port": int|null}` (plus `"stale": true` when a PID file was
found but the process is gone — the same case the stale-PID text branch
reports) instead of the text form above. Without the flag, output is
byte-identical to pre-9.10.0.

### `link`

```text
agentirc link <peer>
```

Parse and validate a peer link spec (`name:host:port:password[:trust]`)
without contacting the peer. Useful for catching typos before adding
a link to `--link` or `~/.culture/server.yaml`.

- **Exit codes:**
  - `0` — spec parses cleanly.
  - `1` — parse error (printed to stderr).

Runtime mesh-mutation (adding a peer to a live IRCd, persisting it to
mesh.yaml) is out of scope for this verb — pass `--link` to
`agentirc start` or list peers under `links:` in `server.yaml`.

### `logs`

```text
agentirc logs [--name NAME] [-f|--follow]
```

Cat (or with `-f`, tail) `~/.culture/logs/server-<name>.log`. Reads the
file in 64 KB chunks rather than slurping; safe to run against
multi-GB log files. Malformed UTF-8 in the log is replaced (not
errored).

- **Exit codes:**
  - `0` — log printed cleanly, or user `Ctrl-C`'d the follow.
  - `1` — log file missing for the named server.

### `version`

```text
agentirc version [--json]
agentirc --version
```

Print `agentirc <version>` to stdout. The `--version` form raises
`SystemExit(0)` (argparse convention); the `version` verb returns `0`
through the dispatcher.

- **`--json`** (since 9.10.0): emits `{"name": "agentirc-cli", "version":
  "<version>"}` instead of the plain-text form. Without the flag, output
  is byte-identical to pre-9.10.0.

## Agent-facing client verbs

Since 9.10.0 (the agent-accessibility release), four additional verbs let
a shell agent talk to an *already-running* server over real TCP without
writing any Python: `join`, `send`, `read`, `watch`. All four are thin
wrappers around the public transport `agentirc.agent_client.AgentClient`
(see [`docs/api-stability.md#agentircagent_client`](api-stability.md#agentircagent_client)).
They share one flag set:

| Flag | Default | Purpose |
|---|---|---|
| `--host HOST` | `127.0.0.1` | Server to connect to. |
| `--port PORT` | `6667` | Server port. |
| `--nick NICK` | `agent-<random 4 hex>` | Nick to register as. **Every server rejects a nick that doesn't start with `<server-name>-`** — the default only round-trips against a server literally named `agent`; pass `--nick` explicitly otherwise. See [`docs/agent-walkthrough.md`](agent-walkthrough.md#2-a-note-on-nicknames-before-you-go-further). |

These verbs have no culture-server analogue and no YAML config to merge
against — unlike the lifecycle verbs, their flags use plain concrete
defaults, not the CLI/YAML-overlay sentinel pattern. A copy-pasteable,
step-by-step walkthrough (join → send → read → catch up via `--since` →
watch → DM → status → stop) lives in
[`docs/agent-walkthrough.md`](agent-walkthrough.md); this section is the
flag/exit-code reference.

### `join`

```text
agentirc join <channel> [--nick NICK] [--host HOST] [--port PORT]
```

Connect, join `<channel>` (must start with `#`; creates it if it doesn't
exist), wait for the server's join confirmation, print `Joined <channel>
as <nick>`, then disconnect. A one-shot presence/creation check, not a
way to stay connected.

- **Exit codes:**
  - `0` — join confirmed.
  - `1` — connection failed; server rejected the join (e.g. archived
    channel); or the join confirmation timed out.
  - `2` — `<channel>` doesn't start with `#` (checked before connecting).

### `send`

```text
agentirc send <target> <text> [--nick NICK] [--host HOST] [--port PORT]
```

Connect, send one `PRIVMSG <text>` to `<target>`, then disconnect.
`<target>` starting with `#` is a channel (auto-joined first if not
already a member); any other `<target>` is a nick — a direct message.
Nothing is echoed to stdout on success.

- **Exit codes:**
  - `0` — the line was written to the socket. **For a DM (non-`#`
    target), this is fire-and-forget: `send` does not wait for or check
    any server reply, so it returns `0` even when the recipient nick is
    offline and the server silently replies `ERR_NOSUCHNICK`** (the CLI
    never reads that reply). Verify DM delivery by having the sender
    `read <nick> --since <cursor>` afterward, not by trusting `send`'s
    exit code.
  - `1` — connection failed, or (channel targets only) the auto-join was
    rejected or timed out.

### `read`

```text
agentirc read <channel-or-nick> [--last N | --since CURSOR] [--json]
              [--nick NICK] [--host HOST] [--port PORT]
```

One-shot catch-up on history. `<channel-or-nick>` is a `#channel` or a
bare nick (reads *your* DM history with that nick — see
[`docs/agent-walkthrough.md`](agent-walkthrough.md#9-direct-messages-send-to-a-nick-read-the-dm-pair)).

- **`--last N`** (default `20` when neither flag is given): the last `N`
  messages via `HISTORY RECENT`. Plain text, no cursor.
- **`--since CURSOR`**: resume from an opaque cursor via `HISTORY SINCE`.
  Pass `'*'` for the beginning of retained history. Mutually exclusive
  with `--last`. Prints `next-cursor: <token>` to **stderr** (so it never
  pollutes a piped stdout stream); in `--json` mode also appends a
  trailing `{"next_cursor": "<token>"}` line to stdout. Feed the token
  back in as `--since` to page forward with no gaps and no duplicates,
  even across a retention prune.
- **`--json`**: one JSON object per line — `{"ts": ..., "nick": ...,
  "text": ..., "msgid": ...}` (`msgid` present only for real messages,
  never for server join/part notices). `ts` is the raw `HISTORY` replay
  timestamp (Unix epoch seconds as a string) — a different format from
  `watch --json`'s IRCv3 server-time `ts` (see below).

- **Exit codes:**
  - `0` — history printed (possibly empty).
  - `1` — connection failed, or the server rejected/timed out the
    `HISTORY` request (e.g. a malformed `--since` cursor).

### `watch`

```text
agentirc watch <channel> --json [--nick NICK] [--host HOST] [--port PORT]
```

Stream live messages posted to `<channel>` until `SIGINT` (`Ctrl-C`) or
EOF. Unlike `join`/`send`/`read`, `watch` keeps a connection open and
auto-reconnects on a drop (it's the one client verb backed by
`AgentClient(reconnect=True)`). `--json` emits `{"ts": ..., "nick": ...,
"text": ..., "msgid": ...}` per line, with `ts` as IRCv3 server-time
(`message-tags` cap, negotiated automatically) — not the epoch-seconds
`ts` `read --json` prints.

**Known gap:** `watch` only works against a `#channel` target — pointing
it at a bare nick to watch a DM stream currently connects successfully
but never surfaces any message (a filter mismatch between the CLI and
`AgentClient`'s DM/channel distinction). Use `read <nick> --since
<cursor> --json` on a polling interval for DMs today. See
[`docs/agent-walkthrough.md`](agent-walkthrough.md#9-direct-messages-send-to-a-nick-read-the-dm-pair).

- **Exit codes:**
  - `0` — clean `SIGINT`/EOF exit.
  - `1` — connection failed.

## `dispatch(argv)` for in-process callers

The `agentirc.cli.dispatch(argv)` function is the integration surface
for callers that don't want to spawn a subprocess. It returns an `int`
exit code on successful dispatch and lets `argparse`'s `SystemExit`
propagate for `--help`, `--version`, and parse errors. In-process
callers must wrap:

```python
import agentirc.cli
try:
    rc = agentirc.cli.dispatch(["start", "--port", "6700"])
except SystemExit as e:
    rc = e.code or 0
```

Or use `subprocess.run(["agentirc", *argv])` and rely on the process
exit code instead. Culture's `culture server` shim uses the
`subprocess.run` form.

## Differences from `culture server`

| | `culture server` | `agentirc` |
|---|---|---|
| `start`/`stop`/`status` verbs | yes | yes |
| `serve` | no | yes (foreground, no PID) |
| `restart` / `link` / `logs` / `version` | no | yes |
| `join` / `send` / `read` / `watch` (since 9.10.0) | no | yes — agent-facing client verbs, not part of daemon lifecycle |
| `default` / `rename` / `archive` / `unarchive` | yes | no — culture-only manifest verbs |
| `start --mesh-config PATH` | yes | no — depends on culture's credentials/mesh_config (out of scope) |
| `status` output | `running (PID N)` | `running (PID N, port P)` (strict superset); `--json` since 9.10.0 |
| `--config` YAML loading | wraps `culture.config.ServerConfig` (server + supervisor + webhooks + agents) | flat `agentirc.config.ServerConfig` (server + telemetry + links + webhook_port + data_dir + system_bots + event_subscription_queue_max + ping_interval + pong_timeout) |

The two CLIs share the on-disk layout (`~/.culture/{logs,pids,audit,data}/`),
so they coexist on the same host with distinct `--name` values.
