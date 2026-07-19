"""Stateful (hypothesis) fuzzing of the Transactions tab's history/pending
machinery across interleaved lifecycle events.

The ordering hazards here: a broadcast tx going pending then confirming or
dropping, a Blockscout page landing that carries a same-hash confirmed row over
a local pending one, a confirmation delivered repeatedly (ws re-probes every
head), an external-nonce bump lifting the exhausted short-circuit, scroll paging.
A RuleBasedStateMachine mixes send / confirm / re-confirm / drop / page-fetch /
external-nonce / scroll / switch-account and checks after every step that the
per-key cache stays well-formed:

  * no duplicate hashes (every write funnels through merge_txs, which dedups);
  * rows stay nonce-descending;
  * a tx that has EVER confirmed is never pending/dropped again (both drop and
    confirm bail on a non-pending row — a confirmed tx must not regress);
  * a dropped tx stays in the cache flipped to terminal dropped (not removed);
  * the table rows are a prefix of the current view's cache by hash.

No network: the plugin's workers are never started; the confirmation / drop /
page handlers are driven directly as the events the machine interleaves. The
disk cache is redirected to a per-example tmp dir (this runs outside tmp_qeth);
the plugin is built with store=None so it never touches ~/.qeth at all.
"""

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

import qeth.transactions_cache as txcache_mod
from qeth.plugins.transactions import TransactionsPlugin
from qeth.transactions import Transaction

CID = 1
ETH = SimpleNamespace(chain_id=CID, name="Ethereum", symbol="ETH", eip1559=True)
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
ACCTS = [A, B]
TO = "0x" + "cc" * 20
PAGE_SIZE = 50


class _Source:
    """A transactions source that never networks — the machine drives page
    results directly, so this only needs to exist for the constructor."""

    def supports(self, _chain):
        return True

    def list_transactions(self, _chain, _addr, page=1, limit=50,
                          before_block=None):
        return []


class TxTreeMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="qeth-tx-sm-")
        # Redirect the on-disk tx cache to tmp BEFORE building the plugin — this
        # runs outside tmp_qeth. store=None means no config/keystore touch.
        self._saved_dir = txcache_mod.CACHE_DIR
        txcache_mod.CACHE_DIR = Path(self.tmp)

        self.block = 100
        self.hseq = 0
        # Ground truth per account.
        self.nnext = {A: 0, B: 0}             # next nonce to assign on send
        self.pending: dict[str, dict[str, int]] = {A: {}, B: {}}   # hash→nonce
        self.confirmed: dict[str, dict[str, Transaction]] = {A: {}, B: {}}
        self.confirmed_ever: set[str] = set()
        self.dropped_ever: set[str] = set()

        self.plugin = TransactionsPlugin(source=_Source())  # type: ignore[arg-type]
        # Deterministic drops: one reading flips (the real guard needs 3 spaced
        # ≥10s apart, which a fast test can't produce).
        self.plugin.DROP_CONFIRM_READINGS = 1
        self.widget = self.plugin.widget()          # builds the panel
        panel = self.plugin._panel
        assert panel is not None
        self.panel = panel
        self.host = SimpleNamespace(
            selected_address=A, current_chain=lambda: ETH,
            start_worker=lambda w: None, token_info=lambda cid, addr: None,
            status_message=lambda *a, **k: None,
            chain_by_id=lambda cid: ETH if cid == CID else None)
        self.plugin.host = self.host
        self.current = A

    @initialize(n_confirmed=st.integers(0, 4), seed_pending=st.booleans())
    def seed(self, n_confirmed, seed_pending):
        """Non-empty COLD START: a 'previous session' disk cache holding some
        confirmed history plus, optionally, a stuck pending tx the chain may
        have since confirmed or dropped. So the reconciliation rules (confirm /
        drop / page-fetch) start against real prior state, not a blank slate."""
        acct = A
        rows = []
        for nonce in range(n_confirmed):
            blk = self._next_block()
            h = self._hash()
            tx = Transaction(
                chain_id=CID, hash=h, block_number=blk, timestamp=blk,
                nonce=nonce, from_addr=acct, to_addr=TO, value_wei=1,
                gas_used=21000, gas_price_wei=1, method_id="", input_data="0x",
                success=True)
            self.confirmed[acct][h] = tx
            self.confirmed_ever.add(h)
            rows.append(tx)
        self.nnext[acct] = n_confirmed
        if seed_pending:
            nonce = self.nnext[acct]
            self.nnext[acct] += 1
            h = self._hash()
            self.pending[acct][h] = nonce
            rows.append(Transaction(
                chain_id=CID, hash=h, block_number=0, timestamp=self.block,
                nonce=nonce, from_addr=acct, to_addr=TO, value_wei=1,
                gas_used=0, gas_price_wei=1, method_id="", input_data="0x",
                success=True, pending=True))
        rows.sort(key=lambda t: t.nonce, reverse=True)
        key = (CID, acct.lower())
        self.plugin._cache[key] = rows
        self.plugin._disk_cache.save(CID, acct.lower(), rows)
        self.plugin.on_account_changed(acct)

    def teardown(self):
        self.widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        txcache_mod.CACHE_DIR = self._saved_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers ----------------------------------------------------------
    def _next_block(self) -> int:
        self.block += 1
        return self.block

    def _hash(self) -> str:
        self.hseq += 1
        return "0x" + format(self.hseq, "064x")

    def _key(self):
        return (CID, self.current.lower())

    def _cache_rows(self):
        return self.plugin._cache.get(self._key(), [])

    def _receipt(self, block: int) -> dict:
        return {"status": "0x1", "blockNumber": hex(block),
                "gasUsed": "0x5208", "effectiveGasPrice": "0x1"}

    def _row_tx(self, row: int):
        t = self.panel.table
        for c in range(t.columnCount()):
            it = t.item(row, c)
            if it is not None:
                d = it.data(Qt.ItemDataRole.UserRole)
                if isinstance(d, Transaction):
                    return d
        return None

    # --- rules: user + chain events ---------------------------------------
    @rule()
    def send(self):
        acct = self.current
        nonce = self.nnext[acct]
        self.nnext[acct] += 1
        h = self._hash()
        self.pending[acct][h] = nonce
        req = SimpleNamespace(from_addr=acct, to_addr=TO, value_wei=1,
                              data="0x", nonce=nonce, gas_price=1,
                              max_fee_per_gas=1)
        self.plugin.add_pending(h, req, ETH,  # type: ignore[arg-type]
                                raw_signed="0x00")

    @rule(data=st.data())
    def confirm(self, data):
        pend = sorted(self.pending[self.current])
        if not pend:
            return
        h = data.draw(st.sampled_from(pend))
        nonce = self.pending[self.current].pop(h)
        blk = self._next_block()
        self.confirmed[self.current][h] = Transaction(
            chain_id=CID, hash=h, block_number=blk, timestamp=blk, nonce=nonce,
            from_addr=self.current, to_addr=TO, value_wei=1, gas_used=21000,
            gas_price_wei=1, method_id="", input_data="0x", success=True)
        self.confirmed_ever.add(h)
        self.plugin._on_receipt_confirmed(ETH, h, self._receipt(blk))

    @rule(data=st.data())
    def reconfirm(self, data):
        # Re-deliver a confirmation (the ws watcher re-probes every head) — must
        # be idempotent and never regress the already-confirmed row.
        conf = sorted(self.confirmed[self.current])
        if not conf:
            return
        h = data.draw(st.sampled_from(conf))
        self.plugin._on_receipt_confirmed(ETH, h, self._receipt(self.block))

    @rule(data=st.data())
    def drop(self, data):
        pend = sorted(self.pending[self.current])
        if not pend:
            return
        h = data.draw(st.sampled_from(pend))
        self.pending[self.current].pop(h)
        self.dropped_ever.add(h)
        self.plugin._on_tx_dropped(ETH, h)

    @rule()
    def page_fetched(self):
        acct = self.current
        page = sorted(self.confirmed[acct].values(),
                      key=lambda t: t.nonce, reverse=True)
        raw_oldest = min((t.block_number for t in page), default=None)
        has_more = len(page) >= PAGE_SIZE
        self.plugin._on_page_fetched(CID, acct.lower(), 1, list(page),
                                     has_more, raw_oldest=raw_oldest)

    @rule()
    def external_nonce(self):
        # On-chain mined-nonce count = number of txs we've ever sent.
        self.plugin._on_external_nonce(self._key(), self.nnext[self.current])

    @rule()
    def scroll(self):
        self.plugin._on_scroll_bottom()

    @rule(acct=st.sampled_from(ACCTS))
    def switch_account(self, acct):
        self.host.selected_address = acct
        self.plugin.on_account_changed(acct)
        self.current = acct

    @rule()
    def activate(self):
        self.plugin.on_activated()

    # --- invariants -------------------------------------------------------
    @invariant()
    def no_duplicate_hashes(self):
        for key, txs in self.plugin._cache.items():
            hashes = [t.hash for t in txs]
            assert len(hashes) == len(set(hashes)), f"dup hash in {key}"

    @invariant()
    def rows_nonce_descending(self):
        for key, txs in self.plugin._cache.items():
            nonces = [t.nonce for t in txs]
            assert nonces == sorted(nonces, reverse=True), \
                f"out-of-order nonces in {key}: {nonces}"

    @invariant()
    def confirmed_never_regresses(self):
        for key, txs in self.plugin._cache.items():
            for t in txs:
                if t.hash in self.confirmed_ever:
                    assert not t.pending, f"confirmed {t.hash} back to pending"
                    assert not t.dropped, f"confirmed {t.hash} marked dropped"

    @invariant()
    def dropped_tx_stays_terminal(self):
        present = {t.hash: t for txs in self.plugin._cache.values() for t in txs}
        for h in self.dropped_ever:
            t = present.get(h)
            if t is not None:                       # never removed, only flipped
                assert not t.pending and t.dropped, \
                    f"dropped {h} not in terminal state"

    @invariant()
    def displayed_rows_are_a_cache_prefix(self):
        if self.plugin._rendered_for != self._key():
            return
        cache_hashes = [t.hash for t in self._cache_rows()]
        rows = self.panel.table.rowCount()
        shown = [self._row_tx(r) for r in range(rows)]
        shown_hashes = [t.hash for t in shown if t is not None]
        assert shown_hashes == cache_hashes[:len(shown_hashes)], \
            "displayed rows are not a prefix of the cache"


TestTxTree = TxTreeMachine.TestCase
TestTxTree.settings = settings(
    max_examples=80, stateful_step_count=25, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
