"""agentirc embedded bot framework (public subsystem, since 9.7.0).

Deterministic, YAML-spec'd chatops bots that run as in-process
``VirtualClient`` presences inside an :class:`agentirc.ircd.IRCd`. Public
exports (``BotManager``, ``Bot``) are populated as the subsystem lands; this
module is the package anchor for the wave-0 vendored modules
(``template_engine``, ``filter_dsl``, ``config``, ``virtual_client``).

Quick start::

    from agentirc.bots import BotManager, Bot, BotConfig

Public members (semver-tracked since 9.7.0):

- :class:`BotManager` — central registry for bot lifecycle and webhook dispatch.
- :class:`Bot` — single bot instance (config + VirtualClient + handler logic).
- :class:`BotConfig` — YAML-spec'd bot configuration dataclass.
"""

from __future__ import annotations

from agentirc.bots.bot import Bot
from agentirc.bots.bot_manager import BotManager
from agentirc.bots.config import BotConfig

__all__ = [
    "BotManager",
    "Bot",
    "BotConfig",
]
