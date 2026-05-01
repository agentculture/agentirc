"""Mesh and link helpers for the agentirc CLI.

Vendored from culture@df50942 (`culture/cli/shared/mesh.py`), reduced
to the single helper agentirc needs: ``parse_link``. The upstream
helpers ``resolve_links_from_mesh`` and ``generate_mesh_from_agents``
depend on culture's ``mesh_config`` / ``credentials`` modules, which
manage agent mesh and OS-keyring credential lookup — concepts that do
not belong in agentirc's dependency-bounded surface.
"""

from __future__ import annotations

import argparse


def parse_link(value: str):
    """Parse a link spec: ``name:host:port:password[:trust]``.

    Trust is extracted from the end if it matches a known value. This
    allows passwords containing colons to round-trip through argparse
    without escaping.
    """
    from agentirc.config import LinkConfig

    trust = "full"
    if value.endswith(":full") or value.endswith(":restricted"):
        value, trust = value.rsplit(":", 1)

    parts = value.split(":", 3)
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"Link must be name:host:port:password[:trust], got: {value}"
        )
    name, host, port_str, password = parts
    try:
        port = int(port_str)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid port: {port_str}") from exc
    return LinkConfig(name=name, host=host, port=port, password=password, trust=trust)
