"""Docs-vs-implementation lock (task t8, honesty condition h9).

``docs/extension-api.md`` promises bots a set of verbs they can issue. This
file extracts every backtick-quoted ALL-CAPS token the doc mentions,
classifies each one by hand (a curated allowlist — see
``CLIENT_ISSUABLE_VERBS``/``NOT_A_CLIENT_VERB`` below), and for every verb
classified as "a client can issue this" asserts a live handler exists on a
running server, checked against the same two lookups
``Client._dispatch`` actually uses at runtime: a ``_handle_<verb>`` method,
or a skill claiming the verb via its ``commands`` set
(``IRCd.get_skill_for_command``).

Two ways this test fails on purpose:

- A doc edit introduces a new bare backtick ALL-CAPS token nobody has
  classified yet -> ``test_curated_allowlist_covers_every_doc_token`` fails,
  forcing a conscious classification.
- A doc edit (or a classification edit) claims a verb is client-issuable but
  no handler exists for it -> ``test_every_documented_client_verb_has_a_live_handler``
  fails. This is exactly the bug this task fixes for ``BACKFILL``: before
  task t8, ``BACKFILL`` was documented at
  ``docs/extension-api.md``'s Backpressure section but had no client-side
  handler (server-to-server only, in ``agentirc/server_link.py``) — this
  test would have failed had it existed then.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentirc.client import Client

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "extension-api.md"

# Verbs docs/extension-api.md documents (via a bare backtick ALL-CAPS
# reference) as something a CLIENT sends. Hand-curated, not mechanical.
CLIENT_ISSUABLE_VERBS = frozenset(
    {
        "MODE",
        "NAMES",
        "NOTICE",
        "PRIVMSG",
        "WHO",
        "EVENTSUB",
        "EVENTPUB",
        "BACKFILL",
        "TOPIC",
        "TAGS",
        "ROOMCREATE",
        "ROOMARCHIVE",
        "ROOMMETA",
        # task t9 (agent-accessibility release): runtime verb discovery --
        # any registered client (no BOT_CAP needed) can issue this.
        "VERBS",
    }
)

# Backtick ALL-CAPS tokens the doc mentions that are NOT a verb a client
# issues, with the reason -- so a future reader (and a future doc edit)
# knows why they're excluded rather than just missing.
NOT_A_CLIENT_VERB = {
    "B": "the WHO/NAMES output flag letter ('Identifying yourself in NAMES "
    "/ WHO'), not a verb",
    "EVENT": "server-to-client only -- a bot never sends a bare EVENT line "
    "(it only appears in EVENTSUB/BACKFILL *replies*)",
    "SEVENT": "server-to-server federation verb (agentirc/server_link.py); "
    "not reachable from a client connection at all",
}


def _extract_backtick_allcaps_tokens(text: str) -> set[str]:
    """Backtick-quoted, standalone ALL-CAPS tokens, e.g. `` `BACKFILL` ``.

    Deliberately scoped to single-backtick *inline* references -- the
    convention this doc uses for "here is a verb name" in prose -- not the
    ```text fenced wire-trace blocks, which are noisy (prefixes, numerics,
    params) and are instead exercised byte-for-byte by
    tests/test_client_backfill.py's walkthrough test.
    """
    return set(re.findall(r"`([A-Z][A-Z_]*)`", text))


def test_curated_allowlist_covers_every_doc_token():
    """Every backtick ALL-CAPS token the doc mentions is consciously classified."""
    found = _extract_backtick_allcaps_tokens(DOC_PATH.read_text())
    classified = CLIENT_ISSUABLE_VERBS | set(NOT_A_CLIENT_VERB)
    unclassified = found - classified
    assert not unclassified, (
        f"docs/extension-api.md mentions {sorted(unclassified)!r}, not yet "
        "classified in tests/test_docs_promises.py's CLIENT_ISSUABLE_VERBS "
        "/ NOT_A_CLIENT_VERB allowlists -- classify it there (with a "
        "handler check if it's client-issuable) so this test keeps locking "
        "docs to implementation."
    )
    # Guard the other direction too: don't let the allowlist silently
    # accumulate stale entries for tokens the doc no longer mentions.
    stale = classified - found
    assert not stale, (
        f"tests/test_docs_promises.py's allowlist still classifies "
        f"{sorted(stale)!r}, which docs/extension-api.md no longer mentions "
        "-- remove the stale entry."
    )


@pytest.mark.asyncio
async def test_every_documented_client_verb_has_a_live_handler(server):
    """The failure mode this file exists to catch: a verb the doc says a
    client can issue but that has no dispatch path on a running server."""
    for verb in sorted(CLIENT_ISSUABLE_VERBS):
        has_core_handler = hasattr(Client, f"_handle_{verb.lower()}")
        has_skill_handler = server.get_skill_for_command(verb) is not None
        assert has_core_handler or has_skill_handler, (
            f"docs/extension-api.md documents `{verb}` as something a "
            "client can issue, but no live handler exists for it (checked "
            "Client._handle_* and every registered skill's `commands` set)"
        )
