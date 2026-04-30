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
]
