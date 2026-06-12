"""Agentirc's wrapper around the public VirtualClient for bot-host usage.

After Phase A2 (agentirc-cli >= 9.6.0), all virtual-presence behavior —
JOIN/PART, channel broadcasts, DMs, @-mention notices, IRC-text
sanitization — lives in :class:`agentirc.virtual_client.VirtualClient`.
That class was promoted to public in agentirc 9.6.0 (agentculture/agentirc#22)
and is semver-tracked from that release forward.

This module re-exports it as ``agentirc.bots.virtual_client.VirtualClient``
to serve as the bot-host subclass anchor within agentirc's embedded bot
framework. The subclass exists so bot-host-specific extensions can land
here without forking the upstream class — none are needed today (all
virtual-presence behavior is inherited from the public base class).
"""

from __future__ import annotations

from agentirc.virtual_client import VirtualClient as _PublicVirtualClient


class VirtualClient(_PublicVirtualClient):
    """A bot's IRC presence within the embedded bot framework.

    All behavior is inherited from :class:`agentirc.virtual_client.VirtualClient`.
    Subclassed so the bot-host system can extend without forking the upstream class.
    """


__all__ = ["VirtualClient"]
