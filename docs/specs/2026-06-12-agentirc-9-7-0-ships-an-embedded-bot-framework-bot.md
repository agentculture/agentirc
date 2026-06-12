# agentirc 9.7.0 ships an embedded bot framework: BotManager + Bot + filter DSL + template engine + YAML bot config live in agentirc/bots/, tested here, so culture can replace its in-tree culture/bots/* with thin forwards in a single cutover PR with no functionality gap

> agentirc 9.7.0 ships an embedded bot framework: BotManager + Bot + filter DSL + template engine + YAML bot config live in agentirc/bots/, tested here, so culture can replace its in-tree culture/bots/* with thin forwards in a single cutover PR with no functionality gap

## Audience

- culture (the immediate consumer wiring its IRCd to install BotManager + forwarding culture bot CLI verbs) and any standalone agentirc embedder wanting deterministic YAML chatops bots inside their IRCd

## Before → After

- Before: the bot framework lives only in culture/bots/* (~1340 lines incl config.py); agentirc ships only no-op BotManager/http_listener stubs in _internal/bots/; culture cannot slim its front door because deleting bots would lose functionality
- After: manager + Bot + filter_dsl + template_engine + config (YAML BotConfig) live in agentirc, are unit-tested here (filter DSL + template engine edge cases especially), and a documented public API lets an embedder install a BotManager onto a running IRCd and register YAML-spec'd bots

## Why it matters

- the bot brain is already bound to agentirc protocol types (filter DSL evaluates Event/EventType; bot host subclasses agentirc.virtual_client.VirtualClient) — bots have no coherent life without an IRCd, so the framework belongs here, not in culture or a third package

## Requirements

- the real BotManager must satisfy the existing _internal/bots stub contract (__init__(server), async load_bots, load_system_bots, get_bot(nick), async on_event(event), async stop_all) because IRCd.start() already calls these methods
  - honesty: an IRCd booted in-process with a BotManager installed loads a YAML bot, dispatches a matching Event to it, and the bot replies — exercised by a test
- vendored via cite-don't-copy: each moved file gets a [tool.citation] entry (quote/paraphrase/synthesize) with source URL + sha256; culture.bots.* imports rewrite to agentirc.bots.*; bot.py's culture.constants.EVENT_TYPE_RE rewrites to agentirc._internal.constants
  - honesty: cite check passes after the move and the new [tool.citation] entries resolve against real culture source SHAs
- no imports back into culture: git grep '^(from|import) culture' over agentirc/ must stay empty after the move
  - honesty: CI's git grep portability check returns nothing for 'from culture'/'import culture' across agentirc/ and tests/

## Honesty conditions

- agentirc-cli 9.7.0 is published to PyPI with the bot framework importable, and culture's cutover PR replaces culture/bots/* with forwards while keeping its suite green
- culture's cutover PR consumes the published agentirc API (installs BotManager + forwards CLI) without reimplementing bot logic
- the _internal/bots stubs and culture/bots/* line counts are verifiable in the two repos at cutover time
- docs/api-stability.md documents the agentirc.bots install/register API with a worked example
- filter_dsl.evaluate runs against real agentirc Event objects in a test, and the bot host resolves agentirc.virtual_client.VirtualClient as its base
- no file under culture/bots/system/ is vendored, no backend SDK is added to pyproject deps, and BOTS_DIR default still resolves under ~/.culture
- a clean-venv install test imports agentirc.bots and the DSL/template/lifecycle tests run green in agentirc CI

## Success signals

- pip install agentirc-cli==9.7.0 in a clean venv imports the bot framework; pytest covers filter DSL + template engine + manager lifecycle; culture's cutover PR replaces culture/bots/* with re-export shims and its test suite stays green

## Scope / boundaries

- NOT in scope: porting culture's system bots (culture/bots/system/*), any LLM/agent-backend bot harness (forbidden by the dependency boundary), and renaming on-disk artifacts (BOTS_DIR stays ~/.culture/bots for culture continuity)

## Decisions

- version bump is minor (9.7.0): the move is additive — a new public bot subsystem and CLI verbs, no breaking change to the five existing public modules
- Webhook: port http_listener.py too — add aiohttp + opentelemetry-instrumentation-aiohttp-server deps, reversing the 9.5.0 deferral; bots ship with full webhook-ingress parity
- Placement: agentirc/bots/ is a PUBLIC, semver-tracked subsystem (6th public surface); embedders import agentirc.bots.BotManager / Bot; _internal/bots stubs become re-export shims or are removed; public API table in CLAUDE.md + docs/api-stability.md gains an agentirc.bots row
- CLI: ship 'agentirc bot create/start/stop/list/inspect/archive/unarchive' verbs in 9.7.0, ported from culture/cli/bot.py, so culture bot can become a thin forward to agentirc.cli.dispatch
