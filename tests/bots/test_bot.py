"""Tests for agentirc.bots.bot — vendored paraphrase of culture/bots/bot.py.

TDD acceptance criterion:
- Bot loads from BotConfig, matches an Event via the filter DSL, renders a
  reply via the template engine; all imports resolve to agentirc.* with
  EVENT_TYPE_RE from agentirc._internal.constants; zero culture imports.
"""

from __future__ import annotations

import textwrap

import pytest

from agentirc._internal.constants import EVENT_TYPE_RE
from agentirc.bots.bot import Bot
from agentirc.bots.config import BotConfig, load_bot_config
from agentirc.bots.filter_dsl import compile_filter, evaluate
from agentirc.protocol import Event, EventType


# ---------------------------------------------------------------------------
# Import-boundary assertion (no culture imports)
# ---------------------------------------------------------------------------


def test_bot_module_has_no_culture_imports():
    """agentirc/bots/bot.py must import zero symbols from the culture package."""
    import importlib
    import pkgutil

    # Reload the module object and inspect its __dict__ for culture references
    import agentirc.bots.bot as bot_module

    # Check all imports in the module are from agentirc.* or stdlib/third-party
    source_path = bot_module.__file__
    with open(source_path) as f:
        source = f.read()

    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("from culture", "import culture"))
    ]
    assert import_lines == [], (
        "bot.py has forbidden culture imports:\n" + "\n".join(import_lines)
    )


def test_event_type_re_from_agentirc_internal_constants():
    """EVENT_TYPE_RE used in bot.py resolves from agentirc._internal.constants."""
    # Verify the imported constant is a compiled regex with expected behavior
    assert EVENT_TYPE_RE.match("user.join") is not None
    assert EVENT_TYPE_RE.match("user.part") is not None
    assert EVENT_TYPE_RE.match("custom.bot.event") is not None
    assert EVENT_TYPE_RE.match("invalidevent") is None  # no dots → no match
    assert EVENT_TYPE_RE.match("") is None


# ---------------------------------------------------------------------------
# BotConfig construction helpers
# ---------------------------------------------------------------------------


def _make_bot_config(
    tmp_path,
    *,
    name: str = "testbot",
    channels: list[str] | None = None,
    template: str | None = "{event.nick} said something",
    event_filter: str | None = "event.type == 'user.message'",
) -> BotConfig:
    """Write a bot.yaml to tmp_path and load it via load_bot_config."""
    channels = channels or ["#general"]
    yaml_text = textwrap.dedent(f"""\
        bot:
          name: {name}
          owner: ori
          description: A test bot
          created: "2026-01-01"
        trigger:
          type: event
          filter: "{event_filter}"
        output:
          channels:
            - "{channels[0]}"
          dm_owner: false
          template: "{template}"
          fallback: json
    """)
    cfg_path = tmp_path / "bot.yaml"
    cfg_path.write_text(yaml_text)
    return load_bot_config(cfg_path)


# ---------------------------------------------------------------------------
# Filter DSL matching (exercises the real collaborator)
# ---------------------------------------------------------------------------


def test_filter_matches_event_with_matching_type():
    """A filter 'event.type == user.message' matches a dict with that type."""
    event_filter = "event.type == 'user.message'"
    ast = compile_filter(event_filter)

    matching_payload = {"event": {"type": "user.message", "nick": "alice"}}
    non_matching_payload = {"event": {"type": "user.join", "nick": "alice"}}

    assert evaluate(ast, matching_payload) is True
    assert evaluate(ast, non_matching_payload) is False


def test_filter_non_match_returns_false():
    """A filter that does not match returns falsy for unrelated event types."""
    event_filter = "event.type == 'user.message'"
    ast = compile_filter(event_filter)

    payload = {"event": {"type": "user.part", "nick": "bob"}}
    assert not evaluate(ast, payload)


# ---------------------------------------------------------------------------
# Bot construction: Bot loads from BotConfig
# ---------------------------------------------------------------------------


def test_bot_constructed_from_bot_config(tmp_path, server):
    """Bot(config, server) stores config and server; starts inactive."""
    config = _make_bot_config(tmp_path)
    bot = Bot(config, server)

    assert bot.config is config
    assert bot.server is server
    assert bot.name == "testbot"
    assert bot.active is False
    assert bot.virtual_client is None


@pytest.mark.asyncio
async def test_bot_start_activates_and_joins_channels(tmp_path, server):
    """Bot.start() creates a VirtualClient and joins the configured channel."""
    config = _make_bot_config(tmp_path, name="startbot", channels=["#startchan"])
    bot = Bot(config, server)

    await bot.start()
    try:
        assert bot.active is True
        assert bot.virtual_client is not None
        assert bot.virtual_client.nick == "startbot"

        channel = server.channels.get("#startchan")
        assert channel is not None
        assert bot.virtual_client in channel.members
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_bot_stop_deactivates(tmp_path, server):
    """Bot.stop() removes the VirtualClient and parts channels."""
    config = _make_bot_config(tmp_path, name="stopbot", channels=["#stopchan"])
    bot = Bot(config, server)

    await bot.start()
    assert bot.active is True

    await bot.stop()
    assert bot.active is False
    assert bot.virtual_client is None


# ---------------------------------------------------------------------------
# Bot.handle: matching Event → templated reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_handle_matching_payload_returns_templated_reply(tmp_path, server):
    """Bot.handle(payload) returns the template-rendered string for a matching payload.

    The filter evaluation (matching/non-matching) is the BotManager's
    responsibility; Bot.handle() always renders and delivers given a payload.
    This test verifies the full rendering pipeline using the real template engine.
    Template: "{event.nick} triggered the bot"
    Payload:  {"event": {"type": "user.message", "nick": "alice"}}
    Expected: "alice triggered the bot"
    """
    config = _make_bot_config(
        tmp_path,
        name="handlebot",
        channels=["#testchan"],
        template="{event.nick} triggered the bot",
        event_filter="event.type == 'user.message'",
    )
    bot = Bot(config, server)
    await bot.start()

    try:
        # Payload that matches the filter (filter evaluated externally, but
        # content matches so we can confirm round-trip)
        matching_payload = {"event": {"type": "user.message", "nick": "alice"}}

        # Verify that the filter DSL would match this payload
        ast = compile_filter(config.event_filter)
        assert evaluate(ast, matching_payload) is True

        # Call handle() — renders template + delivers to channel
        reply = await bot.handle(matching_payload)
        assert reply == "alice triggered the bot"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_bot_handle_non_matching_payload_still_renders(tmp_path, server):
    """Bot.handle() renders regardless of filter — filtering is BotManager's job.

    This test verifies that for a non-matching payload (wrong event.type),
    the filter DSL returns False (non-match), and the caller (BotManager) would
    not invoke handle(). If handle() IS called (e.g. for a non-event trigger),
    it renders the template with the given payload.
    """
    config = _make_bot_config(
        tmp_path,
        name="filterbot",
        channels=["#filterchan"],
        template="{event.nick} triggered the bot",
        event_filter="event.type == 'user.message'",
    )
    bot = Bot(config, server)
    await bot.start()

    try:
        non_matching_payload = {"event": {"type": "user.join", "nick": "bob"}}

        # Confirm filter DSL non-match
        ast = compile_filter(config.event_filter)
        assert evaluate(ast, non_matching_payload) is False

        # BotManager would skip handle() here; but if called directly,
        # the template still renders (Bot.handle is filter-agnostic)
        reply = await bot.handle(non_matching_payload)
        assert reply == "bob triggered the bot"
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_bot_handle_fallback_when_template_unresolvable(tmp_path, server):
    """When template tokens can't resolve, bot falls back to JSON serialization."""
    config = BotConfig(
        name="fallbackbot",
        trigger_type="event",
        channels=["#fallbackchan"],
        template="{missing.field} value",  # field won't resolve
        fallback="json",
    )
    bot = Bot(config, server)
    await bot.start()

    try:
        payload = {"event": {"type": "user.message", "nick": "carol"}}
        reply = await bot.handle(payload)
        # fallback=json: payload is serialized as JSON
        import json
        assert reply == json.dumps(payload, indent=None, ensure_ascii=False)
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_bot_handle_raises_when_not_active(tmp_path, server):
    """Bot.handle() raises RuntimeError if the bot was never started."""
    config = _make_bot_config(tmp_path, name="inactivebot")
    bot = Bot(config, server)
    # Do not call bot.start()

    with pytest.raises(RuntimeError, match="not active"):
        await bot.handle({"event": {"type": "user.message"}})


# ---------------------------------------------------------------------------
# Protocol.Event integration: Event dataclass imports resolve correctly
# ---------------------------------------------------------------------------


def test_protocol_event_and_eventtype_importable():
    """agentirc.protocol.Event and EventType are importable and functional."""
    evt = Event(type=EventType.MESSAGE, channel="#general", nick="alice", data={"text": "hi"})
    assert evt.type == EventType.MESSAGE
    assert evt.channel == "#general"
    assert evt.nick == "alice"
    assert evt.data == {"text": "hi"}


def test_event_type_re_matches_dotted_event_types():
    """EVENT_TYPE_RE (from agentirc._internal.constants) matches dotted EventType values.

    The regex requires at least two dot-separated segments (e.g. 'user.join').
    Single-segment types like 'message' and 'topic' are legacy IRC-level types
    that predate the dotted convention; they don't match the fires_event validator
    by design — EVENT_TYPE_RE is only used to validate fires_event.type in bot configs.
    """
    dotted_types = [et for et in EventType if "." in et.value]
    assert dotted_types, "There should be at least some dotted EventType values"
    for et in dotted_types:
        assert EVENT_TYPE_RE.match(et.value) is not None, (
            f"EVENT_TYPE_RE should match EventType.{et.name} = {et.value!r}"
        )
