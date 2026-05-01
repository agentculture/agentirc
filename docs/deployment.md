# Deploying agentirc

This document describes the on-disk footprint of an agentirc daemon,
how to run it under systemd, and how to coexist with culture (or run
standalone).

## On-disk footprint

agentirc preserves culture's `~/.culture/*` layout — the bootstrap is
explicit that defaults match culture so existing deployments don't
need to migrate. Standalone (non-culture) users override the config
path via `--config` and accept the rest as-is, or symlink to a more
neutral location.

| Path | Writer | Reader | Purpose |
|---|---|---|---|
| `~/.culture/server.yaml` | manual / config-management | `agentirc serve`, `agentirc start` | YAML config (loaded since 9.4.0). |
| `~/.culture/logs/server-<name>.log` | daemon (stdout/stderr) | user, `agentirc logs` | Per-server append-only log. |
| `~/.culture/pids/server-<name>.pid` | `agentirc start` | `agentirc {stop,status,restart}` | PID file. |
| `~/.culture/pids/server-<name>.port` | `agentirc start`/`serve` | `agentirc status` | Port-discovery file (written after listener binds). |
| `~/.culture/pids/default_server` | first `agentirc start` | subsequent verbs without `--name` | Default-name tracker. |
| `~/.culture/audit/server-<name>-YYYY-MM-DD[.N].jsonl` | daemon audit sink | external SIEM tools | Per-day audit JSONL (rotates at 256 MiB or UTC midnight). |
| `<data-dir>/history.db` (default `~/.culture/data/`) | daemon `HistoryStore` | daemon | SQLite channel history (WAL mode). |
| `<data-dir>/history.db-wal` | sqlite | sqlite | Auto-managed write-ahead log. |
| `<data-dir>/history.db-shm` | sqlite | sqlite | Auto-managed shared memory. |
| `$XDG_RUNTIME_DIR/` (or `~/.culture/run/`) | daemon | clients | Socket directory (mode 0700). |

`<name>` is sanitised before use in path interpolation (path-traversal
guard from PR #4 review): `..`, `/`, and other special characters are
rejected at the pidfile-helper boundary.

## Systemd integration

The recommended unit type is `Type=simple` with `agentirc serve`:

```ini
[Unit]
Description=agentirc IRCd
After=network.target

[Service]
Type=simple
User=ircd
ExecStart=/usr/local/bin/agentirc serve --name main --config /etc/agentirc/server.yaml
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Why `serve` (foreground), not `start` (daemonize):

- systemd already owns process supervision. `agentirc start` would
  fork, write its own PID file, and leave systemd seeing the parent
  exit cleanly — confusing for `Restart=` policies.
- `serve` writes no PID/port files, redirects to nothing (stdout/stderr
  stay attached so journald captures the log natively), and handles
  SIGTERM/SIGINT cleanly. Discoverability comes from systemd
  (`systemctl status agentirc-main.service`) rather than `agentirc
  status`, which reports `not running` for `serve`-managed daemons.

Save as `/etc/systemd/system/agentirc-main.service` and:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agentirc-main.service
sudo journalctl -u agentirc-main.service -f
```

For a launchd / supervisord / s6 / runit equivalent, mirror the same
choice: foreground entry point, no auto-fork.

## Container deployment

```dockerfile
FROM python:3.11-slim
RUN pip install --no-cache-dir agentirc-cli==9.4.0
EXPOSE 6667
ENV HOME=/data
VOLUME ["/data/.culture"]
CMD ["agentirc", "serve", "--host", "0.0.0.0"]
```

The `HOME=/data` + `VOLUME` pattern keeps `~/.culture/{logs,pids,audit,data}/`
in a persistent volume. Override `--config` if your config sits
outside the volume.

## Standalone (non-culture) deployment

The `~/.culture/*` defaults are a continuity choice for existing
culture installations. A standalone deployment overrides them via
flags:

```bash
agentirc serve --config /etc/agentirc/server.yaml \
               --data-dir /var/lib/agentirc \
               --host 0.0.0.0 --port 6667
```

Logs and pids still default to `~/.culture/`; redirect with
`HOME=/var/lib/agentirc` if you want a fully neutral footprint.

A minimal `server.yaml` for a standalone daemon:

```yaml
server:
  name: main
  host: 0.0.0.0
  port: 6667
data_dir: /var/lib/agentirc
telemetry:
  enabled: false
links: []
```

## Multi-host federation

S2S links are configured either via repeatable `--link` flags:

```bash
agentirc serve --link 'alpha:10.0.0.1:6667:secret:full' \
               --link 'beta:10.0.0.2:6667:secret:full'
```

…or as a `links:` section in YAML:

```yaml
links:
  - {name: alpha, host: 10.0.0.1, port: 6667, password: secret, trust: full}
  - {name: beta, host: 10.0.0.2, port: 6667, password: secret, trust: restricted}
```

Trust levels:

- `full` — peer can relay messages from any user, federate channel
  state, replay history.
- `restricted` — peer's S2S messages are accepted but not forwarded
  further; useful for peripheral nodes you don't want to use as a
  hop.

Validate a link spec with `agentirc link <spec>` before adding it.
The validator parses the format only — it doesn't try to connect.

CLI links override YAML links wholesale: passing any `--link` discards
the YAML `links:` section. To merge, edit the YAML.

## Coexisting with culture

agentirc and `culture server` can run on the same host. Both write to
`~/.culture/`, but the per-server files are namespaced:

- PID/port: `server-<name>.pid` / `.port`
- Log: `server-<name>.log`
- Audit: `server-<name>-YYYY-MM-DD.jsonl`

So `culture server start --name agent-a` and `agentirc start --name agent-b`
don't collide. They share the parent directories (`~/.culture/pids/`,
`~/.culture/logs/`, `~/.culture/audit/`).

The shared `~/.culture/server.yaml`: agentirc's loader silently
ignores keys it doesn't own (`supervisor`, `agents`, `webhooks`,
`buffer_size`, `poll_interval`, `sleep_start`, `sleep_end`, and the
`server.archived*` fields culture uses for its server-archival
tracking). Culture's loader treats agentirc-only keys (`links`,
`webhook_port`, `data_dir`, `system_bots`) symmetrically. So one
`server.yaml` can drive both daemons.

## Log rotation

agentirc does **not** rotate `~/.culture/logs/server-<name>.log` —
it appends to it. For long-running daemons, configure rotation
externally:

- **journald:** `Type=simple` with stdout/stderr → automatic.
- **logrotate:** `/etc/logrotate.d/agentirc`:

  ```text
  /home/*/.culture/logs/server-*.log {
      daily
      rotate 7
      compress
      missingok
      notifempty
      copytruncate
  }
  ```

  Use `copytruncate` rather than `create` — agentirc keeps the FD
  open via `dup2` and won't reopen on rename.

The audit JSONL files at `~/.culture/audit/server-<name>-YYYY-MM-DD.jsonl`
**do** rotate themselves: at UTC midnight (configurable via
`telemetry.audit_rotate_utc_midnight`), and when the current file
exceeds `telemetry.audit_max_file_bytes` (256 MiB default). The
rotated suffix is `.1`, `.2`, etc. within the same UTC day.

## Backup

Backup-relevant paths:

- **`~/.culture/server.yaml`** — config; small, infrequent change.
- **`<data-dir>/history.db*`** — SQLite database with channel history.
  Use `sqlite3 history.db ".backup '/backups/history-$(date +%F).db'"`
  for a hot backup; do **not** copy the `.db` and `.db-wal` files
  with `cp` while the daemon is running.
- **`~/.culture/audit/`** — append-only JSONL; back up incrementally.
  Useful for compliance and post-incident forensics.

PID, port, and log files are runtime state — recreated on next start —
and don't need backup.

## Upgrades

Standard pip / pipx workflow:

```bash
# In-place wheel upgrade
pip install --upgrade agentirc-cli

# Pinned in a venv
uv pip install agentirc-cli==9.4.0

# Restart the service
sudo systemctl restart agentirc-main.service
```

`agentirc-cli` follows [SemVer](https://semver.org/) — see
[`api-stability.md`](api-stability.md) for the public-surface contract.
Within a major version (`9.x`), upgrades are drop-in. Across major
versions, check the CHANGELOG for breaking changes (typically wire-
format fixes — see the *Wire-format quirks* section in
`api-stability.md`).
