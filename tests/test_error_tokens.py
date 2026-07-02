"""Stable error tokens (agent-accessibility, task t2).

Every error reply in ``agentirc.skills.{rooms,threads,history}`` now carries
a stable named reason token via the ``agentirc.io/error`` IRCv3 message tag
(``agentirc.protocol.ERROR_TAG``) whenever the receiving client negotiated
``message-tags``. The token rides additively: reply numerics, NOTICE prose,
and the ad-hoc "400"/"404"/"405" numeric-looking commands in threads.py are
byte-identical to before for clients that haven't negotiated
``message-tags`` — see ``tests/test_wire_format_envelope.py`` for the golden
baseline this file must not disturb.

This module enumerates every error-reply call site in the three skills (walked
by hand against the source — see the per-case comments below) and, for each
one, drives two independently-registered clients against the same server: one
that negotiated ``message-tags`` (``CAP REQ :message-tags``) and one that
didn't. For every case we assert:

  (a) the message-tags client's reply carries ``agentirc.io/error=<token>``
      for the expected token, and
  (b) the non-negotiated client's reply is byte-identical to what it would
      have been before this change — checked structurally (tags stripped,
      client-own-nick substrings normalised away since the two probing
      clients necessarily have different nicks) for all cases, and via an
      exact literal string for the subset of cases that overlap with the
      golden/characterization tests already locked into
      ``tests/test_wire_format_envelope.py``.

One deliberately-skipped case: ``RoomsSkill._handle_tags``'s "no such nick"
branch on the SET path (rooms.py) is unreachable through the wire protocol —
the preceding ``nick != client.nick`` check only lets a client's *own*
(always-registered) nick through, so ``self.server.clients.get(nick)`` can
never miss. The token is still attached in source for defensive consistency;
see the module docstring in ``agentirc/skills/rooms.py`` history for detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

import pytest

from agentirc._internal.protocol.message import Message
from agentirc.protocol import (
    ERROR_TAG,
    ERROR_TOKEN_CHANNEL_ALREADY_EXISTS,
    ERROR_TOKEN_INVALID_CHANNEL_NAME,
    ERROR_TOKEN_INVALID_COUNT,
    ERROR_TOKEN_INVALID_CURSOR,
    ERROR_TOKEN_INVALID_META_VALUE,
    ERROR_TOKEN_INVALID_THREAD_NAME,
    ERROR_TOKEN_MISSING_PARAMS,
    ERROR_TOKEN_NOT_MANAGED_ROOM,
    ERROR_TOKEN_NOT_ON_CHANNEL,
    ERROR_TOKEN_NO_SUCH_CHANNEL,
    ERROR_TOKEN_NO_SUCH_NICK,
    ERROR_TOKEN_NO_SUCH_THREAD,
    ERROR_TOKEN_PERMISSION_DENIED,
    ERROR_TOKEN_READONLY_META_KEY,
    ERROR_TOKEN_THREAD_ALREADY_EXISTS,
    ERROR_TOKEN_THREAD_ARCHIVED,
    ERROR_TOKEN_UNKNOWN_SUBCOMMAND,
    ERROR_TOKEN_USER_NOT_IN_CHANNEL,
)

# ---------------------------------------------------------------------------
# Small IRC-test-client helpers
# ---------------------------------------------------------------------------


async def _make_client(make_client, nick: str, *, tags: bool = False):
    """Register a test client and (optionally) negotiate message-tags.

    ``IRCTestClient`` (tests/conftest.py) doesn't track its own nick, but
    every probe below needs it (to build collision-free channel/thread names
    and to normalise the client's own nick out of reply params before
    comparing two differently-nicked clients' replies) — so we stash it.
    """
    client = await make_client(nick, "u")
    client.nick = nick
    if tags:
        await client.send("CAP REQ :message-tags")
        await client.recv_until("CAP")
    return client


async def _join(client, channel: str) -> None:
    """JOIN a channel and drain the (JOIN echo + NAMES) response."""
    await client.send(f"JOIN {channel}")
    await client.recv_until("366")


async def _roomcreate(client, channel: str, purpose: str = "x") -> None:
    """ROOMCREATE a managed room and drain the (JOIN + NAMES + ROOMCREATED) response."""
    await client.send(f"ROOMCREATE {channel} :purpose={purpose}")
    await client.recv_until("ROOMCREATED")


async def _thread_create(client, channel: str, name: str, text: str = "hi") -> None:
    """THREAD CREATE. No reply is ever sent to the creator on success."""
    await client.send(f"THREAD CREATE {channel} {name} :{text}")


async def _thread_close(client, channel: str, name: str, summary: str = "done") -> None:
    """THREADCLOSE. Exactly one NOTICE is delivered back to the closer (a member)."""
    await client.send(f"THREADCLOSE {channel} {name} :{summary}")
    await client.recv()


async def _send_and_recv(client, line: str) -> str:
    await client.send(line)
    return await client.recv()


def _normalize(msg: Message, own_nick: str) -> tuple:
    """Structural signature of *msg* ignoring tags and the client's own nick.

    The tagged and untagged probes in a case run as two independently
    registered clients with necessarily-different nicks (and, for
    self-contained cases, nick-derived channel/thread names) — replacing
    each client's own nick substring with a placeholder makes the two
    otherwise-identical replies comparable byte-for-byte.
    """
    return (
        msg.prefix,
        msg.command,
        tuple(p.replace(own_nick, "<NICK>") for p in msg.params),
    )


# ---------------------------------------------------------------------------
# Case table
# ---------------------------------------------------------------------------

Probe = Callable[[object, dict], Awaitable[str]]
Setup = Callable[[object, object, str], Awaitable[dict]]


@dataclass
class ErrorCase:
    id: str
    token: str
    probe: Probe
    setup: Setup | None = None
    # Exact untagged wire line, with the probing client's own nick spliced
    # in — populated only for cases that overlap a golden/characterization
    # test in tests/test_wire_format_envelope.py, to cross-check literally
    # against the locked baseline in addition to the generic structural
    # parity check every case gets.
    expected_untagged: Callable[[str], str] | None = None


def _bare(cmd: str) -> Probe:
    """Probe: send *cmd* verbatim, no setup, exactly one reply line."""

    async def probe(client, _ctx):
        return await _send_and_recv(client, cmd)

    return probe


def _after_join(cmd_fmt: str) -> Probe:
    """Probe: JOIN a client-nick-unique channel, then send *cmd_fmt* (uses {chan})."""

    async def probe(client, _ctx):
        chan = f"#noc-{client.nick}"
        return await _send_and_recv(client, cmd_fmt.format(chan=chan))

    return probe


def _not_managed(cmd_fmt: str) -> Probe:
    """Probe: JOIN (plain, unmanaged) a client-nick-unique channel, then send *cmd_fmt*."""

    async def probe(client, _ctx):
        chan = f"#plain-{client.nick}"
        await _join(client, chan)
        return await _send_and_recv(client, cmd_fmt.format(chan=chan))

    return probe


# --- shared-rig setups (permission-denied cases: actor must NOT be owner/creator) ---


async def _setup_owned_room(_server, make_client, case_id: str) -> dict:
    owner = await _make_client(make_client, f"testserv-o{case_id}")
    channel = f"#rig-{case_id}"
    await _roomcreate(owner, channel)
    return {"channel": channel}


async def _setup_owned_thread(_server, make_client, case_id: str) -> dict:
    owner = await _make_client(make_client, f"testserv-o{case_id}")
    channel = f"#rig-{case_id}"
    await _join(owner, channel)
    await _thread_create(owner, channel, "t1")
    return {"channel": channel, "thread": "t1"}


# --- individual probes needing more than one line of setup ---


async def _probe_roomcreate_exists(client, _ctx):
    chan = f"#dup-{client.nick}"
    await _roomcreate(client, chan)
    return await _send_and_recv(client, f"ROOMCREATE {chan} :purpose=y")


async def _probe_promote_breakout_conflict(client, _ctx):
    src = f"#src-{client.nick}"
    await _join(client, src)
    await _thread_create(client, src, "t1")
    breakout = f"{src}-t1"
    await _join(client, breakout)  # pre-create the colliding channel, unmanaged
    return await _send_and_recv(client, f"THREADCLOSE PROMOTE {src} t1")


async def _probe_roomkick_target_missing(client, _ctx):
    chan = f"#kick-{client.nick}"
    await _roomcreate(client, chan)
    return await _send_and_recv(client, f"ROOMKICK {chan} testserv-ghost")


async def _probe_readonly_meta_key(client, _ctx):
    chan = f"#meta-{client.nick}"
    await _roomcreate(client, chan)
    return await _send_and_recv(client, f"ROOMMETA {chan} room_id newval")


async def _probe_invalid_meta_value(client, _ctx):
    chan = f"#meta2-{client.nick}"
    await _roomcreate(client, chan)
    return await _send_and_recv(client, f"ROOMMETA {chan} agent_limit notanumber")


async def _probe_roominvite_no_such_nick(client, _ctx):
    chan = f"#inv-{client.nick}"
    await _join(client, chan)
    return await _send_and_recv(client, f"ROOMINVITE {chan} testserv-ghost-ri")


async def _probe_roommeta_denied(client, ctx):
    return await _send_and_recv(client, f"ROOMMETA {ctx['channel']} purpose newval")


async def _probe_roomkick_denied(client, ctx):
    return await _send_and_recv(client, f"ROOMKICK {ctx['channel']} testserv-ghost")


async def _probe_roomarchive_denied(client, ctx):
    return await _send_and_recv(client, f"ROOMARCHIVE {ctx['channel']}")


async def _probe_tags_set_other_denied(client, _ctx):
    return await _send_and_recv(client, "TAGS testserv-someoneelse newtag")


async def _probe_threadclose_denied(client, ctx):
    await _join(client, ctx["channel"])
    return await _send_and_recv(client, f"THREADCLOSE {ctx['channel']} {ctx['thread']} :nope")


async def _probe_promote_denied(client, ctx):
    await _join(client, ctx["channel"])
    return await _send_and_recv(client, f"THREADCLOSE PROMOTE {ctx['channel']} {ctx['thread']}")


async def _probe_thread_create_invalid_name(client, _ctx):
    chan = f"#thr-{client.nick}"
    await _join(client, chan)
    return await _send_and_recv(client, f"THREAD CREATE {chan} --bad-name :hello")


async def _probe_thread_create_already_exists(client, _ctx):
    chan = f"#thr2-{client.nick}"
    await _join(client, chan)
    await _thread_create(client, chan, "dup")
    return await _send_and_recv(client, f"THREAD CREATE {chan} dup :again")


async def _probe_thread_reply_no_such_thread(client, _ctx):
    chan = f"#thr3-{client.nick}"
    await _join(client, chan)
    return await _send_and_recv(client, f"THREAD REPLY {chan} nope :hi")


async def _probe_threadclose_no_such_thread(client, _ctx):
    chan = f"#thr4-{client.nick}"
    await _join(client, chan)
    return await _send_and_recv(client, f"THREADCLOSE {chan} nope :summary")


async def _probe_promote_no_such_thread(client, _ctx):
    chan = f"#thr5-{client.nick}"
    await _join(client, chan)
    return await _send_and_recv(client, f"THREADCLOSE PROMOTE {chan} nope")


async def _probe_thread_reply_archived(client, _ctx):
    chan = f"#thr6-{client.nick}"
    await _join(client, chan)
    await _thread_create(client, chan, "done", text="starting")
    await _thread_close(client, chan, "done", summary="all done")
    return await _send_and_recv(client, f"THREAD REPLY {chan} done :too late")


async def _probe_threadclose_already_closed(client, _ctx):
    chan = f"#thr7-{client.nick}"
    await _join(client, chan)
    await _thread_create(client, chan, "done", text="starting")
    await _thread_close(client, chan, "done", summary="all done")
    return await _send_and_recv(client, f"THREADCLOSE {chan} done :again")


async def _probe_promote_already_closed(client, _ctx):
    chan = f"#thr8-{client.nick}"
    await _join(client, chan)
    await _thread_create(client, chan, "done", text="starting")
    await _thread_close(client, chan, "done", summary="all done")
    return await _send_and_recv(client, f"THREADCLOSE PROMOTE {chan} done")


async def _probe_history_invalid_count_nonnumeric(client, _ctx):
    chan = f"#hist-{client.nick}"
    return await _send_and_recv(client, f"HISTORY RECENT {chan} notanumber")


async def _probe_history_invalid_count_negative(client, _ctx):
    chan = f"#hist2-{client.nick}"
    return await _send_and_recv(client, f"HISTORY RECENT {chan} -5")


async def _probe_history_since_invalid_cursor(client, _ctx):
    chan = f"#hist3-{client.nick}"
    # "bm9jb2xvbmhlcmU=" is valid base64 (decodes to "nocolonhere") but has
    # no ":" separator once decoded, so it deterministically fails cursor
    # parsing regardless of base64-decoder leniency quirks — see
    # agentirc/skills/history.py's _decode_cursor.
    return await _send_and_recv(client, f"HISTORY SINCE {chan} bm9jb2xvbmhlcmU=")


CASES: list[ErrorCase] = [
    # -- missing-params: ERR_NEEDMOREPARAMS across every rooms/threads/history verb --
    ErrorCase("roomcreate-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("ROOMCREATE #x")),
    ErrorCase("roommeta-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("ROOMMETA")),
    ErrorCase("tags-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("TAGS")),
    ErrorCase("roominvite-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("ROOMINVITE #x")),
    ErrorCase("roomkick-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("ROOMKICK #x")),
    ErrorCase("roomarchive-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("ROOMARCHIVE")),
    ErrorCase("thread-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("THREAD")),
    ErrorCase(
        "thread-create-missing-params",
        ERROR_TOKEN_MISSING_PARAMS,
        _bare("THREAD CREATE #x name"),
    ),
    ErrorCase(
        "thread-reply-missing-params",
        ERROR_TOKEN_MISSING_PARAMS,
        _bare("THREAD REPLY #x name"),
    ),
    ErrorCase("threads-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("THREADS")),
    ErrorCase("threadclose-missing-params-bare", ERROR_TOKEN_MISSING_PARAMS, _bare("THREADCLOSE")),
    ErrorCase(
        "threadclose-missing-params-short",
        ERROR_TOKEN_MISSING_PARAMS,
        _bare("THREADCLOSE #x"),
    ),
    ErrorCase(
        "threadclose-promote-missing-params",
        ERROR_TOKEN_MISSING_PARAMS,
        _bare("THREADCLOSE PROMOTE #x"),
    ),
    ErrorCase("history-missing-params", ERROR_TOKEN_MISSING_PARAMS, _bare("HISTORY")),
    ErrorCase(
        "history-recent-missing-params",
        ERROR_TOKEN_MISSING_PARAMS,
        _bare("HISTORY RECENT #x"),
    ),
    ErrorCase(
        "history-search-missing-params",
        ERROR_TOKEN_MISSING_PARAMS,
        _bare("HISTORY SEARCH #x"),
    ),
    # -- invalid-channel-name --
    ErrorCase(
        "roomcreate-invalid-channel-name",
        ERROR_TOKEN_INVALID_CHANNEL_NAME,
        _bare("ROOMCREATE notachannel :purpose=x"),
        expected_untagged=lambda nick: (f":testserv NOTICE {nick} :Channel name must start with #"),
    ),
    # -- channel-already-exists --
    ErrorCase(
        "roomcreate-already-exists", ERROR_TOKEN_CHANNEL_ALREADY_EXISTS, _probe_roomcreate_exists
    ),
    ErrorCase(
        "threadclose-promote-breakout-conflict",
        ERROR_TOKEN_CHANNEL_ALREADY_EXISTS,
        _probe_promote_breakout_conflict,
    ),
    # -- no-such-channel --
    ErrorCase("roommeta-no-such-channel", ERROR_TOKEN_NO_SUCH_CHANNEL, _bare("ROOMMETA #nope-rm")),
    ErrorCase(
        "roominvite-no-such-channel",
        ERROR_TOKEN_NO_SUCH_CHANNEL,
        _bare("ROOMINVITE #nope-ri testserv-ghost"),
    ),
    ErrorCase(
        "roomkick-no-such-channel",
        ERROR_TOKEN_NO_SUCH_CHANNEL,
        _bare("ROOMKICK #nope-rk testserv-ghost"),
    ),
    ErrorCase(
        "roomarchive-no-such-channel",
        ERROR_TOKEN_NO_SUCH_CHANNEL,
        _bare("ROOMARCHIVE #nope-ra"),
    ),
    # -- not-managed-room --
    ErrorCase(
        "roommeta-not-managed-room", ERROR_TOKEN_NOT_MANAGED_ROOM, _not_managed("ROOMMETA {chan}")
    ),
    ErrorCase(
        "roomkick-not-managed-room",
        ERROR_TOKEN_NOT_MANAGED_ROOM,
        _not_managed("ROOMKICK {chan} testserv-ghost"),
    ),
    ErrorCase(
        "roomarchive-not-managed-room",
        ERROR_TOKEN_NOT_MANAGED_ROOM,
        _not_managed("ROOMARCHIVE {chan}"),
    ),
    # -- user-not-in-channel --
    ErrorCase(
        "roomkick-user-not-in-channel",
        ERROR_TOKEN_USER_NOT_IN_CHANNEL,
        _probe_roomkick_target_missing,
    ),
    # -- readonly-meta-key / invalid-meta-value --
    ErrorCase("roommeta-readonly-key", ERROR_TOKEN_READONLY_META_KEY, _probe_readonly_meta_key),
    ErrorCase(
        "roommeta-invalid-meta-value", ERROR_TOKEN_INVALID_META_VALUE, _probe_invalid_meta_value
    ),
    # -- no-such-nick --
    ErrorCase("tags-query-no-such-nick", ERROR_TOKEN_NO_SUCH_NICK, _bare("TAGS testserv-ghost-tq")),
    ErrorCase("roominvite-no-such-nick", ERROR_TOKEN_NO_SUCH_NICK, _probe_roominvite_no_such_nick),
    # -- permission-denied --
    ErrorCase(
        "roommeta-permission-denied",
        ERROR_TOKEN_PERMISSION_DENIED,
        _probe_roommeta_denied,
        setup=_setup_owned_room,
    ),
    ErrorCase(
        "roomkick-permission-denied",
        ERROR_TOKEN_PERMISSION_DENIED,
        _probe_roomkick_denied,
        setup=_setup_owned_room,
    ),
    ErrorCase(
        "roomarchive-permission-denied",
        ERROR_TOKEN_PERMISSION_DENIED,
        _probe_roomarchive_denied,
        setup=_setup_owned_room,
    ),
    ErrorCase("tags-set-other-denied", ERROR_TOKEN_PERMISSION_DENIED, _probe_tags_set_other_denied),
    ErrorCase(
        "threadclose-permission-denied",
        ERROR_TOKEN_PERMISSION_DENIED,
        _probe_threadclose_denied,
        setup=_setup_owned_thread,
    ),
    ErrorCase(
        "threadclose-promote-permission-denied",
        ERROR_TOKEN_PERMISSION_DENIED,
        _probe_promote_denied,
        setup=_setup_owned_thread,
    ),
    # -- not-on-channel --
    ErrorCase(
        "thread-create-not-on-channel",
        ERROR_TOKEN_NOT_ON_CHANNEL,
        _after_join("THREAD CREATE {chan} name :hi"),
    ),
    ErrorCase(
        "thread-reply-not-on-channel",
        ERROR_TOKEN_NOT_ON_CHANNEL,
        _after_join("THREAD REPLY {chan} name :hi"),
    ),
    ErrorCase("threads-not-on-channel", ERROR_TOKEN_NOT_ON_CHANNEL, _after_join("THREADS {chan}")),
    ErrorCase(
        "threadclose-not-on-channel",
        ERROR_TOKEN_NOT_ON_CHANNEL,
        _after_join("THREADCLOSE {chan} name :summary"),
    ),
    ErrorCase(
        "threadclose-promote-not-on-channel",
        ERROR_TOKEN_NOT_ON_CHANNEL,
        _after_join("THREADCLOSE PROMOTE {chan} name"),
    ),
    # -- invalid-thread-name / thread-already-exists --
    ErrorCase(
        "thread-create-invalid-name",
        ERROR_TOKEN_INVALID_THREAD_NAME,
        _probe_thread_create_invalid_name,
        expected_untagged=lambda nick: (
            f":testserv 400 {nick} --bad-name"
            " :Invalid thread name (alphanumeric + hyphens, 1-32 chars)"
        ),
    ),
    ErrorCase(
        "thread-create-already-exists",
        ERROR_TOKEN_THREAD_ALREADY_EXISTS,
        _probe_thread_create_already_exists,
    ),
    # -- no-such-thread --
    ErrorCase(
        "thread-reply-no-such-thread",
        ERROR_TOKEN_NO_SUCH_THREAD,
        _probe_thread_reply_no_such_thread,
        expected_untagged=lambda nick: f":testserv 404 {nick} nope :No such thread",
    ),
    ErrorCase(
        "threadclose-no-such-thread",
        ERROR_TOKEN_NO_SUCH_THREAD,
        _probe_threadclose_no_such_thread,
    ),
    ErrorCase(
        "threadclose-promote-no-such-thread",
        ERROR_TOKEN_NO_SUCH_THREAD,
        _probe_promote_no_such_thread,
    ),
    # -- thread-archived --
    ErrorCase(
        "thread-reply-archived",
        ERROR_TOKEN_THREAD_ARCHIVED,
        _probe_thread_reply_archived,
        expected_untagged=lambda nick: f":testserv 405 {nick} done :Thread is closed",
    ),
    ErrorCase(
        "threadclose-already-closed",
        ERROR_TOKEN_THREAD_ARCHIVED,
        _probe_threadclose_already_closed,
    ),
    ErrorCase(
        "threadclose-promote-already-closed",
        ERROR_TOKEN_THREAD_ARCHIVED,
        _probe_promote_already_closed,
    ),
    # -- invalid-count --
    ErrorCase(
        "history-recent-invalid-count-nonnumeric",
        ERROR_TOKEN_INVALID_COUNT,
        _probe_history_invalid_count_nonnumeric,
        expected_untagged=lambda nick: f":testserv NOTICE {nick} :Invalid count",
    ),
    ErrorCase(
        "history-recent-invalid-count-negative",
        ERROR_TOKEN_INVALID_COUNT,
        _probe_history_invalid_count_negative,
    ),
    # -- invalid-cursor --
    ErrorCase(
        "history-since-invalid-cursor",
        ERROR_TOKEN_INVALID_CURSOR,
        _probe_history_since_invalid_cursor,
        expected_untagged=lambda nick: f":testserv NOTICE {nick} :Invalid cursor",
    ),
    # -- unknown-subcommand --
    ErrorCase(
        "thread-unknown-subcommand",
        ERROR_TOKEN_UNKNOWN_SUBCOMMAND,
        _bare("THREAD BOGUS"),
        expected_untagged=lambda nick: f":testserv NOTICE {nick} :Unknown THREAD subcommand: BOGUS",
    ),
    ErrorCase(
        "history-unknown-subcommand",
        ERROR_TOKEN_UNKNOWN_SUBCOMMAND,
        _bare("HISTORY BOGUS"),
        expected_untagged=lambda nick: (
            f":testserv NOTICE {nick} :Unknown HISTORY subcommand: BOGUS"
        ),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_error_token_additive(case: ErrorCase, server, make_client):
    """For every rooms/threads/history error path: tag when negotiated, byte-identical when not."""
    ctx: dict = {}
    if case.setup is not None:
        ctx = await case.setup(server, make_client, case.id)

    nick_mt = f"testserv-{case.id}-mt"
    nick_pl = f"testserv-{case.id}-pl"
    tagged = await _make_client(make_client, nick_mt, tags=True)
    plain = await _make_client(make_client, nick_pl, tags=False)

    tagged_line = await case.probe(tagged, ctx)
    plain_line = await case.probe(plain, ctx)

    tmsg = Message.parse(tagged_line)
    pmsg = Message.parse(plain_line)

    # (a) message-tags client sees the expected stable token.
    assert tmsg.tags.get(ERROR_TAG) == case.token, (
        f"{case.id}: expected agentirc.io/error={case.token!r}, "
        f"got tags {tmsg.tags!r} on line {tagged_line!r}"
    )

    # (b) non-negotiated client is byte-identical to the untagged baseline:
    # no tag block at all, ...
    assert not pmsg.tags, f"{case.id}: untagged client unexpectedly got tags {pmsg.tags!r}"

    # ...and (generic, for every case) the tagged reply stripped of tags is
    # structurally identical to the untagged reply once each client's own
    # nick is normalised away.
    assert _normalize(tmsg, nick_mt) == _normalize(pmsg, nick_pl), (
        f"{case.id}: tag-stripped tagged reply diverges from the untagged reply\n"
        f"  tagged (raw):  {tagged_line!r}\n"
        f"  untagged:      {plain_line!r}"
    )

    # ...and (for cases overlapping the golden/characterization tests in
    # tests/test_wire_format_envelope.py) an exact literal match against the
    # locked baseline shape.
    if case.expected_untagged is not None:
        assert plain_line == case.expected_untagged(nick_pl), (
            f"{case.id}: untagged line does not match the golden-locked shape\n"
            f"  got:      {plain_line!r}\n"
            f"  expected: {case.expected_untagged(nick_pl)!r}"
        )


def test_all_tokens_covered():
    """Sanity: every token in the public vocabulary is exercised by at least one case."""
    from agentirc import protocol

    vocabulary = {
        getattr(protocol, name) for name in dir(protocol) if name.startswith("ERROR_TOKEN_")
    }
    covered = {c.token for c in CASES}
    assert covered == vocabulary, f"uncovered tokens: {vocabulary - covered}"
