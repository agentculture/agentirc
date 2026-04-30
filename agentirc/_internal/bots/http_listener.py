"""No-op ``HttpListener`` stub.

Pairs with the no-op :class:`agentirc._internal.bots.bot_manager.BotManager`.
The real implementation lives in ``culture.bots.http_listener`` and exposes
a webhook surface for triggering bot events. In a standalone agentirc
deployment there is nothing to listen for, so ``start()`` and ``stop()``
are no-ops.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentirc._internal.bots.bot_manager import BotManager


class HttpListener:
    def __init__(self, bot_manager: "BotManager", host: str, port: int) -> None:
        self.bot_manager = bot_manager
        self.host = host
        self.port = port

    async def start(self) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        return None

    async def stop(self) -> None:  # NOSONAR S7503: stub method must remain async to match the abstract contract real implementations override.
        return None
