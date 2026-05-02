"""Deprecated. Import :class:`VirtualClient` from :mod:`agentirc.virtual_client`.

The class was promoted to the public API in 9.6.0 (issue #22). This module
remains as a transitional re-export that emits :class:`DeprecationWarning`
on import. Removal is scheduled for 10.0.0.
"""

from __future__ import annotations

import warnings

from agentirc.virtual_client import VirtualClient as _VirtualClient

warnings.warn(
    "agentirc._internal.virtual_client.VirtualClient is deprecated; "
    "import from agentirc.virtual_client instead. The shim will be "
    "removed in 10.0.0.",
    DeprecationWarning,
    stacklevel=2,
)

VirtualClient = _VirtualClient

__all__ = ["VirtualClient"]
