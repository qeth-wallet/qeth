"""Unit-style tests for the three right/left-pane widgets.

These don't need a MainWindow — they construct the widget directly
under the offscreen platform and exercise the public ``show_*`` /
``clear`` rendering methods. Useful both for catching renderer bugs
and for pinning the contract each panel will expose once they
become Plugin instances.
"""

from decimal import Decimal

import pytest
from PySide6.QtCore import Qt

from qeth.chains import DEFAULT_CHAINS
from qeth.icons import IconCache
from qeth.store import Store
from qeth.token_discovery import TokenBalance
from qeth.plugins.tokens import TokenListPanel
from qeth.transactions import Transaction
from qeth.plugins.transactions import TransactionListPanel
from qeth.plugins.wallets import AccountInfoDialog


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"


# --- chain icon URL derivation ---------------------------------------------

class TestChainIconUrls:
    """Curve names chain logos by the chain name lowercased
    (hyperliquid.png, fraxtal.png), so a dapp-added chain not in the
    hardcoded id→slug map should still resolve a Curve URL from its
    name — that's how new chains get icons without a code change."""

    def test_name_derives_curve_url_for_unmapped_chain(self):
        from qeth.icons import _chain_icon_urls
        # Hyperliquid (999) isn't in _CURVE_CHAIN_SLUGS.
        urls = _chain_icon_urls(999, "Hyperliquid")
        assert any(u.endswith("/chains/hyperliquid.png") for u in urls)

    def test_multiword_name_tries_compact_and_hyphenated(self):
        from qeth.icons import _chain_icon_urls
        urls = _chain_icon_urls(196, "X Layer")
        assert any(u.endswith("/chains/xlayer.png") for u in urls)
        assert any(u.endswith("/chains/x-layer.png") for u in urls)

    def test_mapped_alias_still_wins_first(self):
        from qeth.icons import _chain_icon_urls
        # Gnosis' Curve file is "xdai", which the display name can't
        # yield — the id→slug map must still be tried, and first.
        urls = _chain_icon_urls(100, "Gnosis")
        curve = [u for u in urls if "curve-assets" in u]
        assert curve and curve[0].endswith("/chains/xdai.png")

    def test_no_name_no_map_yields_nothing(self):
        from qeth.icons import _chain_icon_urls
        assert _chain_icon_urls(123456789) == []


# --- notification_icon -----------------------------------------------------

class TestNotificationIcon:
    def test_returns_icon_with_base_pixmap(self, qtbot):
        from PySide6.QtGui import QIcon, QPixmap
        from qeth.icons import notification_icon
        base = QPixmap(32, 32)
        base.fill(Qt.GlobalColor.blue)
        icon = notification_icon(base, outgoing=True)
        assert isinstance(icon, QIcon) and not icon.isNull()

    def test_returns_standalone_badge_without_base(self, qtbot):
        from PySide6.QtGui import QIcon
        from qeth.icons import notification_icon
        # No token logo cached → the direction badge fills the icon itself.
        icon = notification_icon(None, outgoing=False)
        assert isinstance(icon, QIcon) and not icon.isNull()


# --- vault_icon (underlying + sparkle badge) -------------------------------

class TestVaultIcon:
    def _has_color(self, img, color, box, tol=48):
        from PySide6.QtGui import QColor
        c = QColor(color)
        x0, y0, x1, y1 = box
        for x in range(x0, x1, 2):
            for y in range(y0, y1, 2):
                px = img.pixelColor(x, y)
                if (abs(px.red() - c.red()) + abs(px.green() - c.green())
                        + abs(px.blue() - c.blue())) < tol:
                    return True
        return False

    def _max_green(self, img, box):
        x0, y0, x1, y1 = box
        return max(img.pixelColor(x, y).green()
                   for x in range(x0, x1, 2) for y in range(y0, y1, 2))

    def test_underlying_shows_with_sparkle_in_the_corner(self, qtbot):
        from PySide6.QtGui import QPixmap
        from qeth.icons import vault_icon
        base = QPixmap(64, 64)
        base.fill(Qt.GlobalColor.red)          # green channel 0 everywhere
        img = vault_icon(base, 64).toImage()
        # Underlying (red) shows in the top-left, un-badged.
        assert self._has_color(img, Qt.GlobalColor.red, (6, 6, 22, 22))
        # The gold sparkle sits in the bottom-right: the red underlying has no
        # green, so a raised green channel there marks the sparkle (robust to
        # its 70% opacity blending with the icon).
        assert self._max_green(img, (40, 40, 63, 63)) > 90
        assert self._max_green(img, (6, 6, 22, 22)) < 40    # top-left un-badged

    def test_badge_is_semi_transparent(self, qtbot):
        from qeth.icons import vault_icon, _VAULT_BADGE_OPACITY
        # Centred sparkle over a transparent canvas. Sample pure-fill pixels near
        # the centre (bright yellow, clear of the brown outline) — they keep the
        # badge's ~70% alpha, so the underlying icon shows through.
        img = vault_icon(None, 64).toImage()
        alphas = [img.pixelColor(x, y).alpha()
                  for x in range(26, 39) for y in range(26, 39)
                  if img.pixelColor(x, y).red() > 220
                  and img.pixelColor(x, y).green() > 180]
        assert alphas                                    # the sparkle drew
        assert abs(max(alphas) - round(255 * _VAULT_BADGE_OPACITY)) <= 15


class TestStackedIcon:
    """An LP token's icon stacks its pooled coins' icons."""

    def _circle(self, color):
        from PySide6.QtGui import QPixmap, QPainter, QColor
        b = QPixmap(64, 64)
        b.fill(Qt.GlobalColor.transparent)
        p = QPainter(b)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 60, 60)
        p.end()
        return b

    def _present(self, img, color, tol=60):
        from PySide6.QtGui import QColor
        c = QColor(color)
        for x in range(0, 64, 2):
            for y in range(0, 64, 2):
                px = img.pixelColor(x, y)
                if px.alpha() > 200 and (abs(px.red() - c.red())
                        + abs(px.green() - c.green()) + abs(px.blue() - c.blue())) < tol:
                    return True
        return False

    def test_two_coins_both_appear(self, qtbot):
        from qeth.icons import stacked_icon
        img = stacked_icon([self._circle("#3b5bdb"), self._circle("#f7931a")], 64).toImage()
        assert self._present(img, "#3b5bdb")   # coin 0 (blue) shows
        assert self._present(img, "#f7931a")   # coin 1 (orange) shows

    def test_empty_bases_is_blank(self, qtbot):
        from qeth.icons import stacked_icon
        img = stacked_icon([], 64).toImage()
        assert all(img.pixelColor(x, y).alpha() == 0
                   for x in range(0, 64, 8) for y in range(0, 64, 8))


class TestLpRowIcon:
    """An LP row (source onchain-curve-lp/univ2-lp with pool_tokens) shows its
    pooled coins' icons stacked."""

    LP = "0x516c3ecfe45f0820653e08dd7c93633d71b93cb5"
    C0 = "0x" + "c0" * 20
    C1 = "0x" + "c1" * 20

    def _cached(self):
        from qeth.wallet_cache import CachedToken, CachedWallet
        return CachedWallet(
            chain_id=1, address="0x" + "aa" * 20,
            tokens=[CachedToken(
                contract=self.LP, symbol="crvUSD-LP", name="Curve LP",
                decimals=18, balance_raw=10 ** 18, price_usd="1.29",
                price_source="onchain-curve-lp", pool_tokens=[self.C0, self.C1])],
        )

    def _row(self, panel):
        for r in range(panel.table.rowCount()):
            it = panel.table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == (1, self.LP):
                return it
        return None

    def test_stacks_pooled_coins_when_cached(self, qtbot, tmp_qeth):
        from PySide6.QtGui import QPixmap
        store = Store.load()
        icons = IconCache()
        for c, col in ((self.C0, Qt.GlobalColor.blue), (self.C1, Qt.GlobalColor.red)):
            pm = QPixmap(20, 20)
            pm.fill(col)
            icons._mem[(1, c)] = pm
        panel = TokenListPanel(icons, store)
        qtbot.addWidget(panel)
        panel.show_cached(ETH, self._cached())
        assert panel._lp_coins.get(self.LP) == (self.C0, self.C1)
        item = self._row(panel)
        assert item is not None and not item.icon().isNull()

    def test_coin_arriving_async_repaints_the_lp_row(self, qtbot, tmp_qeth):
        from PySide6.QtGui import QPixmap
        store = Store.load()
        icons = IconCache()
        panel = TokenListPanel(icons, store)
        qtbot.addWidget(panel)
        panel.show_cached(ETH, self._cached())     # no coin icons cached yet
        assert self._row(panel).icon().isNull()
        pm = QPixmap(20, 20)
        pm.fill(Qt.GlobalColor.blue)
        icons._mem[(1, self.C0)] = pm
        panel._on_icon_ready(1, self.C0)            # a pooled coin's icon lands
        assert not self._row(panel).icon().isNull()


class TestVaultRowIcon:
    """A vault row (price source onchain-yb/4626 with an underlying) shows the
    underlying's icon composited with a sparkle badge."""

    VAULT = "0x931d40dd07b25b91932b481b63631ea86d236e09"
    UNDER = "0x" + "c0" * 20

    def _cached(self):
        from qeth.wallet_cache import CachedToken, CachedWallet
        return CachedWallet(
            chain_id=1, address="0x" + "aa" * 20, native_price_usd="3000",
            tokens=[CachedToken(
                contract=self.VAULT, symbol="yb-WETH", name="Yield Basis WETH",
                decimals=18, balance_raw=10 ** 18, price_usd="3062",
                price_source="onchain-yb", underlying=self.UNDER)],
        )

    def _row(self, panel):
        for r in range(panel.table.rowCount()):
            it = panel.table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == (1, self.VAULT):
                return it
        return None

    def test_composites_underlying_when_cached(self, qtbot, tmp_qeth):
        from PySide6.QtGui import QPixmap
        store = Store.load()
        icons = IconCache()
        base = QPixmap(20, 20)
        base.fill(Qt.GlobalColor.red)
        icons._mem[(1, self.UNDER)] = base        # underlying icon already cached
        panel = TokenListPanel(icons, store)
        qtbot.addWidget(panel)
        panel.show_cached(ETH, self._cached())
        assert panel._vault_underlying.get(self.VAULT) == self.UNDER
        item = self._row(panel)
        assert item is not None and not item.icon().isNull()   # composited icon

    def test_underlying_arriving_async_repaints_the_vault_row(self, qtbot, tmp_qeth):
        from PySide6.QtGui import QPixmap
        store = Store.load()
        icons = IconCache()
        panel = TokenListPanel(icons, store)
        qtbot.addWidget(panel)
        panel.show_cached(ETH, self._cached())     # underlying NOT cached yet
        item = self._row(panel)
        assert item is not None and item.icon().isNull()   # no icon yet (yb has none)
        # the underlying's logo lands → _on_icon_ready composites it onto the row
        icons._mem[(1, self.UNDER)] = QPixmap(20, 20)
        icons._mem[(1, self.UNDER)].fill(Qt.GlobalColor.red)
        panel._on_icon_ready(1, self.UNDER)
        assert not self._row(panel).icon().isNull()


# --- tray minimise-to-tray -------------------------------------------------

class TestTrayDehydrate:
    def test_minimise_only_hides_no_state_change(self):
        """Minimise→tray must ONLY hide() — it must not call setWindowState().
        Running setWindowState on the just-hidden (unmapped) window blocks Qt's
        X11 backend waiting on a WM reply that never comes, hanging the app on
        every minimise. The minimised bit is cleared on restore via
        showNormal() instead."""
        from PySide6.QtCore import Qt
        from qeth.tray import _TrayController

        calls = []

        class FakeWin:
            def windowState(self):
                return Qt.WindowState.WindowMinimized

            def hide(self):
                calls.append("hide")

            def setWindowState(self, _state):
                calls.append("setWindowState")

        ctrl = _TrayController.__new__(_TrayController)   # skip Qt __init__
        ctrl._win = FakeWin()
        ctrl._dehydrate_to_tray()

        assert calls == ["hide"]


# --- AccountInfoDialog -----------------------------------------------------
#
# The per-account QR + address/path/source/scheme used to sit in a permanent
# DetailsPanel below the tree; it's now a modal popup opened from the QR
# button on the action row (label editing / connect / sign moved to their
# own action-row buttons).

class TestAccountInfoDialog:
    def test_fields_and_qr_filled(self, qtbot, tmp_qeth):
        dlg = AccountInfoDialog({
            "address": ADDR, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44",
            "label": "Cold storage",
        })
        qtbot.addWidget(dlg)
        assert dlg.address_lbl.text() == ADDR
        assert dlg.path_lbl.text() == "44'/60'/0'/0/0"
        assert dlg.source_lbl.text() == "ledger"
        assert dlg.scheme_lbl.text() == "BIP-44"
        # The receive QR rendered into the fixed-size label.
        assert not dlg.qr_lbl.pixmap().isNull()

    def test_missing_fields_show_dash(self, qtbot, tmp_qeth):
        dlg = AccountInfoDialog({"address": ADDR, "source": "ledger"})
        qtbot.addWidget(dlg)
        assert dlg.path_lbl.text() == "—"
        assert dlg.scheme_lbl.text() == "—"


# --- TokenListPanel rendering ----------------------------------------------

@pytest.fixture
def token_panel(qtbot, tmp_qeth):
    store = Store.load()
    icons = IconCache()
    panel = TokenListPanel(icons, store)
    qtbot.addWidget(panel)
    return panel


def _has_copy_shortcut(table) -> bool:
    from PySide6.QtGui import QKeySequence
    return any(
        a.shortcut() == QKeySequence(QKeySequence.Copy)
        and a.shortcutContext() == Qt.WidgetWithChildrenShortcut
        for a in table.actions()
    )


class TestTokenListPanel:
    def test_ctrl_c_copies_contract_address(self, token_panel):
        # Ctrl+C is wired on the table (scoped to it), and triggers the
        # same handler as the Copy button — copying the contract address.
        assert _has_copy_shortcut(token_panel.table)

    def test_zero_balance_never_shows_even_pinned(self, qtbot, tmp_qeth):
        """A token at exactly zero balance is hidden even when pinned/custom
        ('pin'/'add' mean show-when-held, not show-a-zero). A pinned or custom
        token with any non-zero balance shows even below the dust threshold; an
        ordinary dust token is hidden."""
        from decimal import Decimal
        from qeth.token_discovery import TokenBalance
        from qeth.pricing import Price
        store = Store.load()
        pin0 = "0x" + "a0" * 20      # pinned, zero balance
        pinN = "0x" + "a1" * 20      # pinned, non-zero (sub-dust USD)
        cusN = "0x" + "a2" * 20      # custom, non-zero (sub-dust USD)
        ordy = "0x" + "a3" * 20      # ordinary, sub-dust
        store.force_show_token(1, pin0)
        store.force_show_token(1, pinN)
        store.add_custom_token(1, cusN)
        panel = TokenListPanel(IconCache(), store)
        qtbot.addWidget(panel)
        toks = [
            TokenBalance(contract=pin0, symbol="P0", name="", decimals=18, balance_raw=0),
            TokenBalance(contract=pinN, symbol="PN", name="", decimals=18, balance_raw=1),
            TokenBalance(contract=cusN, symbol="CN", name="", decimals=18, balance_raw=1),
            TokenBalance(contract=ordy, symbol="OR", name="", decimals=18, balance_raw=1),
        ]
        tiny = Price(price_usd=Decimal("0.00000001"), timestamp=0, source="t")
        prices = {c: tiny for c in (pin0, pinN, cusN, ordy)}
        panel.render_full(ETH, 0, toks, {}, prices, apply_dust_filter=True)
        shown = {}
        for row in range(panel.table.rowCount()):
            it = panel.table.item(row, 0)
            key = it.data(Qt.ItemDataRole.UserRole) if it else None
            if key:
                shown[key[1]] = not panel.table.isRowHidden(row)
        assert shown[pin0] is False    # pinned but zero → hidden
        assert shown[pinN] is True     # pinned, non-zero dust → shown
        assert shown[cusN] is True     # custom, non-zero dust → shown
        assert shown[ordy] is False    # ordinary dust → hidden

    def test_recognised_token_shows_before_its_price_loads(self, qtbot, tmp_qeth):
        """A just-received RECOGNISED token (in the curated lists) is added to
        the cache without a price; it must still show — 'no price → hide' is
        only for unrecognised spam. An unrecognised no-price token stays hidden.
        (The 'swapped LT→WETH but WETH didn't appear' bug.)"""
        from types import SimpleNamespace
        from qeth.token_discovery import TokenBalance
        store = Store.load()
        weth = "0x" + "c0" * 20      # pretend-recognised
        spam = "0x" + "5e" * 20      # unrecognised, no price
        panel = TokenListPanel(IconCache(), store)
        qtbot.addWidget(panel)
        panel._token_lists = SimpleNamespace(
            is_known=lambda cid, a: a.lower() == weth,
            is_likely_scam=lambda *a, **k: False)
        toks = [
            TokenBalance(contract=weth, symbol="WETH", name="", decimals=18,
                         balance_raw=10**15),
            TokenBalance(contract=spam, symbol="SPAM", name="", decimals=18,
                         balance_raw=10**21),
        ]
        # No prices for either (price fetch hasn't landed / spam has none).
        panel.render_full(ETH, 0, toks, {}, {}, apply_dust_filter=True)
        shown = {}
        for row in range(panel.table.rowCount()):
            it = panel.table.item(row, 0)
            key = it.data(Qt.ItemDataRole.UserRole) if it else None
            if key and key[1]:
                shown[key[1]] = not panel.table.isRowHidden(row)
        assert shown[weth] is True     # recognised, price pending → shown
        assert shown[spam] is False    # unrecognised, no price → hidden

    def test_recognised_unpriced_token_hidden_once_grace_lapses(
        self, qtbot, tmp_qeth,
    ):
        """Display-time: with the grace clock wired in, the panel hides a
        recognised-but-unpriced token once its window lapses (so it agrees with
        the discovery filter instead of lingering until the next discovery)."""
        from types import SimpleNamespace
        from qeth.token_discovery import TokenBalance
        store = Store.load()
        addr = "0x" + "c0" * 20
        panel = TokenListPanel(IconCache(), store)
        qtbot.addWidget(panel)
        panel._token_lists = SimpleNamespace(
            is_known=lambda cid, a: True, is_likely_scam=lambda *a, **k: False)
        expired: set = set()
        panel._unpriced_grace = lambda cid, a: (cid, a.lower()) not in expired
        toks = [TokenBalance(contract=addr, symbol="SUSD", name="", decimals=18,
                             balance_raw=10 ** 18)]

        def hidden():
            for row in range(panel.table.rowCount()):
                it = panel.table.item(row, 0)
                key = it.data(Qt.ItemDataRole.UserRole) if it else None
                if key and key[1] == addr:
                    return panel.table.isRowHidden(row)
            return None

        panel.render_full(ETH, 0, toks, {}, {}, apply_dust_filter=True)
        assert hidden() is False              # inside grace → shown
        expired.add((1, addr))                # window lapses
        panel.render_full(ETH, 0, toks, {}, {}, apply_dust_filter=True)
        assert hidden() is True               # now hidden

    def test_native_row_pinned_at_index_zero(self, token_panel):
        token_panel.show_balances(ETH, native_wei=10**18, tokens=[], list_entries={})
        assert token_panel.table.rowCount() == 1
        sym = token_panel.table.item(0, 0)
        assert sym.text() == ETH.symbol
        # The symbol cell stores (chain_id, "") for the native asset.
        assert sym.data(Qt.UserRole) == (ETH.chain_id, "")
        assert token_panel.table.item(0, 1).text() == "1"

    def test_native_row_falls_back_to_chain_icon(self, qtbot, tmp_qeth):
        """A native symbol with no bundled icon (AVAX/BNB/XDAI) uses the
        chain logo via the getter; bundled ones (ETH) don't call it."""
        from types import SimpleNamespace
        from PySide6.QtGui import QPixmap
        from qeth.store import Store
        from qeth.icons import IconCache
        from qeth.plugins.tokens import TokenListPanel
        pix = QPixmap(8, 8); pix.fill()
        calls = []
        getter = lambda cid: (calls.append(cid), pix)[1]
        panel = TokenListPanel(IconCache(), Store.load(), chain_icon_getter=getter)
        qtbot.addWidget(panel)

        avax = SimpleNamespace(chain_id=43114, symbol="AVAX", name="Avalanche")
        panel.show_balances(avax, native_wei=10**18, tokens=[], list_entries={})
        assert calls == [43114]                                   # fallback used
        assert not panel.table.item(0, 0).icon().isNull()         # icon set

        # ETH is bundled → getter not consulted.
        calls.clear()
        panel.show_balances(ETH, native_wei=10**18, tokens=[], list_entries={})
        assert calls == []

    def test_native_icon_filled_async_on_chain_icon_ready(self, qtbot, tmp_qeth):
        """When the chain logo wasn't cached at render time, a later
        chain-icon-ready fills the native row's icon."""
        from types import SimpleNamespace
        from PySide6.QtGui import QPixmap
        from qeth.store import Store
        from qeth.icons import IconCache
        from qeth.plugins.tokens import TokenListPanel
        panel = TokenListPanel(IconCache(), Store.load(),
                               chain_icon_getter=lambda cid: None)  # miss
        qtbot.addWidget(panel)
        avax = SimpleNamespace(chain_id=43114, symbol="AVAX", name="Avalanche")
        panel.show_balances(avax, native_wei=10**18, tokens=[], list_entries={})
        assert panel.table.item(0, 0).icon().isNull()        # blank at first
        pix = QPixmap(8, 8); pix.fill()
        panel.update_native_icon(43114, pix)                 # logo arrives
        assert not panel.table.item(0, 0).icon().isNull()
        # Wrong chain id is ignored.
        panel.show_balances(avax, native_wei=10**18, tokens=[], list_entries={})
        panel.update_native_icon(999, pix)
        assert panel.table.item(0, 0).icon().isNull()

    def test_erc20_rows_follow_native(self, token_panel):
        tokens = [
            TokenBalance(
                contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                symbol="USDC", name="USD Coin", decimals=6,
                balance_raw=2_500_000,  # 2.5 USDC
            ),
        ]
        token_panel.show_balances(ETH, native_wei=0, tokens=tokens, list_entries={})
        assert token_panel.table.rowCount() == 2
        assert token_panel.table.item(1, 0).text() == "USDC"
        assert token_panel.table.item(1, 1).text() == "2.5"

    def test_context_menu_mirrors_send_button(self, qtbot, tmp_qeth, monkeypatch):
        """The row context menu mirrors the action buttons for the clicked
        row: every row (native asset included) offers Send; ERC-20 rows add
        Copy/Pin/Hide. The native row previously got no menu at all, and the
        token menu lacked Send even though the Send button below handles both."""
        import qeth.plugins.tokens as tokmod

        class _FakeAction:
            def __init__(self, text: str) -> None:
                self.text = text

        class _FakeMenu:
            instances: list = []
            choose: str | None = None

            def __init__(self, *a, **k):
                self.actions: list = []
                _FakeMenu.instances.append(self)

            def addAction(self, *a, **k):
                act = _FakeAction(a[-1] if a else "")
                self.actions.append(act)
                return act

            def addSeparator(self):
                pass

            def exec(self, *a, **k):
                return next((act for act in self.actions
                             if act.text == _FakeMenu.choose), None)

        monkeypatch.setattr(tokmod, "QMenu", _FakeMenu)

        store = Store.load()
        panel = TokenListPanel(IconCache(), store)
        qtbot.addWidget(panel)
        usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        tokens = [TokenBalance(contract=usdc, symbol="USDC", name="USD Coin",
                               decimals=6, balance_raw=2_500_000)]
        panel.show_balances(ETH, native_wei=10**18, tokens=tokens, list_entries={})
        panel.show()
        qtbot.wait(50)

        def meta(row):
            return panel.table.item(row, 0).data(Qt.ItemDataRole.UserRole)

        native_row = next(r for r in range(panel.table.rowCount())
                          if meta(r)[1] == "")
        erc_row = next(r for r in range(panel.table.rowCount())
                       if meta(r)[1] != "")

        sent: list = []
        panel.send_requested.connect(lambda cid, addr: sent.append((cid, addr)))

        def open_menu(row):
            _FakeMenu.instances.clear()
            rect = panel.table.visualItemRect(panel.table.item(row, 0))
            panel._on_context_menu(rect.center())
            return _FakeMenu.instances[-1]

        # Native row: Send is the only applicable action (no contract to
        # copy, can't pin/hide) — but it MUST offer a menu now.
        _FakeMenu.choose = "Send ETH"
        m = open_menu(native_row)
        assert [a.text for a in m.actions] == ["Send ETH"]
        assert sent == [meta(native_row)]

        # ERC-20 row: Send heads the menu, then Copy/Pin/Hide.
        sent.clear()
        _FakeMenu.choose = "Send USDC"
        m = open_menu(erc_row)
        labels = [a.text for a in m.actions]
        assert labels[0] == "Send USDC"
        assert "Copy Contract Address" in labels
        assert any(x.startswith("Pin ") for x in labels)
        assert any(x.startswith("Hide ") for x in labels)
        assert sent == [meta(erc_row)]

    def test_huge_erc20_balance_does_not_overflow(self, token_panel):
        """ASF-style raw balances exceed qint64; if we ever marshal them
        through PySide6's int signals they overflow. Rendering should
        not depend on signal marshalling — verify the raw value reaches
        the cell intact via the panel's Decimal balance store."""
        big = 10**25
        tokens = [
            TokenBalance(
                contract="0xdeadbeef00000000000000000000000000000001",
                symbol="ASF", name="Big", decimals=18, balance_raw=big,
            ),
        ]
        token_panel.show_balances(ETH, native_wei=0, tokens=tokens, list_entries={})
        # Internal Decimal balance store keyed by (chain_id, addr_lower).
        key = (ETH.chain_id, tokens[0].contract.lower())
        assert token_panel._balances[key] == Decimal(big) / Decimal(10**18)

    def test_clear_empties_table(self, token_panel):
        token_panel.show_balances(ETH, 10**18, [], {})
        token_panel.clear()
        assert token_panel.table.rowCount() == 0


# --- TransactionListPanel rendering ----------------------------------------

def _tx(**kw) -> Transaction:
    defaults = dict(
        chain_id=1, hash="0x" + "ab" * 32,
        block_number=25_000_000, timestamp=1_779_618_611,
        nonce=10, from_addr=ADDR, to_addr=None,
        value_wei=0, gas_used=21_000, gas_price_wei=10**9,
        method_id="", input_data="0x", success=True,
    )
    defaults.update(kw)
    return Transaction(**defaults)


class TestTransactionListPanel:
    def test_ctrl_c_copies_tx_hash(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        assert _has_copy_shortcut(panel.table)

    def test_activity_refetch_targets_recent_one_sided_rows(self, qtbot, tmp_qeth):
        """A TOKEN->ETH swap can cache one-sided (token out, ETH not yet indexed
        by Blockscout); re-fetch it while recent so it self-heals, but leave
        settled and two-sided rows alone."""
        import time
        from types import SimpleNamespace
        from qeth.tx_activity import Activity, AssetLeg
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        now = time.time()
        tx = lambda h, ts=now: SimpleNamespace(hash=h, timestamp=ts)

        assert panel._activity_needs_fetch(tx("0xNEW"), now) is True   # uncached
        panel._activities["0xFULL"] = Activity(
            "exchange", (AssetLeg("USDT", "0x1"),), (AssetLeg("ETH", None),))
        assert panel._activity_needs_fetch(tx("0xFULL"), now) is False  # two-sided
        panel._activities["0xSWAP"] = Activity(
            "exchange", (AssetLeg("USDT", "0x1"),), ())
        assert panel._activity_needs_fetch(tx("0xSWAP"), now) is True   # one-sided, recent
        assert panel._activity_needs_fetch(
            tx("0xSWAP", now - 7 * 3600), now) is False                 # one-sided, settled

    def test_empty_list_shows_status_message(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([])
        # isVisible() requires the whole ancestor chain to be shown,
        # which it isn't under the offscreen platform — check the
        # local hidden flag instead.
        assert not panel.status_lbl.isHidden()
        assert "No transactions" in panel.status_lbl.text()
        assert panel.table.rowCount() == 0

    # Column layout: 0=Status, 1=Nonce, 2=Time, 3=Hash.

    def test_columns_match_layout(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        # Status column has an empty header; the others use words.
        labels = [
            panel.table.horizontalHeaderItem(i).text()
            for i in range(panel.table.columnCount())
        ]
        # Status | Nonce | gap | Time | gap | Activity (verb) | coins.
        # The two empty-header gap columns stretch so a wide window
        # justifies the row instead of trailing whitespace on the right.
        assert labels == ["", "Nonce", "", "Time", "", "Activity", ""]

    def test_status_nonce_and_hash_render(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        tx = _tx(nonce=42, to_addr="0xbeef", success=True)
        panel.show_transactions([tx])
        assert panel.table.item(0, 0).toolTip() == "Success"
        assert panel.table.item(0, 1).text() == "42"
        # Time cell (col 3 now — col 2 is a stretch gap) is locale-formatted
        # — just assert non-empty rather than locking in a format string.
        assert panel.table.item(0, 3).text()
        # The Activity verb cell (col 5): blank until its activity resolves,
        # but the full hash is on the tooltip and the Transaction rides on
        # UserRole (so the details dialog / explorer recover it).
        act_cell = panel.table.item(0, 5)
        assert act_cell.text() == ""
        assert act_cell.toolTip() == tx.hash
        assert act_cell.data(Qt.UserRole) is tx

    def test_prepend_clears_stray_current_index(self, qtbot, tmp_qeth):
        """Inserting a pending row at the top must not leave the view's
        current index on the new (0,0) status cell — that draws a stray
        focus outline on the icon until the next rebuild."""
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([_tx(nonce=5), _tx(nonce=4)])
        panel.table.setCurrentCell(0, 0)          # simulate the stray current
        assert panel.table.currentIndex().isValid()
        panel.prepend_transactions([_tx(nonce=6, pending=True)])
        assert not panel.table.currentIndex().isValid()
        assert panel.table.rowCount() == 3

    def test_failed_tx_marked_with_cross(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([
            _tx(to_addr="0xbeef00000000000000000000000000000000beef", success=True),
            _tx(to_addr="0xbeef00000000000000000000000000000000beef", success=False),
        ])
        assert panel.table.item(0, 0).toolTip() == "Success"
        assert panel.table.item(1, 0).toolTip() == "Reverted"

    def test_status_column_uses_font_glyphs(self, qtbot, tmp_qeth):
        """The status column is drawn with font glyphs, NOT themed icons — a
        themed dialog-ok/emblem-ok varied wildly across themes and sizes (a
        pixelized tick vs a glossy square), so the check rendered differently
        machine-to-machine. Glyphs track the font + palette and are identical."""
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([
            _tx(to_addr="0xbeef", success=True, pending=True),
            _tx(to_addr="0xbeef", success=True),
            _tx(to_addr="0xbeef", success=False),
        ])
        assert panel.table.item(0, 0).text().startswith("⏳")   # pending (+ maybe VS15)
        assert panel.table.item(1, 0).text() == "✓"             # confirmed
        assert panel.table.item(2, 0).text() == "✗"             # reverted
        assert panel.table.item(1, 0).icon().isNull()           # a glyph, no icon


    def test_clear_resets_panel(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([_tx(to_addr="0xabc")])
        panel.clear()
        assert panel.table.rowCount() == 0
        assert panel.status_lbl.isHidden()

    def test_pending_tx_marked_with_hourglass(self, qtbot, tmp_qeth):
        """Status column glyph for ``tx.pending=True``."""
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([_tx(to_addr="0xbeef", pending=True)])
        assert panel.table.item(0, 0).toolTip() == "Pending"
        assert panel.table.item(0, 0).toolTip() == "Pending"

    def test_bulk_populate_temporarily_disables_autosize(
        self, qtbot, tmp_qeth, monkeypatch,
    ):
        """Regression: replacing rows on a ResizeToContents column
        re-measures the whole column on every setItem, turning a
        2000-row repopulate into a ~35-second main-thread freeze.
        ``show_transactions`` must switch the affected columns to
        ``Fixed`` during populate and restore the prior resize mode
        afterward. We assert the actual transitions rather than
        timing, so the test stays fast and deterministic."""
        from PySide6.QtWidgets import QHeaderView

        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        # First populate so the table has items at every row (the
        # bug only triggers on REPLACEMENT setItem calls).
        panel.show_transactions([_tx(to_addr="0xbeef") for _ in range(3)])

        header = panel.table.horizontalHeader()
        # Modes recorded by _populate_row each time it's called —
        # if any of them is ResizeToContents we'd be triggering the
        # O(N²) path.
        seen_modes: list[list[QHeaderView.ResizeMode]] = []
        original_populate = panel._populate_row

        def spy_populate(row, tx):
            seen_modes.append([
                header.sectionResizeMode(i)
                for i in range(panel.table.columnCount())
            ])
            original_populate(row, tx)

        monkeypatch.setattr(panel, "_populate_row", spy_populate)

        prior = [header.sectionResizeMode(i)
                  for i in range(panel.table.columnCount())]
        panel.show_transactions([_tx(to_addr="0xbeef") for _ in range(3)])

        # No populate call may run while any column is still on
        # ResizeToContents.
        for modes in seen_modes:
            assert QHeaderView.ResizeToContents not in modes, (
                "_populate_row ran while a column was still "
                "ResizeToContents; the O(N²) re-measure path is "
                "back. Modes seen: %r" % modes
            )
        # And the resize modes are restored to the user's configured
        # state after the bulk populate.
        restored = [header.sectionResizeMode(i)
                     for i in range(panel.table.columnCount())]
        assert restored == prior

    def test_bulk_populate_blocks_table_signals(
        self, qtbot, tmp_qeth,
    ):
        """Same bug had a secondary contributor: itemSelectionChanged
        firing on every setItem when the user had a row selected,
        which ran _update_action_buttons each time. show_transactions
        has to ``blockSignals(True)`` during the populate; we verify
        that no itemSelectionChanged signals reach a subscriber while
        the table is being rebuilt."""
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        # Populate once so the next call replaces existing items.
        panel.show_transactions([_tx(to_addr="0xbeef")])
        panel.table.selectRow(0)

        fires: list[int] = []
        panel.table.itemSelectionChanged.connect(lambda: fires.append(1))
        panel.show_transactions([_tx(to_addr="0xbeef")])
        # No signal should have fired during the bulk replace —
        # blockSignals discards them.
        assert fires == []


class TestFocusAwareDelegate:
    """The list delegate must never paint the per-cell focus rectangle
    on an *unselected* cell — Qt would otherwise outline the view's
    current index (set by a row insert / rebuild / auto-switch), drawing
    a stray box beside e.g. a pending row's status icon."""

    def _delegate(self, qtbot):
        from qeth.ui import _apply_focus_aware_selection
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([_tx(to_addr="0xbeef")])
        _apply_focus_aware_selection(panel.table)
        return panel, panel.table._focus_aware_delegate

    def test_strips_focus_rect_on_unselected_cell(self, qtbot, tmp_qeth, monkeypatch):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem
        panel, delegate = self._delegate(qtbot)
        index = panel.table.model().index(0, 0)

        captured = {}
        def spy(self, painter, opt, idx):
            captured["state"] = opt.state
        monkeypatch.setattr(QStyledItemDelegate, "paint", spy)

        option = QStyleOptionViewItem()
        delegate.initStyleOption(option, index)
        # Current-but-unselected cell while the view has focus.
        option.state |= QStyle.State_HasFocus
        option.state &= ~QStyle.State_Selected

        pm = QPixmap(80, 20)
        painter = QPainter(pm)
        try:
            delegate.paint(painter, option, index)
        finally:
            painter.end()

        assert "state" in captured            # fell through to super().paint
        assert not (captured["state"] & QStyle.State_HasFocus)   # rect suppressed

    def test_strips_hover_on_unselected_cell(self, qtbot, tmp_qeth, monkeypatch):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem
        panel, delegate = self._delegate(qtbot)
        index = panel.table.model().index(0, 0)

        captured = {}
        monkeypatch.setattr(QStyledItemDelegate, "paint",
                            lambda self, p, opt, i: captured.__setitem__("state", opt.state))
        option = QStyleOptionViewItem()
        delegate.initStyleOption(option, index)
        option.state |= QStyle.State_MouseOver
        option.state &= ~QStyle.State_Selected

        pm = QPixmap(80, 20)
        painter = QPainter(pm)
        try:
            delegate.paint(painter, option, index)
        finally:
            painter.end()
        assert not (captured["state"] & QStyle.State_MouseOver)   # hover suppressed

    def test_wallet_tree_drawrow_strips_hover(self, qtbot, tmp_qeth):
        """The wallet tree row paint must drop State_MouseOver so it
        doesn't highlight on hover when the right-hand tables don't."""
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import (
            QStyle, QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem,
        )
        from qeth.plugins.wallets import _ReorderTree
        tree = _ReorderTree()
        qtbot.addWidget(tree)
        tree.addTopLevelItem(QTreeWidgetItem(["Ledger"]))

        captured = {}
        orig = QTreeWidget.drawRow
        def spy(self, painter, option, index):
            captured["mouseover"] = bool(option.state & QStyle.State_MouseOver)
        QTreeWidget.drawRow = spy
        try:
            opt = QStyleOptionViewItem()
            opt.state |= QStyle.State_MouseOver
            pm = QPixmap(120, 20)
            painter = QPainter(pm)
            try:
                tree.drawRow(painter, opt, tree.model().index(0, 0))
            finally:
                painter.end()
        finally:
            QTreeWidget.drawRow = orig
        assert captured["mouseover"] is False

    def test_labeled_wallet_label_paints_sticky_pill(self, qtbot, tmp_qeth):
        """A wallet label renders as a sticky-note pill on the right; the
        address area is *not* tinted (only the label), and an unlabeled
        row has no pill at all."""
        from PySide6.QtGui import QImage, QPainter, QColor
        from PySide6.QtWidgets import (
            QStyle, QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem,
        )
        from PySide6.QtCore import QRect, Qt
        from qeth.ui import _FocusAwareSelectionDelegate, _STICKY_BG
        from qeth.plugins.wallets import ACCOUNT_LABEL_ROLE
        tree = QTreeWidget()
        qtbot.addWidget(tree)
        tree.setTextElideMode(Qt.ElideMiddle)
        delegate = _FocusAwareSelectionDelegate(tree)
        tree.setItemDelegate(delegate)
        tree.addTopLevelItem(QTreeWidgetItem([" 0x" + "a" * 40 + " "]))  # labeled
        tree.addTopLevelItem(QTreeWidgetItem([" 0x" + "b" * 40 + " "]))  # plain
        tree.topLevelItem(0).setData(0, ACCOUNT_LABEL_ROLE, "sig")

        def render(row):
            idx = tree.model().index(row, 0)
            opt = QStyleOptionViewItem()
            delegate.initStyleOption(opt, idx)
            opt.state &= ~QStyle.State_Selected
            opt.rect = QRect(0, 0, 300, 22)
            img = QImage(300, 22, QImage.Format_RGB32)
            img.fill(QColor("#202020"))
            painter = QPainter(img)
            try:
                delegate.paint(painter, opt, idx)
            finally:
                painter.end()
            return img

        labeled = render(0)
        assert labeled.pixelColor(285, 11).name() == _STICKY_BG   # pill (right)
        assert labeled.pixelColor(40, 11).name() != _STICKY_BG    # address area
        plain = render(1)
        assert plain.pixelColor(285, 11).name() != _STICKY_BG     # no pill

    def test_device_tree_label_paints_distinct_pill(self, qtbot, tmp_qeth):
        """A device-tree label renders the same pill in the tree colour
        (blue), distinct from an account label's yellow — so the two read
        apart at a glance."""
        from PySide6.QtGui import QImage, QPainter, QColor
        from PySide6.QtWidgets import (
            QStyle, QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem,
        )
        from PySide6.QtCore import QRect, Qt
        from qeth.ui import (
            _FocusAwareSelectionDelegate, _STICKY_BG, _TREE_STICKY_BG,
        )
        from qeth.plugins.wallets import ACCOUNT_LABEL_ROLE, TREE_LABEL_ROLE
        tree = QTreeWidget()
        qtbot.addWidget(tree)
        tree.setTextElideMode(Qt.ElideMiddle)
        delegate = _FocusAwareSelectionDelegate(tree)
        tree.setItemDelegate(delegate)
        tree.addTopLevelItem(QTreeWidgetItem(["Ledger Live (m/…)"]))   # tree row
        tree.addTopLevelItem(QTreeWidgetItem([" 0x" + "a" * 40 + " "]))  # account
        tree.topLevelItem(0).setData(0, TREE_LABEL_ROLE, "Nano")
        tree.topLevelItem(1).setData(0, ACCOUNT_LABEL_ROLE, "sig")

        def render(row):
            idx = tree.model().index(row, 0)
            opt = QStyleOptionViewItem()
            delegate.initStyleOption(opt, idx)
            opt.state &= ~QStyle.State_Selected
            opt.rect = QRect(0, 0, 300, 22)
            img = QImage(300, 22, QImage.Format_RGB32)
            img.fill(QColor("#202020"))
            painter = QPainter(img)
            try:
                delegate.paint(painter, opt, idx)
            finally:
                painter.end()
            return img

        tree_row = render(0)
        # Sample the pill's right padding (x=290), clear of the centered text
        # glyphs. Tree pill is the blue colour, not the account yellow.
        assert tree_row.pixelColor(290, 11).name() == _TREE_STICKY_BG
        assert tree_row.pixelColor(290, 11).name() != _STICKY_BG
        # An account label still paints the yellow (account wins).
        assert render(1).pixelColor(290, 11).name() == _STICKY_BG


def test_identity_row_loading_placeholder_then_badge(qtbot, tmp_path):
    """The Contract: row shows a muted 'identifying…' while the (multi
    round-trip) fetch runs, so a slow lookup reads as loading rather than
    'no info'; a resolved badge replaces it, and a None (transient error /
    unsupported chain) clears it back to blank."""
    from qeth.plugins.transactions import _make_identity_row
    from qeth.contract_identity import ContractIdentityCache, IdentityBadge

    captured = []
    label, kick = _make_identity_row(
        to_addr="0x" + "ab" * 20, chain=DEFAULT_CHAINS[0],
        identity_source=object(),                 # non-None → kick won't skip
        identity_cache=ContractIdentityCache(root=tmp_path),
        my_addresses=[], start_worker=captured.append, tx_cache=None)
    qtbot.addWidget(label)
    assert label.text() == ""                     # blank before kick
    kick()
    assert label.text() == "identifying…"         # muted loading placeholder
    assert not label.isEnabled()
    assert len(captured) == 1                      # a worker was started
    captured[0].ready.emit(IdentityBadge("RewardClaimHelper", "ok"))
    assert label.text() == "RewardClaimHelper" and label.isEnabled()
    captured[0].ready.emit(None)                   # transient error → blank
    assert label.text() == ""


def test_context_menu_opens_on_justify_gap_columns(qtbot, tmp_qeth, monkeypatch):
    """Right-clicking the empty stretch gap columns must still open the tx
    menu — they hold no QTableWidgetItem, so the old itemAt() lookup
    returned None there and the menu never appeared. (Patch the module's
    QMenu with a fake — a real menu.exec() would block on a modal popup.)"""
    import qeth.plugins.transactions as txmod
    from qeth.plugins.transactions import _C_GAP1, _C_GAP2
    from PySide6.QtCore import QPoint

    class _FakeAction:
        def setEnabled(self, *a):
            pass

    class _FakeMenu:
        opened: list = []

        def __init__(self, *a, **k):
            pass

        def addAction(self, *a, **k):
            return _FakeAction()

        def addSeparator(self):
            pass

        def exec(self, *a, **k):
            _FakeMenu.opened.append(1)

    monkeypatch.setattr(txmod, "QMenu", _FakeMenu)

    panel = TransactionListPanel()
    qtbot.addWidget(panel)
    panel.resize(800, 300)
    panel.set_context(ETH, ADDR)
    panel.show_transactions([_tx(nonce=42, to_addr="0xbeef", success=True)])
    panel.show()
    qtbot.wait(100)                       # let the table lay its columns out
    for gap in (_C_GAP1, _C_GAP2):
        assert panel.table.columnWidth(gap) > 0   # the gap actually stretched
        _FakeMenu.opened.clear()
        x = (panel.table.columnViewportPosition(gap)
             + panel.table.columnWidth(gap) // 2)
        y = panel.table.rowViewportPosition(0) + 4
        panel._on_context_menu(QPoint(x, y))
        assert _FakeMenu.opened == [1], f"menu didn't open on gap {gap}"
