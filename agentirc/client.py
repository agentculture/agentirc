"""IRC client connection state and command handling.

Vendored from culture@df50942 (`culture/agentirc/client.py`) with
import paths rewritten and this vendoring-context docstring added;
the ``Client`` class body is unchanged. Originally the bootstrap
spec said this module would "stay in culture", but its only culture
imports are support modules already vendored in agentirc (`aio`,
`constants`, `protocol`, `telemetry`) and server-core peers
(`channel`, `skill`). Without it,
`agentirc/ircd.py:_accept_c2s_connection` cannot accept a TCP IRC
client.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hmac
import json
import logging
import re
import time
from typing import TYPE_CHECKING

from opentelemetry import trace as _otel_trace
from opentelemetry.context import Context as _OtelContext
from opentelemetry.trace import Span as _OtelSpan

from agentirc._internal.aio import maybe_await
from agentirc.accounts import Account, fold_nick, is_trusted_peer
from agentirc._internal.constants import (
    EVENT_TYPE_RE,
    MAX_INBOUND_LINE,
    PRIVMSG_WIRE_LIMIT,
    SYSTEM_USER_PREFIX,
    new_msgid,
    server_time_now,
    split_message_text,
)
from agentirc._internal.protocol import replies
from agentirc._internal.protocol.message import Message
from agentirc._internal.telemetry.audit import utc_iso_timestamp as _utc_iso_timestamp
from agentirc._internal.telemetry.context import TRACEPARENT_TAG as _TP_TAG_NAME
from agentirc._internal.telemetry.context import (
    context_from_traceparent,
    current_traceparent,
    extract_traceparent_from_tags,
)
from agentirc._internal.telemetry.context import (
    inject_traceparent as _inject_traceparent,
)
from agentirc.channel import Channel
from agentirc.protocol import (
    BACKFILLEND,
    BOT_CAP,
    ERR_SASLABORTED,
    ERR_SASLALREADY,
    ERR_SASLFAIL,
    ERR_SASLTOOLONG,
    ERROR_TAG,
    ERROR_TOKEN_LINE_TOO_LONG,
    ERROR_TOKENS_VERSION,
    EVENT,
    EVENTERR,
    MSGID_TAG,
    RPL_LOGGEDIN,
    RPL_SASLMECHS,
    RPL_SASLSUCCESS,
    SASL_CAP,
    SASL_MECHANISMS,
    SERVER_TIME_TAG,
    VERBS,
    VERBS_DISCOVERY_VERSION,
)
from agentirc.skill import Event, EventType

# OTEL instrumentation name. Kept verbatim ("culture.agentirc") because
# it's a public identifier downstream trace consumers grep for; renaming
# would break their dashboards. Mirrors `_CULTURE_TRACER_NAME` in
# agentirc/_internal/telemetry/tracing.py.
_TRACER_NAME = "culture.agentirc"
# Span attribute keys, defined once so a future rename / sanitization layer
# has one edit point.
_ATTR_BODY = "irc.message.body"
_ATTR_SIZE = "irc.message.size"
_ATTR_NICK = "irc.client.nick"
_ATTR_CHANNEL = "irc.channel"


if TYPE_CHECKING:
    from agentirc.ircd import IRCd
    from agentirc.skills.history import HistoryEntry


# Local nicks are printable ASCII without spaces (culture#506 h2, M7).
_NICK_ASCII_RE = re.compile(r"^[\x21-\x7e]+$")


def _registration_failure_code(account: Account) -> str:
    """Reason code for a registration refused on an inactive or re-keyed account."""
    if account.state == "revoked":
        return "revoked"
    if not account.active:
        return "suspended"
    return "bad_secret"


class Client:
    """A connected IRC client."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        server: IRCd,
    ):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.nick: str | None = None
        self.user: str | None = None
        self.realname: str | None = None
        self.host: str = writer.get_extra_info("peername", ("unknown", 0))[0]
        self.channels: set[Channel] = set()
        self._registered = False
        # IRCv3: CAP LS opens negotiation; registration is held until CAP END.
        self._cap_negotiating = False
        # SASL PLAIN (culture#506 t8). ``account`` is set only after a
        # successful AUTHENTICATE and carries role/grants for enforcement.
        self.account: Account | None = None
        self._sasl_mech: str | None = None
        self._sasl_buf: str = ""
        # The first pre-registration NICK checks the accounts file once; later
        # ones read the cached table (the registration gate re-checks).
        self._nick_checked_accounts = False
        # Set the moment the session is evicted (revoke / nick reclaim):
        # from then on nothing it sent is dispatched (culture#506 h2, M1).
        self._closing = False
        self.tags: list[str] = []
        self.caps: set[str] = set()
        self.modes: set[str] = set()
        self.icon: str | None = None
        self._session_span: _OtelSpan | None = None
        # Server liveness (t5): stamped on every inbound chunk (see `handle`)
        # and explicitly by `_handle_pong` — any traffic from the client
        # counts as liveness, not just PONG replies. `last_ping_sent` lets
        # the IRCd liveness sweep send at most one keepalive PING per idle
        # window instead of re-sending on every sweep tick.
        self.last_activity: float = time.time()
        self.last_ping_sent: float | None = None

    @property
    def prefix(self) -> str:
        return f"{self.nick}!{self.user}@{self.host}"

    async def send(self, message: Message) -> None:
        # Only inject trace context for clients that negotiated IRCv3
        # message-tags; otherwise older clients would see an unexpected @-tag
        # block and `send_tagged`'s tag-stripping for non-capable clients
        # would be undone here.
        if "message-tags" in self.caps:
            tp = current_traceparent()
            if tp is not None:
                _inject_traceparent(message, traceparent=tp, tracestate=None)
        try:
            wire = message.format().encode("utf-8")
            self.writer.write(wire)
            await self.writer.drain()
            # Record bytes after a successful drain so we don't count
            # writes that immediately faulted.
            self.server.metrics.irc_bytes_sent.add(len(wire), {"direction": "s2c"})
        except OSError:
            pass  # Client disconnected; cleanup happens in ircd._handle_connection

    async def send_raw(self, line: str) -> None:
        """Write a pre-formatted IRC line to the client socket.

        Appends CRLF internally, matching ServerLink.send_raw convention.
        Injects `culture.dev/traceparent` as an IRCv3 tag when a span is active
        AND the client negotiated the `message-tags` capability.
        """
        if "message-tags" in self.caps:
            tp = current_traceparent()
            if tp is not None:
                # send_raw takes a pre-formatted line without an existing tag
                # block; prefix a fresh @tag.
                line = f"@{_TP_TAG_NAME}={tp} {line}"
        try:
            wire = f"{line}\r\n".encode("utf-8")
            self.writer.write(wire)
            await self.writer.drain()
            self.server.metrics.irc_bytes_sent.add(len(wire), {"direction": "s2c"})
        except OSError:
            pass  # Client disconnected; cleanup happens in ircd._handle_connection

    async def send_tagged(self, msg: Message) -> None:
        """Send a Message, stripping tags for clients that haven't negotiated message-tags."""
        if msg.tags and "message-tags" not in self.caps:
            msg = Message(
                tags={},
                prefix=msg.prefix,
                command=msg.command,
                params=list(msg.params),
            )
        await self.send(msg)

    async def send_numeric(
        self, code: str, *params: str, tags: dict[str, str] | None = None
    ) -> None:
        """Send a numeric reply, optionally carrying additive IRCv3 tags.

        ``tags`` (e.g. the ``agentirc.io/error`` reason token skills attach
        to error replies) rides through ``send_tagged`` so it is stripped
        for clients that haven't negotiated ``message-tags`` — callers that
        never pass ``tags`` see byte-identical behavior to before.
        """
        target = self.nick or "*"
        msg = Message(
            prefix=self.server.config.name,
            command=code,
            params=[target, *params],
            tags=dict(tags) if tags else {},
        )
        await self.send_tagged(msg)

    async def _send_line_too_long_error(self) -> None:
        """Notify the client that an inbound line exceeded ``MAX_INBOUND_LINE`` bytes.

        Names the limit in the NOTICE text and carries the stable
        ``line-too-long`` reason (``ERROR_TOKEN_LINE_TOO_LONG``) via the
        ``agentirc.io/error`` tag for message-tags clients, mirroring the t2
        stable-error-token pattern (``ERROR_TAG`` + ``send_tagged``). The
        offending line is discarded (see ``_process_buffer``); this method
        only sends the notice, it does not touch the buffer.
        """
        await self.send_tagged(
            Message(
                prefix=self.server.config.name,
                command="NOTICE",
                params=[
                    self.nick or "*",
                    f"Line exceeds the {MAX_INBOUND_LINE}-byte limit and was discarded",
                ],
                tags={ERROR_TAG: ERROR_TOKEN_LINE_TOO_LONG},
            )
        )

    async def _process_buffer(self, buffer: str, skipping_line: bool = False) -> tuple[str, bool]:
        """Parse and dispatch all complete lines from buffer.

        Returns ``(remainder, skipping_line)``. ``skipping_line`` tracks an
        in-progress inbound line that already exceeded ``MAX_INBOUND_LINE``
        bytes before its terminating ``\\n`` arrived (see ``Client.handle``);
        while true, bytes are discarded up to and including the next ``\\n``
        so the real terminator isn't misread as the start of a new command.
        A line that arrives *complete* (its own ``\\n`` already present) and
        is over the limit is caught inline by the per-line check below --
        the ``buffer.split("\\n", 1)`` above it already did the resync, so no
        state needs to carry over for that case. Both paths discard the
        oversized line ONLY; every other well-formed line in the buffer is
        parsed and dispatched exactly as before.
        """
        # Per-call get_tracer: test fixture swaps provider between tests.
        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            "irc.client.process_buffer"
        ) as span:
            if skipping_line:
                if "\n" not in buffer:
                    return "", True
                _discarded, buffer = buffer.split("\n", 1)
                skipping_line = False

            while "\n" in buffer:
                if self._closing:
                    # Evicted mid-buffer: drop every line still queued.
                    return "", False
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                line_len_bytes = len(line.encode("utf-8"))
                if line_len_bytes > MAX_INBOUND_LINE:
                    await self._send_line_too_long_error()
                    continue  # discard this line only; already resynced by the split above
                try:
                    msg = Message.parse(line)
                except Exception as exc:  # noqa: BLE001 -- widen for any parser failure
                    span.add_event(
                        "irc.parse_error",
                        attributes={
                            "line_preview": line[:64],
                            "error": type(exc).__name__,
                        },
                    )
                    self._submit_parse_error_audit(line, exc)
                    continue
                # Record received bytes + message size for every successfully-parsed
                # line.  +2 accounts for the \r\n that was stripped during line-split.
                line_bytes = line_len_bytes + 2
                self.server.metrics.irc_bytes_received.add(line_bytes, {"direction": "c2s"})
                self.server.metrics.irc_message_size.record(
                    line_bytes, {"verb": msg.command, "direction": "c2s"}
                )
                if msg.command:
                    await self._dispatch(msg)

            # No more complete lines. If the still-unterminated remainder has
            # already grown past the limit, it will never be a valid line no
            # matter how much more text follows -- send the error now (rather
            # than waiting indefinitely for a '\n' that bounds nothing), drop
            # the buffered bytes to bound memory, and remember to skip ahead
            # to the next '\n' once it eventually shows up.
            if len(buffer.encode("utf-8")) > MAX_INBOUND_LINE:
                await self._send_line_too_long_error()
                buffer = ""
                skipping_line = True

            return buffer, skipping_line

    def _submit_parse_error_audit(self, line: str, exc: BaseException) -> None:
        """Build and submit a PARSE_ERROR audit record for a malformed inbound line.

        The record cannot go through build_audit_record (which expects an Event);
        PARSE_ERROR is a synthetic event_type with no Event object behind it.
        """
        # Capture trace/span ids from the active span (the
        # `irc.client.process_buffer` we're inside of).
        span = _otel_trace.get_current_span()
        ctx = span.get_span_context()
        trace_id_hex = format(ctx.trace_id, "032x") if ctx.is_valid else ""
        span_id_hex = format(ctx.span_id, "016x") if ctx.is_valid else ""

        peer_info = self.writer.get_extra_info("peername")
        remote_addr = f"{peer_info[0]}:{peer_info[1]}" if peer_info else ""

        tags: dict[str, str] = {}
        tp = current_traceparent()
        if tp:
            tags["culture.dev/traceparent"] = tp

        record = {
            "ts": _utc_iso_timestamp(time.time()),
            "server": self.server.config.name,
            "event_type": "PARSE_ERROR",
            "origin": "local",
            "peer": "",
            "trace_id": trace_id_hex,
            "span_id": span_id_hex,
            "actor": {
                "nick": self.nick or "",
                "kind": "human",
                "remote_addr": remote_addr,
            },
            "target": {"kind": "", "name": ""},
            "payload": {
                "line_preview": line[:64],
                "error": type(exc).__name__,
            },
            "tags": tags,
        }
        self.server.audit.submit(record)

    async def handle(self, initial_msg: str | None = None) -> None:
        peer_info = self.writer.get_extra_info("peername")
        remote_addr = f"{peer_info[0]}:{peer_info[1]}" if peer_info else ""
        kind = "human"  # Plan 5/6 will refine to bot/harness
        self.server.metrics.clients_connected.add(1, {"kind": kind})
        session_started = time.perf_counter()
        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            "irc.client.session",
            attributes={"irc.client.remote_addr": remote_addr},
        ) as span:
            self._session_span = span
            try:
                buffer = ""
                # Tracks an in-progress inbound line that already tripped
                # MAX_INBOUND_LINE before its terminating '\n' arrived; see
                # _process_buffer's docstring for the full resync story.
                skipping_line = False
                if initial_msg:
                    self.last_activity = time.time()
                    buffer = initial_msg.replace("\r\n", "\n").replace("\r", "\n")
                    buffer, skipping_line = await self._process_buffer(buffer, skipping_line)
                while not self._closing:
                    data = await self.reader.read(4096)
                    if not data or self._closing:
                        break
                    # Server liveness (t5): any inbound bytes count as activity,
                    # not just PONG replies — see the field docstring on
                    # `last_activity` in __init__.
                    self.last_activity = time.time()
                    buffer += data.decode("utf-8", errors="replace")
                    # Normalize all line endings to \n for simpler parsing
                    buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
                    buffer, skipping_line = await self._process_buffer(buffer, skipping_line)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                self.server.metrics.clients_connected.add(-1, {"kind": kind})
                self.server.metrics.client_session_duration.record(
                    time.perf_counter() - session_started, {"kind": kind}
                )

    async def _dispatch(self, msg: Message) -> None:
        if self._closing:
            return  # evicted: nothing more runs as this nick (culture#506 h2, M1)
        extract = extract_traceparent_from_tags(msg, peer=None)
        self.server.metrics.trace_inbound.add(1, {"result": extract.status, "peer": ""})
        if extract.status == "valid":
            parent_ctx: _OtelContext | None = context_from_traceparent(extract.traceparent)
        else:
            parent_ctx = _OtelContext()  # force root: detach from session span

        verb = msg.command.upper()
        attrs = {
            "irc.command": verb,
            "irc.prefix_nick": (msg.prefix.split("!")[0] if msg.prefix else ""),
            "culture.trace.origin": ("local" if extract.status == "missing" else "remote"),
        }
        if extract.status in ("malformed", "too_long"):
            attrs["culture.trace.dropped_reason"] = extract.status

        # Per-call get_tracer: test fixture swaps provider between tests.
        cmd_started = time.perf_counter()
        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            f"irc.command.{verb}",
            context=parent_ctx,
            attributes=attrs,
        ):
            handler = getattr(self, f"_handle_{msg.command.lower()}", None)
            if not self._registered and verb not in self._PREREGISTRATION_VERBS:
                # 451 rule (culture#506 h1): before registration a connection
                # has no proven identity, so it may only register. Without
                # this, an unregistered socket could PRIVMSG/NOTICE as the
                # nick it merely requested -- including an account nick.
                await self.send_numeric(
                    replies.ERR_NOTREGISTERED, msg.command, replies.MSG_NOTREGISTERED
                )
            elif handler:
                await maybe_await(handler(msg))
            else:
                await self._dispatch_to_skill(msg)
        self.server.metrics.client_command_duration.record(
            (time.perf_counter() - cmd_started) * 1000.0, {"verb": verb}
        )

    async def _dispatch_to_skill(self, msg: Message) -> None:
        """Route a verb with no ``_handle_*`` method to its skill, else 421."""
        skill = self.server.get_skill_for_command(msg.command)
        if skill and self._registered:
            try:
                await skill.on_command(self, msg)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Skill %s failed on command %s", skill.name, msg.command
                )
        else:
            await self.send_numeric(replies.ERR_UNKNOWNCOMMAND, msg.command, "Unknown command")

    # Verbs dispatched before registration completes; everything else gets
    # 451 ERR_NOTREGISTERED (see ``_dispatch``). The four bot-extension verbs
    # are not registration verbs, but they keep their documented
    # ``EVENTERR <id> :not-registered`` refusal (``_bot_verb_gate``) instead
    # of a 451, so bots parsing EVENTERR see no wire change.
    _PREREGISTRATION_VERBS: frozenset[str] = frozenset(
        {
            "CAP",
            "NICK",
            "USER",
            "PASS",
            "PING",
            "PONG",
            "AUTHENTICATE",
            "QUIT",
            "EVENTSUB",
            "EVENTUNSUB",
            "EVENTPUB",
            "BACKFILL",
        }
    )

    async def _handle_ping(self, msg: Message) -> None:
        token = msg.params[0] if msg.params else ""
        await self.send(
            Message(
                prefix=self.server.config.name,
                command="PONG",
                params=[self.server.config.name, token],
            )
        )

    def _handle_pong(self, _msg: Message) -> None:
        """Client responding to our (server-initiated) liveness PING.

        Explicitly stamps ``last_activity`` (t5) so a unit test can drive
        this handler directly without going through the socket read loop
        (which also stamps liveness for every inbound chunk — this is the
        belt-and-suspenders half of that, not the only place it happens).
        """
        self.last_activity = time.time()

    # Capabilities advertised in CAP LS and accepted in CAP REQ. Adding
    # a new cap here is a minor bump per docs/api-stability.md; removing
    # one is a major bump.
    _SUPPORTED_CAPS: frozenset[str] = frozenset({"message-tags", BOT_CAP, SASL_CAP})

    async def _handle_cap(self, msg: Message) -> None:
        sub = msg.params[0].upper() if msg.params else ""
        # Per IRCv3, CAP LS opens negotiation and suspends registration until
        # CAP END. A bare CAP REQ (no preceding LS) does not suspend it, which
        # keeps legacy clients that REQ before NICK/USER registering as before.
        if sub == "LS" and not self._registered:
            self._cap_negotiating = True
        if sub == "LS":
            await self._cap_ls(msg)
        elif sub == "REQ":
            await self._cap_req(msg)
        elif sub == "END":
            # Negotiation over: complete any registration held since CAP LS/REQ.
            self._cap_negotiating = False
            await self._try_register()

    async def _cap_ls(self, msg: Message) -> None:
        # CAP LS 302 clients get cap values (sasl=PLAIN); older ones the bare name.
        v302 = len(msg.params) >= 2 and msg.params[1].isdigit() and int(msg.params[1]) >= 302
        cap_list = " ".join(
            f"{cap}={SASL_MECHANISMS}" if cap == SASL_CAP and v302 else cap
            for cap in sorted(self._SUPPORTED_CAPS)
        )
        await self.send_raw(f":{self.server.config.name} CAP {self.nick or '*'} LS :{cap_list}")

    async def _cap_req(self, msg: Message) -> None:
        requested = msg.params[1].split() if len(msg.params) >= 2 else []
        if all(cap in self._SUPPORTED_CAPS for cap in requested):
            self.caps.update(requested)
            await self.send_raw(
                f":{self.server.config.name} CAP {self.nick or '*'} ACK :{' '.join(requested)}"
            )
        else:
            await self.send_raw(
                f":{self.server.config.name} CAP {self.nick or '*'} NAK :{' '.join(requested)}"
            )

    async def _handle_nick(self, msg: Message) -> None:
        if not msg.params:
            await self.send_numeric(replies.ERR_NONICKNAMEGIVEN, "No nickname given")
            return

        nick = msg.params[0]

        format_error = self._nick_format_error(nick)
        if format_error is not None:
            await self.send_numeric(replies.ERR_ERRONEUSNICKNAME, nick, format_error)
            return

        # Agent accounts (culture#506 t8). Exact match on the full nick, so
        # ``<agent>__peek…`` observer nicks are never covered by an account.
        # Lookups read the cached table (no file I/O). The first NICK before
        # registration checks the file once, so a freshly written account
        # applies here; the registration gate re-checks before 001.
        if not self._registered and not self._nick_checked_accounts:
            self._nick_checked_accounts = True
            self.server.accounts.maybe_reload()
        # A case variant of an account nick is refused outright (h2, M7).
        canonical = self.server.accounts.lookalike_of(nick)
        if canonical is not None:
            await self.send_numeric(
                replies.ERR_ERRONEUSNICKNAME,
                nick,
                f"Nickname differs only in case from agent account {canonical}",
            )
            return
        # ``is_reserved`` also covers the nicks a rejected accounts file names
        # (fail-closed, h2 M5): they are held like account nicks and refused
        # at registration.
        is_account_nick = self.server.accounts.is_reserved(nick)
        if self._registered and self.account is not None and nick != self.nick:
            await self.send_numeric(
                replies.ERR_ERRONEUSNICKNAME,
                nick,
                "An authenticated session keeps its account nick",
            )
            return
        if is_account_nick and self._registered and nick != self.nick:
            await self.send_numeric(
                replies.ERR_ERRONEUSNICKNAME,
                nick,
                "Nickname is reserved for an agent account; authenticate with SASL",
            )
            return

        if is_account_nick and not self._registered:
            # Hold the nick on this connection without claiming it in
            # ``server.clients``: an unauthenticated connection must never
            # squat an account nick. Collision is resolved at registration,
            # after SASL (see ``_try_register``).
            self._release_nick()
            self.nick = nick
            if self._session_span is not None:
                self._session_span.set_attribute(_ATTR_NICK, nick)
            await self._try_register()
            return

        if nick in self.server.clients:
            await self.send_numeric(replies.ERR_NICKNAMEINUSE, nick, "Nickname is already in use")
            return

        self._release_nick()

        self.nick = nick
        self.server.clients[nick] = self
        if self._session_span is not None:
            self._session_span.set_attribute(_ATTR_NICK, nick)
        await self._try_register()

    def _nick_format_error(self, nick: str) -> str | None:
        """Return the 432 text for a syntactically unacceptable *nick*, else None.

        Checks run in a fixed order; the first failure wins.
        """
        # Reject reserved system-* nick prefix
        if nick.startswith(SYSTEM_USER_PREFIX):
            return "Nickname prefix 'system-' is reserved"
        # Printable ASCII only (culture#506 h2, M7): a non-ASCII lookalike
        # of an account nick (Cyrillic "е" for "e") cannot be told apart
        # from the real one by humans, and no confusables table is needed
        # when only ASCII is allowed.
        if not _NICK_ASCII_RE.match(nick):
            return "Nickname must be printable ASCII"
        # Enforce server name prefix
        expected_prefix = f"{self.server.config.name}-"
        if not nick.startswith(expected_prefix):
            return f"Nickname must start with {expected_prefix}"
        if len(nick) <= len(expected_prefix):
            return f"Nickname must have an agent name after {expected_prefix}"
        return None

    def _release_nick(self) -> None:
        """Drop this connection's current nick from ``server.clients`` (if it owns it)."""
        old_nick = self.nick
        if old_nick and self.server.clients.get(old_nick) is self:
            del self.server.clients[old_nick]

    async def _handle_user(self, msg: Message) -> None:
        if self._registered:
            await self.send_numeric(replies.ERR_ALREADYREGISTRED, "You may not reregister")
            return
        if len(msg.params) < 4:
            await self.send_numeric(replies.ERR_NEEDMOREPARAMS, "USER", replies.MSG_NEEDMOREPARAMS)
            return

        self.user = msg.params[0]
        self.realname = msg.params[3]
        await self._try_register()

    async def _try_register(self) -> None:
        if self.nick and self.user and not self._registered and not self._cap_negotiating:
            if not await self._authorize_registration():
                return
            self._registered = True
            await self._send_welcome()
            if self._closing:
                return  # evicted while the welcome was sent: announce nothing
            # Announce to linked peers
            for link in self.server.links.values():
                await link.send_raw(f"SNICK {self.nick} {self.user} {self.host} :{self.realname}")

    async def _authorize_registration(self) -> bool:
        """Account gate for registration (culture#506 t8).

        Runs on every registration path (CAP LS…END, bare CAP REQ, no CAP), so
        it does not rely on the CAP hold. A nick with no account passes
        untouched. An account nick registers only if this connection completed
        SASL PLAIN as exactly that account and the account is still active
        with the same secret; otherwise 904 and the connection is closed.
        When it passes, any other local connection holding the nick is
        evicted and this one takes it — the credential always wins.
        """
        # One file check per registration: a freshly written account is
        # enforced here without waiting for the 1 s watcher.
        self.server.accounts.maybe_reload()
        account = self.server.accounts.get(self.nick)
        if self.account is not None and (account is None or self.account.nick != self.nick):
            self._record_auth_failure("account_mismatch")
            await self._refuse_and_close("SASL account does not match nick")
            return False
        if account is None:
            if self.server.accounts.is_reserved(self.nick):
                # Fail-closed (h2, M5: the accounts file is invalid and names
                # this nick, or no table loaded at all) or a case variant of
                # an account that appeared after NICK (M7).
                self._record_auth_failure("nick_reserved")
                await self._refuse_and_close(f"Nickname {self.nick} is reserved")
                return False
            return True
        if self.account is None:
            self._record_auth_failure("nick_without_auth")
            await self._refuse_and_close(f"SASL authentication required for {self.nick}")
            return False
        if not account.active or not hmac.compare_digest(
            account.secret_sha256, self.account.secret_sha256
        ):
            self._record_auth_failure(_registration_failure_code(account))
            await self._refuse_and_close("SASL authentication failed")
            return False
        self.account = account  # pick up the freshest role/grants
        return await self._reclaim_account_nick()

    async def _reclaim_account_nick(self) -> bool:
        """Take ``self.nick`` from any other local holder; True once it is ours.

        Re-check after every await (h2, M1): a concurrent login for the
        same nick may have taken it while we waited, and this session may
        itself have been evicted meanwhile. The latest credential wins.
        """
        for _ in range(8):
            holder = self.server.clients.get(self.nick)
            if holder is None or holder is self:
                break
            if not isinstance(holder, Client):
                # Pseudo-users (bots, system) are not evicted by an account.
                await self.send_numeric(
                    replies.ERR_NICKNAMEINUSE, self.nick, "Nickname is already in use"
                )
                return False
            await holder.evict("Nick reclaimed by an authenticated session")
            if self._closing:
                return False
        else:
            await self._refuse_and_close("Nick reclaim did not settle")
            return False
        self.server.clients[self.nick] = self
        return True

    def _record_auth_failure(self, code: str, *, limit_key: tuple | None = None) -> None:
        """Log (nick, peer, reason code), bump the metric, feed the limiter.

        Never receives or logs secret material: only a fixed reason code.
        Only credential failures from trusted peers pass a ``limit_key``
        (``(peer, folded authcid)``, h2 M4); everything else — registration
        gate refusals, malformed payloads, wrong mechanisms, untrusted peers
        — stays out of the brute-force limiter.
        """
        logging.getLogger(__name__).warning(
            "auth failure nick=%r peer=%s reason=%s", self.nick or "*", self.host, code
        )
        self.server.metrics.auth_failures.add(1, {"reason": code})
        if limit_key is not None:
            self.server.auth_limiter.record(limit_key)

    async def _refuse_and_close(self, reason: str) -> None:
        """Send 904 + ERROR and close this connection. Never logs secrets."""
        await self.send_numeric(ERR_SASLFAIL, reason)
        await self.send_raw(f"ERROR :Closing Link: {self.host} ({reason})")
        logging.getLogger(__name__).info(
            "Refused %s from %s: %s", self.nick or "*", self.host, reason
        )
        raise ConnectionError(reason)

    # How long one channel member's QUIT write may take during an eviction
    # before it is abandoned (culture#506 h2, M2).
    _EVICT_SEND_TIMEOUT = 5.0
    # Grace for the evicted socket to flush its ERROR line before it is
    # aborted (a peer that stopped reading must not keep it open).
    _EVICT_ABORT_AFTER = 2.0

    async def evict(self, reason: str) -> None:
        """Disconnect this session (revoke / nick reclaim). Idempotent.

        Order matters (culture#506 h2, M1/M2): the session first stops
        acting (``_closing``, unregistered, no account — buffered lines are
        dropped), then its socket is closed, then channel peers get the
        QUIT as background writes that never wait on a slow peer, then the
        nick is released and the QUIT event (relayed to linked servers as
        SQUITUSER) is emitted once. The read loop then unwinds through
        ``IRCd._remove_client``, whose identity check leaves the new owner's
        ``clients`` entry alone.
        """
        if self._closing:
            return
        self._closing = True
        nick = self.nick
        was_registered = self._registered
        self._registered = False
        self.account = None
        self._sasl_reset()
        quit_msg = Message(prefix=self.prefix, command="QUIT", params=[reason])
        self._close_transport(f"ERROR :Closing Link: {self.host} ({reason})")
        channel_names = [ch.name for ch in self.channels]
        if BOT_CAP not in self.caps:
            self._fan_out_quit(quit_msg)
        self._leave_all_channels()
        self._release_nick()
        self.server.subscription_registry.remove_client(self)
        modes, self.modes = set(self.modes), set()
        if was_registered and nick:
            await self.server.emit_event(
                Event(
                    type=EventType.QUIT,
                    channel=None,
                    nick=nick,
                    data={"reason": reason, "channels": channel_names},
                )
            )
            await self.server._emit_disconnect_events(nick, modes)

    def _fan_out_quit(self, quit_msg: Message) -> None:
        """Queue *quit_msg* once to every channel peer, as background writes."""
        notified: set[Client] = set()
        for channel in [*self.channels]:
            for member in [*channel.members]:
                if member is not self and member not in notified:
                    self._send_in_background(member, quit_msg)
                    notified.add(member)

    def _leave_all_channels(self) -> None:
        """Part every channel; drop the ones left empty unless persistent."""
        for channel in [*self.channels]:
            channel.remove(self)
            if not channel.members and not channel.persistent:
                if self.server.channels.get(channel.name) is channel:
                    del self.server.channels[channel.name]
        self.channels.clear()

    def _close_transport(self, last_line: str) -> None:
        """Write *last_line* without waiting for it to drain, then close."""
        try:
            self.writer.write(f"{last_line}\r\n".encode("utf-8"))
        except (OSError, RuntimeError):
            pass
        try:
            self.writer.close()
        except (OSError, RuntimeError):
            return
        transport = self.writer.transport
        try:
            asyncio.get_running_loop().call_later(self._EVICT_ABORT_AFTER, transport.abort)
        except RuntimeError:
            pass

    def _send_in_background(self, member, message: Message) -> None:
        """Send *message* to *member* without awaiting it (bounded by a timeout)."""

        async def _send() -> None:
            try:
                await asyncio.wait_for(member.send(message), self._EVICT_SEND_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                logging.getLogger(__name__).info(
                    "QUIT to %s abandoned: peer is not reading", getattr(member, "nick", "?")
                )
            except Exception:  # noqa: BLE001 - one bad peer never breaks the eviction
                logging.getLogger(__name__).debug("QUIT fan-out failed", exc_info=True)

        task = asyncio.get_running_loop().create_task(_send())
        self.server._background_tasks.add(task)
        task.add_done_callback(self.server._background_tasks.discard)

    # --- SASL PLAIN (IRCv3 sasl-3.1; culture#506 t8) ---

    # A single AUTHENTICATE chunk is at most 400 bytes; a chunk of exactly
    # 400 means "more follows" and a lone "+" ends the payload. A PLAIN
    # payload for a nick + 64-hex secret fits in one chunk; allow two.
    _SASL_CHUNK = 400
    _SASL_MAX_PAYLOAD = 800

    def _sasl_reset(self) -> None:
        self._sasl_mech = None
        self._sasl_buf = ""

    async def _sasl_fail(
        self, code: str, *extra: tuple[str, str], limit_key: tuple | None = None
    ) -> None:
        """SASL attempt failed: log it (count it if *limit_key*), numerics, 904, close."""
        self._sasl_reset()
        self._record_auth_failure(code, limit_key=limit_key)
        for numeric, text in extra:
            await self.send_numeric(numeric, text)
        await self._refuse_and_close("SASL authentication failed")

    async def _handle_authenticate(self, msg: Message) -> None:
        if not msg.params:
            await self.send_numeric(
                replies.ERR_NEEDMOREPARAMS, "AUTHENTICATE", replies.MSG_NEEDMOREPARAMS
            )
            return
        arg = msg.params[0]
        if self.account is not None:
            await self.send_numeric(ERR_SASLALREADY, "You have already authenticated")
            return
        if self._registered:
            await self.send_numeric(ERR_SASLFAIL, "SASL is only available before registration")
            return
        if SASL_CAP not in self.caps:
            await self.send_numeric(ERR_SASLFAIL, "SASL capability not negotiated")
            return
        if arg == "*":
            self._sasl_reset()
            await self.send_numeric(ERR_SASLABORTED, "SASL authentication aborted")
            return
        if self._sasl_mech is None:
            await self._sasl_start(arg)
            return
        if arg != "+" and not await self._sasl_take_chunk(arg):
            return
        await self._sasl_plain_finish()

    async def _sasl_start(self, arg: str) -> None:
        """First AUTHENTICATE: vet the peer and mechanism, then prompt for the payload."""
        accounts_cfg = self.server.config.accounts
        if not is_trusted_peer(
            self.host, accounts_cfg.trusted_cidrs, trust_loopback=accounts_cfg.trust_loopback
        ):
            # Refuse before the "AUTHENTICATE +" prompt, so an untrusted
            # peer never sends its secret over the network (h2, M3).
            await self._sasl_fail("untrusted_peer")
            return
        if arg.upper() != SASL_MECHANISMS:
            await self.send_numeric(RPL_SASLMECHS, SASL_MECHANISMS, "are available SASL mechanisms")
            # Closed, and not counted (no credential was guessed): one
            # socket can no longer loop on wrong mechanisms (h2, M4).
            await self._sasl_fail("wrong_mechanism")
            return
        self._sasl_mech = SASL_MECHANISMS
        self._sasl_buf = ""
        await self.send_raw("AUTHENTICATE +")

    async def _sasl_take_chunk(self, arg: str) -> bool:
        """Buffer one payload chunk. True when the payload is complete.

        False when more chunks follow or the attempt was refused as too long.
        """
        if len(arg) > self._SASL_CHUNK:
            await self._sasl_fail("too_long", (ERR_SASLTOOLONG, "SASL message too long"))
            return False
        self._sasl_buf += arg
        if len(self._sasl_buf) > self._SASL_MAX_PAYLOAD:
            await self._sasl_fail("too_long", (ERR_SASLTOOLONG, "SASL message too long"))
            return False
        return len(arg) != self._SASL_CHUNK  # a full-size chunk means more follows

    async def _sasl_plain_finish(self) -> None:
        payload = self._sasl_buf
        self._sasl_reset()
        try:
            decoded = base64.b64decode(payload, validate=True).decode("utf-8")
            authzid, authcid, secret = decoded.split("\0")
        except ValueError:  # includes binascii.Error and UnicodeDecodeError
            # Never log the payload: it carries the secret.
            await self._sasl_fail("malformed")
            return
        if (authzid and authzid != authcid) or not authcid:
            await self._sasl_fail("malformed")
            return
        # Per (peer, account) budget (h2, M4): failures against one account
        # never lock out another account from the same address.
        limit_key = (self.host, fold_nick(authcid))
        if self.server.auth_limiter.blocked(limit_key):
            # Over the failure budget: refuse before the secret is checked.
            await self._sasl_fail("rate_limited")
            return
        self.server.accounts.maybe_reload()  # one file check per SASL attempt
        account, reason = self.server.accounts.check(authcid, secret)
        if account is None:
            await self._sasl_fail(reason or "bad_secret", limit_key=limit_key)
            return
        self.account = account
        mask = f"{self.nick or '*'}!{self.user or '*'}@{self.host}"
        await self.send_numeric(
            RPL_LOGGEDIN, mask, account.nick, f"You are now logged in as {account.nick}"
        )
        await self.send_numeric(RPL_SASLSUCCESS, "SASL authentication successful")
        await self._try_register()

    async def _send_welcome(self) -> None:
        await self.send_numeric(
            replies.RPL_WELCOME,
            f"Welcome to CULTURE.DEV on {self.server.config.name} {self.prefix}",
        )
        await self.send_numeric(
            replies.RPL_YOURHOST,
            f"Your host is {self.server.config.name}, running CULTURE.DEV",
        )
        await self.send_numeric(
            replies.RPL_CREATED,
            "This server was created today",
        )
        await self.send_numeric(
            replies.RPL_MYINFO,
            self.server.config.name,
            "culture",
            "o",
            "ov",
        )
        await self._send_motd()

    async def _send_motd(self) -> None:
        """Send the fixed CULTURE.DEV MOTD (375, 372 per line, 376)."""
        name = self.server.config.name
        await self.send_numeric(replies.RPL_MOTDSTART, f"- {name} Message of the day -")
        for line in (
            f"- {name} - CULTURE.DEV",
            "- The professional workspace for agents. https://culture.dev",
        ):
            await self.send_numeric(replies.RPL_MOTD, line)
        await self.send_numeric(replies.RPL_ENDOFMOTD, "End of /MOTD command")

    async def _handle_motd(self, msg: Message) -> None:
        if not self._registered:
            return
        await self._send_motd()

    def account_role(self) -> str | None:
        """Live role of this session's account; ``None`` if it has no account.

        Authenticated sessions whose account has since vanished or been
        deactivated report ``"none"`` (no privileges) rather than ``None``,
        so they never fall back to ungated behaviour.
        """
        if self.account is None:
            return None
        return self.server.accounts.role_of(self.account.nick) or "none"

    def account_access(self, channel_name: str) -> str | None | bool:
        """Channel access for an authenticated session (culture#506 t9).

        Returns ``True`` for sessions without an account (ungated, today's
        behaviour), else ``"speak"`` / ``"read"`` / ``None`` from the live
        account table.
        """
        if self.account is None:
            return True
        return self.server.accounts.channel_access(self.account.nick, channel_name)

    def can_read(self, channel_name: str) -> bool:
        """May this session read *channel_name* (culture#506 h1)?

        Sessions without an account are ungated (``True``). An account needs
        a ``read`` or ``speak`` grant, or the ``operator`` role. Live: read
        from the current account table on every call. The single check every
        read path uses (JOIN, HISTORY, BACKFILL, EVENTSUB, THREAD PROMOTE).
        """
        return bool(self.account_access(channel_name))

    def can_speak(self, channel_name: str) -> bool:
        """May this session post into *channel_name* (culture#506 h1)?

        Sessions without an account are ungated (``True``). An account needs
        a ``speak`` grant or the ``operator`` role. The single check every
        write path uses (PRIVMSG, NOTICE, TOPIC set, THREAD, EVENTPUB).
        """
        return self.account is None or self.account_access(channel_name) == "speak"

    def can_read_event(self, event: Event) -> bool:
        """May this session receive *event* through EVENTSUB (culture#506 h1)?

        True when :meth:`visible_event` would deliver it (possibly with a
        filtered channel list).
        """
        return self.visible_event(event) is not None

    def visible_event(self, event: Event) -> Event | None:
        """The form of *event* this session may receive through EVENTSUB.

        Sessions without an account (and ``operator`` accounts) get *event*
        unchanged, as before. For other account sessions:

        - a channel event needs :meth:`can_read` on its channel;
        - a direct message (a ``message`` event with no channel) is visible
          only to its sender and its target;
        - any other channel-less event is scoped by the channel names it
          carries (:meth:`_scope_channelless_event`).

        Returns ``None`` when the event must not be delivered.
        """
        if self.account is None:
            return event
        if event.channel is not None:
            return event if self.can_read(event.channel) else None
        if str(event.type) == EventType.MESSAGE.value:
            target = event.data.get("target") if isinstance(event.data, dict) else None
            mine = self.nick is not None and self.nick in (event.nick, target)
            return event if mine else None
        if self.account_role() == "operator":
            return event
        return self._scope_channelless_event(event)

    # Top-level payload keys of channel-less events that name one channel.
    _EVENT_CHANNEL_KEYS = ("channel", "target")

    def _scope_channelless_event(self, event: Event) -> Event | None:
        """Scope a channel-less event to the channels this session can read.

        Policy (agentirc#66 review): a payload that names a single channel
        (``channel`` / ``target`` starting with ``#``) or a room
        (``room_id``) the session cannot read drops the event. A
        ``channels`` list is filtered to the readable channels and the event
        is delivered with that filtered list — unless the list was non-empty
        and nothing readable remains, in which case it is dropped (e.g. the
        QUIT of someone who shared no readable channel with this session).
        Events that name no channel (``agent.*``, ``console.*``,
        ``server.*``, ``tags.update``, ``presence.update`` …) are delivered.
        """
        data = event.data if isinstance(event.data, dict) else {}
        for key in self._EVENT_CHANNEL_KEYS:
            name = data.get(key)
            if isinstance(name, str) and name.startswith("#") and not self.can_read(name):
                return None
        if "room_id" in data and not self._can_read_room_id(data["room_id"]):
            return None
        channels = data.get("channels")
        if not isinstance(channels, list):
            return event
        readable = [ch for ch in channels if isinstance(ch, str) and self.can_read(ch)]
        if len(readable) == len(channels):
            return event
        if not readable:
            return None
        return dataclasses.replace(event, data={**data, "channels": readable})

    def _can_read_room_id(self, room_id: object) -> bool:
        """Can this session read the (current) channel whose room id is *room_id*?

        An unknown room id is treated as unreadable: it cannot be checked.
        """
        for channel in self.server.channels.values():
            if getattr(channel, "room_id", None) == room_id:
                return self.can_read(channel.name)
        return False

    async def _handle_join(self, msg: Message) -> None:
        if not self._registered:
            return
        if not msg.params:
            await self.send_numeric(replies.ERR_NEEDMOREPARAMS, "JOIN", replies.MSG_NEEDMOREPARAMS)
            return

        channel_name = msg.params[0]
        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            "irc.join",
            attributes={_ATTR_CHANNEL: channel_name, _ATTR_NICK: self.nick or ""},
        ):
            if not channel_name.startswith("#"):
                return

            # Block joins to archived rooms
            existing = self.server.channels.get(channel_name)
            if existing and existing.archived:
                await self.send(
                    Message(
                        prefix=self.server.config.name,
                        command="NOTICE",
                        params=[
                            self.nick,
                            f"{channel_name} is archived and cannot be joined",
                        ],
                    )
                )
                return

            if not self.can_read(channel_name):
                # 474 ERR_BANNEDFROMCHAN: the authenticated account holds no
                # grant (read or speak) on this channel.
                await self.send_numeric(
                    "474",
                    channel_name,
                    "Cannot join channel (no grant for this account)",
                )
                return

            channel = self.server.get_or_create_channel(channel_name)
            if self in channel.members:
                return

            channel.add(self)
            self.channels.add(channel)

            # Notify all channel members (including self).
            # Bot-CAP clients skip the broadcast entirely — the user.join
            # event still fires below, so EVENTSUB subscribers see the join,
            # but no human-visible JOIN line hits other channel members.
            # (Channel membership IS added regardless; topic + NAMES below
            # still go to the joining client so it knows the join succeeded.)
            if BOT_CAP not in self.caps:
                join_msg = Message(prefix=self.prefix, command="JOIN", params=[channel_name])
                for member in [*channel.members]:
                    await member.send(join_msg)

            # Send topic if set
            if channel.topic:
                await self.send_numeric(replies.RPL_TOPIC, channel_name, channel.topic)

            # Send names list
            await self._send_names(channel)

            # Emit event AFTER delivering all join-related numerics (topic, NAMES)
            # so that the event PRIVMSG doesn't interleave with 353/366 in client buffers.
            await self.server.emit_event(
                Event(type=EventType.JOIN, channel=channel_name, nick=self.nick)
            )

    async def _handle_part(self, msg: Message) -> None:
        if not msg.params:
            await self.send_numeric(replies.ERR_NEEDMOREPARAMS, "PART", replies.MSG_NEEDMOREPARAMS)
            return

        channel_name = msg.params[0]
        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            "irc.part",
            attributes={_ATTR_CHANNEL: channel_name, _ATTR_NICK: self.nick or ""},
        ):
            reason = msg.params[1] if len(msg.params) > 1 else ""

            channel = self.server.channels.get(channel_name)
            if not channel or self not in channel.members:
                await self.send_numeric(
                    replies.ERR_NOTONCHANNEL,
                    channel_name,
                    replies.MSG_NOTONCHANNEL,
                )
                return

            part_params = [channel_name, reason] if reason else [channel_name]
            # Bot-CAP clients skip the broadcast — symmetrical with the
            # silent JOIN behaviour. The user.part event still fires.
            if BOT_CAP not in self.caps:
                part_msg = Message(prefix=self.prefix, command="PART", params=part_params)
                for member in [*channel.members]:
                    await member.send(part_msg)

            await self.server.emit_event(
                Event(
                    type=EventType.PART,
                    channel=channel_name,
                    nick=self.nick,
                    data={"reason": reason},
                )
            )

            channel.remove(self)
            self.channels.discard(channel)

            if not channel.members and not channel.persistent:
                del self.server.channels[channel_name]

    async def _handle_topic(self, msg: Message) -> None:
        if not msg.params:
            await self.send_numeric(replies.ERR_NEEDMOREPARAMS, "TOPIC", replies.MSG_NEEDMOREPARAMS)
            return

        channel_name = msg.params[0]
        channel = self.server.channels.get(channel_name)
        if not channel or self not in channel.members:
            await self.send_numeric(
                replies.ERR_NOTONCHANNEL,
                channel_name,
                replies.MSG_NOTONCHANNEL,
            )
            return

        if len(msg.params) == 1:
            # Query topic
            if channel.topic:
                await self.send_numeric(replies.RPL_TOPIC, channel_name, channel.topic)
            else:
                await self.send_numeric(replies.RPL_NOTOPIC, channel_name, "No topic is set")
        else:
            # Set topic: posting into the channel, so it needs speak
            # (culture#506 h1); ungated for sessions without an account.
            if not self.can_speak(channel_name):
                await self.send_numeric(
                    replies.ERR_CHANOPRIVSNEEDED,
                    channel_name,
                    "Cannot set topic (account lacks a speak grant)",
                )
                return
            channel.topic = msg.params[1]
            topic_msg = Message(
                prefix=self.prefix,
                command="TOPIC",
                params=[channel_name, channel.topic],
            )
            for member in [*channel.members]:
                await member.send(topic_msg)
            await self.server.emit_event(
                Event(
                    type=EventType.TOPIC,
                    channel=channel_name,
                    nick=self.nick,
                    data={"topic": channel.topic},
                )
            )

    async def _handle_names(self, msg: Message) -> None:
        if not msg.params:
            return
        channel_name = msg.params[0]
        channel = self.server.channels.get(channel_name)
        if channel:
            await self._send_names(channel)

    async def _send_names(self, channel: Channel) -> None:
        nicks = " ".join(f"{channel.get_prefix(m)}{m.nick}" for m in channel.members)
        await self.send_numeric(replies.RPL_NAMREPLY, "=", channel.name, nicks)
        await self.send_numeric(replies.RPL_ENDOFNAMES, channel.name, "End of /NAMES list")

    async def _handle_list(self, _msg: Message) -> None:
        for name, channel in self.server.channels.items():
            topic = channel.topic or ""
            await self.send_numeric(replies.RPL_LIST, name, str(len(channel.members)), topic)
        await self.send_numeric(replies.RPL_LISTEND, "End of LIST")

    async def _handle_mode(self, msg: Message) -> None:
        if not msg.params:
            await self.send_numeric(replies.ERR_NEEDMOREPARAMS, "MODE", replies.MSG_NEEDMOREPARAMS)
            return

        target = msg.params[0]
        if target.startswith("#"):
            await self._handle_channel_mode(msg)
        else:
            await self._handle_user_mode(msg)

    def _apply_mode_r(self, channel, adding, applied_modes):
        if adding:
            channel.restricted = True
        else:
            channel.restricted = False
        applied_modes.append(("+" if adding else "-") + "R")

    def _apply_mode_s(self, channel, adding, param_value, applied_modes, applied_params):
        if adding:
            channel.shared_with.add(param_value)
        else:
            channel.shared_with.discard(param_value)
        applied_modes.append(("+" if adding else "-") + "S")
        applied_params.append(param_value)

    async def _apply_mode_membership(
        self,
        channel,
        channel_name,
        ch,
        adding,
        param_value,
        applied_modes,
        applied_params,
    ):
        target_nick = param_value
        target_client = self.server.clients.get(target_nick)
        if not target_client or target_client not in channel.members:
            await self.send_numeric(
                replies.ERR_USERNOTINCHANNEL,
                target_nick,
                channel_name,
                "They aren't on that channel",
            )
            return
        if ch == "o":
            if adding:
                channel.operators.add(target_client)
            else:
                channel.operators.discard(target_client)
        elif ch == "v":
            if adding:
                channel.voiced.add(target_client)
            else:
                channel.voiced.discard(target_client)
        applied_modes.append(("+" if adding else "-") + ch)
        applied_params.append(target_nick)

    _PARAM_MODES = frozenset({"o", "v", "S"})

    async def _apply_mode_char(
        self,
        channel,
        channel_name: str,
        ch: str,
        adding: bool,
        param_queue: list[str],
        applied_modes: list[str],
        applied_params: list[str],
    ) -> None:
        """Apply a single mode character. Consumes one param from param_queue when needed."""
        if ch == "R":
            self._apply_mode_r(channel, adding, applied_modes)
            return
        if ch not in self._PARAM_MODES or not param_queue:
            return
        param_value = param_queue.pop(0)
        if ch == "S":
            self._apply_mode_s(channel, adding, param_value, applied_modes, applied_params)
        else:
            await self._apply_mode_membership(
                channel,
                channel_name,
                ch,
                adding,
                param_value,
                applied_modes,
                applied_params,
            )

    async def _broadcast_mode_change(
        self,
        channel,
        channel_name: str,
        applied_modes: list[str],
        applied_params: list[str],
    ) -> None:
        """Send the aggregated MODE message to all channel members."""
        if not applied_modes:
            return
        mode_msg = Message(
            prefix=self.prefix,
            command="MODE",
            params=[channel_name, "".join(applied_modes)] + applied_params,
        )
        for member in [*channel.members]:
            await member.send(mode_msg)

    async def _handle_channel_mode(self, msg: Message) -> None:
        channel_name = msg.params[0]
        channel = self.server.channels.get(channel_name)
        if not channel:
            await self.send_numeric(
                replies.ERR_NOSUCHCHANNEL, channel_name, replies.MSG_NOSUCHCHANNEL
            )
            return

        if len(msg.params) == 1:
            await self.send_numeric(replies.RPL_CHANNELMODEIS, channel_name, "+")
            return

        if not channel.is_operator(self):
            await self.send_numeric(
                replies.ERR_CHANOPRIVSNEEDED,
                channel_name,
                "You're not channel operator",
            )
            return

        modestring = msg.params[1]
        param_queue = list(msg.params[2:])
        adding = True
        applied_modes: list[str] = []
        applied_params: list[str] = []
        for ch in modestring:
            if ch == "+":
                adding = True
            elif ch == "-":
                adding = False
            else:
                await self._apply_mode_char(
                    channel,
                    channel_name,
                    ch,
                    adding,
                    param_queue,
                    applied_modes,
                    applied_params,
                )

        # Auto-promote if no operators remain
        if not channel.operators and channel.members:
            channel.operators.add(min(channel.members, key=lambda m: m.nick))

        await self._broadcast_mode_change(channel, channel_name, applied_modes, applied_params)

    _VALID_USER_MODE_CHARS = frozenset("HABC")
    _USER_MODE_EDGE_EVENTS: dict[tuple[str, bool], EventType] = {
        ("A", True): EventType.AGENT_CONNECT,
        ("A", False): EventType.AGENT_DISCONNECT,
        ("C", True): EventType.CONSOLE_OPEN,
        ("C", False): EventType.CONSOLE_CLOSE,
    }

    def _apply_user_mode_char(self, ch: str, adding: bool) -> EventType | None:
        """Mutate ``self.modes`` for a single mode char and return the edge event, if any.

        Returns None if the char is unknown or the transition was a no-op
        (setting an already-set mode or clearing an already-clear one).
        """
        if ch not in self._VALID_USER_MODE_CHARS:
            return None
        had = ch in self.modes
        if adding:
            self.modes.add(ch)
        else:
            self.modes.discard(ch)
        if had == adding:
            return None
        return self._USER_MODE_EDGE_EVENTS.get((ch, adding))

    def _parse_mode_edges(self, modestring: str) -> list[EventType]:
        """Apply each char of ``modestring`` and collect the emitted edge events."""
        pending: list[EventType] = []
        adding = True
        for ch in modestring:
            if ch == "+":
                adding = True
            elif ch == "-":
                adding = False
            else:
                event = self._apply_user_mode_char(ch, adding)
                if event is not None:
                    pending.append(event)
        return pending

    async def _emit_user_mode_events(self, pending: list[EventType]) -> None:
        for event_type in pending:
            await self.server.emit_event(
                Event(
                    type=event_type,
                    channel=None,
                    nick=self.nick,
                    data={"nick": self.nick},
                )
            )

    async def _handle_user_mode(self, msg: Message) -> None:
        # Reject pre-registration so an unregistered socket cannot inject
        # agent.connect / console.open into #system by sending MODE after NICK
        # but before USER.
        if not self._registered:
            return
        target_nick = msg.params[0]
        if target_nick != self.nick:
            await self.send_numeric(
                replies.ERR_USERSDONTMATCH,
                "Can't change mode for other users",
            )
            return

        modestring = msg.params[1] if len(msg.params) > 1 else ""
        pending = self._parse_mode_edges(modestring)
        await self._emit_user_mode_events(pending)

        mode_str = "+" + "".join(sorted(self.modes)) if self.modes else "+"
        await self.send_numeric(replies.RPL_UMODEIS, mode_str)

    async def _send_to_channel(self, channel, target, relay, text, is_notice, msgid=None):
        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            "irc.privmsg.deliver.channel",
            attributes={
                "irc.channel": target,
                _ATTR_BODY: text,
                _ATTR_SIZE: len(text),
                "irc.notice": is_notice,
            },
        ):
            for member in [*channel.members]:
                if member is not self:
                    await self._deliver_relay(member, relay)
            self.server.metrics.privmsg_delivered.add(1, {"kind": "channel", "channel": target})
            event_data = {"text": text}
            if msgid is not None:
                event_data["msgid"] = msgid
            if is_notice:
                event_data["notice"] = True
            await self.server.emit_event(
                Event(
                    type=EventType.MESSAGE,
                    channel=target,
                    nick=self.nick,
                    data=event_data,
                )
            )

    @staticmethod
    async def _deliver_relay(member, relay: Message) -> None:
        """Deliver a relayed message, stripping tags for non-message-tags clients.

        Local :class:`Client` recipients go through ``send_tagged`` so any
        ``msgid``/``time``/``agentirc.io/thread`` tags on ``relay`` are dropped
        for clients that didn't negotiate ``message-tags`` (byte-identical wire
        output). ``RemoteClient``/``VirtualClient`` have no ``send_tagged`` and
        a no-op ``send`` — federation delivers to remotes via the S2S path, so
        the tag block never rides the link (quirk #9 stays local-only).
        """
        send_tagged = getattr(member, "send_tagged", None)
        if send_tagged is not None:
            await send_tagged(relay)
        else:
            await member.send(relay)

    async def _send_to_client(self, target, relay, text, is_notice, msgid=None):
        from agentirc.remote_client import RemoteClient

        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            "irc.privmsg.deliver.dm",
            attributes={
                "irc.target.nick": target,
                _ATTR_BODY: text,
                _ATTR_SIZE: len(text),
                "irc.notice": is_notice,
            },
        ):
            recipient = self.server.get_client(target)
            if not recipient:
                return False
            is_remote_recipient = isinstance(recipient, RemoteClient)
            if is_remote_recipient:
                # S2S relay is intentionally untagged — msgid/time are
                # local-delivery-only and must not ride the federation link.
                s2s_cmd = "SNOTICE" if is_notice else "SMSG"
                await recipient.link.send_raw(
                    f":{self.server.config.name} {s2s_cmd} {target} {self.nick} :{text}"
                )
            else:
                await self._deliver_relay(recipient, relay)
            self.server.metrics.privmsg_delivered.add(1, {"kind": "dm"})
            event_data = {"text": text, "target": target}
            if msgid is not None:
                event_data["msgid"] = msgid
            if is_notice:
                event_data["notice"] = True
            event = Event(
                type=EventType.MESSAGE,
                channel=None,
                nick=self.nick,
                data=event_data,
            )
            await self.server.emit_event(event)
            # DM-history capture (task t7): a narrow, direct hook — NOT a
            # new/widened event — see HistorySkill.record_dm's docstring.
            # Federated recipients are out of scope: the peer server owns
            # storing its own copy on its side of the link, and this
            # server has no visibility into whether/how it does.
            if not is_remote_recipient:
                self._store_dm_history(target, text, event.timestamp, msgid)
            return True

    def _store_dm_history(
        self, target: str, text: str, timestamp: float, msgid: str | None
    ) -> None:
        """Store a delivered local DM into history, bypassing the event bus.

        Looks up the same ``HistorySkill`` instance that serves live
        ``HISTORY`` queries (:meth:`agentirc.ircd.IRCd.get_skill_for_command`,
        the lookup command dispatch itself uses) and calls its
        ``record_dm`` directly. See that method's docstring for why this
        must not go through ``IRCd.emit_event``/``on_event``.
        """
        from agentirc.skills.history import HistorySkill

        skill = self.server.get_skill_for_command("HISTORY")
        if isinstance(skill, HistorySkill):
            skill.record_dm(self.nick, target, text, timestamp, msgid)

    def _split_outbound_text(self, target: str, text: str) -> list[str]:
        """Split ``text`` into ordered PRIVMSG-wire-safe chunks for ``target``.

        Splits BEFORE msgid assignment (design choice, see ``_handle_privmsg``):
        each returned chunk flows through the relay as an independent
        message with its own fresh msgid/time tags, its own MESSAGE event,
        and — via history's MESSAGE subscription — its own history entry.
        There is no "parent" message that owns sub-parts; every wire line is
        a self-contained, independently addressable PRIVMSG. This keeps the
        split orthogonal to the existing msgid/history machinery instead of
        threading a fragmentation concept through it.

        The byte budget is computed conservatively from the sender's own
        prefix (``self.prefix``, i.e. ``nick!user@host``) because that's the
        exact prefix stamped on *every* recipient's relayed copy — this
        server never rewrites the prefix per recipient (see
        ``_send_to_channel`` / ``_send_to_client``) — so one budget bounds
        every recipient's wire line. IRCv3 tags are attached after this
        call and ride in their own tags-excluded budget (they don't count
        against the classic 512-byte PRIVMSG_WIRE_LIMIT).
        """
        overhead = len(f":{self.prefix} PRIVMSG {target} :".encode("utf-8")) + len(b"\r\n")
        budget = PRIVMSG_WIRE_LIMIT - overhead
        return split_message_text(text, budget)

    async def _handle_privmsg(self, msg: Message) -> None:
        if len(msg.params) < 2:
            await self.send_numeric(
                replies.ERR_NEEDMOREPARAMS, "PRIVMSG", replies.MSG_NEEDMOREPARAMS
            )
            return

        target = msg.params[0]
        text = msg.params[1]
        # Per-call get_tracer: test fixture swaps provider between tests.
        with _otel_trace.get_tracer(_TRACER_NAME).start_as_current_span(
            "irc.privmsg.dispatch",
            attributes={
                "irc.target": target,
                _ATTR_BODY: text,
                _ATTR_SIZE: len(text),
            },
        ):
            if target.startswith("#"):
                channel = self.server.channels.get(target)
                if not channel:
                    await self.send_numeric(
                        replies.ERR_NOSUCHCHANNEL, target, replies.MSG_NOSUCHCHANNEL
                    )
                    return
                if self not in channel.members:
                    await self.send_numeric(
                        replies.ERR_CANNOTSENDTOCHAN, target, "Cannot send to channel"
                    )
                    return
                if not self.can_speak(target):
                    await self.send_numeric(
                        replies.ERR_CANNOTSENDTOCHAN,
                        target,
                        "Cannot send to channel (account lacks a speak grant)",
                    )
                    return
                for chunk in self._split_outbound_text(target, text):
                    # One fresh msgid per chunk, stamped identically on
                    # every recipient's delivery of *that* chunk (fan-out
                    # stability) and echoed into that chunk's own MESSAGE
                    # event data. Tags ride only to message-tags clients —
                    # `_deliver_relay` strips them for everyone else.
                    msgid = new_msgid()
                    relay = Message(
                        prefix=self.prefix,
                        command="PRIVMSG",
                        params=[target, chunk],
                        tags={MSGID_TAG: msgid, SERVER_TIME_TAG: server_time_now()},
                    )
                    await self._send_to_channel(channel, target, relay, chunk, False, msgid=msgid)
                await self._notify_mentions(target, text)
            else:
                await self._relay_direct_privmsg(target, text)

    async def _relay_direct_privmsg(self, target: str, text: str) -> None:
        """Split, relay, and mention-notify a direct (non-channel) PRIVMSG.

        Extracted from ``_handle_privmsg`` (S3776 cognitive-complexity fix):
        the direct-message half of PRIVMSG dispatch, unchanged apart from
        living in its own method. Sends ``ERR_NOSUCHNICK`` if not even the
        first chunk could be delivered (target offline / unknown) — the
        same ``delivered`` bookkeeping ``_handle_privmsg`` used inline
        before this extraction.
        """
        delivered = False
        for chunk in self._split_outbound_text(target, text):
            msgid = new_msgid()
            relay = Message(
                prefix=self.prefix,
                command="PRIVMSG",
                params=[target, chunk],
                tags={MSGID_TAG: msgid, SERVER_TIME_TAG: server_time_now()},
            )
            sent = await self._send_to_client(target, relay, chunk, False, msgid=msgid)
            if not sent:
                break
            delivered = True
        if not delivered:
            await self.send_numeric(replies.ERR_NOSUCHNICK, target, replies.MSG_NOSUCHNICK)
            return
        await self._notify_mentions(None, text)

    async def _notify_mentions(self, channel_name: str | None, text: str) -> None:
        from agentirc.remote_client import RemoteClient

        mentioned_nicks = re.findall(r"@(\S+)", text)
        if not mentioned_nicks:
            return
        seen: set[str] = set()
        channel = self.server.channels.get(channel_name) if channel_name else None
        source = channel_name or "a direct message"
        for raw_nick in mentioned_nicks:
            nick = raw_nick.rstrip(".,;:!?")
            if nick in seen or nick == self.nick:
                continue
            seen.add(nick)
            target_client = self.server.get_client(nick)
            if not target_client:
                continue
            if channel and target_client not in channel.members:
                continue
            notice = Message(
                prefix=self.server.config.name,
                command="NOTICE",
                params=[
                    nick,
                    f"{self.nick} mentioned you in {source}: {text}",
                ],
            )
            if isinstance(target_client, RemoteClient):
                # Send mention notice through S2S link
                await target_client.link.send_raw(
                    f":{self.server.config.name} SNOTICE {nick}"
                    f" {self.server.config.name}"
                    f" :{self.nick} mentioned you in {source}: {text}"
                )
            else:
                await target_client.send(notice)

    async def _handle_notice(self, msg: Message) -> None:
        # Same as PRIVMSG but no error replies per RFC 2812
        if len(msg.params) < 2:
            return

        target = msg.params[0]
        text = msg.params[1]
        relay = Message(prefix=self.prefix, command="NOTICE", params=[target, text])

        if target.startswith("#"):
            channel = self.server.channels.get(target)
            if not channel:
                return
            if self not in channel.members:
                return
            if not self.can_speak(target):
                return  # NOTICE never gets error replies (RFC 2812)
            await self._send_to_channel(channel, target, relay, text, True)
        else:
            await self._send_to_client(target, relay, text, True)

    def _build_who_flags(self, member, channel) -> str:
        flags = "H"
        # Bot-CAP clients get a `B` flag in the user-modes column so vanilla
        # IRC clients can filter bots from presence panels by checking for
        # `B` (agentirc-extension flag, composes with the standard H/A flags).
        if BOT_CAP in getattr(member, "caps", frozenset()):
            flags += "B"
        if channel and channel.is_operator(member):
            flags += "@"
        elif channel and channel.is_voiced(member):
            flags += "+"
        if hasattr(member, "modes") and member.modes:
            flags += "[" + "".join(sorted(member.modes)) + "]"
        if hasattr(member, "icon") and member.icon:
            flags += "{" + member.icon + "}"
        return flags

    async def _send_who_reply(self, member, channel_name: str, channel=None) -> None:
        from agentirc.remote_client import RemoteClient  # noqa: F811

        flags = self._build_who_flags(member, channel)
        server_name = (
            member.server_name if isinstance(member, RemoteClient) else self.server.config.name
        )
        await self.send_numeric(
            replies.RPL_WHOREPLY,
            channel_name,
            member.user or "*",
            member.host,
            server_name,
            member.nick,
            flags,
            f"0 {member.realname or ''}",
        )

    async def _handle_who(self, msg: Message) -> None:
        if not msg.params:
            await self.send_numeric(replies.RPL_ENDOFWHO, "*", replies.MSG_ENDOFWHO)
            return

        target = msg.params[0]
        if target.startswith("#"):
            channel = self.server.channels.get(target)
            if channel:
                for member in [*channel.members]:
                    await self._send_who_reply(member, target, channel)
            await self.send_numeric(replies.RPL_ENDOFWHO, target, replies.MSG_ENDOFWHO)
        else:
            client = self.server.get_client(target)
            if client:
                chan_name = "*"
                chan_context = None
                for ch in client.channels:
                    chan_name = ch.name
                    chan_context = ch
                    break
                await self._send_who_reply(client, chan_name, chan_context)
            await self.send_numeric(replies.RPL_ENDOFWHO, target, replies.MSG_ENDOFWHO)

    async def _handle_whois(self, msg: Message) -> None:
        from agentirc.remote_client import RemoteClient

        if not msg.params:
            await self.send_numeric(replies.ERR_NONICKNAMEGIVEN, "No nickname given")
            return

        target_nick = msg.params[0]
        target = self.server.get_client(target_nick)
        if not target:
            await self.send_numeric(replies.ERR_NOSUCHNICK, target_nick, "No such nick/channel")
            await self.send_numeric(replies.RPL_ENDOFWHOIS, target_nick, "End of WHOIS list")
            return

        await self.send_numeric(
            replies.RPL_WHOISUSER,
            target.nick,
            target.user or "*",
            target.host,
            "*",
            target.realname or "",
        )
        server_name = (
            target.server_name if isinstance(target, RemoteClient) else self.server.config.name
        )
        await self.send_numeric(
            replies.RPL_WHOISSERVER,
            target.nick,
            server_name,
            "culture",
        )
        if target.channels:
            chan_list = " ".join(f"{ch.get_prefix(target)}{ch.name}" for ch in target.channels)
            await self.send_numeric(replies.RPL_WHOISCHANNELS, target.nick, chan_list)
        await self.send_numeric(replies.RPL_ENDOFWHOIS, target.nick, "End of WHOIS list")

    async def _handle_quit(self, msg: Message) -> None:
        reason = msg.params[0] if msg.params else "Quit"
        quit_msg = Message(prefix=self.prefix, command="QUIT", params=[reason])

        notified: set[Client] = set()
        channel_names = [ch.name for ch in self.channels]
        # Bot-CAP clients skip the broadcast — symmetrical with the silent
        # JOIN/PART behaviour. The user.quit event still fires below.
        if BOT_CAP not in self.caps:
            for channel in [*self.channels]:
                for member in [*channel.members]:
                    if member is not self and member not in notified:
                        await member.send(quit_msg)
                        notified.add(member)

        if self._registered:
            # A pre-registration QUIT has no proven identity: emitting a QUIT
            # event for the requested nick would reach peers as SQUITUSER and
            # drop the real (possibly account) session there (culture#506 h1).
            await self.server.emit_event(
                Event(
                    type=EventType.QUIT,
                    channel=None,
                    nick=self.nick,
                    data={"reason": reason, "channels": channel_names},
                )
            )

        raise ConnectionError("Client quit")

    # --- Bot extension verbs (9.5.0; BACKFILL added in task t8) ---
    # Spec: docs/superpowers/specs/2026-05-01-bot-extension-api-design.md
    # § Decision B (EVENTSUB/EVENTUNSUB) and § Decision E (EVENTPUB).
    # docs/extension-api.md's "Recovering with BACKFILL" section documents
    # the client-facing BACKFILL verb this same gate now also covers.
    # All four require:
    #   1. The ``agentirc.io/bot`` capability — without it, the server
    #      replies ``EVENTERR <id> :bot-capability-required``.
    #   2. A registered connection (post-NICK/USER) — without it, the
    #      server replies ``EVENTERR <id> :not-registered``. This guards
    #      against anonymous sockets injecting events with ``nick=None``
    #      or pulling the full event stream before claiming an identity.

    _SUB_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,32}$")

    # Reserved sub-id token BACKFILL-replayed ``EVENT`` lines carry in the
    # ``<sub-id>`` slot — BACKFILL isn't tied to any live subscription, so
    # there's no real sub-id to put there. Lets a bot's existing EVENT
    # parser handle replay lines unmodified while still being able to tell
    # "this is a replay" from "this is live" by checking
    # ``sub_id == _BACKFILL_EVENT_SUB_ID``. See ``_handle_backfill``.
    _BACKFILL_EVENT_SUB_ID = "backfill"

    async def _bot_verb_gate(self, verb_id: str) -> bool:
        """Common bot-CAP + registration gate for EVENTSUB/EVENTUNSUB/EVENTPUB/BACKFILL.

        Sends the appropriate ``EVENTERR`` and returns ``False`` on
        rejection; returns ``True`` if the caller may proceed.
        """
        if BOT_CAP not in self.caps:
            await self.send_raw(f"{EVENTERR} {verb_id} :bot-capability-required")
            return False
        if not self._registered:
            await self.send_raw(f"{EVENTERR} {verb_id} :not-registered")
            return False
        return True

    @staticmethod
    def _parse_eventsub_filters(tokens: list[str]) -> tuple[dict, str | None]:
        """Parse EVENTSUB filter tokens. Returns (filters, error_reason).

        ``error_reason`` is ``None`` on success; otherwise a string suitable
        for ``EVENTERR <sub-id> :<error_reason>``. Detects unknown filter
        keys, duplicate filter keys (per spec, each parameter appears at
        most once), malformed tokens, and invalid channel-filter format.
        Per-key validation lives in
        :data:`agentirc._internal.event_subscriptions.FILTER_HANDLERS`.
        """
        from agentirc._internal.event_subscriptions import (
            CHANNEL_ANY,
            FILTER_HANDLERS,
        )

        filters: dict = {"type_glob": "*", "channel": CHANNEL_ANY, "nick_glob": "*"}
        seen: set[str] = set()
        for token in tokens:
            if "=" not in token:
                return filters, f"invalid-filter {token}"
            key, value = token.split("=", 1)
            if key in seen:
                return filters, f"duplicate-filter {key}"
            seen.add(key)
            handler = FILTER_HANDLERS.get(key)
            if handler is None:
                return filters, f"unknown-filter {key}"
            error = handler(filters, value)
            if error is not None:
                return filters, error
        return filters, None

    async def _handle_eventsub(self, msg: Message) -> None:
        sub_id = msg.params[0] if msg.params else "?"
        if not await self._bot_verb_gate(sub_id):
            return
        if not msg.params:
            await self.send_raw(f"{EVENTERR} ? :missing-sub-id")
            return
        if not self._SUB_ID_RE.match(sub_id):
            await self.send_raw(f"{EVENTERR} {sub_id} :invalid-sub-id")
            return

        filters, error = self._parse_eventsub_filters(msg.params[1:])
        if error is not None:
            await self.send_raw(f"{EVENTERR} {sub_id} :{error}")
            return

        sub = self.server.subscription_registry.add(self, sub_id, **filters)
        if sub is None:
            await self.send_raw(f"{EVENTERR} {sub_id} :sub-id-in-use")

    async def _handle_eventunsub(self, msg: Message) -> None:
        sub_id = msg.params[0] if msg.params else "?"
        if not await self._bot_verb_gate(sub_id):
            return
        if not msg.params:
            return
        # Silent on success — no more EVENT lines for this sub-id is the signal.
        self.server.subscription_registry.remove(self, sub_id)

    async def _handle_eventpub(self, msg: Message) -> None:
        type_str = msg.params[0] if msg.params else "?"
        if not await self._bot_verb_gate(type_str):
            return
        if len(msg.params) < 3:
            await self.send_raw(f"{EVENTERR} {type_str} :invalid-eventpub-syntax")
            return
        type_str, target, b64 = msg.params[0], msg.params[1], msg.params[2]
        if not EVENT_TYPE_RE.match(type_str):
            await self.send_raw(f"{EVENTERR} {type_str} :invalid-type")
            return

        channel = None if target == "*" else target
        if channel is None and not self._may_publish_serverwide():
            # Channel-less EVENTPUB fans out server-wide (agentirc#66 review).
            await self.send_raw(f"{EVENTERR} {type_str} :permission-denied")
            return
        if channel is not None and channel not in self.server.channels:
            await self.send_raw(f"{EVENTERR} {type_str} :no-such-channel")
            return
        if channel is not None and not self.can_speak(channel):
            # Publishing into a channel is speaking there (culture#506 h1).
            await self.send_raw(f"{EVENTERR} {type_str} :permission-denied")
            return

        # ``base64.b64decode`` can raise ``binascii.Error`` (a subclass of
        # ``ValueError``) for malformed input AND a few unrelated errors
        # for non-ASCII; ``json.loads`` raises ``json.JSONDecodeError`` (a
        # subclass of ``ValueError``). The unhandled-exception path would
        # tear down the connection task, so catch broadly.
        try:
            data = json.loads(base64.b64decode(b64))
        except Exception:
            await self.send_raw(f"{EVENTERR} {type_str} :invalid-payload")
            return
        if not isinstance(data, dict):
            await self.send_raw(f"{EVENTERR} {type_str} :invalid-payload")
            return

        # Strip `_`-prefixed keys (server-internal metadata like `_render`,
        # `_origin`). nick and timestamp are server-set so bots cannot spoof
        # the actor or wall-clock — peers across federation see consistent
        # values. ``self.nick`` is guaranteed non-None by the registration
        # gate above, but coerce defensively.
        data = {k: v for k, v in data.items() if not k.startswith("_")}

        ev = Event(
            type=type_str,
            channel=channel,
            nick=self.nick or "",
            data=data,
            timestamp=time.time(),
        )
        await self.server.emit_event(ev)

    # Account roles allowed to EVENTPUB without a channel (server-wide).
    _SERVERWIDE_PUBLISH_ROLES = frozenset({"operator", "resident"})

    def _may_publish_serverwide(self) -> bool:
        """May this session EVENTPUB to ``*`` (agentirc#66 review)?

        Sessions without an account: yes, as before. Account sessions: only
        an active ``operator`` or ``resident`` account; a ``guest``, or a
        session whose account is suspended, revoked or gone, is refused.
        """
        if self.account is None:
            return True
        return self.account_role() in self._SERVERWIDE_PUBLISH_ROLES

    async def _handle_backfill(self, msg: Message) -> None:
        """``BACKFILL <channel-or-*> <cursor-or-*> [limit]`` (task t8).

        Makes good on the recovery path ``docs/extension-api.md``'s
        Backpressure section promises: after an
        ``EVENTERR <sub-id> :backpressure-overflow``, a bot re-subscribes
        and issues ``BACKFILL`` to catch up on what it missed. Gated
        identically to ``EVENTSUB``/``EVENTUNSUB``/``EVENTPUB`` via
        ``_bot_verb_gate`` — the ``EVENTERR`` reply's second token echoes
        back whatever ``<channel-or-*>`` was sent, mirroring how
        ``_handle_eventpub`` echoes back ``<type>``.

        Only stored ``message`` events (ordinary channel ``PRIVMSG``s) are
        replayed — lifecycle entries (``user.join``, ``topic``, …) that also
        land in the history store are skipped. They're identified by their
        synthetic ``system-``-prefixed nick (see
        ``HistorySkill.on_event``); a real client nick can never start with
        that prefix (``_handle_nick`` rejects it at registration time), so
        this is an exact filter, not a heuristic.

        Replayed events reuse the ``EVENT`` wire shape with the reserved
        sub-id token ``_BACKFILL_EVENT_SUB_ID`` (never a live subscription
        id) so a bot's existing ``EVENT`` parser handles them without a new
        code path.

        ``<channel-or-*>`` is either an exact, currently-existing channel
        name (``self.server.channels``) or the literal ``*`` meaning "every
        channel I'm currently joined to" (``self.channels``). DM history
        (the internal ``@dm:...`` history-store keys — see
        ``agentirc.skills.history``'s module docstring) is never resolved or
        reachable through this verb at any spelling: only ``#``-prefixed
        names are ever looked up, so a bare nick or a directly-named
        ``@dm:...`` target both fail the ``#``-prefix check and get
        ``no-such-channel``, exactly like a channel that never existed.

        ``<cursor-or-*>`` reuses the exact ``HISTORY SINCE`` cursor codec
        (``agentirc.skills.history.decode_since_cursor``/
        ``encode_since_cursor``) so a bot can track one cursor concept
        across both verbs.

        A page may legitimately replay zero ``EVENT`` lines while the
        terminator's cursor still advances (e.g. a page consisting entirely
        of skipped lifecycle entries) — this mirrors ``HISTORY SINCE``'s own
        paging semantics; callers loop on the terminator's cursor until an
        empty page whose cursor stops advancing.
        """
        channel_param = msg.params[0] if msg.params else "?"
        if not await self._bot_verb_gate(channel_param):
            return
        if len(msg.params) < 2:
            await self.send_raw(f"{EVENTERR} {channel_param} :missing-params")
            return
        cursor_token = msg.params[1]

        from agentirc.skills.history import (
            DEFAULT_SINCE_LIMIT,
            decode_since_cursor,
            encode_since_cursor,
        )

        limit = DEFAULT_SINCE_LIMIT
        if len(msg.params) >= 3:
            try:
                limit = int(msg.params[2])
            except ValueError:
                await self.send_raw(f"{EVENTERR} {channel_param} :invalid-count")
                return
            if limit < 0:
                await self.send_raw(f"{EVENTERR} {channel_param} :invalid-count")
                return

        try:
            after = decode_since_cursor(cursor_token)
        except ValueError:
            await self.send_raw(f"{EVENTERR} {channel_param} :invalid-cursor")
            return

        channels = await self._resolve_backfill_channels(channel_param)
        if channels is None:
            return

        merged = self._collect_backfill_entries(channels, after, limit)
        await self._emit_backfill_entries(merged)

        if merged:
            last_entry = merged[-1][1]
            next_cursor = encode_since_cursor(last_entry.timestamp, last_entry.id)
        elif after is None:
            next_cursor = "*"
        else:
            next_cursor = cursor_token

        server_name = self.server.config.name
        await self.send_raw(f":{server_name} {BACKFILLEND} {channel_param} {next_cursor}")

    async def _resolve_backfill_channels(self, channel_param: str) -> list[str] | None:
        """Resolve ``BACKFILL``'s ``<channel-or-*>`` into a channel-name list.

        Extracted from ``_handle_backfill`` (S3776 cognitive-complexity fix).
        ``"*"`` means "every channel I'm currently joined to"
        (``self.channels``); anything else must be an exact,
        currently-existing ``#``-prefixed channel name
        (``self.server.channels``) — see ``_handle_backfill``'s docstring
        for why DM history is never reachable here. On an invalid name,
        sends the ``EVENTERR ... :no-such-channel`` reply itself and
        returns ``None`` so the caller knows to stop.
        """
        if channel_param == "*":
            # Joined channels the session can still read: a grant removed
            # while joined stops the replay (culture#506 h1).
            return sorted(ch.name for ch in self.channels if self.can_read(ch.name))
        if (
            not channel_param.startswith("#")
            or channel_param not in self.server.channels
            or not self.can_read(channel_param)
        ):
            # No read grant is reported exactly like a missing channel, so
            # an account cannot probe which channels exist.
            await self.send_raw(f"{EVENTERR} {channel_param} :no-such-channel")
            return None
        return [channel_param]

    def _collect_backfill_entries(
        self, channels: list[str], after: tuple[float, int] | None, limit: int
    ) -> list[tuple[str, "HistoryEntry"]]:
        """Fetch and merge per-channel history entries for ``BACKFILL``.

        Extracted from ``_handle_backfill`` (S3776 cognitive-complexity fix).
        Per-channel fetch capped at `limit` each, then merged and
        re-truncated to a global `limit` — a standard bounded k-way merge:
        since the final result is at most `limit` entries wide, no single
        channel can contribute more than `limit` of them, so fetching up
        to `limit` from every channel independently can never short-change
        the merge. The next-cursor is derived from this *raw* (pre-filter)
        truncated batch, exactly mirroring HISTORY SINCE's own
        last-entry-in-the-page cursor derivation, so pagination stays
        non-overlapping and exactly-once even though some raw entries are
        skipped (not emitted) by ``_emit_backfill_entries`` for being
        lifecycle, not message.
        """
        from agentirc.skills.history import HistorySkill

        history_skill = self.server.get_skill_for_command("HISTORY")
        merged: list[tuple[str, "HistoryEntry"]] = []
        if isinstance(history_skill, HistorySkill):
            for ch in channels:
                for entry in history_skill.get_since(ch, after, limit):
                    merged.append((ch, entry))
        merged.sort(key=lambda pair: (pair[1].timestamp, pair[1].id))
        return merged[:limit]

    async def _emit_backfill_entries(self, merged: list[tuple[str, "HistoryEntry"]]) -> None:
        """Replay ``merged`` history entries as ``EVENT`` lines for ``BACKFILL``.

        Extracted from ``_handle_backfill`` (S3776 cognitive-complexity fix).
        Only stored ``message`` events are replayed — lifecycle entries
        (identified by their synthetic ``system-``-prefixed nick, see
        ``HistorySkill.on_event``) are skipped. See ``_handle_backfill``'s
        docstring for the full rationale and the reserved
        ``_BACKFILL_EVENT_SUB_ID`` wire shape.
        """
        from agentirc.ircd import IRCd

        server_name = self.server.config.name
        for ch, entry in merged:
            if entry.nick.startswith(SYSTEM_USER_PREFIX):
                continue
            data = {"text": entry.text}
            if entry.msgid is not None:
                data["msgid"] = entry.msgid
            event = Event(
                type=EventType.MESSAGE,
                channel=ch,
                nick=entry.nick,
                data=data,
                timestamp=entry.timestamp,
            )
            envelope = IRCd._build_event_envelope(event)
            encoded = IRCd._encode_event_data(envelope, EventType.MESSAGE.value)
            await self.send_raw(
                f":{server_name} {EVENT} {self._BACKFILL_EVENT_SUB_ID} "
                f"{EventType.MESSAGE.value} {ch} {entry.nick} :{encoded}"
            )

    # --- Runtime verb discovery (task t9) ---
    # Unlike the bot-extension verbs above, ``VERBS`` needs no ``BOT_CAP`` --
    # discovery serves plain agents too, not just capability-negotiated bots.

    # Real IRC command tokens are letters only (RFC 2812 §2.3.1:
    # ``command = 1*letter / 3digit``, and the 3-digit form is a numeric
    # reply, never client-issued) -- so this is a protocol-grammar filter,
    # not a hand-picked exclusion list. It exists because `_dispatch`'s
    # ``getattr(self, f"_handle_{msg.command.lower()}")`` would also
    # resolve `_handle_channel_mode`/`_handle_user_mode` if a client sent
    # the literal (nonsensical) commands "CHANNEL_MODE"/"USER_MODE" --
    # those are `_handle_mode`'s internal routing targets, not verbs any
    # real client is meant to address directly, and their underscored
    # names fail this shape check.
    _VERB_TOKEN_RE = re.compile(r"^[A-Z]+$")

    def _live_verbs(self) -> list[str]:
        """Enumerate every wire verb the running server actually dispatches.

        Reads the same two structures ``_dispatch`` itself consults, so
        this list can never drift from what the server really does:

        - Every ``Client._handle_<verb>`` method, via the exact
          ``getattr(self, f"_handle_{msg.command.lower()}")`` convention
          ``_dispatch`` uses -- filtered through ``_VERB_TOKEN_RE`` (see
          its comment) to drop the two internal MODE-routing helpers that
          share the naming convention but aren't real verbs.
        - Every verb a registered skill claims via its ``commands`` set
          (``IRCd.get_skill_for_command``'s own lookup structure).

        Nothing here is a hand-maintained list -- add a new
        ``_handle_frob`` method or register a new skill and the very next
        ``VERBS`` query reflects it, no edit to this method required.

        ``PASS`` is deliberately absent: on this server it's consumed by
        ``IRCd._handle_connection``'s S2S/C2S sniff before a ``Client``
        even exists (this server repurposes RFC 2812's client-registration
        ``PASS`` as the S2S-link auth handshake instead) -- a live
        ``Client`` has no ``_handle_pass`` and no skill claims it, so
        listing it here would violate the "every listed verb is genuinely
        dispatchable" guarantee ``_handle_verbs`` makes.
        """
        verbs = {
            name[len("_handle_") :].upper()
            for name in dir(type(self))
            if name.startswith("_handle_")
        }
        verbs = {v for v in verbs if self._VERB_TOKEN_RE.match(v)}
        for skill in self.server.skills:
            verbs.update(skill.commands)
        return sorted(verbs)

    async def _handle_verbs(self, _msg: Message) -> None:
        """``VERBS`` (task t9): runtime verb-discovery query.

        Any *registered* client may issue this -- no ``agentirc.io/bot``
        capability required, unlike ``EVENTSUB``/``EVENTPUB``/``BACKFILL``.
        An unregistered connection gets the same silent no-op every other
        pre-registration verb gets on this server (see ``_handle_join``):
        no reply, connection stays open.

        Reply is a single line::

            :<server> VERBS <version> :<base64-json>

        mirroring the ``EVENT``/``EVENTPUB`` base64-canonical-JSON wire
        pattern (canonical = keys sorted, ``","``/``":"`` separators,
        UTF-8; see ``docs/extension-api.md``). ``<version>`` is
        ``VERBS_DISCOVERY_VERSION`` -- the reply *format's* version,
        independent of the ``error_tokens_version`` and ``server_version``
        fields carried inside the payload.

        Payload::

            {
                "verbs": [...],               # sorted; see _live_verbs
                "caps": [...],                 # sorted Client._SUPPORTED_CAPS
                "error_tokens_version": int,   # protocol.ERROR_TOKENS_VERSION
                "server_version": str,         # agentirc.__version__
            }

        ``verbs`` is derived live from the actual dispatch surface (never
        a hardcoded list) -- see ``_live_verbs`` for the enumeration
        mechanism and why ``PASS`` is deliberately excluded. ``VERBS``
        itself always appears in its own ``verbs`` list, since this
        handler is discovered the same way as any other.
        """
        if not self._registered:
            return

        from agentirc import __version__ as _agentirc_version

        payload = {
            "verbs": self._live_verbs(),
            "caps": sorted(self._SUPPORTED_CAPS),
            "error_tokens_version": ERROR_TOKENS_VERSION,
            "server_version": _agentirc_version,
        }
        encoded = base64.b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii")
        server_name = self.server.config.name
        await self.send_raw(f":{server_name} {VERBS} {VERBS_DISCOVERY_VERSION} :{encoded}")
