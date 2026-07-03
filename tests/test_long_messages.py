"""Long-message handling: outbound split, explicit inbound limit error (task t4).

Agent-accessibility release. Two independent mechanisms:

Outbound (server relay): a PRIVMSG whose serialized wire line would exceed
the classic 512-byte RFC 2812 budget (prefix + PRIVMSG + target + text +
CRLF -- IRCv3 message tags ride in their own budget and do NOT count) gets
its TEXT split into multiple consecutive PRIVMSGs, each fitting the budget,
order preserved, never splitting a UTF-8 codepoint. The split happens
BEFORE msgid assignment: each chunk is its own independent message with its
own msgid/time tags, its own MESSAGE event, and its own history entry. DMs,
channel messages, and thread messages (``skills/threads.py``) all split;
thread messages stay a single *logical* message (one THREAD_CREATE /
THREAD_MESSAGE event, one dedicated-storage entry) with only the wire
delivery chunked.

Inbound (the read loop in ``Client.handle()``): a single line over
``MAX_INBOUND_LINE`` bytes (``agentirc/_internal/constants.py``) gets an
explicit NOTICE naming the limit, carrying the stable ``line-too-long``
token (``agentirc.protocol.ERROR_TOKEN_LINE_TOO_LONG``) via the
``agentirc.io/error`` tag for message-tags clients (the t2 pattern). The
line is discarded; the connection stays open; every other well-formed line
in the stream is unaffected. This replaces the old silent
"cap the buffer at 8192 chars, keep the trailing 4096" truncation -- no
input line under the limit is ever modified.

The pure ``split_message_text`` helper (``agentirc/_internal/constants.py``)
is tested directly first (no networking, deterministic budgets), then the
wire-level behavior is exercised end-to-end against a real ``IRCd``.
"""

from __future__ import annotations

import pytest

from agentirc._internal.constants import MAX_INBOUND_LINE, split_message_text
from agentirc._internal.protocol.message import Message
from agentirc.protocol import ERROR_TAG, ERROR_TOKEN_LINE_TOO_LONG, MSGID_TAG, THREAD_TAG

# Mirrors agentirc._internal.constants.PRIVMSG_WIRE_LIMIT; imported by value
# (not by name) in a couple of assertions below so the test also reads
# correctly if a reader doesn't have the module open.
PRIVMSG_WIRE_LIMIT = 512


# ---------------------------------------------------------------------------
# split_message_text: pure-function unit tests (no networking)
# ---------------------------------------------------------------------------


def test_split_returns_single_chunk_when_within_budget():
    """A short text is returned unsplit -- the common case stays a no-op."""
    text = "short message"
    assert split_message_text(text, 512) == [text]


def test_split_never_breaks_a_codepoint_and_reassembles_exactly():
    """A budget that lands mid-run of 4-byte emoji never slices one in half."""
    text = "ab" + ("\U0001f600" * 5) + "cd"  # each emoji is 4 bytes in UTF-8
    chunks = split_message_text(text, 7)
    assert len(chunks) > 1
    assert "".join(chunks) == text
    for chunk in chunks:
        # Every chunk must itself be a clean UTF-8 round trip (would already
        # be true for any Python str -- this makes the property explicit)
        # and fit the budget.
        assert chunk.encode("utf-8").decode("utf-8") == chunk
        assert len(chunk.encode("utf-8")) <= 7


def test_split_prefers_trailing_space_within_final_20_percent_window():
    """A space inside the final 20% of the window is preferred over a hard cut."""
    text = "hi there world"
    # cut = 10 chars ("hi there w"); window = chars [8, 10); the space at
    # index 8 falls inside it, so the split lands right after that space.
    chunks = split_message_text(text, 10)
    assert chunks == ["hi there ", "world"]
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-8")) <= 10 for c in chunks)


def test_split_hard_splits_when_no_space_in_window():
    """No space anywhere -> a hard split exactly at the byte budget."""
    text = "abcdefghijklmnopqrstuvwxyz"
    chunks = split_message_text(text, 10)
    assert chunks[0] == "abcdefghij"
    assert "".join(chunks) == text
    assert all(len(c.encode("utf-8")) <= 10 for c in chunks)


def test_split_degenerate_zero_budget_still_terminates():
    """A pathological (<=0) budget floors to 1 char/chunk instead of looping forever."""
    chunks = split_message_text("abc", 0)
    assert "".join(chunks) == "abc"
    assert all(chunks)  # no empty chunks


def test_split_empty_text_returns_single_empty_chunk():
    assert split_message_text("", 512) == [""]


# ---------------------------------------------------------------------------
# Wire-level helpers
# ---------------------------------------------------------------------------


async def _mt_client(make_client, nick: str, user: str):
    """Register a client and negotiate the ``message-tags`` capability."""
    c = await make_client(nick, user)
    await c.send("CAP REQ :message-tags")
    await c.recv_until("CAP")
    return c


async def _join(client, channel: str) -> None:
    await client.send(f"JOIN {channel}")
    await client.recv_until("366")


def _untagged_wire_len(line: str) -> int:
    """Byte length of *line* on the wire with IRCv3 tags stripped, + CRLF.

    IRCv3 message tags ride in their own budget and do not count against
    the classic 512-byte PRIVMSG limit (per the task's design directive) --
    so this reconstructs the tag-free core (prefix + command + params) the
    way a non-``message-tags`` client would actually see it, exactly as
    ``Client.send_tagged`` does for real.
    """
    parsed = Message.parse(line)
    core = Message(prefix=parsed.prefix, command=parsed.command, params=parsed.params)
    return len(core.format().encode("utf-8"))


# ---------------------------------------------------------------------------
# Outbound: short messages unchanged (golden, single line, byte-identical)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_channel_privmsg_is_a_single_unmodified_line(server, make_client):
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    await _join(alice, "#short-room")
    await _join(bob, "#short-room")
    await alice.recv_all(timeout=0.2)
    await bob.recv_all(timeout=0.2)

    await alice.send("PRIVMSG #short-room :hello there")
    lines = await bob.recv_all(timeout=0.3)
    assert lines == [":testserv-alice!alice@127.0.0.1 PRIVMSG #short-room :hello there"]


@pytest.mark.asyncio
async def test_short_dm_privmsg_is_a_single_unmodified_line(server, make_client):
    carol = await make_client("testserv-carol", "carol")
    dave = await make_client("testserv-dave", "dave")
    await dave.recv_all(timeout=0.2)

    await carol.send("PRIVMSG testserv-dave :hi dave")
    lines = await dave.recv_all(timeout=0.3)
    assert lines == [":testserv-carol!carol@127.0.0.1 PRIVMSG testserv-dave :hi dave"]


@pytest.mark.asyncio
async def test_short_thread_message_is_a_single_unmodified_line(server, make_client):
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    await _join(alice, "#short-thread-room")
    await _join(bob, "#short-thread-room")
    await alice.recv_all(timeout=0.2)
    await bob.recv_all(timeout=0.2)

    await alice.send("THREAD CREATE #short-thread-room greet :hello thread")
    lines = await bob.recv_all(timeout=0.3)
    assert lines == [
        ":testserv-alice!alice@127.0.0.1 PRIVMSG #short-thread-room"
        " :[thread:greet] hello thread"
    ]


# ---------------------------------------------------------------------------
# Outbound: long messages split into ordered, budget-safe, reassembling chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_channel_privmsg_splits_ordered_within_budget_and_reassembles(
    server, make_client
):
    alice = await make_client("testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#split-room")
    await _join(bob, "#split-room")
    await alice.recv_all(timeout=0.2)
    await bob.recv_all(timeout=0.2)

    text = ("the quick brown fox jumps over the lazy dog " * 15).strip()
    assert len(text.encode("utf-8")) > PRIVMSG_WIRE_LIMIT

    await alice.send(f"PRIVMSG #split-room :{text}")
    lines = await bob.recv_all(timeout=0.5)
    assert len(lines) > 1, "text should have required more than one wire line"

    for line in lines:
        assert _untagged_wire_len(line) <= PRIVMSG_WIRE_LIMIT, line

    msgs = [Message.parse(line) for line in lines]
    assert all(m.params[0] == "#split-room" for m in msgs)

    # Order preserved and exact reassembly -- concatenating chunk texts in
    # delivery order reproduces the original text.
    reassembled = "".join(m.params[1] for m in msgs)
    assert reassembled == text

    # Distinct msgid per chunk for a message-tags observer.
    msgids = [m.tags.get(MSGID_TAG) for m in msgs]
    assert all(msgids), f"missing msgid on some chunk: {msgids}"
    assert len(set(msgids)) == len(msgids), f"duplicate msgid across chunks: {msgids}"


@pytest.mark.asyncio
async def test_long_channel_privmsg_plain_client_sees_no_tag_block(server, make_client):
    """A non-message-tags recipient still gets clean, budget-safe, ordered chunks."""
    alice = await make_client("testserv-alice", "alice")
    carol = await make_client("testserv-carol", "carol")
    await _join(alice, "#split-room-plain")
    await _join(carol, "#split-room-plain")
    await alice.recv_all(timeout=0.2)
    await carol.recv_all(timeout=0.2)

    text = ("lorem ipsum dolor sit amet consectetur " * 15).strip()
    assert len(text.encode("utf-8")) > PRIVMSG_WIRE_LIMIT

    await alice.send(f"PRIVMSG #split-room-plain :{text}")
    lines = await carol.recv_all(timeout=0.5)
    assert len(lines) > 1
    for line in lines:
        assert not line.startswith("@"), f"untagged client got a tag block: {line!r}"
        assert len(line.encode("utf-8")) + 2 <= PRIVMSG_WIRE_LIMIT

    msgs = [Message.parse(line) for line in lines]
    assert "".join(m.params[1] for m in msgs) == text


@pytest.mark.asyncio
async def test_long_dm_privmsg_splits_ordered_within_budget_and_reassembles(server, make_client):
    alice = await make_client("testserv-alice", "alice")
    dave = await _mt_client(make_client, "testserv-dave", "dave")
    await dave.recv_all(timeout=0.2)

    text = ("ping " * 150).strip()
    assert len(text.encode("utf-8")) > PRIVMSG_WIRE_LIMIT

    await alice.send(f"PRIVMSG testserv-dave :{text}")
    lines = await dave.recv_all(timeout=0.5)
    assert len(lines) > 1

    for line in lines:
        assert _untagged_wire_len(line) <= PRIVMSG_WIRE_LIMIT, line

    msgs = [Message.parse(line) for line in lines]
    assert all(m.params[0] == "testserv-dave" for m in msgs)
    reassembled = "".join(m.params[1] for m in msgs)
    assert reassembled == text

    msgids = [m.tags.get(MSGID_TAG) for m in msgs]
    assert len(set(msgids)) == len(msgids), f"duplicate msgid across chunks: {msgids}"


@pytest.mark.asyncio
async def test_long_privmsg_with_multibyte_utf8_at_chunk_boundary_reassembles(server, make_client):
    """A payload engineered to straddle chunk boundaries with multibyte UTF-8 survives intact."""
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    await _join(alice, "#split-utf8")
    await _join(bob, "#split-utf8")
    await alice.recv_all(timeout=0.2)
    await bob.recv_all(timeout=0.2)

    # Repeating 3-byte (é encoded as e + combining char is 1 codepoint at
    # 2 bytes; use an explicit multi-byte codepoint) and 4-byte (emoji)
    # characters throughout so *any* mid-codepoint split would corrupt the
    # UTF-8 stream and fail the reassembly/round-trip below.
    unit = "café \U0001f600 "
    text = (unit * 60).strip()
    assert len(text.encode("utf-8")) > PRIVMSG_WIRE_LIMIT

    await alice.send(f"PRIVMSG #split-utf8 :{text}")
    lines = await bob.recv_all(timeout=0.5)
    assert len(lines) > 1

    msgs = [Message.parse(line) for line in lines]
    for m in msgs:
        # Message.parse only succeeds cleanly on valid UTF-8; re-encoding
        # each chunk must round-trip without alteration.
        chunk = m.params[1]
        assert chunk.encode("utf-8").decode("utf-8") == chunk

    reassembled = "".join(m.params[1] for m in msgs)
    assert reassembled == text


# ---------------------------------------------------------------------------
# Outbound: thread messages split too (wire delivery only; single logical event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_thread_message_splits_with_shared_prefix_and_distinct_msgids(
    server, make_client
):
    alice = await make_client("testserv-alice", "alice")
    bob = await _mt_client(make_client, "testserv-bob", "bob")
    await _join(alice, "#thread-split")
    await _join(bob, "#thread-split")
    await alice.recv_all(timeout=0.2)
    await bob.recv_all(timeout=0.2)

    text = ("thread words go here and there and everywhere " * 15).strip()
    assert len(text.encode("utf-8")) > PRIVMSG_WIRE_LIMIT

    await alice.send(f"THREAD CREATE #thread-split splitty :{text}")
    lines = await bob.recv_all(timeout=0.5)
    assert len(lines) > 1

    for line in lines:
        assert _untagged_wire_len(line) <= PRIVMSG_WIRE_LIMIT, line

    msgs = [Message.parse(line) for line in lines]
    prefix = "[thread:splitty] "
    for m in msgs:
        assert m.params[1].startswith(prefix)
        assert m.tags.get(THREAD_TAG) == "splitty"

    reassembled = "".join(m.params[1][len(prefix):] for m in msgs)
    assert reassembled == text

    msgids = [m.tags.get(MSGID_TAG) for m in msgs]
    assert all(msgids)
    assert len(set(msgids)) == len(msgids), f"duplicate msgid across chunks: {msgids}"


# ---------------------------------------------------------------------------
# Outbound: all chunks land in history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_channel_privmsg_all_chunks_land_in_history(server, make_client):
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    await _join(alice, "#hist-split")
    await _join(bob, "#hist-split")
    await alice.recv_all(timeout=0.2)
    await bob.recv_all(timeout=0.2)

    text = ("word " * 200).strip()
    assert len(text.encode("utf-8")) > PRIVMSG_WIRE_LIMIT

    await alice.send(f"PRIVMSG #hist-split :{text}")
    delivered = await bob.recv_all(timeout=0.5)
    assert len(delivered) > 1

    # Exactly as many history entries were just appended to this channel as
    # chunks were delivered (they're the most recent entries in the deque),
    # so an exact-count RECENT query returns precisely our chunks in
    # chronological (= delivery) order.
    await alice.send(f"HISTORY RECENT #hist-split {len(delivered)}")
    reply = await alice.recv_until("HISTORYEND")
    hist_lines = [line for line in reply.split("\r\n") if " HISTORY " in line]
    assert len(hist_lines) == len(delivered)

    hist_msgs = [Message.parse(line) for line in hist_lines]
    reassembled = "".join(m.params[-1] for m in hist_msgs)
    assert reassembled == text
    assert all(m.params[1] == "testserv-alice" for m in hist_msgs)


# ---------------------------------------------------------------------------
# Inbound: explicit over-limit error naming the limit + stable token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_oversized_line_error_names_limit_and_carries_token(server, make_client):
    alice = await _mt_client(make_client, "testserv-alice", "alice")

    oversized = "Q" * (MAX_INBOUND_LINE + 100)
    await alice.send(oversized)
    reply = await alice.recv()
    msg = Message.parse(reply)

    assert msg.command == "NOTICE"
    assert str(MAX_INBOUND_LINE) in msg.params[-1]
    assert msg.tags.get(ERROR_TAG) == ERROR_TOKEN_LINE_TOO_LONG


@pytest.mark.asyncio
async def test_inbound_oversized_line_plain_client_gets_no_tag_block(server, make_client):
    alice = await make_client("testserv-alice", "alice")

    oversized = "R" * (MAX_INBOUND_LINE + 1)
    await alice.send(oversized)
    reply = await alice.recv()

    assert not reply.startswith("@")
    assert f"{MAX_INBOUND_LINE}-byte" in reply


@pytest.mark.asyncio
async def test_inbound_oversized_line_connection_stays_alive_and_next_line_works(
    server, make_client
):
    alice = await make_client("testserv-alice", "alice")

    oversized = "W" * (MAX_INBOUND_LINE + 1)
    await alice.send(oversized)
    err_line = await alice.recv()
    assert f"{MAX_INBOUND_LINE}-byte" in err_line

    await alice.send("PING still-alive")
    pong_line = await alice.recv()
    assert "PONG" in pong_line and "still-alive" in pong_line


@pytest.mark.asyncio
async def test_inbound_well_formed_neighbor_lines_unaffected(server, make_client):
    """A well-formed line before AND after an oversized one are both untouched."""
    alice = await make_client("testserv-alice", "alice")

    oversized = "Z" * (MAX_INBOUND_LINE + 1)
    payload = f"PING before\r\n{oversized}\r\nPING after\r\n".encode()
    alice.writer.write(payload)
    await alice.writer.drain()

    line1 = await alice.recv()
    assert "PONG" in line1 and "before" in line1

    line2 = await alice.recv()
    assert f"{MAX_INBOUND_LINE}-byte" in line2

    line3 = await alice.recv()
    assert "PONG" in line3 and "after" in line3


@pytest.mark.asyncio
async def test_inbound_oversized_line_without_early_newline_resyncs_cleanly(server, make_client):
    """A line that never gets a '\\n' for a long stretch is bounded + resynced, not left hanging."""
    alice = await make_client("testserv-alice", "alice")

    # Twice the limit, no '\n' anywhere inside -- forces the read loop to
    # proactively detect the overflow across multiple reader.read(4096)
    # calls (rather than waiting on a per-line split), discard the buffered
    # bytes to bound memory, and remember to skip forward to the eventual
    # terminator instead of misreading it as a fresh command.
    huge = "Y" * (MAX_INBOUND_LINE * 2)
    payload = (huge + "\r\nPING resync-token\r\n").encode()
    alice.writer.write(payload)
    await alice.writer.drain()

    err_line = await alice.recv()
    assert f"{MAX_INBOUND_LINE}-byte" in err_line

    pong_line = await alice.recv()
    assert "PONG" in pong_line and "resync-token" in pong_line


@pytest.mark.asyncio
async def test_inbound_line_exactly_at_limit_is_accepted_untouched(server, make_client):
    """A line whose *total* wire length is exactly MAX_INBOUND_LINE bytes is not rejected."""
    alice = await make_client("testserv-alice", "alice")
    bob = await make_client("testserv-bob", "bob")
    await _join(alice, "#at-limit")
    await _join(bob, "#at-limit")
    await alice.recv_all(timeout=0.2)
    await bob.recv_all(timeout=0.2)

    header = "PRIVMSG #at-limit :"
    pad_len = MAX_INBOUND_LINE - len(header.encode("utf-8"))
    text = "Q" * pad_len
    line = header + text
    assert len(line.encode("utf-8")) == MAX_INBOUND_LINE

    await alice.send(line)
    delivered = await bob.recv_all(timeout=0.4)

    # No line-too-long rejection anywhere -- every byte of the original
    # text arrives, even though the *outbound* 512-byte relay split (a
    # separate, expected mechanism) may fan it out across several PRIVMSGs.
    assert not any("line-too-long" in d or f"{MAX_INBOUND_LINE}-byte" in d for d in delivered)
    msgs = [Message.parse(d) for d in delivered if " PRIVMSG " in d]
    reassembled = "".join(m.params[1] for m in msgs)
    assert reassembled == text
