from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentirc.client import Client
    from agentirc.ircd import IRCd
    from agentirc._internal.protocol.message import Message


class EventType(Enum):
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
    # Lifecycle + link events introduced by mesh-events feature.
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
    type: EventType
    channel: str | None
    nick: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


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
