"""Public protocol surface for agentirc — verbs, numerics, and tags.

Semver-tracked module. Three categories of constants live here:

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
   and agentirc-specific event tags.

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
from agentirc._internal.constants import EVENT_TAG_DATA, EVENT_TAG_TYPE
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

# Bot extension verbs.
EVENTSUB = "EVENTSUB"
EVENTUNSUB = "EVENTUNSUB"
EVENT = "EVENT"
EVENTERR = "EVENTERR"
EVENTPUB = "EVENTPUB"

# Bot-CAP token. Vendored namespace per IRCv3 conventions, prevents collision
# with hypothetical bare-`bot` caps from non-agentirc IRC servers.
BOT_CAP = "agentirc.io/bot"


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
    "EVENT_TAG_DATA",
    "EVENT_TAG_TYPE",
    "TRACEPARENT_TAG",
    "TRACESTATE_TAG",
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
]
