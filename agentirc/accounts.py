"""Agent accounts: the credential table SASL PLAIN authenticates against.

culture (the admin surface) writes ``<data_dir>/accounts.json``; agentirc
only reads it. The format is versioned and documented in
``docs/accounts.md``::

    {
      "version": 1,
      "accounts": [
        {
          "nick": "spark-culture",
          "secret_sha256": "<64 lowercase hex chars: sha256(secret)>",
          "role": "resident",            # resident | operator | guest
          "grants": {"#general": "speak"},  # channel -> read | speak
          "state": "active"              # active | suspended | revoked
        }
      ]
    }

The whole file is validated before it replaces the in-memory table: a
malformed file (bad JSON, unknown version, a bad record, a duplicate nick)
is rejected as a unit, the last good table is kept and an error is logged.
A missing file (or no ``data_dir``) means "no accounts" — every nick
registers as before (no flag day, culture#506 c18).

Reload is lazy: every lookup ``stat()``s the file and re-reads it when its
``(mtime_ns, size, inode)`` signature changed. ``reload()`` forces a
re-read regardless (wired to SIGHUP by ``agentirc serve``).

Account data is server-local. Nothing here is relayed over a server link.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

ACCOUNTS_FILENAME = "accounts.json"
ACCOUNTS_FORMAT_VERSION = 1

ROLES = frozenset({"resident", "operator", "guest"})
STATES = frozenset({"active", "suspended", "revoked"})
GRANT_LEVELS = frozenset({"read", "speak"})

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# Constant used when the account does not exist, so a miss costs the same
# sha256 + compare_digest as a hit.
_DUMMY_HASH = "0" * 64


@dataclass(frozen=True)
class Account:
    """One agent account. ``secret_sha256`` never leaves this server."""

    nick: str
    secret_sha256: str = field(repr=False)
    role: str
    grants: dict[str, str]
    state: str

    @property
    def active(self) -> bool:
        return self.state == "active"


class AccountsFileError(ValueError):
    """The accounts file is malformed; the message never includes secrets."""


def _parse_account(index: int, raw: object, guest_channel_prefixes: Iterable[str] = ()) -> Account:
    if not isinstance(raw, dict):
        raise AccountsFileError(f"accounts[{index}] must be an object")
    nick = raw.get("nick")
    if not isinstance(nick, str) or not nick or " " in nick:
        raise AccountsFileError(f"accounts[{index}].nick must be a non-empty string")
    secret_hash = raw.get("secret_sha256")
    if not isinstance(secret_hash, str) or not _HEX64_RE.match(secret_hash):
        raise AccountsFileError(
            f"accounts[{index}] ({nick}): secret_sha256 must be 64 lowercase hex chars"
        )
    # isinstance before membership: an unhashable JSON value ([] / {}) must
    # be a validation error, never a TypeError (culture#506 h1).
    role = raw.get("role")
    if not isinstance(role, str) or role not in ROLES:
        raise AccountsFileError(f"accounts[{index}] ({nick}): role must be one of {sorted(ROLES)}")
    state = raw.get("state")
    if not isinstance(state, str) or state not in STATES:
        raise AccountsFileError(
            f"accounts[{index}] ({nick}): state must be one of {sorted(STATES)}"
        )
    grants = raw.get("grants", {})
    if not isinstance(grants, dict) or not all(
        isinstance(ch, str) and ch and isinstance(lvl, str) and lvl in GRANT_LEVELS
        for ch, lvl in grants.items()
    ):
        raise AccountsFileError(
            f"accounts[{index}] ({nick}): grants must map channel -> one of "
            f"{sorted(GRANT_LEVELS)}"
        )
    if role == "guest":
        prefixes = tuple(p.casefold() for p in guest_channel_prefixes)
        for ch in grants:
            if not prefixes or not ch.casefold().startswith(prefixes):
                raise AccountsFileError(
                    f"accounts[{index}] ({nick}): guest grant on {ch!r} is outside "
                    "the guest sandbox (accounts.guest_channel_prefixes)"
                )
    return Account(
        nick=nick,
        secret_sha256=secret_hash,
        role=role,
        grants=dict(grants),
        state=state,
    )


def parse_accounts(data: object, guest_channel_prefixes: Iterable[str] = ()) -> dict[str, Account]:
    """Validate a decoded accounts document and return ``{nick: Account}``.

    ``guest_channel_prefixes`` is the guest sandbox: a ``guest`` account may
    only hold grants on channels starting with one of these prefixes (empty =
    guests may hold no grants). A violation rejects the whole file.
    """
    if not isinstance(data, dict):
        raise AccountsFileError("root must be an object")
    version = data.get("version")
    if version != ACCOUNTS_FORMAT_VERSION:
        raise AccountsFileError(
            f"unsupported version {version!r} (expected {ACCOUNTS_FORMAT_VERSION})"
        )
    records = data.get("accounts")
    if not isinstance(records, list):
        raise AccountsFileError("'accounts' must be a list")
    table: dict[str, Account] = {}
    for index, raw in enumerate(records):
        account = _parse_account(index, raw, guest_channel_prefixes)
        if account.nick in table:
            raise AccountsFileError(f"duplicate nick {account.nick!r}")
        table[account.nick] = account
    return table


def hash_secret(secret: str) -> str:
    """sha256 hex digest of *secret* — the value stored in ``secret_sha256``."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class AccountStore:
    """Read-only, auto-reloading view of ``<data_dir>/accounts.json``."""

    def __init__(self, data_dir: str | None, guest_channel_prefixes: Iterable[str] = ()):
        self.guest_channel_prefixes: tuple[str, ...] = tuple(guest_channel_prefixes)
        self.path: str | None = (
            os.path.join(os.path.expanduser(data_dir), ACCOUNTS_FILENAME) if data_dir else None
        )
        self._table: dict[str, Account] = {}
        self._signature: tuple | None = None
        # Called (synchronously, no args) after every successful table
        # replacement from a *later* reload — the IRCd uses it to close live
        # sessions whose account was revoked (culture#506 t10).
        self.on_reload: Callable[[], None] | None = None
        self.reload()

    def _stat_signature(self) -> tuple | None:
        if self.path is None:
            return None
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.error("accounts: cannot stat %s: %s", self.path, exc)
            return self._signature  # keep whatever we have
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def reload(self) -> bool:
        """Re-read the file unconditionally. Returns True if the table was replaced.

        A missing file empties the table. A malformed or unreadable file keeps
        the last good table and logs an error.
        """
        if self.path is None:
            self._table = {}
            return True
        signature = self._stat_signature()
        if signature is None:
            if self._table:
                logger.info("accounts: %s is gone; no accounts are enforced", self.path)
            self._table = {}
            self._signature = None
            self._notify_reload()
            return True
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            table = parse_accounts(data, self.guest_channel_prefixes)
        except Exception as exc:  # noqa: BLE001 - a bad file must never crash the server
            # Anything goes: json.JSONDecodeError / AccountsFileError
            # (ValueErrors), OSError, RecursionError on deeply nested JSON, or
            # a TypeError we did not foresee. The message carries only
            # structural info (our own AccountsFileError text, else just the
            # exception type) — never record contents. ``_signature`` is
            # updated so the same bad file is not re-parsed on every lookup.
            logger.error(
                "accounts: rejected %s (%s: %s); keeping the last good table (%d accounts)",
                self.path,
                type(exc).__name__,
                exc if isinstance(exc, AccountsFileError) else "invalid file",
                len(self._table),
            )
            self._signature = signature
            return False
        self._table = table
        self._signature = signature
        logger.info("accounts: loaded %d accounts from %s", len(table), self.path)
        self._notify_reload()
        return True

    def _notify_reload(self) -> None:
        if self.on_reload is not None:
            try:
                self.on_reload()
            except Exception:  # noqa: BLE001 - a listener must never break a lookup
                logger.exception("accounts: reload listener failed")

    def maybe_reload(self) -> None:
        """Reload when the file's (mtime, size, inode) signature changed."""
        if self.path is None:
            return
        if self._stat_signature() != self._signature:
            self.reload()

    def get(self, nick: str | None) -> Account | None:
        """Exact-match lookup (after a lazy reload check)."""
        if not nick:
            return None
        self.maybe_reload()
        return self._table.get(nick)

    def current(self, nick: str | None) -> Account | None:
        """Lookup without the lazy reload check (for use inside reload hooks)."""
        return self._table.get(nick) if nick else None

    def channel_access(self, nick: str | None, channel: str) -> str | None:
        """Live access level of account *nick* on *channel*.

        Returns ``"speak"``, ``"read"`` or ``None`` (no access). Reads the
        current table (lazy reload), so a grant change applies on the next
        check. ``operator`` accounts have ``speak`` everywhere; a missing or
        non-active account has no access.
        """
        account = self.get(nick)
        if account is None or not account.active:
            return None
        if account.role == "operator":
            return "speak"
        want = channel.casefold()
        level = None
        for ch, lvl in account.grants.items():
            if ch.casefold() == want and (level is None or lvl == "speak"):
                level = lvl
        return level

    def role_of(self, nick: str | None) -> str | None:
        """Live role of an active account, else ``None``."""
        account = self.get(nick)
        return account.role if account is not None and account.active else None

    def is_account(self, nick: str | None) -> bool:
        return self.get(nick) is not None

    def check(self, nick: str, secret: str) -> tuple[Account | None, str | None]:
        """Like :meth:`verify`, but also returns a failure reason code.

        Reasons: ``unknown_account``, ``bad_secret``, ``revoked``,
        ``suspended``. The reason is for server-side logs/metrics only; the
        client always gets a generic 904. A wrong secret on a revoked or
        suspended account reports ``bad_secret``; the state is reported only
        when the secret was right.
        """
        account = self.get(nick)
        expected = account.secret_sha256 if account is not None else _DUMMY_HASH
        ok = hmac.compare_digest(hash_secret(secret), expected)
        if account is None:
            return None, "unknown_account"
        if not ok:
            return None, "bad_secret"
        if not account.active:
            return None, account.state
        return account, None

    def verify(self, nick: str, secret: str) -> Account | None:
        """Return the account if *secret* matches and the account is active.

        Compares sha256 hex digests with ``hmac.compare_digest``. A missing
        account still pays for one hash + compare so a miss and a wrong secret
        cost the same.
        """
        account = self.get(nick)
        expected = account.secret_sha256 if account is not None else _DUMMY_HASH
        ok = hmac.compare_digest(hash_secret(secret), expected)
        if account is None or not ok or not account.active:
            return None
        return account


class AuthFailureLimiter:
    """Per-peer-address sliding-window limiter for failed authentications.

    ``record(addr)`` notes a failure; ``blocked(addr)`` is True once *limit*
    failures happened within the last *window* seconds. ``limit <= 0``
    disables it. Loopback is deliberately not exempt. State is in-memory and
    pruned as windows expire.
    """

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def _prune(self, addr: str) -> deque[float] | None:
        q = self._events.get(addr)
        if q is None:
            return None
        cutoff = self._clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._events[addr]
            return None
        return q

    def record(self, addr: str) -> None:
        if self.limit <= 0:
            return
        if len(self._events) > 4096:  # bound memory under address churn
            for key in list(self._events):
                self._prune(key)
        self._events.setdefault(addr, deque()).append(self._clock())
        q = self._events[addr]
        while len(q) > self.limit:
            q.popleft()

    def blocked(self, addr: str) -> bool:
        if self.limit <= 0:
            return False
        q = self._prune(addr)
        return q is not None and len(q) >= self.limit


def _parse_ip(host: str):
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def is_trusted_peer(host: str | None, trusted_cidrs: Iterable[str]) -> bool:
    """True if *host* is loopback or inside one of *trusted_cidrs*.

    Loopback (127.0.0.0/8, ::1, and their IPv4-mapped forms) is always
    trusted. Anything that does not parse as an IP address is untrusted.
    """
    if not host:
        return False
    ip = _parse_ip(host)
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    for cidr in trusted_cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if ip.version == net.version and ip in net:
            return True
    return False
