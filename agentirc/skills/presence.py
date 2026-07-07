"""PRESENCE skill -- resident presence heartbeats + per-nick registry.

Wire contract (authoritative): ``docs/protocol/extensions/presence.md``
(culture@b69705e), summarized in
``docs/specs/2026-07-07-agentirc-now-speaks-presence-the-ircd-parses-resid.md``.

Task t3 scope: publish parsing (``PRESENCE :<json>``), the in-memory
per-nick registry with latest-wins updates, server-side ``last_refresh``
stamping, and offline retention on disconnect.

Task t4 scope (this module, added on top of t3): the query surface
(``PRESENCE LIST`` -> one ``PRESENCELIST :<json>`` line per resident,
nick-sorted, followed by exactly one ``PRESENCEEND`` terminator) and the
read-time ``presumed_hung`` computation -- see ``_handle_list`` and
``_presumed_hung`` below. This is a coordination contract with the culture
repo (``culture_core/resource_view.py``'s ``_query_presence_wire``): the
wire shape must stay byte-compatible. Federation (emitting
``Event(type=EventType.PRESENCE)`` through ``IRCd.emit_event`` and reacting
to ``SERVER_LINK``/``SERVER_UNLINK``) is task t5.

Publish is fire-and-forget: the contract defines **no** reply/ack line, ever
-- neither on success nor on failure. Invalid payloads are dropped silently
(logged at debug/warning) and never crash the connection. ``PRESENCE LIST``
is the one exception -- it always replies -- but it is strictly a read: it
never mutates the registry (no last_refresh bump, no state change). Presence
state and token counters are observe-only: nothing in this module gates,
defers, or rejects any command, message, or connection.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agentirc.skill import Event, EventType, Skill

if TYPE_CHECKING:
    from agentirc.client import Client
    from agentirc._internal.protocol.message import Message

logger = logging.getLogger(__name__)

# The four "busy" states the stale-busy watchdog can flag. `idle` and
# `offline` are never flagged, no matter how old `last_refresh` is -- an
# idle resident is expected to go quiet, and an offline row already reflects
# a known disconnect, not a hang.
_BUSY_STATES = frozenset({"listening", "thinking", "working", "draining"})

# Six-state enum per the wire contract. "offline" is a valid client-published
# state (a resident may announce its own shutdown) as well as the state the
# server stamps implicitly on disconnect (see PresenceSkill.on_event).
_VALID_STATES = frozenset(
    {"idle", "listening", "thinking", "working", "draining", "offline"}
)

# Task text is capped, not rejected -- an over-length task is truncated to
# exactly this many characters rather than dropping the whole update.
_TASK_MAX_LEN = 128

# Disconnect-shaped events this skill reacts to. `EventType.QUIT` fires on an
# explicit client QUIT. `EventType.AGENT_DISCONNECT` / `EventType.CONSOLE_CLOSE`
# fire on a plain TCP close (no QUIT) for clients that negotiated the `+A`
# (agent) or `+C` (console) user mode -- see `IRCd._emit_disconnect_events`
# in agentirc/ircd.py. A client with neither an explicit QUIT nor an `+A`/`+C`
# mode that simply drops the TCP connection currently emits no event at all
# on this codebase's disconnect path (`IRCd._remove_client` only fires those
# two mode-gated events); flipping such a row to offline would need a new,
# unconditional disconnect hook in `IRCd._remove_client`, which is out of
# scope for this task (ircd.py is registration-only here). See the
# docstring on `PresenceSkill.on_event` for the full analysis.
_DISCONNECT_EVENT_TYPES = frozenset(
    {EventType.QUIT, EventType.AGENT_DISCONNECT, EventType.CONSOLE_CLOSE}
)


@dataclass
class PresenceRecord:
    """Latest known presence for one nick.

    ``last_refresh`` is always a raw epoch float (``time.time()``) stamped
    server-side on every accepted publish or offline flip; ``_handle_list``
    renders it (and computes ``presumed_hung``) at read time for the
    ``PRESENCELIST`` wire reply -- ``since`` is passed through verbatim as
    given by the client.
    """

    state: str
    since: str
    task: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    last_refresh: float = 0.0
    server: str = ""


class PresenceSkill(Skill):
    name = "presence"
    commands = {"PRESENCE"}

    def __init__(self) -> None:
        # nick -> PresenceRecord. Public (not `_registry`) so tests -- and any
        # future in-process embedder -- can inspect it directly via the
        # server's `skills` list, per the retention contract: keyed by nick,
        # overwritten on reconnect/republish, cleared only by server restart
        # (no TTL/prune in v1).
        self.registry: dict[str, PresenceRecord] = {}

    def get_record(self, nick: str) -> PresenceRecord | None:
        """Look up the latest presence record for *nick*, if any."""
        return self.registry.get(nick)

    async def on_command(self, client: Client, msg: Message) -> None:
        if msg.command != "PRESENCE":
            return
        if not msg.params:
            logger.debug("presence: PRESENCE with no params from %s", client.nick)
            return

        first = msg.params[0]
        if first.upper() == "LIST":
            # Subcommand match is case-insensitive (`PRESENCE list` works
            # too) and tolerates any extra params (`PRESENCE LIST EXTRA
            # JUNK` still answers) -- the dispatcher only uppercases the
            # VERB (`PRESENCE`), not subcommand text, so that's handled here.
            await self._handle_list(client)
            return

        self._handle_publish(client, first)

    async def _handle_list(self, client: Client) -> None:
        """Reply to `PRESENCE LIST`: one `PRESENCELIST` line per resident,
        nick-sorted, followed by exactly one `PRESENCEEND` terminator.

        Pure read: iterates the registry and serializes each row with the
        current wall-clock time for the `presumed_hung` computation, but
        never writes back to any `PresenceRecord` -- a `PRESENCE LIST` never
        bumps `last_refresh` or otherwise mutates state. No CAP requirement;
        any registered client (even one that has never published) may query.
        """
        server_name = self.server.config.name
        now = time.time()
        for nick in sorted(self.registry):
            row = self._serialize_row(nick, self.registry[nick], now)
            await client.send_raw(f":{server_name} PRESENCELIST :{row}")
        await client.send_raw(f":{server_name} PRESENCEEND :End of presence list")

    def _serialize_row(self, nick: str, record: PresenceRecord, now: float) -> str:
        """Render one registry row as the compact single-line JSON payload.

        Key order is fixed (nick, server, state, since, task, tokens_in,
        tokens_out, presumed_hung, last_refresh) -- a stable shape culture's
        parser depends on. Compact separators keep the line small; the
        payload is embedded in the reply via an explicit `:` marker (see
        `_handle_list`) so the trailing param is unambiguous regardless of
        whether the JSON itself happens to contain a space (e.g. inside
        `task`).
        """
        payload = {
            "nick": nick,
            "server": record.server,
            "state": record.state,
            "since": record.since,
            "task": record.task,
            "tokens_in": record.tokens_in,
            "tokens_out": record.tokens_out,
            "presumed_hung": self._presumed_hung(record, now),
            "last_refresh": self._format_last_refresh(record.last_refresh),
        }
        return json.dumps(payload, separators=(",", ":"))

    def _presumed_hung(self, record: PresenceRecord, now: float) -> bool:
        """True iff *record* is in a busy state and hasn't refreshed in time.

        Computed at read time (no background sweep task): a resident whose
        last-known state is one of the four "busy" states AND whose
        `last_refresh` is more than `stale_after_seconds` in the past is
        presumed hung. Strictly greater-than -- a resident refreshing
        exactly on the `stale_after` boundary is NOT flagged. `idle` and
        `offline` rows are never flagged, no matter how stale.
        """
        if record.state not in _BUSY_STATES:
            return False
        stale_after = self.server.config.presence.stale_after_seconds
        return (now - record.last_refresh) > stale_after

    @staticmethod
    def _format_last_refresh(epoch_seconds: float) -> str:
        """Render a raw `time.time()` epoch float as ISO-8601 UTC, second precision."""
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _handle_publish(self, client: Client, raw: str) -> None:
        """Parse and apply one `PRESENCE :<json>` publish. Never replies.

        Any validation failure drops the update silently (debug/warning log
        only) -- the wire contract defines no error reply for publish, and a
        malformed heartbeat must never take the connection down.
        """
        nick = client.nick
        if not nick:
            logger.debug("presence: publish from unregistered connection, dropping")
            return

        if not raw:
            logger.debug("presence: empty payload from %s", nick)
            return

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug("presence: malformed JSON from %s: %r", nick, raw)
            return

        if not isinstance(data, dict):
            logger.debug("presence: payload from %s is not a JSON object: %r", nick, raw)
            return

        state = data.get("state")
        if not isinstance(state, str) or state not in _VALID_STATES:
            logger.warning("presence: invalid/missing state from %s: %r", nick, state)
            return

        since = data.get("since")
        if not isinstance(since, str) or not since.strip():
            logger.warning("presence: missing/invalid since from %s: %r", nick, since)
            return

        task = data.get("task")
        if task is not None:
            if not isinstance(task, str):
                logger.debug("presence: invalid task type from %s: %r", nick, task)
                return
            if len(task) > _TASK_MAX_LEN:
                task = task[:_TASK_MAX_LEN]

        tokens_in = data.get("tokens_in")
        if not self._valid_optional_count(tokens_in):
            logger.debug("presence: invalid tokens_in from %s: %r", nick, tokens_in)
            return

        tokens_out = data.get("tokens_out")
        if not self._valid_optional_count(tokens_out):
            logger.debug("presence: invalid tokens_out from %s: %r", nick, tokens_out)
            return

        # Latest-wins: fully replaces any previous record for this nick.
        self.registry[nick] = PresenceRecord(
            state=state,
            since=since,
            task=task,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            last_refresh=time.time(),
            server=self.server.config.name,
        )

    @staticmethod
    def _valid_optional_count(value: object) -> bool:
        """True if *value* is either absent (None) or a non-negative int.

        Rejects bool explicitly -- `isinstance(True, int)` is True in Python,
        but a JSON `true`/`false` is not a valid token count.
        """
        if value is None:
            return True
        if isinstance(value, bool):
            return False
        return isinstance(value, int) and value >= 0

    async def on_event(self, event: Event) -> None:
        """Flip a nick's row to offline on disconnect. Retains the row.

        Reacts to `EventType.QUIT` (explicit client QUIT) and to
        `EventType.AGENT_DISCONNECT` / `EventType.CONSOLE_CLOSE` (which fire
        for `+A`/`+C`-moded clients on a plain TCP close, per
        `IRCd._emit_disconnect_events`) -- see the module-level
        `_DISCONNECT_EVENT_TYPES` docstring for why a mode-less client that
        drops the socket without an explicit QUIT currently emits nothing
        this skill can observe.

        Guarded to local-relevance only (task t5 handles federation): a
        federated event forwarded from a peer carries an `_origin` tag in
        `event.data`, and offline attribution across links is t5's job, not
        this task's.
        """
        if event.type not in _DISCONNECT_EVENT_TYPES:
            return
        if event.data.get("_origin"):
            return

        nick = event.nick
        record = self.registry.get(nick)
        if record is None:
            # Never published presence -- nothing to flip, no row to create.
            return

        record.state = "offline"
        record.task = None
        record.last_refresh = time.time()
        # `since` and `server` intentionally untouched here (retention keeps
        # the row as-is aside from state/task/last_refresh); tokens_in/
        # tokens_out are likewise kept per the retention contract.
