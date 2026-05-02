"""API-stability lock-in for the in-process embedding surface (9.6.0, #22).

Promotes ``agentirc.ircd.IRCd`` and ``agentirc.virtual_client.VirtualClient``
to public, semver-tracked status. Locks in:

* The canonical public import paths resolve.
* The legacy ``agentirc._internal.virtual_client`` re-export still resolves
  but emits :class:`DeprecationWarning` (removal scheduled for 10.0.0).

Behavioural coverage of ``VirtualClient`` and ``IRCd`` lives in the wider
suite (``conftest.py`` exercises both via the ``server`` fixture, and the
36 vendored tests cover member behaviour through ``system_client``); this
file only nails the import contract.
"""

from __future__ import annotations

import importlib
import sys
import warnings

import pytest


def test_public_imports_resolve() -> None:
    from agentirc.ircd import IRCd
    from agentirc.virtual_client import VirtualClient

    assert callable(IRCd)
    assert callable(VirtualClient)


def test_internal_shim_warns_and_re_exports() -> None:
    sys.modules.pop("agentirc._internal.virtual_client", None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        shim = importlib.import_module("agentirc._internal.virtual_client")

    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecation_warnings, "expected DeprecationWarning on shim import"
    assert any("agentirc.virtual_client" in str(w.message) for w in deprecation_warnings)

    from agentirc.virtual_client import VirtualClient as PublicVirtualClient

    assert shim.VirtualClient is PublicVirtualClient


def test_ircd_public_surface_present() -> None:
    """Locks in the named public members of IRCd documented in api-stability.md."""
    from agentirc.ircd import IRCd

    for member in (
        "__init__",
        "start",
        "stop",
        "emit_event",
    ):
        assert hasattr(IRCd, member), f"IRCd missing public member {member!r}"


def test_virtual_client_public_surface_present() -> None:
    """Locks in the named public members of VirtualClient."""
    from agentirc.virtual_client import VirtualClient

    for member in (
        "__init__",
        "join_channel",
        "part_channel",
        "send_to_channel",
        "broadcast_to_channel",
        "send_dm",
        "caps",
    ):
        assert hasattr(VirtualClient, member), (
            f"VirtualClient missing public member {member!r}"
        )

    assert "agentirc.io/bot" in VirtualClient.caps
