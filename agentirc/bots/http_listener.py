"""Webhook HTTP listener for bot event dispatch (public subsystem, since 9.7.0).

Promoted from ``agentirc._internal.bots.http_listener`` as part of the
9.7.0 bot-framework absorption. The old import path remains as a
deprecation re-export until 10.0.0.

In a standalone agentirc deployment there is no webhook consumer, so
``start()`` and ``stop()`` are no-ops. Culture's runtime replaces this
with a real listener when wrapping an IRCd.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentirc.bots.bot_manager import BotManager


class HttpListener:
    def __init__(self, bot_manager: "BotManager", host: str, port: int) -> None:
        self.bot_manager = bot_manager
        self.host = host
        self.port = port

    async def start(self) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        return None

    async def stop(self) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        return None
