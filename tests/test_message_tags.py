"""msgid / server-time / thread tags on delivered messages (task t3).

Agent-accessibility release. Clients that negotiate the IRCv3 ``message-tags``
capability receive a ``msgid`` tag (unique per inbound message, identical
across channel fan-out) and a ``time`` tag (IRCv3 server-time, ISO8601 UTC,
millisecond precision, trailing ``Z``) on every PRIVMSG delivery. Thread
messages additionally carry ``agentirc.io/thread=<name>`` alongside the legacy
``[thread:<name>]`` text prefix. Clients that did NOT negotiate ``message-tags``
see byte-identical wire output — the tags are stripped by ``Client.send_tagged``
(the golden lock lives in ``tests/test_wire_format_envelope.py``, unmodified).

The MESSAGE event emitted for a channel PRIVMSG carries the same id in
``event.data["msgid"]`` so in-process consumers (history, event subscriptions)
observe the same id the wire recipients saw.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from datetime import datetime, timezone

import pytest

from agentirc._internal.protocol.message import Message
from agentirc.protocol import MSGID_TAG, SERVER_TIME_TAG, THREAD_TAG

# IRCv3 server-time: ISO8601 UTC, millisecond precision, trailing Z.
_SERVER_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
# uuid4 string shape (msgid). Kept loose on the version nibble to only
# assert "looks like a uuid", not to pin the exact generation strategy.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


async def _mt_client(make_client, nick: str, user: str):
    """Register a client and negotiate the ``message-tags`` capability."""
    c = await make_client(nick, user)
    await c.send("CAP REQ :message-tags")
    await c.recv_until("CAP")
    return c


async def _make_bot(make_client, nick: str, user: str):
    """Register a client and negotiate ``agentirc.io/bot`` (for EVENTSUB)."""
    c = await make_client(nick, user)
    await c.send("CAP REQ :agentirc.io/bot")
    await c.recv_until("CAP")
    await asyncio.sleep(0.05)
    await c.recv_all(timeout=0.2)
    return c


async def _join(client, channel: str) -> None:
    await client.send(f"JOIN {channel}")
    await client.recv_until("366")


def _privmsg_line(chunk: str) -> str:
    """Return the single PRIVMSG wire line inside a recv'd chunk."""
    for line in chunk.split("\r\n"):
        if " PRIVMSG " in line:
            return line
    raise AssertionError(f"no PRIVMSG line in {chunk!r}")


# ---------------------------------------------------------------------------
# Channel PRIVMSG: msgid + time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_privmsg_carries_msgid_and_time(server, make_client):
    """A message-tags recipient sees ``msgid`` and ``time`` on a channel PRIVMSG."""
    alice = await _mt_client(make_client, "testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#mt-room")
    await _join(bob, "#mt-room")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    await alice.send("PRIVMSG #mt-room :hello tagged world")
    line = _privmsg_line(await bob.recv())
    msg = Message.parse(line)

    assert msg.command == "PRIVMSG"
    assert msg.params == ["#mt-room", "hello tagged world"]
    assert _UUID_RE.match(msg.tags.get(MSGID_TAG, "")), f"bad msgid: {msg.tags!r}"
    assert _SERVER_TIME_RE.match(msg.tags.get(SERVER_TIME_TAG, "")), f"bad time: {msg.tags!r}"
    # The [thread:] tag must NOT appear on a plain channel message.
    assert THREAD_TAG not in msg.tags


@pytest.mark.asyncio
async def test_channel_msgid_stable_across_fanout(server, make_client):
    """Two recipients of the same channel message see the identical msgid."""
    alice = await _mt_client(make_client, "testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    carol = await _mt_client(make_client, "testserv-carol", "carol")
    await _join(alice, "#fan")
    await _join(bob, "#fan")
    await _join(carol, "#fan")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)
    await carol.recv_all(timeout=0.3)

    await alice.send("PRIVMSG #fan :fan-out message")
    bob_msg = Message.parse(_privmsg_line(await bob.recv()))
    carol_msg = Message.parse(_privmsg_line(await carol.recv()))

    bob_id = bob_msg.tags.get(MSGID_TAG)
    carol_id = carol_msg.tags.get(MSGID_TAG)
    assert bob_id and carol_id
    assert bob_id == carol_id, f"fan-out msgid diverged: {bob_id!r} != {carol_id!r}"


@pytest.mark.asyncio
async def test_msgids_differ_across_messages(server, make_client):
    """Two distinct channel messages get two distinct msgids."""
    alice = await _mt_client(make_client, "testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#uniq")
    await _join(bob, "#uniq")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    await alice.send("PRIVMSG #uniq :first")
    first = Message.parse(_privmsg_line(await bob.recv()))
    await alice.send("PRIVMSG #uniq :second")
    second = Message.parse(_privmsg_line(await bob.recv()))

    assert first.tags[MSGID_TAG] != second.tags[MSGID_TAG]


@pytest.mark.asyncio
async def test_time_tag_is_iso8601_utc_and_recent(server, make_client):
    """The ``time`` tag parses as ISO8601 UTC (ms + Z) and is close to now."""
    alice = await _mt_client(make_client, "testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#clock")
    await _join(bob, "#clock")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    before = time.time()
    await alice.send("PRIVMSG #clock :tick")
    msg = Message.parse(_privmsg_line(await bob.recv()))
    after = time.time()

    value = msg.tags[SERVER_TIME_TAG]
    assert _SERVER_TIME_RE.match(value), f"server-time not ms-precision Z: {value!r}"
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    epoch = parsed.timestamp()
    # Allow a generous window for slow CI; the point is "server clock, now".
    assert before - 5 <= epoch <= after + 5, f"server-time {value!r} not near now"


# ---------------------------------------------------------------------------
# DM PRIVMSG: msgid + time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_privmsg_carries_msgid_and_time(server, make_client):
    """A message-tags recipient sees ``msgid`` and ``time`` on a direct message."""
    carol = await make_client("testserv-carol", "carol")
    dave = await _mt_client(make_client, "testserv-dave", "dave")
    await dave.recv_all(timeout=0.2)

    await carol.send("PRIVMSG testserv-dave :direct tagged hello")
    msg = Message.parse(_privmsg_line(await dave.recv()))

    assert msg.params == ["testserv-dave", "direct tagged hello"]
    assert _UUID_RE.match(msg.tags.get(MSGID_TAG, "")), f"bad msgid: {msg.tags!r}"
    assert _SERVER_TIME_RE.match(msg.tags.get(SERVER_TIME_TAG, "")), f"bad time: {msg.tags!r}"


# ---------------------------------------------------------------------------
# Thread messages: thread tag + msgid + time, [thread:] prefix preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_message_carries_thread_tag(server, make_client):
    """THREAD CREATE delivery carries the thread tag, msgid, time; prefix stays."""
    alice = await _mt_client(make_client, "testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#threadroom")
    await _join(bob, "#threadroom")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    await alice.send("THREAD CREATE #threadroom my-thread :kick it off")
    msg = Message.parse(_privmsg_line(await bob.recv()))

    # The legacy text prefix is untouched.
    assert msg.params == ["#threadroom", "[thread:my-thread] kick it off"]
    # The tag carries the bare thread name.
    assert msg.tags.get(THREAD_TAG) == "my-thread"
    assert _UUID_RE.match(msg.tags.get(MSGID_TAG, "")), f"bad msgid: {msg.tags!r}"
    assert _SERVER_TIME_RE.match(msg.tags.get(SERVER_TIME_TAG, "")), f"bad time: {msg.tags!r}"


@pytest.mark.asyncio
async def test_thread_reply_carries_thread_tag(server, make_client):
    """THREAD REPLY delivery also carries the thread tag alongside the prefix."""
    alice = await _mt_client(make_client, "testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#threadreply")
    await _join(bob, "#threadreply")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    await alice.send("THREAD CREATE #threadreply conv :first")
    await bob.recv_all(timeout=0.3)
    await alice.send("THREAD REPLY #threadreply conv :follow up")
    msg = Message.parse(_privmsg_line(await bob.recv()))

    assert msg.params == ["#threadreply", "[thread:conv] follow up"]
    assert msg.tags.get(THREAD_TAG) == "conv"
    assert _UUID_RE.match(msg.tags.get(MSGID_TAG, ""))


# ---------------------------------------------------------------------------
# Non-negotiated clients see no tags (byte-identical)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_client_sees_no_tags(server, make_client):
    """A client that did NOT negotiate message-tags receives an untagged PRIVMSG."""
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    await _join(alice, "#plainroom")
    await _join(bob, "#plainroom")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    await alice.send("PRIVMSG #plainroom :no tags here")
    line = _privmsg_line(await bob.recv())

    # No leading @-tag block at all — exact byte shape as before this change.
    assert line == ":testserv-alice!alice@127.0.0.1 PRIVMSG #plainroom :no tags here"
    assert not Message.parse(line).tags


@pytest.mark.asyncio
async def test_plain_client_thread_message_untagged(server, make_client):
    """A plain client sees the [thread:] prefix but no tag block."""
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    await _join(alice, "#plainthread")
    await _join(bob, "#plainthread")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    await alice.send("THREAD CREATE #plainthread topic :hi there")
    line = _privmsg_line(await bob.recv())

    assert line == (
        ":testserv-alice!alice@127.0.0.1 PRIVMSG #plainthread :[thread:topic] hi there"
    )
    assert not Message.parse(line).tags


# ---------------------------------------------------------------------------
# The MESSAGE event carries data["msgid"] (via EVENTSUB), matching the wire id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_event_carries_msgid_matching_wire(server, make_client):
    """The MESSAGE event's ``data['msgid']`` matches the id the recipient saw."""
    watcher = await _make_bot(make_client, "testserv-watcher", "watcher")
    await watcher.send("EVENTSUB sub1 type=message channel=#eventroom")
    await asyncio.sleep(0.05)

    alice = await make_client("testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#eventroom")
    await _join(bob, "#eventroom")
    await alice.recv_all(timeout=0.3)
    await bob.recv_all(timeout=0.3)

    await alice.send("PRIVMSG #eventroom :event carries id")

    # Wire id the recipient saw.
    wire_msg = Message.parse(_privmsg_line(await bob.recv()))
    wire_msgid = wire_msg.tags[MSGID_TAG]

    # Event id the subscription stream carried.
    block = await watcher.recv_until("EVENT")
    event_line = next(ln for ln in block.split("\r\n") if " EVENT sub1 " in ln)
    b64 = event_line.split(":", 2)[-1]
    envelope = json.loads(base64.b64decode(b64))

    assert envelope["type"] == "message"
    assert envelope["channel"] == "#eventroom"
    assert envelope["data"].get("text") == "event carries id"
    assert envelope["data"].get("msgid") == wire_msgid, (
        f"event msgid {envelope['data'].get('msgid')!r} != wire msgid {wire_msgid!r}"
    )
