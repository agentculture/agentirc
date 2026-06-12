# Build Plan — agentirc 9.7.0 ships an embedded bot framework: BotManager + Bot + filter DSL + template engine + YAML bot config live in agentirc/bots/, tested here, so culture can replace its in-tree culture/bots/* with thin forwards in a single cutover PR with no functionality gap

slug: `agentirc-9-7-0-ships-an-embedded-bot-framework-bot` · status: `exported` · from frame: `agentirc-9-7-0-ships-an-embedded-bot-framework-bot`

> agentirc 9.7.0 ships an embedded bot framework: BotManager + Bot + filter DSL + template engine + YAML bot config live in agentirc/bots/, tested here, so culture can replace its in-tree culture/bots/* with thin forwards in a single cutover PR with no functionality gap

## Tasks

### t1 — Vendor template_engine.py -> agentirc/bots/template_engine.py (verbatim quote)

- covers: c4
- acceptance:
  - render_template + render_fallback produce identical output to culture's template_engine across a case table incl. missing-key fallback and json-value rendering

### t2 — Vendor filter_dsl.py -> agentirc/bots/filter_dsl.py (verbatim quote)

- covers: c5, h8
- acceptance:
  - compile_filter parses every DSL operator; evaluate() returns correct bool against a constructed agentirc Event; malformed filter raises FilterParseError

### t3 — Vendor config.py BotConfig + BOTS_DIR -> agentirc/bots/config.py (paraphrase)

- covers: c4, h9
- acceptance:
  - BotConfig loads a valid YAML bot spec; BOTS_DIR default resolves under ~/.culture; malformed YAML rejected without partial state

### t4 — Vendor bot-host virtual_client subclass -> agentirc/bots/virtual_client.py (paraphrase)

- covers: c5, h8
- acceptance:
  - agentirc.bots.virtual_client.VirtualClient subclasses agentirc.virtual_client.VirtualClient with no behavioral override; instantiation registers presence in a channel

### t5 — Packaging: pyproject deps (aiohttp + opentelemetry-instrumentation-aiohttp-server) + version 9.7.0 + all [tool.citation] entries + uv.lock + CHANGELOG

- covers: h4, c6, c9
- acceptance:
  - pyproject declares both aiohttp deps; agentirc version reads 9.7.0; uv.lock regenerated+staged; [tool.citation] has quote/paraphrase entries for all 8 vendored bot files; no backend SDK added

### t6 — Vendor bot.py -> agentirc/bots/bot.py (paraphrase: import rewrites to agentirc.*)

- depends on: t1, t2, t3, t4
- covers: c4, c5, c8, h8
- acceptance:
  - Bot loads from BotConfig, matches an Event via the filter DSL, renders a reply via the template engine; all imports resolve to agentirc.* with EVENT_TYPE_RE from agentirc._internal.constants; zero culture imports

### t7 — Vendor bot_manager.py -> agentirc/bots/bot_manager.py satisfying the _internal/bots stub contract

- depends on: t6
- covers: c2, c3, c8, h1
- acceptance:
  - BotManager matches the stub contract (__init__(server), async load_bots/on_event/stop_all, load_system_bots, get_bot); an in-process IRCd with BotManager installed loads a YAML bot, dispatches a matching Event, and the bot replies — proven by a test

### t8 — Vendor http_listener.py -> agentirc/bots/http_listener.py (aiohttp webhook ingress)

- depends on: t7
- covers: c4
- acceptance:
  - http_listener binds an aiohttp webhook endpoint that dispatches an inbound webhook to a registered bot; a test posts a webhook and asserts the bot fired; otel aiohttp instrumentation wired

### t9 — Public surface: agentirc/bots/__init__.py exports + convert _internal/bots stubs to deprecation re-export shims

- depends on: t7
- covers: c4, h5
- acceptance:
  - from agentirc.bots import BotManager, Bot works in a clean session; _internal/bots stubs re-export from agentirc.bots and emit DeprecationWarning

### t10 — Port culture/cli/bot.py -> agentirc bot CLI verbs wired into agentirc.cli.dispatch

- depends on: t7, t9
- covers: c2, h5
- acceptance:
  - agentirc bot create/start/stop/list/inspect/archive/unarchive dispatch through agentirc.cli; agentirc bot list runs; flags match culture/cli/bot.py so culture bot can forward

### t11 — Docs: agentirc.bots in docs/api-stability.md (worked example) + CLAUDE.md public API table row

- depends on: t9
- covers: c4, h7
- acceptance:
  - docs/api-stability.md gains an agentirc.bots row + worked example installing BotManager onto a running IRCd and registering a YAML bot; CLAUDE.md public API table lists agentirc.bots

### t12 — Integration gate: cite check + full suite + clean-venv import + portability grep

- depends on: t5, t6, t7, t8, t9, t10, t11
- covers: c1, c3, c6, c7, c9, c10, h2, h3, h6, h9, h10
- acceptance:
  - cite check passes (entries resolve to real culture SHAs); pytest -n auto green; clean-venv install of built wheel imports agentirc.bots; CI grep finds no 'from culture'/'import culture' in agentirc/+tests/; no culture/bots/system/ file vendored
