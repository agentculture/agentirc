"""PRESENCE skill -- resident presence heartbeats + per-nick registry.

Wire contract (authoritative): ``docs/protocol/extensions/presence.md``
(culture@b69705e), summarized in
``docs/specs/2026-07-07-agentirc-now-speaks-presence-the-ircd-parses-resid.md``.

Task t3 scope (this module): publish parsing (``PRESENCE :<json>``), the
in-memory per-nick registry with latest-wins updates, server-side
``last_refresh`` stamping, and offline retention on disconnect. The query
surface (``PRESENCE LIST`` / ``PRESENCELIST`` / ``PRESENCEEND``, and the
read-time ``presumed_hung`` computation) is task t4 -- see the ``TODO(t4)``
marker below. Federation (emitting ``Event(type=EventType.PRESENCE)`` through
``IRCd.emit_event`` and reacting to ``SERVER_LINK``/``SERVER_UNLINK``) is
task t5.

Publish is fire-and-forget: the contract defines **no** reply/ack line, ever
-- neither on success nor on failure. Invalid payloads are dropped silently
(logged at debug/warning) and never crash the connection. Presence state and
token counters are observe-only: nothing in this module gates, defers, or
rejects any command, message, or connection.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentirc.skill import Event, EventType, Skill

if TYPE_CHECKING:
    from agentirc.client import Client
    from agentirc._internal.protocol.message import Message

logger = logging.getLogger(__name__)

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
    server-side on every accepted publish or offline flip -- rendering it
    (and ``since``) to ISO-8601 UTC strings for the wire is task t4's job
    (``PRESENCE LIST`` / ``PRESENCELIST``), not this module's.
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
            # TODO(t4): implement the PRESENCE LIST query reply --
            # one `PRESENCELIST :<json>` line per resident (all nine keys,
            # presumed_hung computed at read time) followed by
            # `PRESENCEEND :End of presence list`. Until t4 lands, `PRESENCE
            # LIST` is a silent no-op (never a crash, never a reply) --
            # exactly like an unrecognized publish payload.
            return

        self._handle_publish(client, first)

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
        if not isinstance(since, str) or not since:
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
