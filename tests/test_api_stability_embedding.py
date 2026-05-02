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
    """The legacy ``agentirc._internal.virtual_client`` path resolves but warns.

    Implementation note: the shim uses PEP 562 module-level ``__getattr__``
    so the ``DeprecationWarning`` fires at the consumer's *attribute access
    site* (where ``stacklevel=2`` actually points at user code), not at
    module-load time (where any fixed stacklevel reports importlib internals).
    Importing the module by itself is silent; accessing ``VirtualClient`` on
    it is what triggers the warning.
    """
    sys.modules.pop("agentirc._internal.virtual_client", None)

    # Import alone is silent under the __getattr__ pattern.
    with warnings.catch_warnings(record=True) as caught_on_import:
        warnings.simplefilter("always")
        shim = importlib.import_module("agentirc._internal.virtual_client")
    import_warnings = [
        w for w in caught_on_import if issubclass(w.category, DeprecationWarning)
    ]
    assert not import_warnings, (
        "module import should not warn; attribute access does. "
        f"Got: {[str(w.message) for w in import_warnings]}"
    )

    # Attribute access on the shim triggers the warning.
    with warnings.catch_warnings(record=True) as caught_on_access:
        warnings.simplefilter("always")
        shim_class = shim.VirtualClient
    access_warnings = [
        w for w in caught_on_access if issubclass(w.category, DeprecationWarning)
    ]
    assert access_warnings, "expected DeprecationWarning on attribute access"
    assert any(
        "agentirc.virtual_client" in str(w.message) for w in access_warnings
    )

    from agentirc.virtual_client import VirtualClient as PublicVirtualClient

    assert shim_class is PublicVirtualClient


def test_ircd_public_surface_present() -> None:
    """Locks in the named public members of IRCd documented in api-stability.md.

    Class-level methods (constructor + lifecycle) are checked on the class;
    instance attributes documented as part of the embedding contract
    (``subscription_registry``, ``clients``, ``channels``, ``config``,
    ``system_client``) are checked on an instance constructed without
    binding sockets — i.e. ``IRCd(config)`` with ``start()`` not called.
    """
    from agentirc.config import ServerConfig
    from agentirc.ircd import IRCd

    for member in (
        "__init__",
        "start",
        "stop",
        "emit_event",
    ):
        assert hasattr(IRCd, member), f"IRCd missing public member {member!r}"

    # Instance-attribute surface — locked in by the docs/api-stability.md
    # "Public surface on IRCd" table. Constructor must not bind sockets,
    # so this is safe to run in unit-test context.
    config = ServerConfig(name="stability-test", host="127.0.0.1", port=0)
    ircd = IRCd(config)
    try:
        for attr in (
            "subscription_registry",
            "clients",
            "channels",
            "config",
            "system_client",
        ):
            assert hasattr(ircd, attr), f"IRCd instance missing public attr {attr!r}"

        # Documented types / contracts of the read attributes.
        assert ircd.config is config
        assert isinstance(ircd.clients, dict)
        assert isinstance(ircd.channels, dict)
        # system_client is None until start() runs.
        assert ircd.system_client is None
    finally:
        # Constructor doesn't bind sockets, but it does start the audit
        # writer task lazily via init_audit; nothing to tear down here.
        pass


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
