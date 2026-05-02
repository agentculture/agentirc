"""No-op ``HttpListener`` stub (scheduled for removal in 9.6.0).

Pairs with the no-op :class:`agentirc._internal.bots.bot_manager.BotManager`.
The real implementation lives in ``culture.bots.http_listener`` and exposes
a webhook surface for triggering bot events. In a standalone agentirc
deployment there is nothing to listen for, so ``start()`` and ``stop()``
are no-ops.

As of 9.5.0, :class:`agentirc.ircd.IRCd` no longer instantiates this stub —
the webhook listener is the consumer's responsibility (see
``docs/api-stability.md`` and ``docs/deployment.md``). The class itself
stays in the codebase for one cycle so any vendored test or culture-runtime
override that imports it keeps working; it is scheduled for deletion in
9.6.0 once Phase A2 of agentculture/culture#308 confirms no consumer
imports it.
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
