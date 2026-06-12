"""Deprecated. Import :class:`BotManager` from :mod:`agentirc.bots.bot_manager`.

The class was promoted to the public API in 9.7.0 as part of the
bot-framework absorption. This module remains as a transitional re-export.
To make the warning point at the *consumer's* import site (rather than at
importlib internals — which is what ``warnings.warn`` at module-init time
with any fixed ``stacklevel`` would report), we use the PEP 562
module-level :func:`__getattr__` hook so the warning fires when
``BotManager`` is *accessed* on the module, not when the module is loaded.
Removal scheduled for 10.0.0.
"""

from __future__ import annotations

import warnings
from typing import Any

__all__ = ["BotManager"]

_DEPRECATION_MESSAGE = (
    "agentirc._internal.bots.bot_manager.BotManager is deprecated; "
    "import from agentirc.bots.bot_manager instead. The shim will be "
    "removed in 10.0.0."
)


def __getattr__(name: str) -> Any:
    if name == "BotManager":
        warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
        from agentirc.bots.bot_manager import BotManager

        return BotManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
