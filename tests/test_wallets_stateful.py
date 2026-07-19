"""Stateful (hypothesis) fuzzing of the Wallets tree across interleaved
selection / add / remove / set-default / rebuild.

The bug this guards: `_rebuild_tree`'s clear+reselect used to re-fire
`selected_address_changed` (an emit(None) then emit(addr)) even when the
selection was PRESERVED, churning every downstream plugin's reload (and dropping
ENS's in-flight Helios proof). The fix blocks signals during the rebuild and
re-broadcasts once, only on a genuine change (`now_addr != _last_emitted`).

A RuleBasedStateMachine mixes selecting accounts, adding/removing them, setting
the default, and bare rebuilds, recording every `selected_address_changed`
emission. Invariants after every step: the tree's account leaves equal the
store's accounts; the plugin's last-broadcast address stays in sync with the
tree's single selection. And the `rebuild` rule directly asserts the guard — a
bare rebuild that preserves the selection emits nothing.

No network, no workers (WalletsPlugin is timer/worker-free once the add-dialog
ENS lookups are bypassed by driving store.add_account + rebuild_tree directly).
The Store config is redirected to a per-example tmp dir (this runs outside
tmp_qeth) — never the real ~/.qeth/config.json.
"""

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

import qeth.store as store_mod
from qeth.plugins.wallets import WalletsPlugin
from qeth.store import Store

# A fixed pool of candidate accounts across the source sections.
POOL = [
    {"address": "0x" + "a1" * 20, "path": "m/44'/60'/0'/0/0",
     "source": "hot", "scheme": "BIP-44", "label": ""},
    {"address": "0x" + "b2" * 20, "path": "m/44'/60'/0'/0/1",
     "source": "ledger", "scheme": "BIP-44", "label": ""},
    {"address": "0x" + "c3" * 20, "path": "",
     "source": "watch_only", "scheme": "", "label": ""},
    {"address": "0x" + "d4" * 20, "path": "m/44'/60'/0'/0/2",
     "source": "ledger", "scheme": "BIP-44", "label": ""},
]
ADDRS = [a["address"] for a in POOL]
IDX = list(range(len(POOL)))


class WalletsTreeMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="qeth-wal-sm-")
        self._saved_cfg = (store_mod.CONFIG_DIR, store_mod.CONFIG_FILE)
        store_mod.CONFIG_DIR = Path(self.tmp)
        store_mod.CONFIG_FILE = Path(self.tmp) / "config.json"

        self.store = Store.load()
        self.plugin = WalletsPlugin(self.store)
        self.widget = self.plugin.widget()             # builds the tree
        self.emissions: list = []
        self.plugin.selected_address_changed.connect(
            lambda a: self.emissions.append(a))

    def teardown(self):
        self.widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        store_mod.CONFIG_DIR, store_mod.CONFIG_FILE = self._saved_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers ----------------------------------------------------------
    def _in_store(self, addr: str) -> bool:
        return any(a["address"].lower() == addr.lower()
                   for a in self.store.accounts)

    def _tree_leaf_addrs(self) -> set[str]:
        """Addresses carried by the tree's account leaves (UserRole on col 0)."""
        tree = self.plugin._tree
        assert tree is not None
        out = set()
        stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it is None:
                continue
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, str) and d.startswith("0x") and len(d) == 42:
                out.add(d.lower())
            for i in range(it.childCount()):
                stack.append(it.child(i))
        return out

    # --- rules ------------------------------------------------------------
    @rule(i=st.sampled_from(IDX))
    def add(self, i):
        acct = POOL[i]
        if self._in_store(acct["address"]):
            return
        self.store.add_account(dict(acct))
        self.plugin.rebuild_tree()

    @rule()
    def remove_selected(self):
        key = self.plugin.selected_key
        if key is None:
            return
        addr, path = key
        self.store.remove_account(addr, path)
        self.plugin.rebuild_tree()

    @rule(i=st.sampled_from(IDX))
    def select(self, i):
        addr = POOL[i]["address"]
        if self._in_store(addr):
            self.plugin.select_address(addr)

    @rule()
    def clear_selection(self):
        tree = self.plugin._tree
        if tree is not None:
            tree.clearSelection()

    @rule(i=st.sampled_from(IDX))
    def set_default(self, i):
        acct = POOL[i]
        if not self._in_store(acct["address"]):
            return
        self.store.set_default_account(acct["address"], acct["path"])
        self.plugin.rebuild_tree()

    @rule()
    def rebuild(self):
        # The spurious-rebroadcast guard: a bare rebuild that PRESERVES a live
        # single selection must emit nothing (the regression re-fired None→addr
        # every time). With nothing (or a group) selected the rebuild may
        # legitimately re-select the default account — but still at most once.
        key = self.plugin.selected_key           # (addr, path) or None
        n0 = len(self.emissions)
        self.plugin.rebuild_tree()
        new = self.emissions[n0:]
        if key is not None:
            assert new == [], \
                f"preserved selection {key[0]} was re-broadcast on rebuild: {new}"
        else:
            assert len(new) <= 1, f"rebuild emitted more than once: {new}"

    # --- invariants -------------------------------------------------------
    @invariant()
    def tree_matches_store(self):
        store_addrs = {a["address"].lower() for a in self.store.accounts}
        assert self._tree_leaf_addrs() == store_addrs, \
            "tree leaves diverged from store accounts"

    @invariant()
    def last_emitted_tracks_selection(self):
        # _last_emitted is only written when a broadcast fires; the plugin's
        # single-selection address and the last broadcast must stay in sync.
        assert self.plugin._last_emitted == self.plugin.selected_address, \
            (f"_last_emitted {self.plugin._last_emitted!r} != selection "
             f"{self.plugin.selected_address!r}")


TestWalletsTree = WalletsTreeMachine.TestCase
TestWalletsTree.settings = settings(
    max_examples=80, stateful_step_count=25, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
