"""OpenTelemetry integration for Culture.

Public surface re-exported here; call sites import from `culture.telemetry`.
"""

from agentirc._internal.telemetry.audit import AuditSink, build_audit_record, init_audit, utc_iso_timestamp
from agentirc._internal.telemetry.context import (
    TRACEPARENT_TAG,
    TRACESTATE_TAG,
    ExtractResult,
    context_from_traceparent,
    current_traceparent,
    extract_traceparent_from_tags,
    inject_traceparent,
)
from agentirc._internal.telemetry.metrics import MetricsRegistry, init_metrics
from agentirc._internal.telemetry.tracing import init_telemetry

__all__ = [
    "AuditSink",
    "ExtractResult",
    "MetricsRegistry",
    "TRACEPARENT_TAG",
    "TRACESTATE_TAG",
    "build_audit_record",
    "context_from_traceparent",
    "current_traceparent",
    "extract_traceparent_from_tags",
    "init_audit",
    "init_metrics",
    "init_telemetry",
    "inject_traceparent",
    "utc_iso_timestamp",
]
