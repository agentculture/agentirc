"""Project-wide constants. Keep strings here, never in source code."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

# System pseudo-user and channel
SYSTEM_USER_PREFIX = "system-"
SYSTEM_CHANNEL = "#system"
SYSTEM_USER_REALNAME = "Culture system messages"

# IRCv3 message-tag keys we emit/consume
EVENT_TAG_TYPE = "event"
EVENT_TAG_DATA = "event-data"
ERROR_TAG = "agentirc.io/error"

# Event-type name regex (dotted lowercase, ≥2 segments)
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$")


def new_msgid() -> str:
    """Return a fresh, globally-unique message id (IRCv3 ``msgid`` tag value).

    One id is generated per inbound message at the point it enters the relay
    path; the same id is stamped on every recipient's delivery and echoed into
    the MESSAGE event's ``data['msgid']`` so in-process consumers observe the
    same id the wire recipients saw.
    """
    return str(uuid.uuid4())


def server_time_now() -> str:
    """Return the current time as an IRCv3 ``time`` (server-time) tag value.

    ISO 8601 UTC, millisecond precision, trailing ``Z``
    (e.g. ``2026-07-02T12:34:56.789Z``) per the IRCv3 server-time spec.
    """
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
