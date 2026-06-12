"""Deprecated. Import :class:`HttpListener` from :mod:`agentirc.bots.http_listener`.

The class was promoted to the public API in 9.7.0 as part of the
bot-framework absorption. This module remains as a transitional re-export.
To make the warning point at the *consumer's* import site (rather than at
importlib internals — which is what ``warnings.warn`` at module-init time
with any fixed ``stacklevel`` would report), we use the PEP 562
module-level :func:`__getattr__` hook so the warning fires when
``HttpListener`` is *accessed* on the module, not when the module is loaded.
Removal scheduled for 10.0.0.
"""

from __future__ import annotations

import warnings
from typing import Any

__all__ = ["HttpListener"]

_DEPRECATION_MESSAGE = (
    "agentirc._internal.bots.http_listener.HttpListener is deprecated; "
    "import from agentirc.bots.http_listener instead. The shim will be "
    "removed in 10.0.0."
)


def __getattr__(name: str) -> Any:
    if name == "HttpListener":
        warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
        from agentirc.bots.http_listener import HttpListener

        return HttpListener
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
