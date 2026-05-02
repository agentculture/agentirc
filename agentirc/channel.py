from __future__ import annotations

from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from agentirc.client import Client
    from agentirc.remote_client import RemoteClient

    Member = Union[Client, RemoteClient]


class Channel:
    """Represents an IRC channel with members and topic."""

    def __init__(self, name: str):
        self.name = name
        self.topic: str | None = None
        self.members: set[Client] = set()
        self.operators: set[Client] = set()
        self.voiced: set[Client] = set()
        self.restricted = False  # +R mode — never federate
        self.shared_with: set[str] = set()  # +S servers — share with these servers

        # Room metadata (populated by ROOMCREATE, None for plain channels)
        self.room_id: str | None = None
        self.creator: str | None = None
        self.owner: str | None = None
        self.purpose: str | None = None
        self.instructions: str | None = None
        self.tags: list[str] = []
        self.persistent: bool = False
        self.agent_limit: int | None = None
        self.extra_meta: dict[str, str] = {}
        self.archived: bool = False
        self.created_at: float | None = None

    @property
    def is_managed(self) -> bool:
        """True if this channel was created via ROOMCREATE."""
        return self.room_id is not None

    def _local_members(self) -> set[Client]:
        """Return only local (non-remote, non-virtual, non-bot-CAP) members.

        Used as the auto-op eligibility predicate by :meth:`add`. Bot-CAP
        clients are excluded so a bot joining an empty channel never becomes
        op — a human joining later does.
        """
        from agentirc.remote_client import RemoteClient
        from agentirc._internal.virtual_client import VirtualClient

        return {
            m
            for m in self.members
            if not isinstance(m, (RemoteClient, VirtualClient))
            and "agentirc.io/bot" not in getattr(m, "caps", frozenset())
        }

    def add(self, client: Client) -> None:
        # Only grant op to the first LOCAL joiner. Bot-CAP clients
        # (real or VirtualClient) are excluded — they never auto-op,
        # so a bot joining an empty channel stays unprivileged and
        # the next human becomes op.
        if not self._local_members():
            from agentirc.remote_client import RemoteClient
            from agentirc._internal.virtual_client import VirtualClient

            is_op_eligible = (
                not isinstance(client, (RemoteClient, VirtualClient))
                and "agentirc.io/bot" not in getattr(client, "caps", frozenset())
            )
            if is_op_eligible:
                self.operators.add(client)
        self.members.add(client)

    def remove(self, client: Client) -> None:
        self.members.discard(client)
        was_op = client in self.operators
        self.operators.discard(client)
        self.voiced.discard(client)
        if was_op and not self.operators:
            # Auto-promote only among local members
            local = self._local_members()
            if local:
                self.operators.add(min(local, key=lambda m: m.nick))

    def is_operator(self, client: Client) -> bool:
        return client in self.operators

    def is_voiced(self, client: Client) -> bool:
        return client in self.voiced

    def get_prefix(self, client: Client) -> str:
        if client in self.operators:
            return "@"
        if client in self.voiced:
            return "+"
        # Bot-CAP clients render with the voice prefix in NAMES output —
        # the closest standard IRC mode for "non-disruptive participant",
        # so vanilla IRC clients filter bots from presence panels by
        # checking the ``+`` prefix. Op wins on conflict (above), so an
        # explicitly-opped bot still renders as ``@``.
        if "agentirc.io/bot" in getattr(client, "caps", frozenset()):
            return "+"
        return ""
