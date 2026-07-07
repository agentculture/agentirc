"""Tests for the presence extension surface (PRESENCE feature, task t1).

Mirrors the style of `test_protocol_bot_exports.py`: verify the new verb
constants and `EventType` member are importable, wire-value-correct, and
listed in `__all__` so star-imports and downstream consumers pick them up.
"""

from __future__ import annotations


def test_presence_verb_constants():
    """The three new presence verb constants exist with their canonical wire values."""
    from agentirc.protocol import PRESENCE, PRESENCELIST, PRESENCEEND

    assert PRESENCE == "PRESENCE"
    assert PRESENCELIST == "PRESENCELIST"
    assert PRESENCEEND == "PRESENCEEND"


def test_event_type_presence_update_constant():
    """The bare-string EVENT_TYPE_PRESENCE_UPDATE constant matches the wire value."""
    from agentirc.protocol import EVENT_TYPE_PRESENCE_UPDATE

    assert EVENT_TYPE_PRESENCE_UPDATE == "presence.update"


def test_event_type_has_presence_member():
    """`EventType.PRESENCE` is a new StrEnum member with wire value 'presence.update'."""
    from agentirc.protocol import EventType

    assert EventType.PRESENCE == "presence.update"
    assert EventType("presence.update") is EventType.PRESENCE


def test_protocol_all_exports_presence_symbols():
    """`__all__` lists the new presence members so star-imports get them."""
    from agentirc import protocol

    expected_new = {
        "PRESENCE",
        "PRESENCELIST",
        "PRESENCEEND",
        "EVENT_TYPE_PRESENCE_UPDATE",
    }
    assert expected_new.issubset(set(protocol.__all__))
