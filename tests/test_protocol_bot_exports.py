"""Tests for the public bot extension API surface (9.5.0a1).

Behavior wires up in 9.5.0a2/a3; these tests only verify that the public
symbols are importable from `agentirc.protocol`, that the type-string
vocabulary is internally consistent (enum members ↔ per-type constants),
and that `agentirc.skill` still re-exports `Event` / `EventType` so internal
call sites keep working.

When 9.5.0 final ships and the spec's "9.5.0-pending" block in
`docs/api-stability.md` flips to "current," this file is the golden anchor
for the public surface — adding a test here is the convention for asserting
that a new symbol is part of the contract.
"""

from __future__ import annotations

from dataclasses import is_dataclass
from enum import StrEnum

import pytest


def test_event_dataclass_importable_from_protocol():
    from agentirc.protocol import Event

    assert is_dataclass(Event)
    e = Event(type="user.join", channel="#room", nick="alice")
    assert e.type == "user.join"
    assert e.channel == "#room"
    assert e.nick == "alice"
    assert e.data == {}
    assert isinstance(e.timestamp, float)


def test_event_type_is_strenum():
    from agentirc.protocol import EventType

    assert issubclass(EventType, StrEnum)
    # StrEnum members compare equal to their string values — this is the whole
    # point of using StrEnum at JSON boundaries.
    assert EventType.JOIN == "user.join"
    assert EventType.MESSAGE == "message"
    assert EventType.ROOM_CREATE == "room.create"
    # Member is a string instance.
    assert isinstance(EventType.JOIN, str)


def test_event_type_has_twenty_members():
    from agentirc.protocol import EventType

    assert len(list(EventType)) == 20


@pytest.mark.parametrize(
    "member_name,wire_value,constant_name",
    [
        ("MESSAGE", "message", "EVENT_TYPE_MESSAGE"),
        ("JOIN", "user.join", "EVENT_TYPE_USER_JOIN"),
        ("PART", "user.part", "EVENT_TYPE_USER_PART"),
        ("QUIT", "user.quit", "EVENT_TYPE_USER_QUIT"),
        ("TOPIC", "topic", "EVENT_TYPE_TOPIC"),
        ("ROOMMETA", "room.meta", "EVENT_TYPE_ROOM_META"),
        ("TAGS", "tags.update", "EVENT_TYPE_TAGS_UPDATE"),
        ("ROOMARCHIVE", "room.archive", "EVENT_TYPE_ROOM_ARCHIVE"),
        ("THREAD_CREATE", "thread.create", "EVENT_TYPE_THREAD_CREATE"),
        ("THREAD_MESSAGE", "thread.message", "EVENT_TYPE_THREAD_MESSAGE"),
        ("THREAD_CLOSE", "thread.close", "EVENT_TYPE_THREAD_CLOSE"),
        ("AGENT_CONNECT", "agent.connect", "EVENT_TYPE_AGENT_CONNECT"),
        ("AGENT_DISCONNECT", "agent.disconnect", "EVENT_TYPE_AGENT_DISCONNECT"),
        ("CONSOLE_OPEN", "console.open", "EVENT_TYPE_CONSOLE_OPEN"),
        ("CONSOLE_CLOSE", "console.close", "EVENT_TYPE_CONSOLE_CLOSE"),
        ("SERVER_WAKE", "server.wake", "EVENT_TYPE_SERVER_WAKE"),
        ("SERVER_SLEEP", "server.sleep", "EVENT_TYPE_SERVER_SLEEP"),
        ("SERVER_LINK", "server.link", "EVENT_TYPE_SERVER_LINK"),
        ("SERVER_UNLINK", "server.unlink", "EVENT_TYPE_SERVER_UNLINK"),
        ("ROOM_CREATE", "room.create", "EVENT_TYPE_ROOM_CREATE"),
    ],
)
def test_event_type_vocabulary_parity(member_name, wire_value, constant_name):
    """Every EventType member has a matching EVENT_TYPE_* constant with the same wire value."""
    from agentirc import protocol

    member = getattr(protocol.EventType, member_name)
    constant = getattr(protocol, constant_name)
    assert member.value == wire_value
    assert constant == wire_value
    assert member == constant  # StrEnum makes this hold


def test_bot_extension_verb_constants():
    """The five new verb constants exist with their canonical wire values."""
    from agentirc.protocol import EVENT, EVENTERR, EVENTPUB, EVENTSUB, EVENTUNSUB

    assert EVENTSUB == "EVENTSUB"
    assert EVENTUNSUB == "EVENTUNSUB"
    assert EVENT == "EVENT"
    assert EVENTERR == "EVENTERR"
    assert EVENTPUB == "EVENTPUB"


def test_bot_cap_token():
    """The bot capability is the IRCv3 vendored-namespace token."""
    from agentirc.protocol import BOT_CAP

    assert BOT_CAP == "agentirc.io/bot"


def test_skill_module_still_re_exports():
    """`agentirc.skill` re-exports `Event` / `EventType` for backward compat.

    Internal call sites (`agentirc/ircd.py`, `agentirc/skills/*`) and any
    pre-9.5 vendored consumer that imports from `agentirc.skill` keep working.
    The re-export shim is scheduled for removal in 9.6.0.
    """
    from agentirc.skill import Event as SkillEvent
    from agentirc.skill import EventType as SkillEventType
    from agentirc.protocol import Event as ProtoEvent
    from agentirc.protocol import EventType as ProtoEventType

    # `is` identity — re-export, not a copy.
    assert SkillEvent is ProtoEvent
    assert SkillEventType is ProtoEventType


def test_event_tolerates_unknown_type_string():
    """`Event.type` is widened to `EventType | str` for forward-compat."""
    from agentirc.protocol import Event

    # An unknown type from a federation peer running newer agentirc round-trips
    # without raising. The type is the bare string; callers must tolerate it.
    e = Event(type="some.future.event", channel=None, nick="")
    assert e.type == "some.future.event"


def test_server_config_event_queue_field():
    """The new event_subscription_queue_max field is part of ServerConfig."""
    from agentirc.config import ServerConfig

    cfg = ServerConfig()
    assert cfg.event_subscription_queue_max == 1024


def test_server_config_yaml_loads_event_queue_max(tmp_path):
    """ServerConfig.from_yaml recognises event_subscription_queue_max."""
    from agentirc.config import ServerConfig

    yaml_path = tmp_path / "server.yaml"
    yaml_path.write_text("event_subscription_queue_max: 256\n")
    cfg = ServerConfig.from_yaml(yaml_path)
    assert cfg.event_subscription_queue_max == 256


def test_cli_resolve_config_propagates_event_queue_max(tmp_path, monkeypatch):
    """`agentirc serve --config ...` honors event_subscription_queue_max from YAML.

    Regression guard for PR #17 review (Copilot 3176158134): the cli-level
    config resolver previously dropped the field on the floor, so the dataclass
    default leaked through even when YAML overrode it.
    """
    import argparse

    from agentirc.cli import _resolve_config

    yaml_path = tmp_path / "server.yaml"
    yaml_path.write_text("event_subscription_queue_max: 42\n")

    args = argparse.Namespace(
        config=str(yaml_path),
        name=None,
        host=None,
        port=None,
        webhook_port=None,
        data_dir=None,
        link=None,
    )
    cfg = _resolve_config(args)
    assert cfg.event_subscription_queue_max == 42


def test_protocol_all_exports_new_symbols():
    """`__all__` lists the new bot-extension members so star-imports get them."""
    from agentirc import protocol

    expected_new = {
        "BOT_CAP",
        "Event",
        "EventType",
        "EVENT",
        "EVENTERR",
        "EVENTPUB",
        "EVENTSUB",
        "EVENTUNSUB",
        "EVENT_TYPE_AGENT_CONNECT",
        "EVENT_TYPE_AGENT_DISCONNECT",
        "EVENT_TYPE_CONSOLE_CLOSE",
        "EVENT_TYPE_CONSOLE_OPEN",
        "EVENT_TYPE_MESSAGE",
        "EVENT_TYPE_ROOM_ARCHIVE",
        "EVENT_TYPE_ROOM_CREATE",
        "EVENT_TYPE_ROOM_META",
        "EVENT_TYPE_SERVER_LINK",
        "EVENT_TYPE_SERVER_SLEEP",
        "EVENT_TYPE_SERVER_UNLINK",
        "EVENT_TYPE_SERVER_WAKE",
        "EVENT_TYPE_TAGS_UPDATE",
        "EVENT_TYPE_THREAD_CLOSE",
        "EVENT_TYPE_THREAD_CREATE",
        "EVENT_TYPE_THREAD_MESSAGE",
        "EVENT_TYPE_TOPIC",
        "EVENT_TYPE_USER_JOIN",
        "EVENT_TYPE_USER_PART",
        "EVENT_TYPE_USER_QUIT",
    }
    assert expected_new.issubset(set(protocol.__all__))
