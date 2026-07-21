"""Stateful (hypothesis) fuzzing of the Approvals plugin across interleaved
scans, account/chain switches, worker completions, and stops.

The bug this guards: the host deleteLater()s a finished ScanWorker, so a scan
that completes (or is stopped) leaves the plugin's self._scan a stale wrapper;
a subsequent account switch that runs _invalidate → _stop_scan on it raised
"Internal C++ object (ScanWorker) already deleted" and aborted the switch, so
the tab never renewed to the new account. The machine mixes scan/finish/switch
in random orders and checks after every step that self._scan is valid-or-None,
the tree items aren't stale, and a switch always renews the loaded view.

No network: workers are constructed but never start()ed (the fake host doesn't),
so run() never executes; their signals are emitted by hand as the "events".
"""

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from qeth import QULONGLONG
from qeth.chains import DEFAULT_CHAINS
from qeth.plugins.approvals import (
    ApprovalsPlugin, ReconcileWorker, _format_allowance,
)
from qeth.plugins.approvals.cache import ApprovalsCache
from qeth.plugins.approvals.discovery import _APPROVAL_TOPIC0, ApprovalRow
from qeth.token_metadata import TokenMetadataCache
from qeth.transactions_cache import TransactionCache

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
ACCTS = [A, B]
CHAINS = {c.chain_id: c for c in DEFAULT_CHAINS if c.chain_id in (1, 10)}
CIDS = sorted(CHAINS)
SPENDER = "0x" + "ee" * 20


class _FakeIcons(QObject):
    icon_ready = Signal(QULONGLONG, str)

    def get(self, cid, contract):
        return None

    def request(self, cid, contract, url):
        pass


class _FakeTx(QObject):
    tx_confirmed = Signal(object, str, object)          # chain, hash, receipt


class _Host:
    def __init__(self, address, chain):
        self.selected_address = address
        self._chain = chain
        self._icons = _FakeIcons()
        self._tx = _FakeTx()
        self.started: list = []
        self.requests: list = []

    def plugin(self, plugin_id):
        return self._tx if plugin_id == "transactions" else None

    def current_chain(self):
        return self._chain

    def start_worker(self, worker):
        worker.finished.connect(worker.deleteLater)     # mirror MainWindow
        self.started.append(worker)

    def icon_cache(self):
        return self._icons

    def token_info(self, cid, address):
        return None

    def status_message(self, *a, **k):
        pass

    def request_transaction(self, req, chain, label, on_broadcast=None,
                            on_confirmed=None, on_cancel=None):
        self.requests.append(SimpleNamespace(
            on_broadcast=on_broadcast, on_confirmed=on_confirmed,
            on_cancel=on_cancel))


class ApprovalsMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="qeth-appr-sm-")
        self.plugin = ApprovalsPlugin(SimpleNamespace(etherscan_api_key=None))
        self.plugin._disk_cache = TransactionCache(Path(self.tmp) / "tx")
        self.plugin._metadata = TokenMetadataCache(Path(self.tmp) / "meta")
        self.plugin._cache = ApprovalsCache(Path(self.tmp) / "appr")
        self.host = _Host(A, CHAINS[1])
        self.plugin.attach(self.host)
        panel = self.plugin.widget()
        panel.isVisible = lambda: True                  # visible → switches kick a scan
        self.plugin.on_account_changed(A)

    @property
    def panel(self):
        return self.plugin._panel

    def teardown(self):
        self.plugin.shutdown()
        for w in list(self.host.started):
            if isValid(w):
                w.deleteLater()
        w = self.plugin.widget()
        if w is not None:
            w.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _view(self):
        return (self.host.current_chain().chain_id,
                self.host.selected_address.lower())

    # --- rules: user actions + async events -------------------------------
    @rule(acct=st.sampled_from(ACCTS))
    def switch_account(self, acct):
        self.host.selected_address = acct
        self.plugin.on_account_changed(acct)            # _invalidate + re-kick
        assert self.plugin._loaded_for == self._view()  # renewed, not stuck

    @rule(cid=st.sampled_from(CIDS))
    def switch_chain(self, cid):
        self.host._chain = CHAINS[cid]
        self.plugin.on_chain_changed()
        assert self.plugin._loaded_for == self._view()

    @rule()
    def activate(self):
        self.plugin.on_activated()

    @rule(value=st.integers(min_value=0, max_value=1000), kick=st.booleans())
    def cap_change_confirms(self, value, kick):
        # A modify/revoke of a shown cap: its on_confirmed schedules a reconcile,
        # the read worker is spawned, and the fresh value lands — optionally
        # AFTER a scan/visit bumped _epoch (kick=True). The displayed cap MUST
        # reflect the fresh value regardless. (The reconcile used to be gated on
        # the epoch, so a bump between scheduling and completing dropped it — the
        # exact "modify confirmed but the list didn't change" bug.)
        rows = self.panel.all_rows() if self.panel is not None else []
        if not rows:
            return
        r = rows[0]
        pair = (r.token.lower(), r.spender.lower())
        cid, addr = self._view()
        self.plugin._schedule_reconcile(pair)           # the modify's on_confirmed
        self.plugin._run_reconcile()                    # spawn + capture-epoch
        worker = next((w for w in reversed(self.host.started)
                       if isValid(w) and isinstance(w, ReconcileWorker)), None)
        if worker is None:
            return
        if kick:
            self.plugin._epoch += 1                     # a scan / re-visit in between
        worker.reconciled.emit(cid, addr, {pair: value})   # the fresh on-chain read
        leaf = self.panel._leaf_for(*pair)
        if value > 0:
            assert leaf is not None and \
                leaf.text(1) == _format_allowance(value, r.decimals), \
                "confirmed cap change didn't update the displayed allowance"
        else:
            assert leaf is None, "revoked cap still shown after confirm"

    @rule(same_acct=st.booleans())
    def approve_confirms(self, same_acct):
        # A confirmed approve tx (via ANY path) must refresh the list — whether
        # the approvals tab is active or the sign flow already switched away.
        cid, addr = self._view()
        owner = addr if same_acct else ("0x" + "cc" * 20)   # our account or another's
        receipt = {"logs": [{
            "address": "0x" + "cc" * 20,
            "topics": [_APPROVAL_TOPIC0,
                       "0x" + "00" * 12 + owner[2:],         # owner (padded)
                       "0x" + "00" * 12 + SPENDER[2:]],      # spender (padded)
        }]}
        before_epoch = self.plugin._epoch
        before_pending = set(self.plugin._reconcile_pending)
        self.host._tx.tx_confirmed.emit(self.host.current_chain(), "0xapprove", receipt)
        if same_acct:
            # our own cap changed → a refresh fired (a full re-scan for a new
            # pair, or a targeted reconcile for one already shown)
            assert (self.plugin._epoch > before_epoch
                    or self.plugin._reconcile_pending != before_pending)
        else:
            assert self.plugin._epoch == before_epoch    # another account → nothing
            assert self.plugin._reconcile_pending == before_pending

    @rule(n=st.integers(min_value=0, max_value=3))
    def scan_emits_rows(self, n):
        w = self.plugin._scan
        if w is None or not isValid(w):
            return
        cid, addr = self._view()
        rows = [ApprovalRow(token="0x" + f"{i + 1:02x}" * 20, spender=SPENDER,
                            allowance=i + 1, symbol=f"T{i}", decimals=0)
                for i in range(n)]
        w.rows_ready.emit(cid, addr, rows)

    @rule(seen=st.integers(min_value=0, max_value=50))
    def scan_progress(self, seen):
        w = self.plugin._scan
        if w is None or not isValid(w):
            return
        cid, addr = self._view()
        w.progress.emit(cid, addr, seen, 50)

    @rule(complete=st.booleans())
    def scan_finishes(self, complete):
        w = self.plugin._scan
        if w is None or not isValid(w):
            return
        cid, addr = self._view()
        w.scan_done.emit(cid, addr, complete, 0)        # cid, addr, complete, logs_head
        w.finished.emit()                               # host deleteLater()s it
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # C++ gone

    @rule(zero=st.booleans())
    def scan_finishes_with_maybe_zeroed(self, zero):
        # A completed FRESH scan prunes ONLY pairs it read as DEFINITIVELY zero:
        # a shown cap it did NOT zero must survive (the transient-read fix); a
        # zeroed one must go. Value oracle over prune_zeroed.
        #
        # Force a fresh scan and drive ITS emissions, so the completion isn't
        # ignored as stale — a prior epoch bump (e.g. cap_change_confirms with
        # kick=True) makes an in-flight scan's scan_done a correct no-op, and
        # that staleness path is already covered by cap_change_confirms.
        if self.panel is None:
            return
        self.plugin._kick(force=True)
        w = self.plugin._scan
        if w is None or not isValid(w):
            return
        cid, addr = self._view()
        keep = ("0x" + "51" * 20, SPENDER.lower())
        gone = ("0x" + "52" * 20, SPENDER.lower())
        w.rows_ready.emit(cid, addr, [
            ApprovalRow(token=keep[0], spender=SPENDER, allowance=1,
                        symbol="K", decimals=0),
            ApprovalRow(token=gone[0], spender=SPENDER, allowance=1,
                        symbol="G", decimals=0)])
        if zero:
            w.pairs_zeroed.emit(cid, addr, {gone})      # gone read as zero
        w.scan_done.emit(cid, addr, True, 0)            # complete → prune runs
        w.finished.emit()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if self.panel is None:
            return
        after = {(r.token.lower(), r.spender.lower()) for r in self.panel.all_rows()}
        assert keep in after                            # never-zeroed cap survives
        if zero:
            assert gone not in after                    # zeroed → pruned
        else:
            assert gone in after                        # not zeroed → survives

    @rule()
    def stop(self):
        self.plugin._stop_scan()

    # --- invariants -------------------------------------------------------
    @invariant()
    def scan_ref_valid_or_none(self):
        s = self.plugin._scan
        assert s is None or isValid(s)

    @invariant()
    def tree_items_not_stale(self):
        p = self.panel
        if p is None:
            return
        t = p.tree
        for i in range(t.topLevelItemCount()):
            node = t.topLevelItem(i)
            assert isValid(node)
            for j in range(node.childCount()):
                assert isValid(node.child(j))


TestApprovals = ApprovalsMachine.TestCase
TestApprovals.settings = settings(
    max_examples=60, stateful_step_count=25, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
