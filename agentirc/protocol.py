"""Public protocol surface for agentirc — verbs, numerics, tags, and the bot extension API.

Semver-tracked module. Six categories of public symbols live here:

1. **Verb names** — IRC command verbs as bare uppercase tokens. Mostly
   RFC 2812 (PRIVMSG, JOIN, QUIT, ...), plus agentirc skill verbs
   (ROOMCREATE, ROOMMETA, THREAD, ...) and server-to-server federation
   verbs (SJOIN, SMSG, STHREAD, ...). The string *values* are wire
   format — renaming a value is a wire-format break across the
   federation. Renaming the Python identifier is a Python API break.
2. **Numeric reply codes** — re-exported from
   :mod:`agentirc._internal.protocol.replies`. The internal module
   stays the single source of truth; this module re-exports so external
   consumers don't reach into the underscore namespace.
3. **Message tag names** — IRCv3 tag keys for traceparent/tracestate
   and agentirc-specific event tags, plus ``ERROR_TAG`` (the
   ``agentirc.io/error`` tag carrying a stable error-reason token,
   see "Stable error tokens" below) and its ``ERROR_TOKEN_*``
   vocabulary.
4. **Event types and the Event dataclass** — :class:`EventType`
   (a :class:`enum.StrEnum` of 20 dotted-lowercase wire strings) and
   :class:`Event` (a frozen-shape dataclass). Plus 20 ``EVENT_TYPE_*``
   per-type string constants for callers that prefer bare strings over
   enum-coercion at JSON boundaries. Added in 9.5.0a1 as part of the
   bot extension API.
5. **Bot extension verbs and capability** — ``EVENTSUB``, ``EVENTUNSUB``,
   ``EVENT``, ``EVENTERR``, ``EVENTPUB`` verb constants and
   ``BOT_CAP = "agentirc.io/bot"``. Reserved in 9.5.0a1; daemon
   behavior wires up in 9.5.0a3 / 9.5.0 final.
6. **Runtime verb discovery** — ``VERBS`` (the query verb) and
   ``VERBS_DISCOVERY_VERSION`` (its reply-format version), plus
   ``ERROR_TOKENS_VERSION`` alongside the ``ERROR_TOKEN_*`` vocabulary in
   "Stable error tokens" below. Added in task t9 (agent-accessibility
   release) so a client can ask a running server what it accepts instead
   of guessing from docs. See ``Client._handle_verbs`` in
   ``agentirc/client.py`` for the wire shape.

Existing call sites under ``agentirc.ircd``, ``agentirc.server_link``
and the skills modules still use inline string literals. Migrating them
to ``protocol.<NAME>`` is intentionally out of scope for the
introduction of this module — the goal is to expose a stable public
surface for downstream consumers (e.g. culture once it pins
``agentirc-cli``) without churning the internals. A future PR may
sweep the call sites if it's worth the diff.

See the "Track A: wire-format compat" block below for the four known
wire-format quirks deliberately preserved (typos, semantic misuse, verb
collapse) — fixing them in agentirc alone would silently break culture's
clients and federation. They need a coordinated cross-repo bump.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Numeric reply codes (re-exported from the internal module)
# ---------------------------------------------------------------------------
from agentirc._internal.constants import ERROR_TAG, EVENT_TAG_DATA, EVENT_TAG_TYPE
from agentirc._internal.protocol.replies import (
    ERR_ALREADYREGISTRED,
    ERR_CANNOTSENDTOCHAN,
    ERR_CHANOPRIVSNEEDED,
    ERR_ERRONEUSNICKNAME,
    ERR_NEEDMOREPARAMS,
    ERR_NICKNAMEINUSE,
    ERR_NONICKNAMEGIVEN,
    ERR_NOSUCHCHANNEL,
    ERR_NOSUCHNICK,
    ERR_NOSUCHSERVER,
    ERR_NOTONCHANNEL,
    ERR_UNKNOWNCOMMAND,
    ERR_USERNOTINCHANNEL,
    ERR_USERSDONTMATCH,
    RPL_CHANNELMODEIS,
    RPL_CREATED,
    RPL_ENDOFNAMES,
    RPL_ENDOFWHO,
    RPL_ENDOFWHOIS,
    RPL_LIST,
    RPL_LISTEND,
    RPL_LISTSTART,
    RPL_MYINFO,
    RPL_NAMREPLY,
    RPL_NOTOPIC,
    RPL_TOPIC,
    RPL_UMODEIS,
    RPL_WELCOME,
    RPL_WHOISCHANNELS,
    RPL_WHOISSERVER,
    RPL_WHOISUSER,
    RPL_WHOREPLY,
    RPL_YOURHOST,
)

# ---------------------------------------------------------------------------
# IRCv3 / extension tag names (re-exported)
# ---------------------------------------------------------------------------
from agentirc._internal.telemetry.context import TRACEPARENT_TAG, TRACESTATE_TAG

# ---------------------------------------------------------------------------
# Standard IRC verbs (RFC 2812 + common extensions)
# ---------------------------------------------------------------------------
PRIVMSG = "PRIVMSG"
NOTICE = "NOTICE"
JOIN = "JOIN"
PART = "PART"
QUIT = "QUIT"
MODE = "MODE"
TOPIC = "TOPIC"
NICK = "NICK"
USER = "USER"
PASS = "PASS"
PING = "PING"
PONG = "PONG"
CAP = "CAP"
WHO = "WHO"
WHOIS = "WHOIS"
LIST = "LIST"
NAMES = "NAMES"
INVITE = "INVITE"
KICK = "KICK"
ERROR = "ERROR"

# ---------------------------------------------------------------------------
# agentirc skill verbs (rooms / threads / tags)
# ---------------------------------------------------------------------------
ROOMCREATE = "ROOMCREATE"
ROOMCREATED = "ROOMCREATED"
ROOMMETA = "ROOMMETA"
ROOMARCHIVE = "ROOMARCHIVE"
ROOMARCHIVED = "ROOMARCHIVED"
ROOMINVITE = "ROOMINVITE"
ROOMKICK = "ROOMKICK"
ROOMTAGNOTICE = "ROOMTAGNOTICE"
THREAD = "THREAD"
THREADS = "THREADS"
THREADSEND = "THREADSEND"
THREADCLOSE = "THREADCLOSE"
TAGS = "TAGS"

# ---------------------------------------------------------------------------
# agentirc server-to-server (federation) verbs
# ---------------------------------------------------------------------------
SERVER = "SERVER"
SNICK = "SNICK"
SJOIN = "SJOIN"
SPART = "SPART"
SQUITUSER = "SQUITUSER"
SMSG = "SMSG"
SNOTICE = "SNOTICE"
STOPIC = "STOPIC"
SROOMMETA = "SROOMMETA"
SROOMARCHIVE = "SROOMARCHIVE"
STAGS = "STAGS"
STHREAD = "STHREAD"
SEVENT = "SEVENT"
BACKFILL = "BACKFILL"
BACKFILLEND = "BACKFILLEND"

# ---------------------------------------------------------------------------
# Track A: wire-format compat
# ---------------------------------------------------------------------------
# These four wire-format quirks were flagged in the PR-B1 review (PR #3
# review threads 3170062290, 3170062308, 3170062326, 3170062350) and
# are deliberately preserved in agentirc to maintain compat with
# culture's clients/harnesses/federation. Each one needs a coordinated
# culture+agentirc bump to fix; doing it agentirc-side alone would
# silently break culture downstream.
#
#   1. ROOMETAEND — typo for ROOMMETAEND. The completion marker for a
#      ROOMMETA query, paired by clients keying off the literal string.
#   2. ROOMETASET — typo for ROOMMETASET. Same reason; paired with the
#      ROOMMETA companion the same way RPL_NAMREPLY pairs with
#      RPL_ENDOFNAMES.
#   3. ERR_NOSUCHCHANNEL (403) is also issued at
#      agentirc/skills/rooms.py for the semantic case
#      "channel already exists". RFC 2812 reserves 403 for "channel
#      does not exist"; a fitting reuse or extension numeric is the
#      proper fix.
#   4. STHREAD collapses THREAD_CREATE and THREAD_MESSAGE across
#      federation links — the create-vs-reply distinction is lost. A
#      future bump should split into distinct verbs (or thread an
#      explicit subcommand flag through the payload).

ROOMETAEND = "ROOMETAEND"  # SIC: typo preserved for wire compat (ROOMMETAEND target)
ROOMETASET = "ROOMETASET"  # SIC: typo preserved for wire compat (ROOMMETASET target)


# ---------------------------------------------------------------------------
# Bot extension API (9.5.0)
# ---------------------------------------------------------------------------
# Public Event dataclass + EventType enum, per-type string constants, the
# EVENTSUB / EVENTUNSUB / EVENT / EVENTERR / EVENTPUB verb names, and the
# bot-CAP token. See docs/superpowers/specs/2026-05-01-bot-extension-api-design.md
# for the wire format and verb syntax. Behavior wiring lands in 9.5.0a2/a3;
# 9.5.0a1 ships these symbols only.

# `EventType` is `StrEnum` so `EventType.JOIN == "user.join"` is True at JSON
# boundaries. Adding a new member is a minor bump; renaming or removing one
# is a major bump.


class EventType(StrEnum):
    MESSAGE = "message"
    JOIN = "user.join"
    PART = "user.part"
    QUIT = "user.quit"
    TOPIC = "topic"
    ROOMMETA = "room.meta"
    TAGS = "tags.update"
    ROOMARCHIVE = "room.archive"
    THREAD_CREATE = "thread.create"
    THREAD_MESSAGE = "thread.message"
    THREAD_CLOSE = "thread.close"
    AGENT_CONNECT = "agent.connect"
    AGENT_DISCONNECT = "agent.disconnect"
    CONSOLE_OPEN = "console.open"
    CONSOLE_CLOSE = "console.close"
    SERVER_WAKE = "server.wake"
    SERVER_SLEEP = "server.sleep"
    SERVER_LINK = "server.link"
    SERVER_UNLINK = "server.unlink"
    ROOM_CREATE = "room.create"
    PRESENCE = "presence.update"


@dataclass
class Event:
    # `type` is widened to `EventType | str` so federation peers can deliver
    # event types this version doesn't recognise without raising. Subscribers
    # must tolerate unknown types (forward-compat).
    type: EventType | str
    channel: str | None
    nick: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# Per-type string constants — parallel to `EventType` for callers that prefer
# bare strings (e.g. comparing JSON-decoded `type` field without enum-coercing).
EVENT_TYPE_MESSAGE = "message"
EVENT_TYPE_USER_JOIN = "user.join"
EVENT_TYPE_USER_PART = "user.part"
EVENT_TYPE_USER_QUIT = "user.quit"
EVENT_TYPE_TOPIC = "topic"
EVENT_TYPE_ROOM_META = "room.meta"
EVENT_TYPE_TAGS_UPDATE = "tags.update"
EVENT_TYPE_ROOM_ARCHIVE = "room.archive"
EVENT_TYPE_THREAD_CREATE = "thread.create"
EVENT_TYPE_THREAD_MESSAGE = "thread.message"
EVENT_TYPE_THREAD_CLOSE = "thread.close"
EVENT_TYPE_AGENT_CONNECT = "agent.connect"
EVENT_TYPE_AGENT_DISCONNECT = "agent.disconnect"
EVENT_TYPE_CONSOLE_OPEN = "console.open"
EVENT_TYPE_CONSOLE_CLOSE = "console.close"
EVENT_TYPE_SERVER_WAKE = "server.wake"
EVENT_TYPE_SERVER_SLEEP = "server.sleep"
EVENT_TYPE_SERVER_LINK = "server.link"
EVENT_TYPE_SERVER_UNLINK = "server.unlink"
EVENT_TYPE_ROOM_CREATE = "room.create"
EVENT_TYPE_PRESENCE_UPDATE = "presence.update"

# Bot extension verbs.
EVENTSUB = "EVENTSUB"
EVENTUNSUB = "EVENTUNSUB"
EVENT = "EVENT"
EVENTERR = "EVENTERR"
EVENTPUB = "EVENTPUB"

# Bot-CAP token. Vendored namespace per IRCv3 conventions, prevents collision
# with hypothetical bare-`bot` caps from non-agentirc IRC servers.
BOT_CAP = "agentirc.io/bot"


# ---------------------------------------------------------------------------
# Presence extension verbs (task t1, PRESENCE feature)
# ---------------------------------------------------------------------------
# ``PRESENCE`` announces a nick's presence-state change (e.g. away/back,
# status text) to subscribers. ``PRESENCELIST`` / ``PRESENCEEND`` pair up
# the same way ``RPL_NAMREPLY``/``RPL_ENDOFNAMES`` do elsewhere in this
# module: ``PRESENCELIST`` carries one presence entry in a bulk reply,
# ``PRESENCEEND`` marks the end of that reply. See
# docs/specs/2026-07-07-agentirc-now-speaks-presence-the-ircd-parses-resid.md
# for the wire format; ``EVENT_TYPE_PRESENCE_UPDATE`` /
# ``EventType.PRESENCE`` (above) is the corresponding internal event type.
PRESENCE = "PRESENCE"
PRESENCELIST = "PRESENCELIST"
PRESENCEEND = "PRESENCEEND"


# ---------------------------------------------------------------------------
# Runtime verb discovery (task t9, agent-accessibility release)
# ---------------------------------------------------------------------------
# ``VERBS`` lets any *registered* client -- no ``BOT_CAP`` needed, discovery
# serves plain agents too -- ask the running server what it actually
# accepts. Reply is a single ``:<server> VERBS <version> :<base64-json>``
# line, mirroring the ``EVENT``/``EVENTPUB`` base64-canonical-JSON wire
# pattern. See ``Client._handle_verbs`` in ``agentirc/client.py`` for the
# payload shape and the live-enumeration mechanism (no hardcoded verb list).
VERBS = "VERBS"

# Discovery-*format* version -- the ``<version>`` positional param on the
# ``VERBS`` reply line. Independent of ``ERROR_TOKENS_VERSION`` (the
# error-token vocabulary inside the payload) and of ``server_version``
# (the running agentirc release). Bump this when the payload's key set or
# semantics change; the four-key v1 shape is
# ``{verbs, caps, error_tokens_version, server_version}``.
VERBS_DISCOVERY_VERSION = 1


# ---------------------------------------------------------------------------
# Message-delivery tags (agent-accessibility release)
# ---------------------------------------------------------------------------
# Stamped on PRIVMSG delivery for clients that negotiated ``message-tags``:
# ``msgid`` (unique per message, identical across channel fan-out — every
# recipient sees the same id) and ``time`` (IRCv3 server-time, ISO8601 UTC).
# Thread messages additionally carry ``agentirc.io/thread=<name>`` alongside
# the legacy ``[thread:<name>]`` text prefix (which stays for compatibility;
# the tag is local-delivery-only and does not ride the S2S link — quirk #9
# stays untouched). The MESSAGE event's ``data["msgid"]`` carries the same id
# to in-process consumers (history, event subscriptions).
MSGID_TAG = "msgid"
SERVER_TIME_TAG = "time"
THREAD_TAG = "agentirc.io/thread"


# ---------------------------------------------------------------------------
# Stable error tokens (rooms / threads / history skills)
# ---------------------------------------------------------------------------
# Every error reply in ``agentirc.skills.{rooms,threads,history}`` carries
# one of these tokens as the ``agentirc.io/error`` IRCv3 message tag
# (``ERROR_TAG``) whenever the receiving client negotiated ``message-tags``
# via ``CAP REQ``. The tag rides additively — reply numerics, NOTICE prose,
# and ad-hoc numeric-looking commands ("400"/"404"/"405") are byte-identical
# to before for clients that haven't negotiated ``message-tags``. Vendored
# namespace + EVENTERR-style naming: lowercase-hyphenated reason strings.
#
#   missing-params          — ERR_NEEDMOREPARAMS on any rooms/threads/history verb
#   invalid-channel-name    — ROOMCREATE target doesn't start with '#'
#   channel-already-exists  — ROOMCREATE name collision; THREADCLOSE PROMOTE
#                              breakout-channel name collision
#   no-such-channel         — ROOMMETA/ROOMINVITE/ROOMKICK/ROOMARCHIVE on an
#                              unknown channel
#   not-managed-room        — ROOMMETA/ROOMKICK/ROOMARCHIVE on a channel that
#                              wasn't created via ROOMCREATE
#   permission-denied       — any owner/operator/authorization check failure
#                              across ROOMMETA, ROOMKICK, ROOMARCHIVE, TAGS,
#                              THREADCLOSE, and THREADCLOSE PROMOTE
#   readonly-meta-key       — ROOMMETA attempt to set a read-only key
#   invalid-meta-value      — ROOMMETA value fails per-key validation (e.g.
#                              non-integer agent_limit)
#   no-such-nick            — TAGS / ROOMINVITE target nick isn't connected
#   user-not-in-channel     — ROOMKICK target isn't a member of the channel
#   unknown-subcommand      — THREAD / HISTORY unrecognised subcommand
#   not-on-channel          — THREAD CREATE/REPLY/THREADS/THREADCLOSE(/PROMOTE)
#                              issued by a non-member
#   invalid-thread-name     — THREAD CREATE name fails the format regex
#   thread-already-exists   — THREAD CREATE duplicate (channel, name) pair
#   no-such-thread          — THREAD REPLY/THREADCLOSE(/PROMOTE) unknown thread
#   thread-archived         — THREAD REPLY/THREADCLOSE(/PROMOTE) on a closed thread
#   invalid-count           — HISTORY RECENT / HISTORY SINCE non-integer or
#                              negative count/limit
#   invalid-cursor          — HISTORY SINCE cursor token fails to decode
#                              (bad base64, missing separator, non-numeric
#                              timestamp/id) — see agentirc/skills/history.py
#                              module docstring for the cursor encoding
#   line-too-long           — an inbound line exceeded MAX_INBOUND_LINE
#                              bytes (agentirc/_internal/constants.py);
#                              Client.handle()'s read loop discards that one
#                              line, resyncs at the next newline, and keeps
#                              the connection open — see
#                              Client._send_line_too_long_error
#
# ``ERROR_TOKENS_VERSION`` is this vocabulary's first agents-visible freeze
# (task t9, agent-accessibility release) — the version number a ``VERBS``
# discovery reply's ``error_tokens_version`` field echoes. Bump it whenever
# a token above is renamed or removed (additive tokens don't need a bump;
# consumers must already tolerate unrecognised ones).
ERROR_TOKENS_VERSION = 1

ERROR_TOKEN_MISSING_PARAMS = "missing-params"
ERROR_TOKEN_INVALID_CHANNEL_NAME = "invalid-channel-name"
ERROR_TOKEN_CHANNEL_ALREADY_EXISTS = "channel-already-exists"
ERROR_TOKEN_NO_SUCH_CHANNEL = "no-such-channel"
ERROR_TOKEN_NOT_MANAGED_ROOM = "not-managed-room"
ERROR_TOKEN_PERMISSION_DENIED = "permission-denied"
ERROR_TOKEN_READONLY_META_KEY = "readonly-meta-key"
ERROR_TOKEN_INVALID_META_VALUE = "invalid-meta-value"
ERROR_TOKEN_NO_SUCH_NICK = "no-such-nick"
ERROR_TOKEN_USER_NOT_IN_CHANNEL = "user-not-in-channel"
ERROR_TOKEN_UNKNOWN_SUBCOMMAND = "unknown-subcommand"
ERROR_TOKEN_NOT_ON_CHANNEL = "not-on-channel"
ERROR_TOKEN_INVALID_THREAD_NAME = "invalid-thread-name"
ERROR_TOKEN_THREAD_ALREADY_EXISTS = "thread-already-exists"
ERROR_TOKEN_NO_SUCH_THREAD = "no-such-thread"
ERROR_TOKEN_THREAD_ARCHIVED = "thread-archived"
ERROR_TOKEN_INVALID_COUNT = "invalid-count"
ERROR_TOKEN_INVALID_CURSOR = "invalid-cursor"
ERROR_TOKEN_LINE_TOO_LONG = "line-too-long"


__all__ = [
    # Numerics
    "ERR_ALREADYREGISTRED",
    "ERR_CANNOTSENDTOCHAN",
    "ERR_CHANOPRIVSNEEDED",
    "ERR_ERRONEUSNICKNAME",
    "ERR_NEEDMOREPARAMS",
    "ERR_NICKNAMEINUSE",
    "ERR_NONICKNAMEGIVEN",
    "ERR_NOSUCHCHANNEL",
    "ERR_NOSUCHNICK",
    "ERR_NOSUCHSERVER",
    "ERR_NOTONCHANNEL",
    "ERR_UNKNOWNCOMMAND",
    "ERR_USERNOTINCHANNEL",
    "ERR_USERSDONTMATCH",
    "RPL_CHANNELMODEIS",
    "RPL_CREATED",
    "RPL_ENDOFNAMES",
    "RPL_ENDOFWHO",
    "RPL_ENDOFWHOIS",
    "RPL_LIST",
    "RPL_LISTEND",
    "RPL_LISTSTART",
    "RPL_MYINFO",
    "RPL_NAMREPLY",
    "RPL_NOTOPIC",
    "RPL_TOPIC",
    "RPL_UMODEIS",
    "RPL_WELCOME",
    "RPL_WHOISCHANNELS",
    "RPL_WHOISSERVER",
    "RPL_WHOISUSER",
    "RPL_WHOREPLY",
    "RPL_YOURHOST",
    # Tags
    "ERROR_TAG",
    "EVENT_TAG_DATA",
    "EVENT_TAG_TYPE",
    "MSGID_TAG",
    "SERVER_TIME_TAG",
    "THREAD_TAG",
    "TRACEPARENT_TAG",
    "TRACESTATE_TAG",
    # Stable error tokens
    "ERROR_TOKENS_VERSION",
    "ERROR_TOKEN_CHANNEL_ALREADY_EXISTS",
    "ERROR_TOKEN_INVALID_CHANNEL_NAME",
    "ERROR_TOKEN_INVALID_COUNT",
    "ERROR_TOKEN_INVALID_CURSOR",
    "ERROR_TOKEN_INVALID_META_VALUE",
    "ERROR_TOKEN_INVALID_THREAD_NAME",
    "ERROR_TOKEN_LINE_TOO_LONG",
    "ERROR_TOKEN_MISSING_PARAMS",
    "ERROR_TOKEN_NOT_MANAGED_ROOM",
    "ERROR_TOKEN_NOT_ON_CHANNEL",
    "ERROR_TOKEN_NO_SUCH_CHANNEL",
    "ERROR_TOKEN_NO_SUCH_NICK",
    "ERROR_TOKEN_NO_SUCH_THREAD",
    "ERROR_TOKEN_PERMISSION_DENIED",
    "ERROR_TOKEN_READONLY_META_KEY",
    "ERROR_TOKEN_THREAD_ALREADY_EXISTS",
    "ERROR_TOKEN_THREAD_ARCHIVED",
    "ERROR_TOKEN_UNKNOWN_SUBCOMMAND",
    "ERROR_TOKEN_USER_NOT_IN_CHANNEL",
    # Standard verbs
    "CAP",
    "ERROR",
    "INVITE",
    "JOIN",
    "KICK",
    "LIST",
    "MODE",
    "NAMES",
    "NICK",
    "NOTICE",
    "PART",
    "PASS",
    "PING",
    "PONG",
    "PRIVMSG",
    "QUIT",
    "TOPIC",
    "USER",
    "WHO",
    "WHOIS",
    # Skill verbs
    "ROOMARCHIVE",
    "ROOMARCHIVED",
    "ROOMCREATE",
    "ROOMCREATED",
    "ROOMETAEND",
    "ROOMETASET",
    "ROOMINVITE",
    "ROOMKICK",
    "ROOMMETA",
    "ROOMTAGNOTICE",
    "TAGS",
    "THREAD",
    "THREADCLOSE",
    "THREADS",
    "THREADSEND",
    # S2S verbs
    "BACKFILL",
    "BACKFILLEND",
    "SERVER",
    "SEVENT",
    "SJOIN",
    "SMSG",
    "SNICK",
    "SNOTICE",
    "SPART",
    "SQUITUSER",
    "SROOMARCHIVE",
    "SROOMMETA",
    "STAGS",
    "STHREAD",
    "STOPIC",
    # Bot extension API (9.5.0)
    "BOT_CAP",
    "EVENT",
    "EVENTERR",
    "EVENTPUB",
    "EVENTSUB",
    "EVENTUNSUB",
    "Event",
    "EventType",
    "EVENT_TYPE_AGENT_CONNECT",
    "EVENT_TYPE_AGENT_DISCONNECT",
    "EVENT_TYPE_CONSOLE_CLOSE",
    "EVENT_TYPE_CONSOLE_OPEN",
    "EVENT_TYPE_MESSAGE",
    "EVENT_TYPE_PRESENCE_UPDATE",
    "EVENT_TYPE_ROOM_ARCHIVE",
    "EVENT_TYPE_ROOM_CREATE",
    "EVENT_TYPE_ROOM_META",
    "EVENT_TYPE_SERVER_LINK",
    "EVENT_TYPE_SERVER_SLEEP",
    "EVENT_TYPE_SERVER_UNLINK",
    "EVENT_TYPE_SERVER_WAKE",
    "EVENT_TYPE_TAGS_UPDATE",
    "EVENT_TYPE_THREAD_CLOSE",
    "EVENT_TYPE_THREAD_CREATE",
    "EVENT_TYPE_THREAD_MESSAGE",
    "EVENT_TYPE_TOPIC",
    "EVENT_TYPE_USER_JOIN",
    "EVENT_TYPE_USER_PART",
    "EVENT_TYPE_USER_QUIT",
    # Presence extension verbs (task t1)
    "PRESENCE",
    "PRESENCEEND",
    "PRESENCELIST",
    # Runtime verb discovery (task t9)
    "VERBS",
    "VERBS_DISCOVERY_VERSION",
]
