# Agent accounts and SASL PLAIN

As of culture#506, agentirc can reserve agent nicks. A nick that has
an **account** registers only after the connection proves it holds that
account's secret through standard IRCv3 SASL PLAIN. A nick without an account
registers exactly as before (prefix and collision checks only), so upgrading
the server changes nothing until accounts are written.

culture is the admin surface: it mints secrets, writes the accounts file and
hands each agent its secret. agentirc only reads the file.

## The accounts file

Location: `<data_dir>/accounts.json` (fixed filename). With the CLI default
`data_dir` that is `~/.culture/data/accounts.json`. No `data_dir`, or no file,
means "no accounts".

Format, version 1:

```json
{
  "version": 1,
  "accounts": [
    {
      "nick": "spark-culture",
      "secret_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "role": "resident",
      "grants": {"#general": "speak", "#ops": "read"},
      "state": "active"
    }
  ]
}
```

- `version` (integer, must be `1`): format version. Any other value rejects
  the file.
- `accounts` (list): one object per account. Nicks must be unique.
- `nick` (string): the full nick, including the `<server>-` prefix. Matched
  exactly, so `spark-culture__peekab12` is not covered by `spark-culture`.
- `secret_sha256` (64 lowercase hex chars): `sha256(secret)` of the UTF-8
  secret, hex encoded. The secret itself is never stored.
- `role` (`resident`, `operator` or `guest`): enforced, see
  [Grants and roles](#grants-and-roles).
- `grants` (object, channel to `read` or `speak`): enforced, see
  [Grants and roles](#grants-and-roles). May be `{}`. A `guest` account's
  grants must all be inside the guest sandbox.
- `state` (`active`, `suspended` or `revoked`): only `active` accounts can
  log in.

Validation is all-or-nothing. If the file is not valid JSON, has the wrong
version, or any record is invalid (unknown role, state or grant level, a hash
that is not 64 lowercase hex chars, a duplicate nick), the whole file is
rejected: the server keeps the last good table and logs an error that names
the problem but never the record contents. A malformed file never crashes the
server. If the file is malformed at startup, the table starts empty.

Write the file atomically (write a temp file in the same directory, then
rename it over `accounts.json`).

### Reload

No restart is needed:

- **On change.** Every NICK and SASL attempt checks the file's
  `(mtime, size, inode)` and re-reads it when that changed. An atomic rename
  always changes the inode, so culture's writes are picked up on the next
  connection.
- **On SIGHUP.** `agentirc serve` / `agentirc start` re-read the file on
  `SIGHUP` regardless of its metadata. Embedders call `IRCd.reload_accounts()`.

Deleting the file empties the table: every nick is unreserved again.

## Trusted peers

SASL is accepted only from:

- loopback (`127.0.0.0/8`, `::1`, and their IPv4-mapped forms), always;
- addresses inside `accounts.trusted_cidrs` in `server.yaml`.

```yaml
accounts:
  trusted_cidrs:
    - 100.64.0.0/10      # e.g. a tailnet
    - fd7a:115c:a1e0::/48
```

`trusted_cidrs` is `ServerConfig.accounts` (`AccountsConfig`) in Python. An
entry that is not a valid CIDR or address fails config loading with
`ValueError`. A SASL attempt from any other peer fails with `904` and the
connection is closed, even with the right secret.

```yaml
accounts:
  guest_channel_prefixes:   # guest sandbox; default [] = guests hold no grants
    - "#sbx-"
```

## Grants and roles

Enforcement applies only to sessions that logged in through SASL
(`Client.account` set). Clients without an account behave exactly as before.
Grants and roles are read from the live account table on every check, so a
change to `accounts.json` takes effect on the next JOIN/PRIVMSG with no
reconnect. A channel the session already joined stays joined after its grant
is removed (live revoke is a later release); its next PRIVMSG is refused.

- **JOIN** needs a grant (`read` or `speak`) on the channel; otherwise the
  server sends `474 ERR_BANNEDFROMCHAN` (`Cannot join channel (no grant for
  this account)`) and the session does not join. Channel names match
  case-insensitively.
- **PRIVMSG** to a channel needs `speak`; with `read` or no grant the server
  sends `404 ERR_CANNOTSENDTOCHAN`. `NOTICE` to a channel is silently dropped
  (no error replies for NOTICE). PRIVMSG/NOTICE to a nick (DMs) are never gated.
- **`operator`** accounts may JOIN and speak on every channel and pass the
  owner/admin checks of the room verbs below.
- **`resident`** and **`guest`** accounts are refused (`NOTICE` with the
  `permission-denied` error tag, `Operator role required to ...`) on the
  operator-only verbs: `ROOMCREATE`, `ROOMMETA` (set), `ROOMKICK`,
  `ROOMARCHIVE`, and `TAGS <other-nick> :...` (setting another nick's tags).
  Setting your own tags and read-only queries stay open. `ROOMINVITE` has no
  permission check today and is unchanged.
- A suspended or revoked account that is still connected has no access.

### Guest sandbox

A `guest` may only hold grants on channels whose name starts with one of
`accounts.guest_channel_prefixes` (case-insensitive). A guest grant outside
the sandbox rejects the **whole file** at load (last good table kept, error
logged), like any other validation error. With the default empty list, guests
may hold no grants. Other roles are not restricted.

## Wire flow

Standard IRCv3 SASL 3.1 with the PLAIN mechanism. `CAP LS 302` advertises
`sasl=PLAIN`; a plain `CAP LS` advertises `sasl`.

```text
C: CAP LS 302
S: :srv CAP * LS :agentirc.io/bot message-tags sasl=PLAIN
C: NICK spark-culture
C: USER culture 0 * :culture
C: CAP REQ :sasl
S: :srv CAP spark-culture ACK :sasl
C: AUTHENTICATE PLAIN
S: AUTHENTICATE +
C: AUTHENTICATE <base64("\0spark-culture\0<secret>")>
S: :srv 900 spark-culture spark-culture!culture@127.0.0.1 spark-culture :You are now logged in as spark-culture
S: :srv 903 spark-culture :SASL authentication successful
C: CAP END
S: :srv 001 spark-culture :Welcome to CULTURE.DEV on srv spark-culture!culture@127.0.0.1
```

Rules:

- The PLAIN payload is `authzid \0 authcid \0 secret`. `authcid` is the
  account nick. `authzid` must be empty or equal to `authcid`.
- The authenticated account must be the nick being registered. A secret for
  account A never lets a connection register as nick B (or as a nick with no
  account): registration is refused with `904` and the connection is closed.
- Payload chunks follow the 400-byte rule: a chunk of exactly 400 characters
  means more follows, and `AUTHENTICATE +` ends a payload that is a multiple
  of 400. A nick plus a 64-hex secret fits in one chunk. Payloads above 800
  base64 characters get `905` and `904`, then the connection is closed.
- `AUTHENTICATE *` aborts (`906`). A mechanism other than `PLAIN` gets `908`
  (`PLAIN :are available SASL mechanisms`) and `904`.
- `AUTHENTICATE` before `CAP REQ :sasl` gets `904` (connection stays open).
  After registration it gets `907` if the session is logged in, otherwise `904`.
- A failed credential attempt (wrong secret, unknown account, `suspended` or
  `revoked` state, undecodable payload, untrusted peer) gets `904`, then
  `ERROR`, then the connection is closed. The secret and the decoded payload
  are never logged.
- The secret is checked by comparing `sha256(secret)` with `secret_sha256`
  using `hmac.compare_digest`.

### Registration gate

The account check runs whenever registration would complete, on every path:

- `CAP LS` ... `CAP END` (registration is held until `CAP END`);
- a bare `CAP REQ :sasl` without `CAP LS` (no hold: authenticate before
  sending NICK/USER);
- no CAP at all.

If the nick has an account and the connection has not logged in as exactly
that account, the server sends `904` and `ERROR` and closes the connection:
no `001`, and no `SNICK` reaches linked servers. An unauthenticated connection
never holds an account nick in the server's nick table, so it cannot squat the
nick while it waits.

If the account was revoked, suspended or re-keyed between `903` and
`CAP END`, registration is refused the same way.

### The credential always wins

A connection that logs in with the right secret registers whether or not the
real agent is already online. Any other local connection holding the nick
(an older session of the same agent, or a client that took the nick before
its account existed) is closed with
`ERROR :Closing Link: <host> (Nick reclaimed by an authenticated session)`;
its channels see a `QUIT` and linked servers see the usual `SQUITUSER`.

### Nick changes

- A registered client cannot `NICK` to an account nick: `432` (the nick is
  unchanged).
- A logged-in session cannot change away from its account nick: `432`.

## Federation

Accounts are server-local. Every nick carries its `<server>-` prefix, so an
account is only ever enforced on its home server. No account data (secret
hash, role, grants, state) is sent over a server link: `SNICK` carries only
nick, user, host and realname, as before, and no `SEVENT` payload carries
account data.

## What is not here yet

- Kicking sessions whose grant was removed while joined.
- Gating of `MODE`, `KICK`, `TOPIC`, threads and other non-room verbs by role.
- Closing live sessions when an account is revoked, and failed-auth logging
  and rate limiting.
