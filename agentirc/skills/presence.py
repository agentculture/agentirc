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
wire shape must stay byte-compatible.

Task t5 scope (this module, added on top of t4): federation. Presence rides
the existing generic event bus -- no new S2S verb, no hop counts. Every
accepted local publish and every local offline flip (QUIT/disconnect) emits
``Event(type=EventType.PRESENCE)`` via ``self.server.emit_event`` (see
``_emit_presence_update``); ``IRCd.emit_event`` relays any event without an
``_origin`` tag to every linked peer through the generic ``SEVENT`` fallback,
which is the whole propagation mechanism. On receipt, ``on_event`` upserts
the registry keyed by nick, attributing ``server`` to the tamper-resistant
``_origin`` tag ``_handle_sevent`` stamps (see ``agentirc/server_link.py``)
rather than the peer-supplied ``data["server"]``. Loop prevention comes free:
``emit_event`` never re-relays an ``_origin``-tagged event, and this module
never re-emits from the federated-receive path. ``EventType.SERVER_LINK``
triggers a re-emit of every local row (burst-on-link, so a newly-linked peer
learns pre-link state); ``EventType.SERVER_UNLINK`` flips every row
attributed to the departed server to offline. ``presence.update`` is also
added to ``NO_SURFACE_EVENT_TYPES`` (``agentirc/events.py``) so 30s
heartbeats never spam ``#system``.

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
    from agentirc._internal.protocol.message import Message
    from agentirc.client import Client

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

# `since` is an ISO-8601 UTC timestamp (~20 chars); cap it generously so a
# hostile or buggy client can't publish a multi-kilobyte value that would
# (a) blow the wire contract's <=512-byte PRESENCELIST line assumption and
# (b) propagate verbatim to every linked peer via the federated event. Like
# `task`, over-length is truncated, not rejected.
_SINCE_MAX_LEN = 64

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

        await self._handle_publish(client, first)

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

    async def _handle_publish(self, client: Client, raw: str) -> None:
        """Parse and apply one `PRESENCE :<json>` publish. Never replies.

        Any validation failure drops the update silently (debug/warning log
        only) -- the wire contract defines no error reply for publish, and a
        malformed heartbeat must never take the connection down.

        On an accepted publish, federates the row to any linked peers by
        emitting a `presence.update` event (see `_emit_presence_update`) --
        task t5.
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
        # json.JSONDecodeError subclasses ValueError
        except (TypeError, ValueError):
            logger.debug("presence: malformed JSON from %s: %r", nick, raw)
            return

        if not isinstance(data, dict):
            logger.debug(
                "presence: payload from %s is not a JSON object: %r", nick, raw
            )
            return

        fields = self._validate_fields(data)
        if fields is None:
            logger.warning("presence: invalid publish payload from %s: %r", nick, data)
            return

        # Latest-wins: fully replaces any previous record for this nick.
        record = PresenceRecord(
            **fields,
            last_refresh=time.time(),
            server=self.server.config.name,
        )
        self.registry[nick] = record
        await self._emit_presence_update(nick, record)

    @classmethod
    def _validate_fields(cls, data: dict) -> dict | None:
        """Validate + normalize the presence fields shared by the local publish
        and federated-ingest paths.

        Returns a dict of ``{state, since, task, tokens_in, tokens_out}`` ready
        to splat into ``PresenceRecord``, or ``None`` if any field is invalid
        (caller drops the update). Both the local `PRESENCE :<json>` publish
        (`_handle_publish`) and the federated `presence.update` ingest
        (`_on_presence_update`) run this, so a malformed row a peer relays is
        rejected exactly as a malformed local publish is -- a federated update
        must never inject a state outside the six-value enum, a non-string /
        oversized `task`, an oversized `since`, or a negative/non-int token
        count into the registry (and thence into culture's parser).

        `state` (enum) and `since` (non-empty string) are required; `task` is
        truncated to `_TASK_MAX_LEN`, `since` to `_SINCE_MAX_LEN`; token counts
        are optional non-negative ints (bool rejected).
        """
        state = data.get("state")
        if not isinstance(state, str) or state not in _VALID_STATES:
            return None

        since = data.get("since")
        if not isinstance(since, str) or not since.strip():
            return None
        if len(since) > _SINCE_MAX_LEN:
            since = since[:_SINCE_MAX_LEN]

        task = data.get("task")
        if task is not None:
            if not isinstance(task, str):
                return None
            if len(task) > _TASK_MAX_LEN:
                task = task[:_TASK_MAX_LEN]

        tokens_in = data.get("tokens_in")
        if not cls._valid_optional_count(tokens_in):
            return None

        tokens_out = data.get("tokens_out")
        if not cls._valid_optional_count(tokens_out):
            return None

        return {
            "state": state,
            "since": since,
            "task": task,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }

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
        """React to disconnects, federated presence updates, and link topology.

        Four independent event families, dispatched by `event.type`:

        - `EventType.QUIT` / `AGENT_DISCONNECT` / `CONSOLE_CLOSE`: flip a
          local nick's row to offline and re-emit the update (task t5) so
          linked peers learn of it too -- see the disconnect-flip branch
          below. Guarded to local-relevance only: a federated event
          forwarded from a peer carries an `_origin` tag in `event.data`
          and is ignored here -- cross-server offline attribution instead
          rides the re-emitted `presence.update` from the origin server
          (see `_on_presence_update`), not the raw disconnect event.
        - `EventType.PRESENCE` (task t5): either our own just-emitted local
          publish/flip echoing back through `IRCd.emit_event`'s local
          skill-hook dispatch (no `_origin` -- ignored, the registry was
          already updated on the local path) or a genuine federated update
          from a peer (`_origin` present -- upsert the row). See
          `_on_presence_update`.
        - `EventType.SERVER_LINK` (task t5): on our own newly-established
          link, re-emit every local row so the new peer learns pre-link
          state. See `_on_server_link`.
        - `EventType.SERVER_UNLINK` (task t5): flip every row attributed to
          the departed server to offline. See `_on_server_unlink`.

        See the module-level `_DISCONNECT_EVENT_TYPES` docstring for why a
        mode-less client that drops the socket without an explicit QUIT
        currently emits nothing this skill can observe.
        """
        if event.type == EventType.PRESENCE:
            await self._on_presence_update(event)
            return

        if event.type == EventType.SERVER_LINK:
            await self._on_server_link(event)
            return

        if event.type == EventType.SERVER_UNLINK:
            self._on_server_unlink(event)
            return

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
        await self._emit_presence_update(nick, record)

    async def _emit_presence_update(self, nick: str, record: PresenceRecord) -> None:
        """Emit a `presence.update` Event carrying *record*'s full row for *nick*.

        Rides the existing event bus (no new S2S verb, no hop counts):
        `IRCd.emit_event` relays any locally-originated event (no `_origin`
        tag) to every linked peer via the generic `SEVENT` fallback -- that
        is the whole propagation mechanism (see `ServerLink.relay_event` in
        `agentirc/server_link.py`). Used both for a freshly-accepted local
        publish/offline-flip (fresh `last_refresh`, stamped by the caller)
        and for a `SERVER_LINK` burst re-emit of an already-stored local row
        -- this method never recomputes `last_refresh` itself, it only
        emits whatever `record.last_refresh` already holds.
        """
        await self.server.emit_event(
            Event(
                type=EventType.PRESENCE,
                channel=None,
                nick=nick,
                data={
                    "nick": nick,
                    "state": record.state,
                    "since": record.since,
                    "task": record.task,
                    "tokens_in": record.tokens_in,
                    "tokens_out": record.tokens_out,
                    "last_refresh": record.last_refresh,
                    "server": record.server,
                },
            )
        )

    async def _on_presence_update(self, event: Event) -> None:
        """Ingest a federated `presence.update` event from a peer.

        `_handle_sevent` (`agentirc/server_link.py`) strips any peer-supplied
        `_`-prefixed keys before stamping its own `_origin` tag onto
        `event.data`, so `_origin` is the tamper-resistant attribution --
        prefer it over the peer-supplied `data["server"]` when deciding which
        server this row belongs to.

        Absent `_origin`, this is our own just-emitted local event echoing
        back through `IRCd.emit_event`'s local skill-hook dispatch (which
        runs for every event -- local or federated -- before the
        relay-to-peers step): the registry was already updated on the local
        path (`_handle_publish`'s publish handler, or the local disconnect
        flip above), so ignore it here. This is also the loop-prevention
        invariant for this path: nothing is re-emitted from here, ever.
        """
        origin = event.data.get("_origin")
        if not origin:
            return

        data = event.data
        nick = data.get("nick")
        if not isinstance(nick, str) or not nick:
            logger.debug(
                "presence: federated update with no nick from %s: %r", origin, data
            )
            return

        # A peer may never overwrite presence for a resident THIS server hosts
        # locally: a nick is hosted by exactly one server, so a federated row
        # for a locally-owned nick is always wrong (stale echo, version skew,
        # or a forged `nick` aimed at clobbering a live local resident's real
        # state). The authoritative row for a local nick only ever changes via
        # `_handle_publish` / the local disconnect flip.
        existing = self.registry.get(nick)
        if existing is not None and existing.server == self.server.config.name:
            logger.debug(
                "presence: ignoring federated update from %s for locally-hosted "
                "nick %s",
                origin,
                nick,
            )
            return

        # Validate/normalize with the SAME rules as a local publish -- a
        # federated peer's payload is not trusted to be well-formed. A row that
        # fails validation is dropped rather than stored (and thus never served
        # to culture's parser with an out-of-enum state or mistyped field).
        fields = self._validate_fields(data)
        if fields is None:
            logger.warning(
                "presence: invalid federated update from %s for %s: %r",
                origin,
                nick,
                data,
            )
            return

        last_refresh = data.get("last_refresh")
        if not isinstance(last_refresh, (int, float)) or isinstance(last_refresh, bool):
            last_refresh = time.time()

        self.registry[nick] = PresenceRecord(
            **fields,
            last_refresh=last_refresh,
            server=origin,
        )

    async def _on_server_link(self, event: Event) -> None:
        """Re-emit local rows so a newly-linked peer learns pre-link state.

        Guarded to OUR OWN newly-established link: a federated `server.link`
        notice forwarded from a peer (`_origin` present) reports on that
        peer's topology, not ours, so it does not trigger a re-burst here.
        Re-emission is idempotent on the receiving peer (latest-wins upsert
        in `_on_presence_update`), so harmless if sent more than once.
        """
        if event.data.get("_origin"):
            return

        local_name = self.server.config.name
        local_rows = [
            (nick, record)
            for nick, record in self.registry.items()
            if record.server == local_name
        ]
        for nick, record in local_rows:
            await self._emit_presence_update(nick, record)

    def _on_server_unlink(self, event: Event) -> None:
        """Flip every row attributed to the departed server to offline.

        No hop-count tracking (per the design decision to ride the plain
        event bus rather than add a new typed S2S verb) -- this reacts to
        whichever side's unlink notice reaches us, local or federated, and
        flips any row currently attributed to that server name. Rows are
        retained (still keyed by nick), counters and `since`/`server`
        untouched -- only `state`, `task`, and `last_refresh` change,
        mirroring the local disconnect flip in `on_event` above.
        """
        peer = event.data.get("peer")
        if not isinstance(peer, str) or not peer:
            return

        now = time.time()
        for record in self.registry.values():
            if record.server != peer:
                continue
            record.state = "offline"
            record.task = None
            record.last_refresh = now
