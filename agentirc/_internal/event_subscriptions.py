"""Per-client event subscription registry for the bot extension API (9.5.0).

Public-facing wire format and verb syntax are described in the design spec at
``docs/superpowers/specs/2026-05-01-bot-extension-api-design.md`` § Decision B.
This module is internal — consumers interact via the ``EVENTSUB``/``EVENTUNSUB``
IRC verbs handled by :class:`agentirc.client.Client`.

Design points:

- One :class:`Subscription` per ``sub-id`` per client. Multiple concurrent
  subscriptions per client are allowed; each gets its own bounded queue and
  its own drain task.
- Filter fields are AND-ed; ``type`` and ``nick`` accept ``fnmatch``-style
  globs (``*``/``?``/``[]``); ``channel`` is exact match (or ``"*"`` for any
  channel, or ``""`` for nick-scoped events only).
- Backpressure: a per-subscription ``asyncio.Queue`` bounded by
  ``ServerConfig.event_subscription_queue_max``. On overflow, the registry
  sends ``EVENTERR <sub-id> :backpressure-overflow`` and removes the
  subscription. The connection itself stays open — the bot can re-subscribe
  with the same or a fresh ``sub-id`` and use ``BACKFILL`` to recover.
- The drain task encodes events via :func:`agentirc.ircd.IRCd._build_event_envelope`
  + :func:`agentirc.ircd.IRCd._encode_event_data`, so the ``EVENT`` line on the
  wire carries the same canonical 5-field envelope as the federated ``SEVENT``
  payload and the IRCv3 ``event-data`` tag.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from agentirc.client import Client
    from agentirc.protocol import Event

logger = logging.getLogger(__name__)


# A per-subscription channel filter that matches every channel, including the
# nick-scoped ``None`` case.
CHANNEL_ANY = "*"
# A per-subscription channel filter that matches only nick-scoped events
# (events with ``event.channel is None``).
CHANNEL_NICK_SCOPED_ONLY = ""

# Channel-filter format for ``EVENTSUB``: an exact channel name (``#``-prefixed),
# the literal ``*`` (any channel including nick-scoped), or the empty string
# (nick-scoped events only). Anything else is rejected so subscribers don't
# silently bind a filter that never matches.
_CHANNEL_FILTER_RE = re.compile(r"^(#[^\s,]+|\*|)$")


# ---------------------------------------------------------------------------
# Filter-parameter dispatch table for EVENTSUB
# ---------------------------------------------------------------------------
# Each handler receives the partially-built ``filters`` dict and the value
# from one ``key=value`` token; it mutates ``filters`` and returns either
# ``None`` on success or an error-reason string suitable for
# ``EVENTERR <sub-id> :<reason>``. ``Client._parse_eventsub_filters`` walks
# the tokens and dispatches via :data:`FILTER_HANDLERS`.


def _set_type_glob(filters: dict, value: str) -> str | None:
    filters["type_glob"] = value or "*"
    return None


def _set_channel(filters: dict, value: str) -> str | None:
    if not _CHANNEL_FILTER_RE.match(value):
        return f"invalid-channel-filter {value}"
    filters["channel"] = CHANNEL_NICK_SCOPED_ONLY if value == "" else value
    return None


def _set_nick_glob(filters: dict, value: str) -> str | None:
    filters["nick_glob"] = value or "*"
    return None


FILTER_HANDLERS: dict[str, Callable[[dict, str], "str | None"]] = {
    "type": _set_type_glob,
    "channel": _set_channel,
    "nick": _set_nick_glob,
}


@dataclass
class Subscription:
    """A single subscription owned by a client.

    Constructed by :meth:`SubscriptionRegistry.add`; do not instantiate
    directly. ``queue`` and ``drain_task`` are populated by the registry.
    """

    sub_id: str
    type_glob: str = "*"
    channel: str = CHANNEL_ANY
    nick_glob: str = "*"
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    drain_task: asyncio.Task | None = None
    dropped: bool = False

    def matches(self, event: Event) -> bool:
        """Return True if *event* satisfies the AND-ed filter fields."""
        type_str = str(event.type)
        if self.type_glob != "*" and not fnmatch.fnmatchcase(type_str, self.type_glob):
            return False
        if self.channel != CHANNEL_ANY:
            if self.channel == CHANNEL_NICK_SCOPED_ONLY:
                if event.channel is not None:
                    return False
            else:
                if event.channel != self.channel:
                    return False
        if self.nick_glob != "*" and not fnmatch.fnmatchcase(event.nick, self.nick_glob):
            return False
        return True


class SubscriptionRegistry:
    """Routes events to per-client subscription queues.

    Owned by :class:`agentirc.ircd.IRCd`. ``IRCd.emit_event`` calls
    :meth:`dispatch` on every event; subscriber clients drain their per-sub
    queues via background tasks.
    """

    def __init__(self, *, queue_max: int = 1024) -> None:
        self._subs: dict[Client, dict[str, Subscription]] = {}
        self._queue_max = queue_max

    @property
    def queue_max(self) -> int:
        return self._queue_max

    def get(self, client: Client, sub_id: str) -> Subscription | None:
        return self._subs.get(client, {}).get(sub_id)

    def list_for_client(self, client: Client) -> list[Subscription]:
        return list(self._subs.get(client, {}).values())

    def add(
        self,
        client: Client,
        sub_id: str,
        *,
        type_glob: str = "*",
        channel: str = CHANNEL_ANY,
        nick_glob: str = "*",
    ) -> Subscription | None:
        """Register a new subscription. Returns None on sub-id collision.

        The caller is responsible for sending the appropriate ``EVENTERR``
        when the result is ``None``.
        """
        per_client = self._subs.setdefault(client, {})
        if sub_id in per_client:
            return None
        sub = Subscription(
            sub_id=sub_id,
            type_glob=type_glob,
            channel=channel,
            nick_glob=nick_glob,
            queue=asyncio.Queue(maxsize=self._queue_max),
        )
        per_client[sub_id] = sub
        sub.drain_task = asyncio.create_task(
            self._drain(client, sub),
            name=f"event-sub-drain[{sub_id}]",
        )
        return sub

    def remove(self, client: Client, sub_id: str) -> bool:
        """Cancel and forget *sub_id* on *client*. Returns True on hit."""
        per_client = self._subs.get(client)
        if not per_client or sub_id not in per_client:
            return False
        sub = per_client.pop(sub_id)
        sub.dropped = True
        if sub.drain_task is not None and not sub.drain_task.done():
            sub.drain_task.cancel()
        if not per_client:
            self._subs.pop(client, None)
        return True

    def remove_client(self, client: Client) -> None:
        """Cancel every subscription owned by *client*. Called on disconnect."""
        per_client = self._subs.pop(client, {})
        for sub in per_client.values():
            sub.dropped = True
            if sub.drain_task is not None and not sub.drain_task.done():
                sub.drain_task.cancel()

    async def dispatch(self, event: Event) -> None:
        """Enqueue *event* on every matching subscription.

        On queue overflow, mark the subscription dropped, send the bot an
        ``EVENTERR <sub-id> :backpressure-overflow`` line, and remove the
        subscription from the registry. The bot's connection stays open.
        """
        # ``dispatch`` may call ``_handle_overflow`` which awaits
        # ``client.send_raw``; while that yields, another coroutine can call
        # ``add`` or ``remove`` and mutate the dicts. The ``list()`` snapshots
        # avoid ``RuntimeError: dictionary changed size during iteration``.
        for client, per_client in list(self._subs.items()):  # NOSONAR python:S7504: defensive snapshot vs. concurrent mutation
            for sub in list(per_client.values()):  # NOSONAR python:S7504: defensive snapshot vs. concurrent mutation
                if sub.dropped:
                    continue
                if not sub.matches(event):
                    continue
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    await self._handle_overflow(client, sub)

    async def _handle_overflow(self, client: Client, sub: Subscription) -> None:
        sub.dropped = True
        try:
            await client.send_raw(f"EVENTERR {sub.sub_id} :backpressure-overflow")
        except Exception:
            logger.exception(
                "Failed to send backpressure-overflow EVENTERR for sub %s",
                sub.sub_id,
            )
        self.remove(client, sub.sub_id)

    async def _drain(self, client: Client, sub: Subscription) -> None:
        """Pull events off *sub.queue* and send them as ``EVENT`` lines.

        Imports :mod:`agentirc.ircd` and :mod:`agentirc.protocol` lazily to
        avoid an import cycle (``ircd.py`` instantiates this registry).
        """
        from agentirc.ircd import IRCd
        from agentirc.protocol import EVENT

        server_name = client.server.config.name

        # ``CancelledError`` (raised when ``remove``/``remove_client`` cancels
        # the drain task) propagates out of this coroutine naturally — no
        # cleanup is needed because the registry already removed the
        # subscription before cancelling the task.
        while True:
            event = await sub.queue.get()
            if sub.dropped:
                return

            type_wire = str(event.type)
            target = event.channel if event.channel is not None else "*"
            envelope = IRCd._build_event_envelope(event)
            encoded = IRCd._encode_event_data(envelope, type_wire)
            line = (
                f":{server_name} {EVENT} {sub.sub_id} {type_wire} "
                f"{target} {event.nick} :{encoded}"
            )
            try:
                await client.send_raw(line)
            except Exception:
                # Connection is gone or send failed; let the disconnect
                # cleanup path remove this subscription.
                logger.debug(
                    "EVENT send to %s sub %s failed; awaiting disconnect cleanup",
                    getattr(client, "nick", "<?>"),
                    sub.sub_id,
                )
                return


__all__ = [
    "CHANNEL_ANY",
    "CHANNEL_NICK_SCOPED_ONLY",
    "Subscription",
    "SubscriptionRegistry",
]


# Re-export ``Any`` to satisfy strict-import linters that flag the typing import
# above. (Subscription.queue is parameterized in docstrings only.)
_ = Any
