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

# Inbound line-length hard cap (task t4, agent-accessibility release). A
# single inbound line -- as accumulated by Client.handle()'s read loop, up
# to the next '\n' -- may not exceed this many UTF-8 bytes. Replaces the old
# silent "cap the buffer at 8192 chars, keep the trailing 4096" truncation:
# a line over this limit is now rejected outright (see
# Client._send_line_too_long_error / ERROR_TOKEN_LINE_TOO_LONG in
# agentirc/protocol.py) rather than silently altered, and no line under the
# limit is ever touched.
MAX_INBOUND_LINE = 8192

# Classic RFC 2812 wire-line budget for outbound PRIVMSG relay (task t4):
# prefix + command + target + trailing text + CRLF must together fit in
# this many bytes. IRCv3 message tags (msgid, time, ...) ride ahead of the
# line in their own budget and do NOT count against this limit.
PRIVMSG_WIRE_LIMIT = 512


def _max_chars_within_byte_budget(text: str, max_bytes: int) -> int:
    """Return the largest ``n`` such that ``text[:n]`` UTF-8-encodes to <= ``max_bytes`` bytes.

    Binary search over the character count (not the byte count) so a cut
    point always falls on a Python ``str`` character boundary. CPython
    ``str`` indexes Unicode code points -- never a UTF-16 surrogate half --
    so a boundary between characters can never land inside a multi-byte
    UTF-8 codepoint. UTF-8-encoded length is monotonically non-decreasing as
    characters are appended, so the search space is valid for bisection.
    """
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return lo


def split_message_text(text: str, max_bytes: int) -> list[str]:
    """Split ``text`` into ordered chunks whose UTF-8 encoding each fits in ``max_bytes``.

    Shared by the outbound PRIVMSG relay split (``Client._split_outbound_text``)
    and the thread-message wire split (``skills/threads.py``'s
    ``_deliver_thread_msg``) -- both need to fit a text body under the
    512-byte ``PRIVMSG_WIRE_LIMIT`` once the caller's fixed overhead (prefix,
    command, target, any thread tag prefix, CRLF) is subtracted out.

    Never splits a UTF-8 codepoint -- splits only happen at Python ``str``
    character boundaries (see ``_max_chars_within_byte_budget``). Prefers to
    split on the last space found in the final 20% of the current window so
    words aren't broken mid-word when a natural break exists; falls back to
    a hard split at the byte budget otherwise. Concatenating the returned
    chunks in order reproduces ``text`` exactly -- a split on a space keeps
    the space with the preceding chunk rather than discarding it, and no
    chunk is ever trimmed or otherwise altered.
    """
    if max_bytes < 1:
        # Degenerate guard (e.g. an enormous sender prefix eating the whole
        # 512-byte budget): floor at 1 so the loop below always makes
        # forward progress instead of spinning forever.
        max_bytes = 1
    if not text or len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            chunks.append(remaining)
            break
        cut = _max_chars_within_byte_budget(remaining, max_bytes)
        if cut <= 0:
            # A single codepoint alone exceeds max_bytes (e.g. budget < 4
            # for an astral character) -- emit it anyway rather than loop.
            cut = 1
        window_start = max(0, int(cut * 0.8))
        space_idx = remaining.rfind(" ", window_start, cut)
        split_at = space_idx + 1 if space_idx != -1 else cut
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


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
