"""Tests for agentirc.bots.bot_manager (the central BotManager).

Two parts:

1. Contract conformance — the real ``BotManager`` exposes the same six
   members the no-op stub (``agentirc._internal.bots.bot_manager``) declares,
   with matching sync/async-ness. ``IRCd.start()`` calls these, so they must
   survive the paraphrase.

2. End-to-end lifecycle — boot a real in-process IRCd, install a real
   ``BotManager``, write a YAML bot spec (event filter + reply template) into
   a tmp bots dir the manager loads from, ``load_bots()``, dispatch a matching
   ``Event`` through ``on_event``, and assert the bot replies. The reply is
   observed for real: a TCP test client joined to the bot's channel receives
   the bot's PRIVMSG carrying the rendered template text.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from agentirc.bots import bot_manager as bot_manager_mod
from agentirc.bots import bot as bot_mod
from agentirc.bots import config as bots_config_mod
from agentirc.bots.bot_manager import BotManager
from agentirc.protocol import Event, EventType


# --------------------------------------------------------------------------
# Part 1 — stub contract conformance
# --------------------------------------------------------------------------

#: (name, must_be_coroutine_function)
_CONTRACT_MEMBERS = [
    ("load_bots", True),
    ("load_system_bots", False),
    ("get_bot", False),
    ("on_event", True),
    ("stop_all", True),
]


def test_bot_manager_init_signature_matches_stub():
    """__init__ accepts a single positional ``server`` arg, like the stub."""
    sig = inspect.signature(BotManager.__init__)
    params = list(sig.parameters)
    assert params[:2] == ["self", "server"]


@pytest.mark.parametrize("name, is_coro", _CONTRACT_MEMBERS)
def test_bot_manager_exposes_contract_member(name, is_coro):
    """Each stub-contract member exists with the right sync/async-ness."""
    assert hasattr(BotManager, name), f"BotManager missing contract member {name!r}"
    member = getattr(BotManager, name)
    assert callable(member)
    assert inspect.iscoroutinefunction(member) is is_coro, (
        f"{name}: expected coroutine={is_coro}, "
        f"got coroutine={inspect.iscoroutinefunction(member)}"
    )


# --------------------------------------------------------------------------
# Part 2 — end-to-end lifecycle (keystone test, proves honesty condition h1)
# --------------------------------------------------------------------------

# A reply line the template will render. Distinct enough to grep for in the
# stream of welcome/JOIN traffic.
_REPLY_TEXT = "pong-from-bot"

# Custom trigger type so the bot's own reply (a "message" event emitted by
# send_to_channel) cannot re-trigger the bot and loop.
_TRIGGER_TYPE = "ping.request"

_BOT_NICK = "pingbot"
_BOT_CHANNEL = "#botchan"

_BOT_YAML = f"""\
bot:
  name: {_BOT_NICK}
  owner: tester
  description: replies to ping.request events
trigger:
  type: event
  filter: "type == '{_TRIGGER_TYPE}'"
output:
  channels:
    - "{_BOT_CHANNEL}"
  template: "{_REPLY_TEXT} {{event.data.who}}"
  fallback: json
"""


@pytest.fixture
def bots_dir(tmp_path, monkeypatch):
    """A hermetic tmp bots dir, wired into every module that reads BOTS_DIR.

    ``BOTS_DIR`` is imported by-name into ``bot``, ``bot_manager`` and read
    from ``config``; patch all three namespaces so the manager loads from the
    tmp dir and the test never touches the real ``~/.culture/bots``.
    """
    d = tmp_path / "bots"
    d.mkdir()
    monkeypatch.setattr(bots_config_mod, "BOTS_DIR", d)
    monkeypatch.setattr(bot_mod, "BOTS_DIR", d)
    monkeypatch.setattr(bot_manager_mod, "BOTS_DIR", d)
    return d


def _write_bot(bots_dir):
    """Lay out the on-disk bot exactly how load_bot_config expects."""
    bot_dir = bots_dir / _BOT_NICK
    bot_dir.mkdir()
    (bot_dir / bots_config_mod.BOT_CONFIG_FILE).write_text(_BOT_YAML)


@pytest.mark.asyncio
async def test_bot_manager_loads_yaml_bot_and_replies(server, make_client, bots_dir):
    """A YAML-defined event bot loads, matches an event, and replies.

    Real path end to end: BotManager.load_bots() reads the YAML, starts the
    bot (which joins #botchan via a real VirtualClient), on_event() evaluates
    the compiled filter and dispatches handle(), and the rendered template is
    delivered as a PRIVMSG. We observe the reply on a real TCP client that is
    a member of #botchan.
    """
    _write_bot(bots_dir)

    # A real client, joined to the bot's channel, to witness the reply.
    # Nicks carry the server-name prefix (culture/agentirc registration
    # convention) — the bare nick would be rejected and JOIN ignored.
    witness = await make_client(nick="testserv-witness", user="witness")
    await witness.send(f"JOIN {_BOT_CHANNEL}")
    await witness.recv_until(_BOT_CHANNEL)  # drain JOIN/NAMES

    manager = BotManager(server)
    await manager.load_bots()

    # Bot loaded and started: registered, active, and joined to the channel.
    bot = manager.get_bot(_BOT_NICK)
    assert bot is not None
    assert bot.active is True
    channel = server.channels.get(_BOT_CHANNEL)
    assert channel is not None
    assert bot.virtual_client in channel.members

    # Drain the bot's own JOIN broadcast so the next recv is the reply.
    await witness.recv_all(timeout=0.3)

    # Dispatch a MATCHING event through the manager. The trigger type is a
    # custom (non-enum) event type, carried by a stand-in mirroring Bot's own
    # dynamic-event-type shape (exposes ``.value`` like an EventType member),
    # which is exactly what on_event reads.
    assert _TRIGGER_TYPE not in {e.value for e in EventType}
    await manager.on_event(
        Event(
            type=_StrType(_TRIGGER_TYPE),
            channel=_BOT_CHANNEL,
            nick="someone",
            data={"who": "alice"},
        )
    )

    # Observe the reply on the witness client: a PRIVMSG from the bot to the
    # channel carrying the rendered template text.
    line = await witness.recv_until(_REPLY_TEXT)
    expected = f"{_REPLY_TEXT} alice"
    assert f"PRIVMSG {_BOT_CHANNEL}" in line, line
    assert f":{_BOT_NICK}!" in line, line
    assert expected in line, line

    await manager.stop_all()
    assert bot.active is False


@pytest.mark.asyncio
async def test_bot_manager_non_matching_event_no_reply(server, make_client, bots_dir):
    """A non-matching event does NOT trigger the bot (filter actually gates)."""
    _write_bot(bots_dir)

    witness = await make_client(nick="testserv-witness2", user="witness2")
    await witness.send(f"JOIN {_BOT_CHANNEL}")
    await witness.recv_until(_BOT_CHANNEL)

    manager = BotManager(server)
    await manager.load_bots()
    await witness.recv_all(timeout=0.3)

    # An event whose type does not match the bot's filter.
    await manager.on_event(
        Event(
            type=EventType.JOIN,
            channel=_BOT_CHANNEL,
            nick="someone",
            data={"who": "bob"},
        )
    )

    line = await witness.recv_until(_REPLY_TEXT)
    assert _REPLY_TEXT not in line

    await manager.stop_all()


class _StrType:
    """Minimal stand-in mirroring Bot's dynamic-event-type shape (has .value)."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value
