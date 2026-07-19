"""Stateful (hypothesis) fuzzing of the ENS tree across interleaved user actions
and simulated chain/async events.

The bugs in the ENS tab live in the ORDERING of events — a fast ownership read
landing before/after the Helios-proven one, an account switch mid-discovery, a
manager reassign whose rediscover races the verify pass. A RuleBasedStateMachine
mixes these in random sequences and checks structural invariants after every
step: the name→item index never strands a deleted Qt object (the "already
deleted" crash that borked the tree), and it stays in bijection with the tree.

No network: the worker-spawning entry points are stubbed and the async callbacks
(_on_names_ready / _on_verified) are driven directly, as the "events" the machine
interleaves with the user actions.
"""

import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from qeth.chains import DEFAULT_CHAINS
from qeth.plugins.ens.ens_app import EnsCache, EnsName, OwnershipCheck
from qeth.plugins.ens import _NAME_ROLE, EnsPlugin

ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
X = "0x" + "e0" * 20            # external — NOT one of the wallet accounts
ACCTS = [A, B]
TARGETS = [A, B, X]

# A fixed universe: 2LDs, subnames, and a DEEP subname (grandchild) — so a
# parent removal has indexed descendants to strand if handled wrong.
LD = ["a.eth", "b.eth"]
SUBS = ["x.a.eth", "y.a.eth", "x.x.a.eth", "p.b.eth"]
ALL_NAMES = LD + SUBS


class _Store:
    def __init__(self):
        self.custom_ens_names: set[str] = set()
        self.custom_text_keys: set[str] = set()
        self.accounts = [{"address": A, "source": "hot"},
                         {"address": B, "source": "hot"}]

    def add_custom_ens_name(self, n):
        self.custom_ens_names.add(n.strip().lower())

    def add_custom_text_key(self, k):
        self.custom_text_keys.add((k or "").strip())


class _Host:
    def __init__(self, address):
        self.selected_address = address

    def status_message(self, *a, **k):
        pass

    def current_chain(self):
        return ETH

    def chain_by_id(self, cid):
        return ETH if cid == 1 else None

    def start_worker(self, w):
        pass

    def open_ens_composer(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _no_helios(monkeypatch):
    # _load prewarms a Helios sidecar; never spawn a process in the fuzzer.
    import qeth.helios
    monkeypatch.setattr(qeth.helios, "prewarm", lambda *a, **k: None,
                        raising=False)


def _is_2ld(n: str) -> bool:
    return n in LD


class EnsTreeMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="qeth-ens-sm-")
        self.block = 100
        # Ground-truth on-chain state (mutated by set_manager / transfer):
        # x.a.eth is owned by B (cross-account for A) while its child x.x.a.eth
        # is A's — so the deep grandchild can surface before its intermediate
        # parent, exercising the ancestor-synthesis / orphan path.
        self.manager = {"a.eth": A, "b.eth": B, "x.a.eth": B,
                        "y.a.eth": B, "x.x.a.eth": A, "p.b.eth": X}
        self.owner = {"a.eth": A, "b.eth": B}        # registrant (2LDs only)
        self.plugin = EnsPlugin(_Store())
        self.plugin._cache = EnsCache(Path(self.tmp))   # isolated on-disk cache
        self.host = _Host(A)
        self.plugin.attach(self.host)
        self.plugin.widget()                          # force the panel to build
        # No real workers/network — we drive the callbacks as explicit events.
        self.plugin._on_refresh = lambda **k: None
        self.plugin._verify = lambda *a, **k: None
        self.plugin._start = lambda w: None
        self.current = A
        self.plugin.on_account_changed(A)

    @property
    def panel(self):
        return self.plugin._panel

    def teardown(self):
        # Destroy this example's plugin widget for real. deleteLater only posts a
        # DeferredDelete event, which plain processEvents() doesn't dispatch — so
        # without the explicit flush the ~150 example widgets pile up in the
        # shared QApplication and hang a later Qt test in the full suite.
        w = self.plugin.widget()
        if w is not None:
            w.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers ----------------------------------------------------------
    def _next_block(self) -> int:
        self.block += 1
        return self.block

    def _discovery_for(self, acct: str) -> list:
        return [EnsName(n, owner=acct, source="owned")
                for n in ALL_NAMES
                if acct in (self.manager.get(n), self.owner.get(n))]

    def _states_for(self, names) -> dict:
        out = {}
        for n in names:
            out[n.lower()] = OwnershipCheck(
                controller=self.manager.get(n),
                registrant=self.owner.get(n) if _is_2ld(n) else None,
                owner_known=True, in_registry=True)
        return out

    def _displayed(self) -> list:
        return list(self.plugin._names_by_l)

    # --- rules: user actions + chain/async events -------------------------
    @rule(acct=st.sampled_from(ACCTS))
    def switch_account(self, acct):
        self.host.selected_address = acct
        self.plugin.on_account_changed(acct)
        self.current = acct

    @rule()
    def discovery_lands(self):
        self.plugin._on_names_ready(self.current, self._discovery_for(self.current))

    @rule(verified=st.booleans(), lag=st.booleans())
    def read_lands(self, verified, lag):
        names = self._displayed()
        if not names:
            return
        block = self._next_block()
        if lag:                                       # simulate Helios head lag
            block -= 5
        self.plugin._on_verified(self.current, self._states_for(names),
                                 verified, block)

    @rule(data=st.data(), to_addr=st.sampled_from(TARGETS))
    def set_manager(self, data, to_addr):
        p = self.panel
        names = [n for n in self._displayed()
                 if p is not None and p._can_set_manager(n.lower())]
        if not names:
            return
        name = data.draw(st.sampled_from(sorted(names)))
        self.manager[name] = to_addr                   # the tx changes the chain
        self.plugin._apply_set_manager(name, {"manager": to_addr},
                                       self._next_block())

    @rule(data=st.data(), to_addr=st.sampled_from(TARGETS))
    def transfer(self, data, to_addr):
        names = [n for n in self._displayed()
                 if _is_2ld(n) and self.owner.get(n) == self.current]
        if not names:
            return
        name = data.draw(st.sampled_from(sorted(names)))
        self.owner[name] = to_addr                     # a transfer moves both roles
        self.manager[name] = to_addr
        if self.panel is not None:
            self.panel.apply_role(name, controller=to_addr, registrant=to_addr,
                                  block=self._next_block())

    @rule(data=st.data())
    def toggle_fold(self, data):
        names = self._displayed()
        if not names or self.panel is None:
            return
        name = data.draw(st.sampled_from(sorted(names)))
        it = self.panel._items_by_name.get(name.lower())
        if it is not None and isValid(it):
            it.setExpanded(not it.isExpanded())

    # --- invariants -------------------------------------------------------
    def _tree_name_rows(self) -> dict:
        out = {}
        if self.panel is None:
            return out
        t = self.panel.tree
        stack = [t.topLevelItem(i) for i in range(t.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it is None:
                continue
            n = it.data(0, _NAME_ROLE)
            if isinstance(n, EnsName):
                out[n.name.lower()] = it
            for i in range(it.childCount()):
                stack.append(it.child(i))
        return out

    @invariant()
    def no_stranded_deleted_items(self):
        if self.panel is None:
            return
        for nl, it in list(self.panel._items_by_name.items()):
            assert isValid(it), f"stranded deleted index item: {nl}"

    @invariant()
    def index_matches_tree(self):
        if self.panel is None:
            return
        idx = set(self.panel._items_by_name)
        tree = set(self._tree_name_rows())
        assert idx == tree, f"index {idx} != tree name-rows {tree}"

    @invariant()
    def no_detached_subnames(self):
        # A name with ANY present ancestor must nest (not sit at the top level) —
        # the render synthesizes missing intermediate ancestors to guarantee it,
        # so a deep grandchild never floats detached from its owned 2LD.
        if self.panel is None:
            return
        rows = self._tree_name_rows()
        t = self.panel.tree
        top = set()
        for i in range(t.topLevelItemCount()):
            n = t.topLevelItem(i).data(0, _NAME_ROLE)
            if isinstance(n, EnsName):
                top.add(n.name.lower())
        for nl in rows:
            anc = EnsName(nl).parent
            while anc:
                if anc.lower() in rows:
                    assert nl not in top, \
                        f"{nl} orphaned at top level despite present ancestor {anc}"
                    break
                anc = EnsName(anc).parent


TestEnsTree = EnsTreeMachine.TestCase
# 80×25 explores a broad spread of event orderings. Each example builds and
# destroys a real ENS-plugin widget tree, so this used to have to stay tiny: the
# Qt churn tipped a pre-existing flaky TransactionsPlugin teardown into a crash
# under the full suite. That leak is fixed at its source now (the plugin's poll
# timers are shut down in teardown — see conftest._dispose_transactions_plugins),
# so the fuzzer is free to be thorough; the ceiling here is just suite wall-clock.
TestEnsTree.settings = settings(
    max_examples=80, stateful_step_count=25, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
