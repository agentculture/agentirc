from __future__ import annotations

from typing import TYPE_CHECKING, Union

from agentirc.protocol import BOT_CAP

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

    @staticmethod
    def _op_eligible(client) -> bool:
        """May *client* be auto-opped (first joiner / auto-promotion)?

        Remote, virtual and bot-CAP clients never are. An authenticated
        account session is only when its live role is ``operator``
        (culture#506 h1): chanop would let a resident or guest change the
        topic and modes of a channel it was only granted to read or speak
        in. Clients without an account are unchanged.
        """
        from agentirc.remote_client import RemoteClient
        from agentirc.virtual_client import VirtualClient

        if isinstance(client, (RemoteClient, VirtualClient)):
            return False
        if BOT_CAP in getattr(client, "caps", frozenset()):
            return False
        if getattr(client, "account", None) is not None:
            return client.account_role() == "operator"
        return True

    def _local_members(self) -> set[Client]:
        """Return only local (non-remote, non-virtual, non-bot-CAP) members.

        Used as the auto-op eligibility predicate by :meth:`add`. Bot-CAP
        clients are excluded so a bot joining an empty channel never becomes
        op — a human joining later does.
        """
        from agentirc.remote_client import RemoteClient
        from agentirc.virtual_client import VirtualClient

        return {
            m
            for m in self.members
            if not isinstance(m, (RemoteClient, VirtualClient))
            and BOT_CAP not in getattr(m, "caps", frozenset())
        }

    def add(self, client: Client) -> None:
        # Only grant op to the first LOCAL joiner. Bot-CAP clients
        # (real or VirtualClient) are excluded — they never auto-op,
        # so a bot joining an empty channel stays unprivileged and
        # the next human becomes op.
        # "First" counts only op-eligible members, so a channel opened by a
        # resident account still gives op to the next eligible joiner, the
        # same way a bot-first channel does.
        if self._op_eligible(client) and not any(
            self._op_eligible(m) for m in self._local_members()
        ):
            self.operators.add(client)
        self.members.add(client)

    def remove(self, client: Client) -> None:
        self.members.discard(client)
        was_op = client in self.operators
        self.operators.discard(client)
        self.voiced.discard(client)
        if was_op and not self.operators:
            # Auto-promote only among local members
            local = {m for m in self._local_members() if self._op_eligible(m)}
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
        if BOT_CAP in getattr(client, "caps", frozenset()):
            return "+"
        return ""
