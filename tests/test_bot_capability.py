"""Tests for the ``agentirc.io/bot`` IRCv3 capability (9.5.0).

Spec: ``docs/superpowers/specs/2026-05-01-bot-extension-api-design.md``
§ Decision C. The capability gates four behaviours when negotiated:

- silent JOIN/PART/QUIT broadcasts (other channel members see nothing)
- no auto-op on first-joiner of a fresh channel
- ``+`` prefix in NAMES output
- ``B`` flag in WHO output

These tests exercise each behaviour against the running daemon. ``CAP``
processing has no registration gate so we register first via ``make_client``
and then negotiate the capability.
"""

from __future__ import annotations

import asyncio

import pytest


async def _negotiate_bot_cap(client) -> None:
    """Send ``CAP REQ :agentirc.io/bot`` and drain the ACK."""
    await client.send("CAP REQ :agentirc.io/bot")
    await client.recv_until("CAP")


@pytest.mark.asyncio
async def test_cap_ls_advertises_bot_capability(make_client):
    """CAP LS lists both ``message-tags`` and ``agentirc.io/bot``."""
    c = await make_client("testserv-prober", "prober")
    await c.send("CAP LS")
    line = await c.recv_until("CAP")
    # CAP LS reply lists the supported caps. Locked-in semver-stable token.
    assert "message-tags" in line
    assert "agentirc.io/bot" in line


@pytest.mark.asyncio
async def test_cap_req_bot_acked(make_client):
    """``CAP REQ :agentirc.io/bot`` is ACKed and adds the cap to the connection."""
    c = await make_client("testserv-asker", "asker")
    await c.send("CAP REQ :agentirc.io/bot")
    line = await c.recv_until("CAP")
    assert " ACK :agentirc.io/bot" in line


@pytest.mark.asyncio
async def test_cap_req_bot_with_message_tags_acked(make_client):
    """Both caps can be requested in one CAP REQ."""
    c = await make_client("testserv-multi", "multi")
    await c.send("CAP REQ :agentirc.io/bot message-tags")
    line = await c.recv_until("CAP")
    assert " ACK :" in line
    assert "agentirc.io/bot" in line
    assert "message-tags" in line


@pytest.mark.asyncio
async def test_cap_req_unknown_cap_naked(make_client):
    """An unknown cap NAKs the entire request — atomic semantics."""
    c = await make_client("testserv-bad", "bad")
    await c.send("CAP REQ :totally-not-a-cap")
    line = await c.recv_until("CAP")
    assert " NAK :" in line


@pytest.mark.asyncio
async def test_silent_join_no_broadcast_to_other_members(server, make_client):
    """A bot-CAP client joining a channel does not produce a JOIN line for other members."""
    # Human observer in #room first.
    human = await make_client("testserv-alice", "alice")
    await human.send("JOIN #room")
    await human.recv_until("366")  # end of NAMES
    await asyncio.sleep(0.05)
    await human.recv_all(timeout=0.2)  # drain join-event PRIVMSGs etc.

    # Bot client registers, negotiates CAP, then joins #room.
    bot = await make_client("testserv-bob", "bob")
    await _negotiate_bot_cap(bot)
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)
    await bot.send("JOIN #room")
    # The bot doesn't receive a JOIN echo either (broadcast suppressed for
    # the whole loop including self), but it still gets topic + NAMES so
    # 366 (end of NAMES) is the success signal.
    await bot.recv_until("366")
    await asyncio.sleep(0.2)

    # Human should NOT see a JOIN line from the bot in #room.
    # recv_all returns a list of received lines; recv() chunks may contain
    # multiple wire lines separated by \r\n, so split each chunk too.
    received = await human.recv_all(timeout=0.5)
    raw_lines = []
    for chunk in received:
        raw_lines.extend(chunk.split("\r\n"))
    for raw in raw_lines:
        # The human gets system PRIVMSGs surfaced from #system events
        # (which include "user.join" event-data). Those go to #system,
        # not #room. We check specifically for a wire-level
        # `:testserv-bob ... JOIN #room` line — that's what silent CAP
        # suppresses.
        assert not (raw.startswith(":testserv-bob") and "JOIN #room" in raw), (
            f"Bot's JOIN leaked to human despite agentirc.io/bot CAP: {raw!r}"
        )


@pytest.mark.asyncio
async def test_bot_no_auto_op_on_fresh_channel(server, make_client):
    """A bot joining an empty channel does NOT become op; the next human does."""
    bot = await make_client("testserv-mybot", "mybot")
    await _negotiate_bot_cap(bot)
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)
    await bot.send("JOIN #brandnewroom")
    await bot.recv_until("366")
    await asyncio.sleep(0.05)

    channel = server.channels.get("#brandnewroom")
    assert channel is not None
    bot_client = server.clients.get("testserv-mybot")
    assert bot_client is not None
    # Bot is in the channel...
    assert bot_client in channel.members
    # ...but is NOT op.
    assert bot_client not in channel.operators

    # A human joins; they become op (first non-bot local joiner).
    human = await make_client("testserv-alice", "alice")
    await human.send("JOIN #brandnewroom")
    await human.recv_until("366")
    await asyncio.sleep(0.05)

    human_client = server.clients.get("testserv-alice")
    assert human_client is not None
    assert human_client in channel.operators


@pytest.mark.asyncio
async def test_names_prefixes_bot_with_plus(server, make_client):
    """NAMES output renders bot-CAP members with the ``+`` prefix."""
    bot = await make_client("testserv-bottie", "bottie")
    await _negotiate_bot_cap(bot)
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)
    await bot.send("JOIN #lobby")
    await bot.recv_until("366")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)

    human = await make_client("testserv-watcher2", "watcher2")
    await human.send("JOIN #lobby")
    names_block = await human.recv_until("366")
    # The human's NAMES list should show the bot prefixed with +.
    assert "+testserv-bottie" in names_block, (
        f"Bot did not render with `+` prefix in NAMES: {names_block!r}"
    )


@pytest.mark.asyncio
async def test_who_marks_bot_with_b_flag(server, make_client):
    """WHO output's user-modes column includes ``B`` for bot-CAP clients."""
    bot = await make_client("testserv-flagged", "flagged")
    await _negotiate_bot_cap(bot)
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)
    await bot.send("JOIN #whotest")
    await bot.recv_until("366")
    await asyncio.sleep(0.05)
    await bot.recv_all(timeout=0.2)

    human = await make_client("testserv-watcher", "watcher")
    await human.send("WHO #whotest")
    who_block = await human.recv_until("315")  # RPL_ENDOFWHO
    bot_who = next(
        (ln for ln in who_block.split("\r\n") if "testserv-flagged" in ln and "352" in ln),
        None,
    )
    assert bot_who is not None, f"No WHO 352 line for bot: {who_block!r}"
    # The H flag is always present (Here); B is the agentirc bot extension.
    # WHO format puts flags as a single token: e.g. "HB", "HB@", "H@".
    assert "HB" in bot_who, (
        f"Bot WHO line missing `B` flag: {bot_who!r}"
    )


@pytest.mark.asyncio
async def test_human_who_has_no_b_flag(server, make_client):
    """A non-bot client's WHO output does NOT include ``B``."""
    human = await make_client("testserv-onlyhuman", "onlyhuman")
    await human.send("JOIN #whotest2")
    await human.recv_until("366")
    await asyncio.sleep(0.05)
    await human.recv_all(timeout=0.2)
    await human.send("WHO #whotest2")
    who_block = await human.recv_until("315")
    alice_who = next(
        (ln for ln in who_block.split("\r\n") if "testserv-onlyhuman" in ln and "352" in ln),
        None,
    )
    assert alice_who is not None
    # `H@` (Here + op) is fine; `HB` is not.
    assert "HB" not in alice_who, f"Human WHO unexpectedly has B flag: {alice_who!r}"
