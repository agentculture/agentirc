from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LinkConfig:
    """Configuration for a server-to-server link."""

    name: str
    host: str
    port: int
    password: str
    trust: str = "full"  # "full" or "restricted"


@dataclass
class TelemetryConfig:
    """OpenTelemetry settings. Mirrors server.yaml `telemetry:` block."""

    enabled: bool = False
    service_name: str = "culture.agentirc"
    otlp_endpoint: str = "http://localhost:4317"
    otlp_protocol: str = "grpc"  # grpc | http/protobuf (only grpc supported initially)
    otlp_timeout_ms: int = 5000
    otlp_compression: str = "gzip"  # gzip | none
    traces_enabled: bool = True
    traces_sampler: str = "parentbased_always_on"
    metrics_enabled: bool = True
    metrics_export_interval_ms: int = 10000
    # Audit JSONL sink (Plan 4). Independent of `enabled` — audit fires
    # even when telemetry is off so admins always have the trail.
    audit_enabled: bool = True
    audit_dir: str = "~/.culture/audit"
    audit_max_file_bytes: int = 256 * 1024 * 1024  # 256 MiB
    audit_rotate_utc_midnight: bool = True
    audit_queue_depth: int = 10000


@dataclass
class PresenceConfig:
    """Resident-presence heartbeat/staleness settings. Mirrors server.yaml
    `presence:` block (culture-shaped: heartbeat_interval_seconds/
    stale_after_seconds).

    Validated fail-fast in ``__post_init__``: both fields must be positive
    integers, and ``stale_after_seconds`` must be strictly greater than
    ``heartbeat_interval_seconds`` — otherwise a resident heartbeating
    exactly on schedule could still be flagged presumed-hung between
    beats.
    """

    heartbeat_interval_seconds: int = 30
    stale_after_seconds: int = 90

    def __post_init__(self) -> None:
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError(
                "presence.heartbeat_interval_seconds must be a positive "
                f"integer, got {self.heartbeat_interval_seconds!r}"
            )
        if self.stale_after_seconds <= 0:
            raise ValueError(
                "presence.stale_after_seconds must be a positive integer, "
                f"got {self.stale_after_seconds!r}"
            )
        if self.stale_after_seconds <= self.heartbeat_interval_seconds:
            raise ValueError(
                "presence.stale_after_seconds "
                f"({self.stale_after_seconds!r}) must be strictly greater "
                "than presence.heartbeat_interval_seconds "
                f"({self.heartbeat_interval_seconds!r})"
            )


@dataclass
class ServerConfig:
    """Configuration for a culture server instance."""

    name: str = "culture"
    host: str = "0.0.0.0"
    port: int = 6667
    webhook_port: int = 7680
    data_dir: str = ""
    links: list[LinkConfig] = field(default_factory=list)
    system_bots: dict = field(default_factory=dict)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    # Bot extension API (9.5.0): per-subscription event-queue bound. When
    # exceeded, the subscription is dropped with EVENTERR :backpressure-overflow
    # and the bot reconciles via re-subscribe + BACKFILL. Behavior wires up in
    # 9.5.0a3; the field is exposed in 9.5.0a1 so consumers can pin against the
    # public surface.
    event_subscription_queue_max: int = 1024
    # Server liveness (t5): seconds of inbound-idle time on a local TCP
    # client before the server sends a keepalive `PING :<server-name>`, and
    # additional seconds of idle time past that before the connection is
    # reaped (closed through the normal disconnect path). `0` or negative
    # disables the liveness sweep loop entirely.
    ping_interval: float = 60.0
    pong_timeout: float = 120.0
    presence: PresenceConfig = field(default_factory=PresenceConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ServerConfig":
        """Load a ServerConfig from a YAML file.

        Recognises top-level ``server`` (host/port/name), ``telemetry``,
        ``presence`` (t2: resident-presence heartbeat/staleness knobs —
        see ``PresenceConfig``), ``links``, ``webhook_port``, ``data_dir``,
        ``system_bots``, ``event_subscription_queue_max`` (added in
        9.5.0a1; consumed by the subscription registry that lands in
        9.5.0a3), and ``ping_interval``/``pong_timeout`` (t5: server
        liveness sweep — see ``ServerConfig.ping_interval``/``pong_timeout``).
        Unknown top-level keys (``supervisor``, ``agents``, ``buffer_size``,
        ``poll_interval``, ``sleep_start``, ``sleep_end``) are silently
        ignored — those belong to culture's broader process supervisor,
        and agentirc must coexist with culture using the same
        ``~/.culture/server.yaml`` file. Unknown keys *inside* the
        ``server:`` block are also tolerated for the same reason
        (culture's ``ServerConnConfig`` carries ``archived``,
        ``archived_at``, ``archived_reason`` that agentirc has no use
        for); the same tolerance applies inside the ``presence:`` block.

        A missing path returns the dataclass defaults rather than
        raising — callers (CLI handlers) treat the file as optional.
        Malformed YAML raises ``yaml.YAMLError`` from the underlying
        loader; we deliberately do not catch it so users see the parse
        error. An invalid ``presence:`` section (non-positive values, or
        ``stale_after_seconds`` not strictly greater than
        ``heartbeat_interval_seconds``) raises ``ValueError`` from
        ``PresenceConfig.__post_init__``; we likewise let it propagate so
        the operator sees the offending keys/values.
        """
        p = Path(path).expanduser()
        if not p.exists():
            return cls()
        raw = _load_root_mapping(p)
        return cls(**_yaml_kwargs(raw))


def _load_root_mapping(p: Path) -> dict[str, Any]:
    """Load YAML at *p* and require the root to be a mapping."""
    with p.open() as f:
        loaded = yaml.safe_load(f)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise yaml.YAMLError(
            f"agentirc config {str(p)!r}: root must be a mapping, "
            f"got {type(loaded).__name__}"
        )
    return loaded


def _yaml_kwargs(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a raw YAML mapping onto ServerConfig constructor kwargs."""
    server_section = raw.get("server") or {}
    kwargs: dict[str, Any] = {}
    for key in ("name", "host", "port"):
        if key in server_section:
            kwargs[key] = server_section[key]
    for key in (
        "webhook_port",
        "data_dir",
        "event_subscription_queue_max",
        "ping_interval",
        "pong_timeout",
    ):
        if key in raw:
            kwargs[key] = raw[key]
    links_section = raw.get("links") or []
    if links_section:
        kwargs["links"] = [LinkConfig(**entry) for entry in links_section]
    telemetry_section = raw.get("telemetry") or {}
    if telemetry_section:
        kwargs["telemetry"] = _build_telemetry(telemetry_section)
    presence_section = raw.get("presence") or {}
    if presence_section:
        kwargs["presence"] = _build_presence(presence_section)
    system_bots = raw.get("system_bots") or {}
    if system_bots:
        kwargs["system_bots"] = system_bots
    return kwargs


def _build_telemetry(yaml_telemetry: dict) -> TelemetryConfig:
    """Build a TelemetryConfig, dropping keys not on the dataclass."""
    known = {f.name for f in TelemetryConfig.__dataclass_fields__.values()}
    tcfg = {k: v for k, v in yaml_telemetry.items() if k in known}
    return TelemetryConfig(**tcfg)


def _build_presence(yaml_presence: dict) -> PresenceConfig:
    """Build a PresenceConfig, dropping keys not on the dataclass.

    Unknown keys inside ``presence:`` are silently ignored — same
    culture-coexistence tolerance as ``_build_telemetry``. Known keys
    with invalid values still raise via ``PresenceConfig.__post_init__``.
    """
    known = {f.name for f in PresenceConfig.__dataclass_fields__.values()}
    pcfg = {k: v for k, v in yaml_presence.items() if k in known}
    return PresenceConfig(**pcfg)
