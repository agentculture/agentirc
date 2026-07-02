# server/skills/history.py
"""HISTORY skill: RECENT / SEARCH / SINCE replay of channel message history.

HISTORY SINCE cursor encoding
------------------------------
``HISTORY SINCE <channel> <cursor> [limit]`` paginates a channel's history
forward from an opaque cursor. Internally a cursor encodes a composite key
``(timestamp, id)`` — the same ``(timestamp, id)`` ordering the SQLite store's
``idx_history_channel_ts`` index is built for — so pagination stays
deterministic even when two entries share an identical ``timestamp`` (the
``id`` component, a strictly monotonic tie-break counter, decides order).

Wire encoding: ``base64(urlsafe, no padding stripped) of "<repr(timestamp)>:<id>"``,
e.g. cursor for ``(1000.5, 42)`` is ``base64("1000.5:42")``. Callers must treat
the token as opaque — decode/encode round-trips through
``_decode_cursor``/``_encode_cursor`` in this module only. Two sentinel forms
decode to "from the beginning": the literal string ``*`` and the empty string
(reachable over the wire via a trailing ``HISTORY SINCE <channel> :`` — a bare
empty middle parameter can't be represented in IRC's space-delimited grammar).
A cursor that fails to decode (bad base64, missing ``:`` separator, or a
non-numeric timestamp/id) is rejected with the ``invalid-cursor`` stable error
token (``agentirc.protocol.ERROR_TOKEN_INVALID_CURSOR``) rather than crashing
the connection.

Reply shape: HISTORY SINCE reuses the existing ``HISTORY <channel> <nick>
<timestamp> :<text>`` replay-line format (see ``_handle_recent``/RECENT), but
— unlike RECENT/SEARCH's ``HISTORYEND <channel> :End of history`` — the
terminator carries the *next* cursor as a trailing parameter:
``HISTORYEND <channel> <next-cursor>``. A client pages by looping SINCE calls,
feeding each response's next-cursor back in, until a page comes back empty;
because the range query is a strict "after cursor" comparison, the swept
pages are guaranteed non-overlapping and cover every stored message exactly
once, in order — including across a retention prune that runs between pages
(pruned rows simply drop out of the range scan; surviving rows keep their
original ids, so the cursor stays valid and pagination doesn't skip or repeat
anything). RECENT and SEARCH replies are untouched by any of this — they keep
their pre-t6 byte-for-byte reply shape (see
``tests/test_wire_format_envelope.py``).

Authoritative backend for SINCE
--------------------------------
RECENT and SEARCH have always read from the in-memory deque only (even when a
SQLite store is configured — the store is purely a startup-restore /
durability mechanism for them). SINCE instead prefers the SQLite store when
one is configured (``self._store is not None``): the store retains every
entry back to the retention-prune boundary regardless of the deque's
``maxlen`` eviction, and its ``AUTOINCREMENT`` row id is reused directly as
each ``HistoryEntry.id`` so live-appended and store-restored entries share one
id space. When no store is configured (no ``data_dir``, memory-only server),
SINCE falls back to scanning the in-memory deque, comparing entries by the
same ``(timestamp, id)`` composite — ``id`` in that path is a process-local
monotonic counter assigned at append time. Either way the cursor's wire
encoding and comparison semantics are identical; only where the range query
runs differs.

msgid / message-tags on SINCE replay
-------------------------------------
``HistoryEntry`` carries an optional ``msgid`` (the id stamped on the
originating MESSAGE event's ``data["msgid"]``, when present — lifecycle
entries and messages recorded before this field existed have ``msgid=None``).
On a SINCE replay line, if the entry has a stored ``msgid`` *and* the
requesting client negotiated ``message-tags``, the line carries
``msgid=<stored-msgid>`` and ``time=<IRCv3 server-time>`` tags (via
``agentirc.protocol.MSGID_TAG``/``SERVER_TIME_TAG``). Entries without a stored
msgid replay untagged even for message-tags clients, and clients that never
negotiated ``message-tags`` always get untagged lines. RECENT/SEARCH replay
lines are never tagged, for anyone.

DM history (task t7)
---------------------
DMs share this exact same store — same deque, same SQLite table, same
retention/prune rules — under a synthetic key that can never collide with a
real channel name: ``@dm:<nickA>:<nickB>``, the two nicks lowercased and
sorted (see :func:`_dm_pair_key`). Channel keys are always ``#``-prefixed and
DM keys are always ``@``-prefixed, so an exact-match lookup against either
namespace can never return an entry from the other — no query-time filtering
needed.

Capture: DMs are stored via :meth:`HistorySkill.record_dm`, called directly
by ``agentirc/client.py``'s ``_send_to_client`` at the DM relay site — NOT
via the ``on_event`` broadcast every registered skill receives (channel-less
``MESSAGE`` events still fall through ``on_event`` untouched, same as before
this task; see ``on_event``'s docstring comment). This keeps DM content off
the generic per-skill event-hook path and off the event bus entirely: the
``MESSAGE`` event ``_send_to_client`` emits is byte-for-byte unchanged, so
whatever could see DM contents via EVENTSUB before this task (if anything)
sees exactly the same thing after — nothing new is emitted and nothing
existing is widened.

Query surface: ``HISTORY RECENT/SEARCH/SINCE`` accept a non-``#`` target
meaning "my DMs with that nick" — the server canonicalizes
``{requesting-client's own nick, target}`` into the pair key via
:func:`_dm_pair_key`, so a requester can only ever address a pair they
themselves belong to (see :meth:`HistorySkill._resolve_history_target`).
Wire replies always echo back the literal target string the client sent
(never the internal ``@dm:`` key). Directly naming an ``@``-prefixed target
is rejected with the existing ``no-such-channel`` token — the internal key
format is never addressable over the wire.
"""

from __future__ import annotations

import base64
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentirc.events import NO_SURFACE_EVENT_TYPES, render_event
from agentirc.protocol import (
    ERROR_TAG,
    ERROR_TOKEN_INVALID_COUNT,
    ERROR_TOKEN_INVALID_CURSOR,
    ERROR_TOKEN_MISSING_PARAMS,
    ERROR_TOKEN_NO_SUCH_CHANNEL,
    ERROR_TOKEN_UNKNOWN_SUBCOMMAND,
    MSGID_TAG,
    SERVER_TIME_TAG,
)
from agentirc.skill import Event, EventType, Skill
from agentirc._internal.constants import SYSTEM_CHANNEL, SYSTEM_USER_PREFIX
from agentirc._internal.protocol import replies
from agentirc._internal.protocol.message import Message

if TYPE_CHECKING:
    from agentirc.client import Client

logger = logging.getLogger(__name__)

# Default page size for HISTORY SINCE when the caller omits [limit].
DEFAULT_SINCE_LIMIT = 100

# Cursor tokens that mean "from the beginning of history". "" is reachable
# over the wire only via a trailing empty parameter (`HISTORY SINCE #x :`).
_CURSOR_BEGIN_TOKENS = frozenset({"", "*"})


def _encode_cursor(timestamp: float, entry_id: int) -> str:
    """Encode a (timestamp, id) composite into an opaque SINCE cursor token."""
    raw = f"{timestamp!r}:{entry_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[float, int] | None:
    """Decode a SINCE cursor token into a (timestamp, id) tuple.

    Returns ``None`` for the begin-of-history sentinel (see
    ``_CURSOR_BEGIN_TOKENS``). Raises ``ValueError`` (covers ``binascii.Error``
    and ``UnicodeDecodeError``, both ``ValueError`` subclasses) if the token
    is malformed — callers should catch ``ValueError`` and surface
    ``ERROR_TOKEN_INVALID_CURSOR`` rather than let it propagate.
    """
    if cursor in _CURSOR_BEGIN_TOKENS:
        return None
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
    ts_str, sep, id_str = raw.rpartition(":")
    if not sep or not ts_str:
        raise ValueError(f"malformed HISTORY SINCE cursor: {cursor!r}")
    return float(ts_str), int(id_str)


def _dm_pair_key(nick_a: str, nick_b: str) -> str:
    """Canonicalize two nicks into the internal DM-history storage key.

    Lowercased and sorted so the same pair of participants always maps to
    the same key regardless of who's "self" and who's "target", or nick
    casing. See the module docstring's "DM history (task t7)" section for
    why the ``@dm:`` prefix can never collide with a channel key.
    """
    a, b = sorted((nick_a.lower(), nick_b.lower()))
    return f"@dm:{a}:{b}"


def _format_server_time(timestamp: float) -> str:
    """Render a unix-epoch-seconds *timestamp* as IRCv3 server-time.

    ``YYYY-MM-DDTHH:MM:SS.sssZ`` — RFC3339 UTC with millisecond precision, per
    the ``server-time`` IRCv3 spec that ``agentirc.protocol.SERVER_TIME_TAG``
    names.
    """
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class HistoryEntry:
    nick: str
    text: str
    timestamp: float
    id: int = -1
    msgid: str | None = None


class HistorySkill(Skill):
    name = "history"
    commands = {"HISTORY"}

    _NO_STORE_TYPES = NO_SURFACE_EVENT_TYPES

    def __init__(self, maxlen: int = 10000, retention_days: int = 30):
        self.maxlen = maxlen
        self.retention_days = retention_days
        self._channels: dict[str, deque[HistoryEntry]] = {}
        self._store = None
        # Process-local monotonic id source for HISTORY SINCE cursors when no
        # SQLite store is configured (see module docstring — "Authoritative
        # backend for SINCE"). Unused once a store is present; the store's
        # AUTOINCREMENT row id is authoritative then.
        self._next_local_id = 0

    async def start(self, server) -> None:
        await super().start(server)
        self._restore_history()

    async def stop(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def _restore_history(self) -> None:
        """Reload persisted history from SQLite on startup."""
        if not self.server.config.data_dir:
            return
        from agentirc.history_store import HistoryStore

        try:
            store = HistoryStore(self.server.config.data_dir)
            store.prune(self.retention_days)
            channel_data = store.load_channels(self.maxlen)
        except Exception:
            logger.warning(
                "Failed to open history database — falling back to in-memory",
                exc_info=True,
            )
            return

        self._store = store
        for channel, entries in channel_data.items():
            buf = deque(maxlen=self.maxlen)
            for e in entries:
                buf.append(
                    HistoryEntry(
                        nick=e["nick"],
                        text=e["text"],
                        timestamp=e["timestamp"],
                        id=e["id"],
                        msgid=e.get("msgid"),
                    )
                )
            self._channels[channel] = buf
        total = sum(len(d) for d in self._channels.values())
        if total:
            logger.info(
                "Restored %d history entries across %d channels",
                total,
                len(self._channels),
            )

    def _record(
        self, channel: str, nick: str, text: str, timestamp: float, msgid: str | None
    ) -> int:
        """Assign the monotonic id for a new entry, persisting it if a store is configured.

        Returns the id to stamp on the in-memory ``HistoryEntry``. See the
        module docstring's "Authoritative backend for SINCE" section: with a
        store configured, its ``AUTOINCREMENT`` row id is reused directly so
        live-appended and store-restored entries share one id space; without
        one, a process-local counter stands in.
        """
        if self._store is not None:
            return self._store.append(channel, nick, text, timestamp, msgid)
        entry_id = self._next_local_id
        self._next_local_id += 1
        return entry_id

    def _append_entry(
        self, key: str, nick: str, text: str, timestamp: float, msgid: str | None
    ) -> None:
        """Persist and buffer one entry under *key* (a channel or DM pair key)."""
        entry_id = self._record(key, nick, text, timestamp, msgid)
        buf = self._channels.setdefault(key, deque(maxlen=self.maxlen))
        buf.append(
            HistoryEntry(nick=nick, text=text, timestamp=timestamp, id=entry_id, msgid=msgid)
        )

    def record_dm(
        self, nick: str, target: str, text: str, timestamp: float, msgid: str | None
    ) -> None:
        """Store one DM under the canonical pair key for *nick* and *target*.

        Called directly by ``agentirc/client.py``'s ``_send_to_client`` at
        the DM relay site — deliberately NOT via the ``on_event`` broadcast
        every registered skill receives. Two reasons:

        1. Privacy: this keeps DM content off the generic event-hook path
           entirely, so it can never leak to a skill (or a test's own
           independently-registered ``HistorySkill`` instance — see
           ``tests/test_history.py::test_history_does_not_record_dms``,
           which asserts a *second* instance sees no DM data) that isn't
           the one instance actually serving live ``HISTORY`` queries
           (resolved by ``IRCd.get_skill_for_command("HISTORY")``, the same
           lookup command dispatch uses).
        2. It does not touch, create, or widen anything on the event bus —
           the ``MESSAGE`` event ``_send_to_client`` already emits is
           unchanged by this call and continues to govern (pre-existing,
           unmodified) EVENTSUB visibility.

        Uses the same ``_append_entry``/``_record`` pipeline as channel
        messages, so retention/prune apply identically (see the module
        docstring's "DM history (task t7)" section).
        """
        pair_key = _dm_pair_key(nick, target)
        self._append_entry(pair_key, nick, text, timestamp, msgid)

    async def on_event(self, event: Event) -> None:
        if event.type == EventType.MESSAGE and event.channel is not None:
            text = event.data["text"]
            # Defensive .get(): the parallel msgid-passthrough task (t3) may
            # not have landed in every tree yet — see module docstring.
            msgid = event.data.get("msgid")
            self._append_entry(event.channel, event.nick, text, event.timestamp, msgid)
            return

        # Skip event types that are delivered via their own IRC verbs
        # (THREAD_*, TOPIC) — they have dedicated storage. MESSAGE was
        # already handled above. Channel-less MESSAGE events (DMs) fall
        # through to this check too and are dropped here, same as before
        # this task — see ``record_dm`` above for where DM storage actually
        # happens instead.
        type_wire = event.type.value if hasattr(event.type, "value") else str(event.type)
        if type_wire in self._NO_STORE_TYPES:
            return

        # Store lifecycle events (agent.connect, server.wake, etc.)
        target = event.channel or SYSTEM_CHANNEL
        origin = event.data.get("_origin") or self.server.config.name
        nick = f"{SYSTEM_USER_PREFIX}{origin}"
        payload = {k: v for k, v in event.data.items() if not k.startswith("_")}
        if event.nick:
            payload.setdefault("nick", event.nick)
        if event.channel:
            payload.setdefault("channel", event.channel)
        body = event.data.get("_render") or render_event(type_wire, payload, event.channel)

        entry_id = self._record(target, nick, body, event.timestamp, None)
        buf = self._channels.setdefault(target, deque(maxlen=self.maxlen))
        buf.append(HistoryEntry(nick=nick, text=body, timestamp=event.timestamp, id=entry_id))

    def get_recent(self, channel: str, count: int) -> list[HistoryEntry]:
        if count <= 0:
            return []
        buf = self._channels.get(channel)
        if not buf:
            return []
        entries = list(buf)
        return entries[-count:]

    def search(self, channel: str, term: str) -> list[HistoryEntry]:
        buf = self._channels.get(channel)
        if not buf:
            return []
        term_lower = term.lower()
        return [e for e in buf if term_lower in e.text.lower()]

    def get_since(self, channel: str, after: tuple[float, int] | None, limit: int) -> list[HistoryEntry]:
        """Return up to *limit* entries strictly after cursor tuple *after*.

        See the module docstring's "Authoritative backend for SINCE" section:
        prefers the SQLite store when configured, else scans the in-memory
        deque. Both paths return entries ordered ascending by
        ``(timestamp, id)``.
        """
        if limit <= 0:
            return []
        if self._store is not None:
            rows = self._store.get_since(channel, after, limit)
            return [
                HistoryEntry(
                    nick=r["nick"],
                    text=r["text"],
                    timestamp=r["timestamp"],
                    id=r["id"],
                    msgid=r.get("msgid"),
                )
                for r in rows
            ]

        buf = self._channels.get(channel)
        if not buf:
            return []
        if after is None:
            candidates = list(buf)
        else:
            candidates = [e for e in buf if (e.timestamp, e.id) > after]
        candidates.sort(key=lambda e: (e.timestamp, e.id))
        return candidates[:limit]

    def _resolve_history_target(self, client: Client, requested: str) -> str | None:
        """Resolve a HISTORY <target> param to an internal store key.

        ``#``-prefixed -> a channel key, used verbatim (unchanged behavior).
        ``@``-prefixed -> rejected (``None``); ``@dm:...`` is the internal DM
        pair-key format and is never directly addressable over the wire —
        the caller must send the ``no-such-channel`` error and stop.
        Anything else -> treated as a nick; canonicalized with the
        *requesting client's own* nick into the DM pair key (see
        ``_dm_pair_key``), so a requester can only ever address a pair they
        themselves belong to.
        """
        if requested.startswith("@"):
            return None
        if requested.startswith("#"):
            return requested
        return _dm_pair_key(client.nick, requested)

    async def _reject_unaddressable_target(self, client: Client, requested: str) -> None:
        """Send the ``no-such-channel`` error for a directly-named ``@dm:`` target."""
        await client.send_numeric(
            replies.ERR_NOSUCHCHANNEL,
            requested,
            replies.MSG_NOSUCHCHANNEL,
            tags={ERROR_TAG: ERROR_TOKEN_NO_SUCH_CHANNEL},
        )

    async def on_command(self, client: Client, msg: Message) -> None:
        if len(msg.params) < 1:
            await client.send_numeric(
                replies.ERR_NEEDMOREPARAMS,
                "HISTORY",
                replies.MSG_NEEDMOREPARAMS,
                tags={ERROR_TAG: ERROR_TOKEN_MISSING_PARAMS},
            )
            return

        subcmd = msg.params[0].upper()
        if subcmd == "RECENT":
            await self._handle_recent(client, msg)
        elif subcmd == "SEARCH":
            await self._handle_search(client, msg)
        elif subcmd == "SINCE":
            await self._handle_since(client, msg)
        else:
            await client.send_tagged(
                Message(
                    prefix=self.server.config.name,
                    command="NOTICE",
                    params=[client.nick, f"Unknown HISTORY subcommand: {subcmd}"],
                    tags={ERROR_TAG: ERROR_TOKEN_UNKNOWN_SUBCOMMAND},
                )
            )

    async def _handle_recent(self, client: Client, msg: Message) -> None:
        if len(msg.params) < 3:
            await client.send_numeric(
                replies.ERR_NEEDMOREPARAMS,
                "HISTORY",
                replies.MSG_NEEDMOREPARAMS,
                tags={ERROR_TAG: ERROR_TOKEN_MISSING_PARAMS},
            )
            return

        channel = msg.params[1]
        store_key = self._resolve_history_target(client, channel)
        if store_key is None:
            await self._reject_unaddressable_target(client, channel)
            return

        try:
            count = int(msg.params[2])
        except ValueError:
            await client.send_tagged(
                Message(
                    prefix=self.server.config.name,
                    command="NOTICE",
                    params=[client.nick, "Invalid count"],
                    tags={ERROR_TAG: ERROR_TOKEN_INVALID_COUNT},
                )
            )
            return

        if count < 0:
            await client.send_tagged(
                Message(
                    prefix=self.server.config.name,
                    command="NOTICE",
                    params=[client.nick, "Invalid count"],
                    tags={ERROR_TAG: ERROR_TOKEN_INVALID_COUNT},
                )
            )
            return

        entries = self.get_recent(store_key, count)
        for entry in entries:
            await client.send(
                Message(
                    prefix=self.server.config.name,
                    command="HISTORY",
                    params=[channel, entry.nick, str(entry.timestamp), entry.text],
                )
            )
        await client.send(
            Message(
                prefix=self.server.config.name,
                command="HISTORYEND",
                params=[channel, "End of history"],
            )
        )

    async def _handle_search(self, client: Client, msg: Message) -> None:
        if len(msg.params) < 3:
            await client.send_numeric(
                replies.ERR_NEEDMOREPARAMS,
                "HISTORY",
                replies.MSG_NEEDMOREPARAMS,
                tags={ERROR_TAG: ERROR_TOKEN_MISSING_PARAMS},
            )
            return

        channel = msg.params[1]
        store_key = self._resolve_history_target(client, channel)
        if store_key is None:
            await self._reject_unaddressable_target(client, channel)
            return

        term = msg.params[2]
        entries = self.search(store_key, term)
        for entry in entries:
            await client.send(
                Message(
                    prefix=self.server.config.name,
                    command="HISTORY",
                    params=[channel, entry.nick, str(entry.timestamp), entry.text],
                )
            )
        await client.send(
            Message(
                prefix=self.server.config.name,
                command="HISTORYEND",
                params=[channel, "End of history"],
            )
        )

    async def _handle_since(self, client: Client, msg: Message) -> None:
        """HISTORY SINCE <channel> <cursor> [limit] — see module docstring."""
        if len(msg.params) < 3:
            await client.send_numeric(
                replies.ERR_NEEDMOREPARAMS,
                "HISTORY",
                replies.MSG_NEEDMOREPARAMS,
                tags={ERROR_TAG: ERROR_TOKEN_MISSING_PARAMS},
            )
            return

        channel = msg.params[1]
        store_key = self._resolve_history_target(client, channel)
        if store_key is None:
            await self._reject_unaddressable_target(client, channel)
            return

        cursor_token = msg.params[2]

        try:
            after = _decode_cursor(cursor_token)
        except ValueError:
            await client.send_tagged(
                Message(
                    prefix=self.server.config.name,
                    command="NOTICE",
                    params=[client.nick, "Invalid cursor"],
                    tags={ERROR_TAG: ERROR_TOKEN_INVALID_CURSOR},
                )
            )
            return

        limit = DEFAULT_SINCE_LIMIT
        if len(msg.params) >= 4:
            try:
                limit = int(msg.params[3])
            except ValueError:
                await client.send_tagged(
                    Message(
                        prefix=self.server.config.name,
                        command="NOTICE",
                        params=[client.nick, "Invalid count"],
                        tags={ERROR_TAG: ERROR_TOKEN_INVALID_COUNT},
                    )
                )
                return

            if limit < 0:
                await client.send_tagged(
                    Message(
                        prefix=self.server.config.name,
                        command="NOTICE",
                        params=[client.nick, "Invalid count"],
                        tags={ERROR_TAG: ERROR_TOKEN_INVALID_COUNT},
                    )
                )
                return

        entries = self.get_since(store_key, after, limit)
        for entry in entries:
            tags: dict[str, str] = {}
            if entry.msgid is not None:
                tags = {
                    MSGID_TAG: entry.msgid,
                    SERVER_TIME_TAG: _format_server_time(entry.timestamp),
                }
            await client.send_tagged(
                Message(
                    prefix=self.server.config.name,
                    command="HISTORY",
                    params=[channel, entry.nick, str(entry.timestamp), entry.text],
                    tags=tags,
                )
            )

        if entries:
            last = entries[-1]
            next_cursor = _encode_cursor(last.timestamp, last.id)
        elif after is None:
            # Empty channel / no history yet — canonicalize to the begin
            # sentinel rather than echoing back whatever spelling ("" vs "*")
            # the caller used for "from the beginning".
            next_cursor = "*"
        else:
            # Caught up: nothing past this cursor (yet) — echo it back
            # unchanged so a client polling in a loop can resume from the
            # same point once more messages arrive.
            next_cursor = cursor_token

        await client.send(
            Message(
                prefix=self.server.config.name,
                command="HISTORYEND",
                params=[channel, next_cursor],
            )
        )
