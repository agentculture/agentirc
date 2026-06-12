"""agentirc embedded bot framework (public subsystem, since 9.7.0).

Deterministic, YAML-spec'd chatops bots that run as in-process
``VirtualClient`` presences inside an :class:`agentirc.ircd.IRCd`. Public
exports (``BotManager``, ``Bot``) are populated as the subsystem lands; this
module is the package anchor for the wave-0 vendored modules
(``template_engine``, ``filter_dsl``, ``config``, ``virtual_client``).
"""
