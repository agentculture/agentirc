"""Companion HTTP server for receiving inbound webhook POSTs.

Paraphrase of ``culture.bots.http_listener`` (import paths rewritten only).
The ``culture.bots.bot_manager.BotManager`` TYPE_CHECKING import is replaced
with ``agentirc.bots.bot_manager.BotManager``.  No logic was changed.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from aiohttp import web
from opentelemetry.instrumentation.aiohttp_server import AioHttpServerInstrumentor

if TYPE_CHECKING:
    from agentirc.bots.bot_manager import BotManager

logger = logging.getLogger(__name__)


class HttpListener:
    """Lightweight HTTP server that routes webhook POSTs to bots."""

    def __init__(self, bot_manager: BotManager, host: str, port: int):
        self.bot_manager = bot_manager
        self.host = host
        self.port = port
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        # Patch aiohttp.web.Application to auto-inject the OTEL server
        # middleware. Deferred from import time so just importing this module
        # has no side effect. Re-instrument each start() so the captured
        # tracer/meter rebinds to the *current* TracerProvider — important for
        # tests that swap providers between runs, harmless in production.
        instrumentor = AioHttpServerInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        instrumentor.instrument()
        self._app = web.Application(middlewares=[self._record_webhook_duration])
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_post("/{bot_name}", self._handle_webhook)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info("Webhook HTTP listener started on %s:%d", self.host, self.port)

    @property
    def bound_port(self) -> int | None:
        """The OS-assigned port after start(); None before start()."""
        return self._site.port if self._site is not None else None

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._app = None
            self._site = None

    @web.middleware
    async def _record_webhook_duration(self, request, handler):
        """Record per-request duration into agentirc.bot.webhook.duration."""
        bot_name = request.match_info.get("bot_name") or "_unrouted"
        start = time.perf_counter()
        # Default for the "handler raised a non-HTTPException" path: aiohttp
        # converts unhandled exceptions to 500 responses outside this
        # middleware, so the histogram should report 5xx for them.
        status_class = "5xx"
        try:
            response = await handler(request)
            status_class = f"{response.status // 100}xx"
            return response
        except web.HTTPException as exc:
            status_class = f"{exc.status // 100}xx"
            raise
        finally:
            duration = time.perf_counter() - start
            self.bot_manager.server.metrics.bot_webhook_duration.record(
                duration,
                {"bot": bot_name, "status_class": status_class},
            )

    async def _handle_health(  # NOSONAR S7503 — aiohttp handler signature requires async
        self, request: web.Request
    ) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        bot_name = request.match_info["bot_name"]

        # Parse JSON body
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid JSON"},
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                {"error": "payload must be a JSON object"},
                status=400,
            )

        # Dispatch to bot
        try:
            message = await self.bot_manager.dispatch(bot_name, payload)
            return web.json_response({"ok": True, "message": message})
        except ValueError:
            return web.json_response(
                {"error": "bot not found"},
                status=404,
            )
        except RuntimeError:
            return web.json_response(
                {"error": "bot not active"},
                status=503,
            )
        except Exception:
            logger.exception("Webhook handler error for bot %s", bot_name)
            return web.json_response(
                {"error": "internal error"},
                status=500,
            )
