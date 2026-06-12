"""Bot filter DSL — safe expressions over agentirc Event dicts.

Covers every DSL operator:
  ==   equality comparison
  !=   inequality comparison
  in   membership (list literal) and (string membership)
  and  logical conjunction
  or   logical disjunction
  not  logical negation
  .    dotted field access (nested dict field)
  ()   parenthesised sub-expression for precedence
  []   list literal

Evaluates the DSL against a real constructed `agentirc.protocol.Event`
converted to a dict via `dataclasses.asdict()`, per the acceptance criterion.

Malformed filter strings are asserted to raise `FilterParseError`.
"""

import dataclasses

import pytest

from agentirc.bots.filter_dsl import (
    FilterParseError,
    compile_filter,
    evaluate,
    _MISSING,
)
from agentirc.protocol import Event, EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_event(
    type: str = EventType.JOIN,
    channel: str | None = "#general",
    nick: str = "ori",
    data: dict | None = None,
) -> dict:
    """Build a real agentirc.protocol.Event then serialise to a plain dict.

    The filter DSL evaluator operates on plain dicts (via _resolve / FieldRef),
    so callers must call dataclasses.asdict() before passing to evaluate().
    This helper centralises that pattern so each test stays focused on the
    DSL behaviour it is exercising.
    """
    if data is None:
        data = {"nick": nick}
    ev = Event(type=type, channel=channel, nick=nick, data=data)
    return dataclasses.asdict(ev)


# ---------------------------------------------------------------------------
# == operator
# ---------------------------------------------------------------------------


def test_equality_true():
    f = compile_filter("type == 'user.join'")
    assert evaluate(f, make_event(type=EventType.JOIN)) is True


def test_equality_false():
    f = compile_filter("type == 'user.join'")
    assert evaluate(f, make_event(type=EventType.PART)) is False


# ---------------------------------------------------------------------------
# != operator
# ---------------------------------------------------------------------------


def test_inequality_true():
    f = compile_filter("type != 'user.join'")
    assert evaluate(f, make_event(type=EventType.PART)) is True


def test_inequality_false():
    f = compile_filter("type != 'user.join'")
    assert evaluate(f, make_event(type=EventType.JOIN)) is False


# ---------------------------------------------------------------------------
# in operator (membership in a list literal)
# ---------------------------------------------------------------------------


def test_in_list_true():
    f = compile_filter("type in ['user.join', 'user.part']")
    assert evaluate(f, make_event(type=EventType.JOIN)) is True
    assert evaluate(f, make_event(type=EventType.PART)) is True


def test_in_list_false():
    f = compile_filter("type in ['user.join', 'user.part']")
    assert evaluate(f, make_event(type=EventType.MESSAGE)) is False


# ---------------------------------------------------------------------------
# in operator (membership in a string / list field value)
# ---------------------------------------------------------------------------


def test_in_string_field_true():
    f = compile_filter("'research' in data.tags")
    ev = make_event(data={"tags": ["research", "ai"]})
    assert evaluate(f, ev) is True


def test_in_string_field_false():
    f = compile_filter("'research' in data.tags")
    ev = make_event(data={"tags": ["games"]})
    assert evaluate(f, ev) is False


# ---------------------------------------------------------------------------
# and operator
# ---------------------------------------------------------------------------


def test_and_both_true():
    f = compile_filter("type == 'user.join' and channel == '#general'")
    assert evaluate(f, make_event(type=EventType.JOIN, channel="#general")) is True


def test_and_one_false():
    f = compile_filter("type == 'user.join' and channel == '#general'")
    assert evaluate(f, make_event(type=EventType.JOIN, channel="#other")) is False


# ---------------------------------------------------------------------------
# or operator
# ---------------------------------------------------------------------------


def test_or_first_branch_true():
    f = compile_filter("type == 'user.join' or type == 'user.part'")
    assert evaluate(f, make_event(type=EventType.JOIN)) is True


def test_or_second_branch_true():
    f = compile_filter("type == 'user.join' or type == 'user.part'")
    assert evaluate(f, make_event(type=EventType.PART)) is True


def test_or_both_false():
    f = compile_filter("type == 'user.join' or type == 'user.part'")
    assert evaluate(f, make_event(type=EventType.MESSAGE)) is False


# ---------------------------------------------------------------------------
# not operator
# ---------------------------------------------------------------------------


def test_not_negates_true_to_false():
    f = compile_filter("not (type == 'user.join')")
    assert evaluate(f, make_event(type=EventType.JOIN)) is False


def test_not_negates_false_to_true():
    f = compile_filter("not (type == 'user.join')")
    assert evaluate(f, make_event(type=EventType.PART)) is True


# ---------------------------------------------------------------------------
# dotted field access  (. operator — nested dict field resolution)
# ---------------------------------------------------------------------------


def test_dotted_field_true():
    f = compile_filter("data.nick == 'ori'")
    assert evaluate(f, make_event(nick="ori", data={"nick": "ori"})) is True


def test_dotted_field_false():
    f = compile_filter("data.nick == 'ori'")
    assert evaluate(f, make_event(nick="bob", data={"nick": "bob"})) is False


def test_dotted_field_missing_short_circuits():
    """Accessing a nonexistent nested key returns _MISSING, which is falsy."""
    f = compile_filter("data.nonexistent == 'x'")
    assert evaluate(f, make_event()) is False


# ---------------------------------------------------------------------------
# Parentheses for precedence
# ---------------------------------------------------------------------------


def test_parens_force_or_before_and_true():
    f = compile_filter("(type == 'user.join' or type == 'user.part') and channel == '#general'")
    assert evaluate(f, make_event(type=EventType.JOIN, channel="#general")) is True
    assert evaluate(f, make_event(type=EventType.PART, channel="#general")) is True


def test_parens_force_or_before_and_false():
    f = compile_filter("(type == 'user.join' or type == 'user.part') and channel == '#general'")
    assert evaluate(f, make_event(type=EventType.JOIN, channel="#other")) is False


# ---------------------------------------------------------------------------
# Bare missing field is _MISSING (falsy in boolean context)
# ---------------------------------------------------------------------------


def test_bare_missing_field_is_missing():
    f = compile_filter("data.missing")
    assert evaluate(f, make_event()) is _MISSING


def test_bare_missing_field_short_circuits_and():
    f = compile_filter("data.missing and type == 'user.join'")
    assert evaluate(f, make_event(type=EventType.JOIN)) is False


def test_not_missing_field_is_true():
    f = compile_filter("not data.missing")
    assert evaluate(f, make_event()) is True


# ---------------------------------------------------------------------------
# Error cases: malformed filter raises FilterParseError
# ---------------------------------------------------------------------------


def test_parse_error_single_equals():
    """Single '=' is not a valid operator; parser must raise FilterParseError."""
    with pytest.raises(FilterParseError) as exc_info:
        compile_filter("type = 'user.join'")
    assert exc_info.value.column >= 0
    assert exc_info.value.expected


def test_parse_error_unclosed_string():
    with pytest.raises(FilterParseError):
        compile_filter("type == 'unclosed")


def test_parse_error_function_calls_not_allowed():
    """Function-call syntax is explicitly rejected by the DSL."""
    with pytest.raises(FilterParseError):
        compile_filter("exec('x')")


def test_parse_error_unknown_character():
    """A character outside the DSL grammar (e.g. '@') must raise FilterParseError."""
    with pytest.raises(FilterParseError):
        compile_filter("@ == 'x'")


def test_parse_error_trailing_tokens():
    """Extra tokens after a complete expression must raise FilterParseError."""
    with pytest.raises(FilterParseError):
        compile_filter("type == 'user.join' extra")
