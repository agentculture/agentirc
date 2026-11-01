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
is rejected as a unit, the last good table is kept and an error is logged;
the nicks the rejected file names stay reserved until it is fixed (fail
closed, culture#506 h2 — see ``AccountStore._fail_closed``).
A missing file (or no ``data_dir``) means "no accounts" — every nick
registers as before (no flag day, culture#506 c18).

Lookups (``get``, ``is_reserved``, ``channel_access``, ``role_of`` …) read
the cached table and never touch the file: they sit on hot paths (every
EVENTSUB dispatch, every channel PRIVMSG). The file is ``stat()``ed — and
re-read when its ``(mtime_ns, size, inode)`` signature changed — only by
``maybe_reload()``, which runs on the IRCd's 1 s watcher tick and at the
registration decision points (a connection's first pre-registration NICK, the
SASL credential check and the registration gate — at most three ``stat()``
calls per connection), so a freshly written account is enforced at the very
next registration. ``reload()`` forces a re-read regardless (wired to SIGHUP
by ``agentirc serve``).

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
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable, Hashable, Iterable

logger = logging.getLogger(__name__)

ACCOUNTS_FILENAME = "accounts.json"
ACCOUNTS_FORMAT_VERSION = 1

ROLES = frozenset({"resident", "operator", "guest"})
STATES = frozenset({"active", "suspended", "revoked"})
GRANT_LEVELS = frozenset({"read", "speak"})

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# Account nicks are printable ASCII without spaces (culture#506 h2, M7): the
# server refuses non-ASCII nicks at NICK, so a non-ASCII account nick could
# never log in, and ASCII-only is what makes the case-variant rule complete.
_ACCOUNT_NICK_RE = re.compile(r"^[\x21-\x7e]+$")
# Best-effort recovery of the nicks a rejected file names (M5 fail-closed).
_RECOVER_NICK_RE = re.compile(r'"nick"\s*:\s*"([^"\\]+)"')
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold_nick(nick: str) -> str:
    """ASCII-only case fold used to spot case variants of account nicks.

    Only ``A-Z`` are folded (no Unicode case mapping), matching the server's
    ASCII-only nick rule.
    """
    return nick.translate(_ASCII_LOWER)


# Default key cap of ``AuthFailureLimiter`` (least recently used evicted).
AUTH_LIMITER_MAX_KEYS = 4096

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
    if not _ACCOUNT_NICK_RE.match(nick):
        raise AccountsFileError(
            f"accounts[{index}].nick must be printable ASCII without spaces (culture#506 M7)"
        )
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
    _check_grants(index, nick, role, grants, guest_channel_prefixes)
    return Account(
        nick=nick,
        secret_sha256=secret_hash,
        role=role,
        grants=dict(grants),
        state=state,
    )


def _check_grants(
    index: int, nick: str, role: str, grants: object, guest_channel_prefixes: Iterable[str]
) -> None:
    """Validate one record's ``grants`` map (shape first, then the guest sandbox)."""
    if not isinstance(grants, dict) or not all(
        isinstance(ch, str) and ch and isinstance(lvl, str) and lvl in GRANT_LEVELS
        for ch, lvl in grants.items()
    ):
        raise AccountsFileError(
            f"accounts[{index}] ({nick}): grants must map channel -> one of "
            f"{sorted(GRANT_LEVELS)}"
        )
    if role == "guest":
        # Exact (case-sensitive) prefix match: channel names are
        # case-sensitive server-wide, so "#SBX-1" is not inside "#sbx-".
        prefixes = tuple(guest_channel_prefixes)
        for ch in grants:
            if not prefixes or not ch.startswith(prefixes):
                raise AccountsFileError(
                    f"accounts[{index}] ({nick}): guest grant on {ch!r} is outside "
                    "the guest sandbox (accounts.guest_channel_prefixes)"
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
    folded: set[str] = set()
    for index, raw in enumerate(records):
        account = _parse_account(index, raw, guest_channel_prefixes)
        if account.nick in table:
            raise AccountsFileError(f"duplicate nick {account.nick!r}")
        if fold_nick(account.nick) in folded:
            raise AccountsFileError(
                f"accounts[{index}] ({account.nick}): nick differs from another account "
                "nick only by letter case"
            )
        table[account.nick] = account
        folded.add(fold_nick(account.nick))
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
        self._folded: dict[str, str] = {}  # fold_nick(nick) -> nick
        self._signature: tuple | None = None
        # Fail-closed state (culture#506 h2, M5). ``_reserved_fallback``: the
        # folded nicks a *rejected* file names — they register only once a
        # valid file loads. ``_lockdown``: no table was ever loaded and no
        # nick could be recovered from the rejected file — every local
        # registration is refused until a valid file loads.
        self._loaded = False
        self._reserved_fallback: frozenset[str] = frozenset()
        self._lockdown = False
        # Called (synchronously, no args) after every successful table
        # replacement from a *later* reload — the IRCd uses it to close live
        # sessions whose account was revoked (culture#506 t10).
        self.on_reload: Callable[[], None] | None = None
        if self.path is not None and not os.path.isdir(os.path.dirname(self.path)):
            logger.warning(
                "accounts: data_dir %s does not exist (not mounted?); no accounts are "
                "enforced until %s appears",
                os.path.dirname(self.path),
                ACCOUNTS_FILENAME,
            )
        self.reload()

    def _stat_signature(self) -> tuple | None:
        if self.path is None:
            return None
        stat_error: str | None = None
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            stat_error = str(exc)
        if stat_error is not None:
            # Logged after the ``except`` block on purpose: one line, no
            # traceback (this runs on every watcher tick while the error persists).
            logger.error("accounts: cannot stat %s: %s", self.path, stat_error)
            return self._signature  # keep whatever we have
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def reload(self) -> bool:
        """Re-read the file unconditionally. Returns True if the table was replaced.

        A missing file empties the table. A malformed or unreadable file keeps
        the last good table, logs an error and fails closed for the nicks it
        names (see ``is_reserved``).
        """
        if self.path is None:
            self._table = {}
            self._folded = {}
            self._loaded = True
            return True
        signature = self._stat_signature()
        if signature is None:
            self._replace({}, None, missing=True)
            return True
        text: str | None = None
        # (exception type name, structural detail) of a rejected file, else None.
        rejection: tuple[str, str] | None = None
        try:
            with open(self.path, encoding="utf-8") as f:
                text = f.read()
            data = json.loads(text)
            table = parse_accounts(data, self.guest_channel_prefixes)
        except Exception as exc:  # noqa: BLE001 - a bad file must never crash the server
            # Anything goes: json.JSONDecodeError / AccountsFileError
            # (ValueErrors), OSError, RecursionError on deeply nested JSON, or
            # a TypeError we did not foresee. Only structural info is kept
            # (our own AccountsFileError text, else just the exception type)
            # — never record contents, and never the exception itself: it is
            # logged below, outside this block, with no traceback that could
            # quote the file.
            rejection = (
                type(exc).__name__,
                str(exc) if isinstance(exc, AccountsFileError) else "invalid file",
            )
        if rejection is not None:
            logger.error(
                "accounts: rejected %s (%s: %s); keeping the last good table (%d accounts)",
                self.path,
                rejection[0],
                rejection[1],
                len(self._table),
            )
            # ``_signature`` is updated so the same bad file is not re-parsed
            # on every lookup.
            self._signature = signature
            self._fail_closed(text)
            return False
        self._replace(table, signature)
        logger.info("accounts: loaded %d accounts from %s", len(table), self.path)
        self._notify_reload()
        return True

    def _replace(self, table: dict[str, Account], signature, *, missing: bool = False) -> None:
        before = len(self._table)
        self._table = table
        self._folded = {fold_nick(nick): nick for nick in table}
        self._signature = signature
        self._loaded = True
        self._reserved_fallback = frozenset()
        self._lockdown = False
        if before and not table:
            # A previously non-empty table becoming empty unreserves every
            # account nick at once: say so loudly (culture#506 h2, M5).
            logger.warning(
                "accounts: %s; the table went from %d accounts to none, so no accounts are "
                "enforced and every account nick can be registered without a secret",
                f"{self.path} is gone" if missing else f"{self.path} lists no accounts",
                before,
            )
        if missing:
            self._notify_reload()

    def _fail_closed(self, text: str | None) -> None:
        """Reserve the nicks a rejected file names (culture#506 h2, M5).

        A typo must not unreserve the very nicks the operator is protecting.
        The nicks are recovered best-effort from the raw text; they stay
        reserved (no secret can log in to them) until a valid file loads.
        If no table was ever loaded and no nick can be recovered, every
        local registration is refused instead.
        """
        recovered = {fold_nick(n) for n in _RECOVER_NICK_RE.findall(text or "")}
        self._reserved_fallback = frozenset(recovered)
        self._lockdown = not self._loaded and not recovered
        if self._lockdown:
            logger.error(
                "accounts: failing closed: no valid accounts table was loaded and no nick "
                "could be recovered from %s; every local registration is refused until the "
                "file is fixed",
                self.path,
            )
        elif recovered - set(self._folded):
            logger.error(
                "accounts: failing closed: %d nick(s) named in the rejected %s are reserved "
                "and cannot register until the file is fixed",
                len(recovered - set(self._folded)),
                self.path,
            )

    def _notify_reload(self) -> None:
        if self.on_reload is not None:
            try:
                self.on_reload()
            except Exception:  # noqa: BLE001 - a listener must never break a lookup
                logger.exception("accounts: reload listener failed")

    def maybe_reload(self) -> None:
        """Reload when the file's (mtime, size, inode) signature changed.

        One ``stat()`` per call. Called by the watcher tick and at the
        registration decision points only; lookups never call it.
        """
        if self.path is None:
            return
        if self._stat_signature() != self._signature:
            self.reload()

    def get(self, nick: str | None) -> Account | None:
        """Exact-match lookup in the cached table (never touches the file)."""
        return self._table.get(nick) if nick else None

    def current(self, nick: str | None) -> Account | None:
        """Alias of :meth:`get` kept for reload hooks."""
        return self.get(nick)

    def is_reserved(self, nick: str | None) -> bool:
        """True if *nick* may only be held by an authenticated session.

        Covers an account nick, any ASCII case variant of one (culture#506
        h2, M7), the nicks a rejected file names, and — in lockdown — every
        nick (M5). Reads the cached table.
        """
        if not nick:
            return False
        if self._lockdown:
            return True
        folded = fold_nick(nick)
        return folded in self._folded or folded in self._reserved_fallback

    def lookalike_of(self, nick: str | None) -> str | None:
        """The account nick *nick* is a case variant of (not equal to), else None."""
        if not nick:
            return None
        canonical = self._folded.get(fold_nick(nick))
        return canonical if canonical is not None and canonical != nick else None

    def channel_access(self, nick: str | None, channel: str) -> str | None:
        """Live access level of account *nick* on *channel*.

        Returns ``"speak"``, ``"read"`` or ``None`` (no access). Reads the
        cached table, which the watcher keeps current, so a grant change
        applies within about 1 s. ``operator`` accounts have ``speak``
        everywhere; a missing or non-active account has no access.
        """
        account = self.get(nick)
        if account is None or not account.active:
            return None
        if account.role == "operator":
            return "speak"
        # Exact match (culture#506 h2, M7): channel names are case-sensitive
        # server-wide, so a grant on #ops never covers #OPS or #Ops.
        return account.grants.get(channel)

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
    """Sliding-window limiter for failed authentications.

    ``record(key)`` notes a failure; ``blocked(key)`` is True once *limit*
    failures happened within the last *window* seconds. ``limit <= 0``
    disables it. The server keys it per ``(peer address, folded authcid)``
    (culture#506 h2, M4), so failures against one account never lock out
    another account from the same address.

    The authcid is attacker-chosen, so the key space is unbounded: state is a
    bounded LRU (agentirc#65). At most *max_keys* keys are kept; touching a
    key (``record`` or ``blocked``) makes it most recent, and the least
    recently used key is evicted beyond the cap. Each key keeps at most
    *limit* timestamps and is pruned lazily when touched, so ``record`` and
    ``blocked`` are O(1) amortised whatever the churn.
    """

    def __init__(
        self,
        limit: int,
        window: float,
        clock: Callable[[], float] = time.monotonic,
        *,
        max_keys: int = AUTH_LIMITER_MAX_KEYS,
    ):
        self.limit = limit
        self.window = window
        self.max_keys = max(1, max_keys)
        self._clock = clock
        self._events: OrderedDict[Hashable, deque[float]] = OrderedDict()

    def _touch(self, key: Hashable) -> deque[float] | None:
        """Prune *key*'s expired failures; mark it most recently used."""
        q = self._events.get(key)
        if q is None:
            return None
        cutoff = self._clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._events[key]
            return None
        self._events.move_to_end(key)
        return q

    def record(self, key: Hashable) -> None:
        if self.limit <= 0:
            return
        q = self._touch(key)
        if q is None:
            q = self._events[key] = deque(maxlen=self.limit)
        q.append(self._clock())
        while len(self._events) > self.max_keys:
            self._events.popitem(last=False)

    def blocked(self, key: Hashable) -> bool:
        if self.limit <= 0:
            return False
        q = self._touch(key)
        return q is not None and len(q) >= self.limit


def _parse_ip(host: str):
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def normalize_network(cidr: str):
    """Parse *cidr* (``strict=False``); an IPv4-mapped IPv6 network becomes IPv4.

    ``::ffff:10.0.0.0/104`` would otherwise never match: peer addresses are
    unmapped before the check (culture#506 h2, M3).
    """
    net = ipaddress.ip_network(str(cidr), strict=False)
    if net.version == 6 and net.prefixlen >= 96:
        mapped = net.network_address.ipv4_mapped
        if mapped is not None:
            return ipaddress.ip_network(f"{mapped}/{net.prefixlen - 96}", strict=False)
    return net


def is_trusted_peer(
    host: str | None, trusted_cidrs: Iterable[str], *, trust_loopback: bool = True
) -> bool:
    """True if *host* is loopback (when *trust_loopback*) or inside *trusted_cidrs*.

    Loopback is 127.0.0.0/8, ::1 and their IPv4-mapped forms. IPv4-mapped
    entries in *trusted_cidrs* are normalised to IPv4. Anything that does not
    parse as an IP address is untrusted.
    """
    if not host:
        return False
    ip = _parse_ip(host)
    if ip is None:
        return False
    if trust_loopback and ip.is_loopback:
        return True
    for cidr in trusted_cidrs:
        try:
            net = normalize_network(cidr)
        except ValueError:
            continue
        if ip.version == net.version and ip in net:
            return True
    return False
