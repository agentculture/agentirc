"""Test the public agentirc.bots surface and _internal deprecation re-exports.

Acceptance criteria (task t9):
1. ``from agentirc.bots import BotManager, Bot`` works and they are the real
   classes (identity check against their canonical module).
2. Importing ``agentirc._internal.bots.bot_manager`` emits a DeprecationWarning.
3. The _internal re-exports are identical objects to the public real classes.
"""

from __future__ import annotations

import importlib
import sys
import warnings


# ---------------------------------------------------------------------------
# 1. Public package surface
# ---------------------------------------------------------------------------


def test_bots_package_exports_botmanager_and_bot():
    """from agentirc.bots import BotManager, Bot succeeds."""
    from agentirc.bots import BotManager, Bot  # noqa: F401 (import check is the test)

    assert BotManager is not None
    assert Bot is not None


def test_public_botmanager_identity():
    """agentirc.bots.BotManager is the same class as agentirc.bots.bot_manager.BotManager."""
    from agentirc.bots import BotManager as public_BotManager
    from agentirc.bots.bot_manager import BotManager as canonical_BotManager

    assert public_BotManager is canonical_BotManager


def test_public_bot_identity():
    """agentirc.bots.Bot is the same class as agentirc.bots.bot.Bot."""
    from agentirc.bots import Bot as public_Bot
    from agentirc.bots.bot import Bot as canonical_Bot

    assert public_Bot is canonical_Bot


def test_public_botconfig_identity():
    """agentirc.bots.BotConfig is the same class as agentirc.bots.config.BotConfig."""
    from agentirc.bots import BotConfig as public_BotConfig
    from agentirc.bots.config import BotConfig as canonical_BotConfig

    assert public_BotConfig is canonical_BotConfig


def test_bots_package_all():
    """agentirc.bots.__all__ contains at least BotManager and Bot."""
    import agentirc.bots as bots_pkg

    assert hasattr(bots_pkg, "__all__"), "agentirc.bots must define __all__"
    assert "BotManager" in bots_pkg.__all__
    assert "Bot" in bots_pkg.__all__


# ---------------------------------------------------------------------------
# 2. _internal stubs emit DeprecationWarning on attribute access
# ---------------------------------------------------------------------------


def test_internal_botmanager_emits_deprecation_warning():
    """Accessing BotManager via agentirc._internal.bots.bot_manager emits DeprecationWarning."""
    # Remove cached module so __getattr__ fires fresh on each access
    mod_name = "agentirc._internal.bots.bot_manager"
    saved = sys.modules.pop(mod_name, None)
    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            mod = importlib.import_module(mod_name)
            # Trigger __getattr__ by accessing the class
            _ = mod.BotManager
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert deprecation_warnings, (
            "Expected at least one DeprecationWarning when accessing "
            "agentirc._internal.bots.bot_manager.BotManager"
        )
    finally:
        if saved is not None:
            sys.modules[mod_name] = saved
        else:
            sys.modules.pop(mod_name, None)


def test_internal_httplistener_emits_deprecation_warning():
    """Accessing HttpListener via agentirc._internal.bots.http_listener emits DeprecationWarning."""
    mod_name = "agentirc._internal.bots.http_listener"
    saved = sys.modules.pop(mod_name, None)
    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            mod = importlib.import_module(mod_name)
            _ = mod.HttpListener
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert deprecation_warnings, (
            "Expected at least one DeprecationWarning when accessing "
            "agentirc._internal.bots.http_listener.HttpListener"
        )
    finally:
        if saved is not None:
            sys.modules[mod_name] = saved
        else:
            sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# 3. _internal re-exports are identical to the real public classes
# ---------------------------------------------------------------------------


def test_internal_botmanager_is_real_class():
    """agentirc._internal.bots.bot_manager.BotManager IS agentirc.bots.bot_manager.BotManager."""
    from agentirc.bots.bot_manager import BotManager as real_BotManager

    mod_name = "agentirc._internal.bots.bot_manager"
    saved = sys.modules.pop(mod_name, None)
    try:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            mod = importlib.import_module(mod_name)
            stub_bot_manager = mod.BotManager
        assert stub_bot_manager is real_BotManager, (
            "_internal BotManager must be the real class from agentirc.bots.bot_manager"
        )
    finally:
        if saved is not None:
            sys.modules[mod_name] = saved
        else:
            sys.modules.pop(mod_name, None)


def test_internal_httplistener_is_real_class():
    """agentirc._internal.bots.http_listener.HttpListener IS agentirc.bots.http_listener.HttpListener."""
    from agentirc.bots.http_listener import HttpListener as real_HttpListener

    mod_name = "agentirc._internal.bots.http_listener"
    saved = sys.modules.pop(mod_name, None)
    try:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            mod = importlib.import_module(mod_name)
            stub_http_listener = mod.HttpListener
        assert stub_http_listener is real_HttpListener, (
            "_internal HttpListener must be the real class from agentirc.bots.http_listener"
        )
    finally:
        if saved is not None:
            sys.modules[mod_name] = saved
        else:
            sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# 4. ircd still imports cleanly (sanity smoke)
# ---------------------------------------------------------------------------


def test_ircd_imports_cleanly_after_stub_conversion():
    """agentirc.ircd can be imported without error after stub conversion."""
    import agentirc.ircd  # noqa: F401

    assert hasattr(agentirc.ircd, "IRCd")
