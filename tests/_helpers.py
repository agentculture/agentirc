"""Shared test helpers, agentirc-native (NOT vendored from culture).

Extracted to address SonarCloud's >3% duplication threshold. The
boot-linked-pair pattern repeats verbatim across the cited
``test_federation.py`` (5 sites), ``test_link_reconnect.py`` (3
sites), and the ``linked_servers`` conftest fixture; CPD flags it as
~22 lines × 9 = ~200 lines of duplicated test scaffold.

Why these tests can't share the existing ``linked_servers`` pytest
fixture: each one needs to drive the boot/link/teardown lifecycle
itself — duplicate-link rejection, slow-link teardown, mid-test
peer kill, backfill replay — and the function-scoped fixture starts
both servers + completes the handshake before the test body runs.

Re-snapshotting from a future culture commit means re-running the
same extraction: replace each ~22-line inline boot block with a
single ``boot_linked_pair(tmp_path)`` call. Mechanical sed pattern.
"""

from __future__ import annotations

import asyncio

from agentirc.config import LinkConfig, ServerConfig, TelemetryConfig
from agentirc.ircd import IRCd

from tests.conftest import TEST_LINK_PASSWORD


async def boot_linked_pair(
    tmp_path,
    *,
    link_password: str = TEST_LINK_PASSWORD,
    with_audit: bool = False,
    webhook_port: int | None = None,
) -> tuple[IRCd, IRCd]:
    """Boot two IRCd instances pre-wired with link configs.

    Returns ``(server_a, server_b)`` after both ``start()`` calls
    have returned with OS-assigned ports populated. Does NOT drive
    the S2S handshake — caller decides whether to call
    :func:`link_pair` or invoke ``connect_to_peer`` manually.

    ``with_audit=True`` plumbs ``TelemetryConfig(audit_dir=...)``
    onto each side, isolating audit JSONL writes to ``tmp_path``.
    ``webhook_port=0`` suppresses the webhook listener — the
    ``linked_servers`` fixture sets this; the inline sites do not.
    """
    kwargs_a: dict = {"links": [
        LinkConfig(name="beta", host="127.0.0.1", port=0, password=link_password)
    ]}
    kwargs_b: dict = {"links": [
        LinkConfig(name="alpha", host="127.0.0.1", port=0, password=link_password)
    ]}
    if with_audit:
        kwargs_a["telemetry"] = TelemetryConfig(audit_dir=str(tmp_path / "audit_alpha"))
        kwargs_b["telemetry"] = TelemetryConfig(audit_dir=str(tmp_path / "audit_beta"))
    if webhook_port is not None:
        kwargs_a["webhook_port"] = webhook_port
        kwargs_b["webhook_port"] = webhook_port

    config_a = ServerConfig(name="alpha", host="127.0.0.1", port=0, **kwargs_a)
    config_b = ServerConfig(name="beta", host="127.0.0.1", port=0, **kwargs_b)
    server_a = IRCd(config_a)
    server_b = IRCd(config_b)
    await server_a.start()
    await server_b.start()
    server_a.config.port = server_a._server.sockets[0].getsockname()[1]
    server_b.config.port = server_b._server.sockets[0].getsockname()[1]
    return server_a, server_b


async def link_pair(
    server_a: IRCd,
    server_b: IRCd,
    *,
    link_password: str = TEST_LINK_PASSWORD,
) -> None:
    """Drive the S2S handshake on a booted linked-pair.

    Updates each side's ``LinkConfig`` with the actual OS-assigned
    port (the ``links=[…port=0]`` placeholder gets resolved here),
    calls ``connect_to_peer`` from A → B, and polls up to ~2.5s for
    both sides to see the link before returning.
    """
    server_a.config.links[0].port = server_b.config.port
    server_b.config.links[0].port = server_a.config.port
    await server_a.connect_to_peer("127.0.0.1", server_b.config.port, link_password)
    for _ in range(50):
        if "beta" in server_a.links and "alpha" in server_b.links:
            break
        await asyncio.sleep(0.05)
