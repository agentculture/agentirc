"""Tests for agentirc.bots.virtual_client (bot-host VirtualClient subclass).

Verifies that the culture bot-host ``VirtualClient`` subclass is properly
vendored as a no-override subclass with no behavioral changes.
"""

from __future__ import annotations

import pytest

from agentirc.bots.virtual_client import VirtualClient as BotHostVirtualClient
from agentirc.virtual_client import VirtualClient as PublicVirtualClient


def test_bot_host_virtual_client_is_subclass():
    """agentirc.bots.virtual_client.VirtualClient subclasses agentirc.virtual_client.VirtualClient."""
    assert issubclass(BotHostVirtualClient, PublicVirtualClient)


def test_bot_host_virtual_client_no_method_overrides():
    """Bot-host VirtualClient adds no overriding methods beyond inheritance."""
    # Collect all methods defined directly on BotHostVirtualClient (not inherited)
    bot_host_methods = {
        name
        for name in dir(BotHostVirtualClient)
        if not name.startswith("_") and callable(getattr(BotHostVirtualClient, name))
    }

    public_methods = {
        name
        for name in dir(PublicVirtualClient)
        if not name.startswith("_") and callable(getattr(PublicVirtualClient, name))
    }

    # The bot-host subclass should have no additional public methods
    # (all public methods are inherited from the base)
    new_methods = bot_host_methods - public_methods
    assert not new_methods, f"Bot-host VirtualClient added unexpected methods: {new_methods}"

    # Verify MRO (Method Resolution Order) places base immediately after subclass
    mro = BotHostVirtualClient.__mro__
    assert len(mro) >= 2
    assert mro[0] is BotHostVirtualClient
    assert mro[1] is PublicVirtualClient


@pytest.mark.asyncio
async def test_bot_host_virtual_client_instantiation_and_channel_registration(server):
    """Instantiating a bot-host VirtualClient registers it in a channel."""
    # Create an instance with test parameters
    bot = BotHostVirtualClient(nick="testbot", user="bot_user", server=server)

    # Verify basic attributes are set (inherited behavior)
    assert bot.nick == "testbot"
    assert bot.user == "bot_user"
    assert bot.host == "bot"
    assert bot.realname == "Bot testbot"
    assert bot.server is server

    # Join a channel
    await bot.join_channel("#test_channel")

    # Verify the bot is registered in the channel
    channel = server.channels.get("#test_channel")
    assert channel is not None
    assert bot in channel.members
    assert "#test_channel" in {c.name for c in bot.channels}

    # Verify bot is not auto-promoted to operator (bot-CAP behavior)
    assert bot not in channel.operators

    # Part the channel
    await bot.part_channel("#test_channel")

    # Verify the bot is unregistered
    assert bot not in channel.members
    assert "#test_channel" not in {c.name for c in bot.channels}
