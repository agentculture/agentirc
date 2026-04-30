"""No-op ``BotManager`` stub.

agentirc is a pure IRCd; bot infrastructure (loading agent backends from
config, dispatching events to them, graceful shutdown) is a culture concern
and lives in ``culture.bots.bot_manager``. This stub keeps ``IRCd.start()``
import-clean for standalone agentirc deployments. Culture's
:class:`culture.bots.bot_manager.BotManager` is API-compatible and replaces
this stub when culture wraps an ``IRCd`` (today by subclassing / attribute
replacement; eventually via a real injection point).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentirc.ircd import IRCd
    from agentirc.skill import Event


class BotManager:
    def __init__(self, server: "IRCd") -> None:
        self.server = server

    async def load_bots(self) -> None:
        return None

    def load_system_bots(self) -> None:
        return None

    def get_bot(self, nick: str):
        return None

    async def on_event(self, event: "Event") -> None:
        return None

    async def stop_all(self) -> None:
        return None
