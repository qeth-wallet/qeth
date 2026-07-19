"""Plugin registry — the built-in plugins as declarative manifests.

MainWindow builds its plugin set from ``enabled_manifests(store)`` instead of
hard-coding the four topics. A ``PluginManifest`` says everything the shell
needs to mount a plugin (which slot, in what order, whether it can be turned
off, what it depends on) without the shell importing the plugin package —
``factory`` lazy-imports it, so a disabled plugin is never even imported.

The shape is deliberately what a ``setuptools`` entry point would resolve to
(``group="qeth.plugins"``, value = a ``PluginManifest``), so third-party /
optional "assembly" plugins can be discovered later with no change to the
plugins themselves or to this file's consumers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..plugin import Plugin
from ..store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginManifest:
    id: str                          # stable key: store setting, plugins dict, header_states
    title: str                       # human name for the toggle UI (tab uses Plugin.name)
    factory: Callable[[Store], Plugin]   # lazy-imports the plugin package
    slot: str                        # "left" | "right"
    order: int = 100                 # mount order within the slot (ascending)
    required: bool = False           # cannot be disabled (account source / core services)
    default_enabled: bool = True
    requires: tuple[str, ...] = ()   # ids of plugins this one needs enabled
    hides_chain_selector: bool = False   # slot chrome: hide the chain combo while active
    persists_header: bool = False    # has header_state()/restore_header_state()
    description: str = ""            # one-liner for the toggle UI tooltip


def _wallets(store: Store) -> Plugin:
    from .wallets import WalletsPlugin
    return WalletsPlugin(store)


def _tokens(store: Store) -> Plugin:
    from .tokens import TokensPlugin
    return TokensPlugin(store)


def _transactions(store: Store) -> Plugin:
    from .transactions import TransactionsPlugin
    return TransactionsPlugin(store=store)


def _ens(store: Store) -> Plugin:
    from .ens import EnsPlugin
    return EnsPlugin(store)


# Order across slots is by `order`; wallets alone in the left slot, the rest in
# the right slot. `transactions` is required in v1: it owns the composer / sign
# dialogs, the ABI/identity/tx caches every sign flow needs, the tx_confirmed /
# tx_dropped signals that back request_transaction and all ENS writes, and the
# Live/Pending watchers. Making it optional means splitting that service layer
# out of the plugin — future work, not v1.
BUILTIN_MANIFESTS: tuple[PluginManifest, ...] = (
    PluginManifest(
        id="wallets", title="Accounts", factory=_wallets,
        slot="left", order=0, required=True,
        description="Wallet accounts — the account source; can't be turned off",
    ),
    PluginManifest(
        id="tokens", title="Tokens", factory=_tokens,
        slot="right", order=10, persists_header=True,
        description="Token balances, prices, discovery, and sending",
    ),
    PluginManifest(
        id="transactions", title="Transactions", factory=_transactions,
        slot="right", order=20, required=True,
        description="Transaction history, sending, and signing",
    ),
    PluginManifest(
        id="ens", title="ENS", factory=_ens,
        slot="right", order=30, requires=("transactions",),
        hides_chain_selector=True,
        description="ENS names: records, renewals, subdomains (Ethereum only)",
    ),
)


def enabled_manifests(
    store: Store,
    manifests: tuple[PluginManifest, ...] = BUILTIN_MANIFESTS,
) -> list[PluginManifest]:
    """The manifests to mount, in `order`. A `required` manifest is always
    included (a stale disable in the store is ignored with a warning). An
    optional manifest is dropped when the user disabled it OR when any plugin
    in its `requires` isn't going to be mounted. Unknown ids in
    `store.disabled_plugins` are ignored."""
    disabled = set(store.disabled_plugins)
    kept: dict[str, PluginManifest] = {}
    for m in sorted(manifests, key=lambda m: m.order):
        if m.required:
            if m.id in disabled:
                log.warning("plugin %r is required and cannot be disabled — "
                            "ignoring the stored disable", m.id)
            kept[m.id] = m
            continue
        if m.id in disabled:
            continue
        if any(dep not in kept for dep in m.requires):
            # A dependency is off/absent — this plugin can't run without it.
            missing = [d for d in m.requires if d not in kept]
            log.info("plugin %r disabled: requires %s", m.id, missing)
            continue
        kept[m.id] = m
    return list(kept.values())
