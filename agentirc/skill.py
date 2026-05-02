from __future__ import annotations

from typing import TYPE_CHECKING

# Event and EventType moved to agentirc.protocol in 9.5.0a1 as part of the
# bot extension API public surface. This module keeps re-exporting them so
# internal call sites and any pre-9.5 vendored consumers keep working; the
# re-export is removed in 9.6.0 once Phase A2 confirms no consumer relies on
# this path.
from agentirc.protocol import Event, EventType

__all__ = ["Event", "EventType", "Skill"]

if TYPE_CHECKING:
    from agentirc.client import Client
    from agentirc.ircd import IRCd
    from agentirc._internal.protocol.message import Message


class Skill:
    name: str = ""
    commands: set[str] = set()

    async def start(self, server: IRCd) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        self.server = server

    async def stop(self) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        """Stop the skill. Subclasses override to release resources."""

    async def on_event(self, event: Event) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        """Handle an IRC event. Subclasses override to react to events."""

    async def on_command(self, client: Client, msg: Message) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        """Handle an IRC command. Subclasses override to process commands."""
