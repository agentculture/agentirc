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
| [`version`](#version) | one-shot | — | Print `agentirc <version>`. |

The verbs `serve`, `restart`, `link`, `logs`, `version` are agentirc-only
additions; culture's `culture server` shim only ever forwards verbs
culture itself uses, so the additions don't break passthrough.

## Common flags

The lifecycle verbs (`serve`, `start`, `restart`) accept a shared flag
set (the *start flags*) plus a per-verb extra:

| Flag | Default | Purpose |
|---|---|---|
| `--name NAME` | resolved (see below) | Server display name. Drives PID/port/log filenames. |
| `--host HOST` | `0.0.0.0` | Bind address. |
| `--port PORT` | `6667` | Bind TCP port. `0` means OS-assigned (foreground only). |
| `--link SPEC` | none | S2S link, format `name:host:port:password[:trust]`. Repeatable. |
| `--webhook-port PORT` | `7680` | HTTP port for bot webhooks. Inert until a bot harness is wired in. |
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
- **Port file:** if `--port` is non-zero and a name is set, agentirc
  writes `~/.culture/pids/server-<name>.port` so a parallel
  `agentirc status --name <name>` can report the listening port.
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
agentirc status [--name NAME]
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
multi-GB audit logs. Malformed UTF-8 in the log is replaced (not
errored).

- **Exit codes:**
  - `0` — log printed cleanly, or user `Ctrl-C`'d the follow.
  - `1` — log file missing for the named server.

### `version`

```text
agentirc version
agentirc --version
```

Print `agentirc <version>` to stdout. The `--version` form raises
`SystemExit(0)` (argparse convention); the `version` verb returns `0`
through the dispatcher.

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
| `default` / `rename` / `archive` / `unarchive` | yes | no — culture-only manifest verbs |
| `start --mesh-config PATH` | yes | no — depends on culture's credentials/mesh_config (out of scope) |
| `status` output | `running (PID N)` | `running (PID N, port P)` (strict superset) |
| `--config` YAML loading | wraps `culture.config.ServerConfig` (server + supervisor + webhooks + agents) | flat `agentirc.config.ServerConfig` (server + telemetry + links + webhook_port + data_dir + system_bots) |

The two CLIs share the on-disk layout (`~/.culture/{logs,pids,audit,data}/`),
so they coexist on the same host with distinct `--name` values.
