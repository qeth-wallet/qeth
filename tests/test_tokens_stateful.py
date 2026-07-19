"""Stateful (hypothesis) fuzzing of the Tokens tab across interleaved async
balance/price reads and user actions.

The bugs in the Tokens tab live in the ORDERING of events — a stale discovery
read landing after a fresh targeted one, a hide/pin/custom toggle racing a
merge, an account switch mid-discovery, an authoritative zero arriving before
or after a claim. A RuleBasedStateMachine mixes user actions (hide / pin / add
custom / show-all / switch account) with simulated chain events (discovery
merge, targeted balanceOf read, own-token discovery, native balance) at fresh
or lagging blocks, and after every step checks structural invariants:

  * a user-hidden token is NEVER rendered (hidden overrides everything);
  * the store's hidden set stays disjoint from shown/custom/discovered (each
    mutation mutually prunes the others);
  * every rendered non-native row is actually held (in the wallet cache with a
    positive balance) and not hidden — no phantom rows, no zero/hidden leaks.

No network: the plugin's workers are never started (their results are injected
directly as the "events" the machine interleaves), and the socket guard in
conftest would refuse any stray connect anyway. The Store's on-disk config is
redirected to a per-example tmp dir here because this machine runs outside the
``tmp_qeth`` fixture — never point it at the real ~/.qeth/config.json.
"""

import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

import qeth.store as store_mod
from qeth.icons import IconCache
from qeth.plugins.tokens import TokenListPanel, TokensPlugin
from qeth.pricing import Price
from qeth.store import Store
from qeth.wallet_cache import WalletCache

CID = 1
ETH = SimpleNamespace(chain_id=CID, name="Ethereum", symbol="ETH")
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
ACCTS = [A, B]

# Token universe, each a distinct gate case:
KP = "0x" + "11" * 20     # known + priced      → shows when value ≥ dust
KU = "0x" + "22" * 20     # known + unpriced     → shows within grace window
UP = "0x" + "33" * 20     # unknown + priced     → shows when value ≥ dust
UU = "0x" + "44" * 20     # unknown + unpriced   → spam, dropped
XX = "0x" + "55" * 20     # promotable via custom/discovered
TOKENS = [KP, KU, UP, UU, XX]
KNOWN = {KP.lower(), KU.lower()}
PRICED = {KP.lower(), UP.lower()}
UNIT = 10 ** 18                       # 1.0 of an 18-decimal token
PRICE = Price(Decimal("2000"), CID, "x")   # $2000 → 1.0 token = $2000 ≫ dust


class TokensTreeMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="qeth-tok-sm-")
        # Redirect the Store's on-disk config to the tmp dir BEFORE constructing
        # it — this machine runs outside tmp_qeth, so the module globals still
        # point at the real ~/.qeth. Restored in teardown.
        self._saved_cfg = (store_mod.CONFIG_DIR, store_mod.CONFIG_FILE)
        store_mod.CONFIG_DIR = Path(self.tmp)
        store_mod.CONFIG_FILE = Path(self.tmp) / "config.json"

        self.block = 100
        # Ground-truth on-chain state, per account (mutated by receive/send).
        self.bal: dict[str, dict[str, int]] = {A: {}, B: {}}
        self.native = {A: UNIT, B: UNIT}

        self.store = Store.load()
        self.plugin = TokensPlugin(self.store)
        # Curated-list knowledge is what the gate consults for unpriced tokens;
        # stub it deterministically instead of loading real lists.
        self.plugin._token_lists._loaded = True
        self.plugin._token_lists.is_known = (          # type: ignore[method-assign]
            lambda chain_id, address: address.lower() in KNOWN)
        # Metadata for every token so the merge upserts them with a symbol.
        self.plugin._token_metadata.put_many(
            CID, {t: {"symbol": "T" + t[-2:], "name": "Tok " + t[-2:],
                      "decimals": 18} for t in TOKENS})
        self.plugin._wallet_cache = WalletCache(cache_dir=Path(self.tmp))
        self._icons = IconCache()
        # No logo fetches → the socket guard never trips on an icon download.
        self._icons.request = lambda *a, **k: None   # type: ignore[method-assign]
        self.panel = TokenListPanel(self._icons, self.store)
        self.plugin._panel = self.panel
        self.host = SimpleNamespace(
            selected_address=A, current_chain=lambda: ETH,
            start_worker=lambda w: None, token_info=lambda cid, addr: None,
            status_message=lambda *a, **k: None,
            chain_by_id=lambda cid: ETH if cid == CID else None)
        self.plugin.host = self.host
        self.current = A
        self.plugin.on_account_changed(A)

    def teardown(self):
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        store_mod.CONFIG_DIR, store_mod.CONFIG_FILE = self._saved_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers ----------------------------------------------------------
    def _next_block(self) -> int:
        self.block += 1
        return self.block

    def _prices(self) -> dict:
        p = {"": PRICE}
        for t in PRICED:
            p[t] = PRICE
        return p

    def _rendered(self) -> list[str]:
        """Contracts (lowercased) of the non-native rows actually VISIBLE on
        screen. The display gate (set_prices) hides filtered rows via
        setRowHidden rather than removing them, so a bare row scan would count
        hidden rows too — skip them."""
        t = self.panel.table
        out = []
        for r in range(t.rowCount()):
            if t.isRowHidden(r):
                continue
            it = t.item(r, 0)
            if it is None:
                continue
            d = it.data(Qt.ItemDataRole.UserRole)
            if (isinstance(d, tuple) and len(d) == 2
                    and d[1] != TokenListPanel.NATIVE_CONTRACT):
                out.append(str(d[1]).lower())
        return out

    def _held(self) -> dict[str, int]:
        cached = self.plugin._wallet_cache.load(CID, self.current)
        if cached is None:
            return {}
        return {t.contract.lower(): t.balance_raw for t in cached.tokens}

    # --- rules: chain/async events ----------------------------------------
    @rule(tok=st.sampled_from(TOKENS))
    def receive(self, tok):
        # Ground truth grows; only a later READ surfaces it in the UI.
        self._next_block()
        self.bal[self.current][tok.lower()] = UNIT

    @rule(tok=st.sampled_from(TOKENS))
    def send(self, tok):
        self._next_block()
        self.bal[self.current][tok.lower()] = 0

    @rule(stale=st.booleans())
    def discovery_lands(self, stale):
        acct = self.current
        block = (self.block - 5) if stale else self.block
        balances = dict(self.bal[acct])
        pv = {
            "chain": ETH, "address": acct, "view_key": (CID, acct.lower()),
            "native_wei": self.native[acct], "block": block,
            "read_failed": False, "balances_raw": balances,
            "blocks": {c: block for c in balances},
            "metadata": {c: ("T" + c[-2:], "Tok " + c[-2:], 18)
                         for c in balances},
        }
        self.plugin._on_combined_ready(pv, CID, self._prices())

    @rule(stale=st.booleans())
    def targeted_read(self, stale):
        acct = self.current
        block = (self.block - 5) if stale else self.block
        balances = {c: raw for c, raw in self.bal[acct].items()}
        if not balances:
            return
        self.plugin._apply_targeted_balances(
            ETH, acct, self.native[acct], balances, block)

    @rule(tok=st.sampled_from(TOKENS))
    def own_discovery(self, tok):
        self.plugin._on_own_tokens_discovered(CID, [tok])

    @rule()
    def native_lands(self):
        self.plugin.on_native_balance(ETH, self.current,
                                      self.native[self.current],
                                      self._next_block())

    # --- rules: user actions ----------------------------------------------
    @rule(acct=st.sampled_from(ACCTS))
    def switch_account(self, acct):
        self.host.selected_address = acct
        self.plugin.on_account_changed(acct)
        self.current = acct

    @rule(tok=st.sampled_from(TOKENS))
    def hide(self, tok):
        self.plugin._on_hide_token(CID, tok)

    @rule(tok=st.sampled_from(TOKENS))
    def unhide(self, tok):
        self.store.unhide_token(CID, tok)
        self.plugin._invalidate_view_and_refresh()

    @rule(tok=st.sampled_from(TOKENS))
    def pin(self, tok):
        self.plugin._on_pin_token(CID, tok)

    @rule(tok=st.sampled_from(TOKENS))
    def add_custom(self, tok):
        self.store.add_custom_token(CID, tok)
        self.plugin._invalidate_view_and_refresh()

    @rule()
    def toggle_show_all(self):
        self.plugin._on_show_all_toggled(not self.plugin._show_all)

    @rule()
    def activate(self):
        self.plugin.on_activated()

    # --- invariants -------------------------------------------------------
    @invariant()
    def hidden_never_rendered(self):
        # "Show all" is the deliberate reveal-hidden/spam toggle (the gate's
        # first rule includes everything), so the hidden-hiding guarantee only
        # holds in the normal filtered view.
        if self.plugin._show_all:
            return
        for addr in self._rendered():
            assert not self.store.is_hidden(CID, addr), \
                f"hidden token {addr} is on screen"

    @invariant()
    def hidden_disjoint_from_overrides(self):
        h = set(self.store.hidden_tokens)
        for other in (self.store.shown_tokens, self.store.custom_tokens,
                      self.store.discovered_tokens):
            overlap = h & set(other)
            assert not overlap, f"hidden overlaps an override set: {overlap}"

    @invariant()
    def rendered_rows_are_held_and_visible(self):
        # Under show-all a zero balance may legitimately render; otherwise every
        # non-native row must be a held (>0), non-hidden token actually in cache.
        if self.plugin._show_all:
            return
        held = self._held()
        for addr in self._rendered():
            assert not self.store.is_hidden(CID, addr), f"{addr} hidden yet shown"
            assert addr in held, f"{addr} rendered but not in wallet cache"
            assert held[addr] > 0, f"{addr} rendered with zero balance"


TestTokensTree = TokensTreeMachine.TestCase
TestTokensTree.settings = settings(
    max_examples=80, stateful_step_count=25, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
