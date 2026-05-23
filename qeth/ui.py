import io

import segno

from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QStatusBar, QStyle, QTableWidget, QTableWidgetItem,
    QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from decimal import Decimal

from .chain import EthClient, wei_to_ether
from .icons import IconCache, bundled_chain_icon, bundled_native_icon
from .ledger import DiscoveredAccount, LedgerWorker, PATH_SCHEMES
from .prices import DefiLlamaPrices, Price, PriceSource
from .tokenlists import TokenListEntry, TokenLists
from .tokens import BlockscoutSource, TokenBalance
from .wallet_cache import CachedWallet, WalletCache


# --- Add Ledger dialog -------------------------------------------------------

class AddLedgerDialog(QDialog):
    def __init__(self, chain, parent=None):
        super().__init__(parent)
        self._chain = chain
        self.setWindowTitle("Add Ledger accounts")
        self.setMinimumSize(640, 460)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.scheme_combo = QComboBox()
        self.scheme_combo.addItems(list(PATH_SCHEMES.keys()))
        form.addRow("Derivation scheme:", self.scheme_combo)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(0, 100)
        self.count_spin.setValue(0)
        self.count_spin.setSpecialValueText("Auto-detect (stop after 3 empty)")
        form.addRow("Accounts to scan:", self.count_spin)
        layout.addLayout(form)

        layout.addWidget(QLabel(
            "Connect your Ledger, unlock it, and open the Ethereum app, then click Scan.\n"
            f"Balances are queried on {chain.name}; non-empty accounts are pre-selected."
        ))

        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.ExtendedSelection)
        font = QFont("monospace")
        self.results.setFont(font)
        layout.addWidget(self.results, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        btns = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.add_btn = QPushButton("Add selected")
        self.add_btn.setEnabled(False)
        self.close_btn = QPushButton("Close")
        btns.addWidget(self.scan_btn)
        btns.addStretch(1)
        btns.addWidget(self.add_btn)
        btns.addWidget(self.close_btn)
        layout.addLayout(btns)

        self.scan_btn.clicked.connect(self._scan)
        self.add_btn.clicked.connect(self.accept)
        self.close_btn.clicked.connect(self.reject)
        self.results.itemSelectionChanged.connect(
            lambda: self.add_btn.setEnabled(bool(self.results.selectedItems()))
        )

        self._worker: LedgerWorker | None = None

    def _scan(self) -> None:
        self.results.clear()
        self.add_btn.setEnabled(False)
        n = self.count_spin.value()
        if n == 0:
            self.progress.setRange(0, 0)  # indeterminate spinner
        else:
            self.progress.setRange(0, n)
            self.progress.setValue(0)
        self.progress.setVisible(True)
        self.scan_btn.setEnabled(False)
        self._worker = LedgerWorker(
            self.scheme_combo.currentText(), n, chain=self._chain
        )
        self._worker.discovered.connect(self._on_found)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_found(self, acct: DiscoveredAccount) -> None:
        balance = wei_to_ether(acct.balance_wei)
        label = (
            f"#{acct.index:<3} {acct.address}   "
            f"{balance:.6f} {self._chain.symbol}"
        )
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, acct)
        item.setSelected(acct.balance_wei > 0)
        self.results.addItem(item)
        if self.progress.maximum() > 0:
            self.progress.setValue(self.progress.value() + 1)

    def _on_done(self) -> None:
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        self.add_btn.setEnabled(bool(self.results.selectedItems()))

    def _on_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        QMessageBox.critical(self, "Ledger error", msg)

    def selected_accounts(self) -> list[DiscoveredAccount]:
        return [it.data(Qt.UserRole) for it in self.results.selectedItems()]


# --- Right-hand details panel ------------------------------------------------

class DetailsPanel(QWidget):
    set_default_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        self.title = QLabel("Select an account on the left")
        f = self.title.font(); f.setPointSize(f.pointSize() + 2); f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)

        form = QFormLayout()
        mono = QFont("monospace")
        self.address_lbl = QLabel("—"); self.address_lbl.setFont(mono)
        self.address_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_lbl = QLabel("—"); self.path_lbl.setFont(mono)
        self.source_lbl = QLabel("—")
        self.scheme_lbl = QLabel("—")
        form.addRow("Address:", self.address_lbl)
        form.addRow("Path:", self.path_lbl)
        form.addRow("Source:", self.source_lbl)
        form.addRow("Scheme:", self.scheme_lbl)
        v.addLayout(form)

        self.qr_lbl = QLabel()
        self.qr_lbl.setAlignment(Qt.AlignCenter)
        self.qr_lbl.setFixedSize(220, 220)
        v.addWidget(self.qr_lbl, 0, Qt.AlignCenter)

        self.set_default_btn = QPushButton("Set as default (exposed to dapps)")
        self.set_default_btn.setEnabled(False)
        self.set_default_btn.clicked.connect(
            lambda: self._current and self.set_default_requested.emit(self._current)
        )
        v.addWidget(self.set_default_btn)
        v.addStretch(1)
        self._current: str | None = None

    def show_account(self, account: dict, is_default: bool) -> None:
        self._current = account["address"]
        self.title.setText(account.get("label") or "Account")
        self.address_lbl.setText(account["address"])
        self.path_lbl.setText(account.get("path", "—"))
        self.source_lbl.setText(account.get("source", "—"))
        self.scheme_lbl.setText(account.get("scheme", "—"))
        self.set_default_btn.setEnabled(not is_default)
        self.set_default_btn.setText(
            "Default ✓" if is_default else "Set as default (exposed to dapps)"
        )
        self._render_qr(account["address"])

    def _render_qr(self, address: str) -> None:
        buf = io.BytesIO()
        # ethereum: URI per EIP-681 so wallets recognize it as a send intent
        segno.make(f"ethereum:{address}", error="m").save(buf, kind="png", scale=6, border=2)
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        self.qr_lbl.setPixmap(pix.scaled(
            self.qr_lbl.size(), Qt.KeepAspectRatio, Qt.FastTransformation
        ))

    def clear(self) -> None:
        self._current = None
        self.title.setText("Select an account on the left")
        for w in (self.address_lbl, self.path_lbl, self.source_lbl, self.scheme_lbl):
            w.setText("—")
        self.qr_lbl.clear()
        self.set_default_btn.setEnabled(False)
        self.set_default_btn.setText("Set as default (exposed to dapps)")


# --- Token list panel + background workers -----------------------------------

class TokenListsLoader(QThread):
    """Background loader for the curated TokenLists (network + cache)."""

    loaded = Signal()
    failed = Signal(str)

    def __init__(self, lists: TokenLists, parent=None):
        super().__init__(parent)
        self.lists = lists

    def run(self) -> None:
        try:
            self.lists.load()
            self.loaded.emit()
        except Exception as e:
            self.failed.emit(str(e))


class TokenListWorker(QThread):
    """Fetch native + ERC-20 balances for (chain, address) and apply the
    visibility rules: hidden tokens are dropped; only known-or-force-shown
    ERC-20s are kept; the native asset is always returned as the first
    element so the UI can render it on top.

    Emits ``fetched(native_wei: int, tokens: list[TokenBalance])``.
    """

    # native_wei must travel as ``object``; declaring ``int`` makes PySide6
    # marshal through qint32 and overflows for any balance above ~2.1e9 wei
    # (well below a millionth of an ETH).
    fetched = Signal(object, list)
    failed = Signal(str)

    def __init__(self, chain, address: str, source: BlockscoutSource,
                 lists: TokenLists, store, parent=None):
        super().__init__(parent)
        self.chain = chain
        self.address = address
        self.source = source
        self.lists = lists
        self.store = store

    def run(self) -> None:
        try:
            # Native balance — best-effort; chain RPC may be flaky.
            try:
                native_wei = EthClient(self.chain).get_balance(self.address)
            except Exception:
                native_wei = 0

            tokens: list[TokenBalance] = []
            if self.source.supports(self.chain):
                cid = self.chain.chain_id
                for b in self.source.list_balances(self.chain, self.address):
                    if b.balance_raw <= 0:
                        continue
                    if self.store.is_hidden(cid, b.contract):
                        continue
                    if (self.lists.is_known(cid, b.contract)
                            or self.store.is_force_shown(cid, b.contract)):
                        tokens.append(b)
                tokens.sort(key=lambda x: x.balance, reverse=True)
            self.fetched.emit(native_wei, tokens)
        except Exception as e:
            self.failed.emit(str(e))


class BalanceWorker(QThread):
    """Refresh balances on-chain via Multicall3 + eth_getBalance.

    Depends only on the user's configured chain RPC, so it produces
    fresh numbers even when Blockscout and DefiLlama are both down.
    Emits ``refreshed(chain_id, native_wei, {addr_lower: balance_raw})``.
    """

    refreshed = Signal(int, object, dict)
    failed = Signal(str)

    def __init__(self, chain, address: str, token_contracts: list[str], parent=None):
        super().__init__(parent)
        self.chain = chain
        self.address = address
        self.contracts = list(token_contracts)

    def run(self) -> None:
        try:
            client = EthClient(self.chain)
            native = client.get_balance(self.address)
            balances = client.multicall_erc20_balances(self.contracts, self.address) if self.contracts else {}
            self.refreshed.emit(self.chain.chain_id, native, balances)
        except Exception as e:
            self.failed.emit(str(e))


class PricesWorker(QThread):
    """Fetch USD prices for the currently-displayed assets.

    Emits ``prices_ready(chain_id: int, prices: dict[str, Price])`` where
    the dict is keyed by lower-case ERC-20 address or ``""`` for native.
    Failures are silent — the value column simply stays empty.
    """

    prices_ready = Signal(int, dict)

    def __init__(self, source: PriceSource, chain, contracts: list[str],
                 include_native: bool, parent=None):
        super().__init__(parent)
        self.source = source
        self.chain = chain
        self.contracts = contracts
        self.include_native = include_native

    def run(self) -> None:
        try:
            prices = self.source.fetch(
                self.chain, self.contracts, include_native=self.include_native,
            )
        except Exception:
            prices = {}
        self.prices_ready.emit(self.chain.chain_id, prices)


def _format_usd(value: Decimal) -> str:
    if value <= 0:
        return ""
    if value < Decimal("0.01"):
        return "<$0.01"
    return f"${value:,.2f}"


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically by an associated Decimal,
    regardless of the formatted display text. Falls back to string compare
    against non-numeric peers so heterogeneous columns still sort sanely."""

    def __init__(self, text: str, value: Decimal):
        super().__init__(text)
        self._value = value

    def set_value(self, value: Decimal) -> None:
        self._value = value

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class TokenListPanel(QWidget):
    """Right pane: native + held ERC-20s for the currently-selected account.

    Native row pinned at index 0; ERC-20s below, sorted by balance. Each row
    carries (chain_id, contract_or_empty) in the Symbol cell's UserRole so
    the icon cache + context menu can find the right row to act on.
    """

    # User asked to hide a specific (chain_id, contract). Empty contract means
    # the native asset row was clicked (no-op for now — can't hide native).
    hide_requested = Signal(int, str)

    NATIVE_CONTRACT = ""  # sentinel for the native row

    DUST_USD_THRESHOLD = Decimal("0.01")

    def __init__(self, icon_cache: IconCache, store, parent=None):
        super().__init__(parent)
        self._icons = icon_cache
        self._icons.icon_ready.connect(self._on_icon_ready)
        self._store = store

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Symbol", "Balance", "Value (USD)", "Name"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QSize(20, 20))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.setSortingEnabled(True)
        # Default: by Value (USD) descending. setSortIndicator only sets the
        # arrow; the actual sort kicks in each time we toggle sortingEnabled
        # off-then-on around a populate/update cycle.
        h = self.table.horizontalHeader()
        h.setSortIndicator(2, Qt.DescendingOrder)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        v.addWidget(self.table, 1)

        # current chain (set by show_balances) — needed to scope icon lookups
        # and context-menu actions.
        self._chain_id: int | None = None

    # ---- displaying data -------------------------------------------------

    def show_loading(self, address: str) -> None:
        self.table.setRowCount(0)

    def show_balances(
        self,
        chain,
        native_wei: int,
        tokens: list[TokenBalance],
        list_entries: dict,    # (chain_id, addr_lower) -> TokenListEntry
    ) -> None:
        """Populate the table with the native asset on top, then ERC-20s."""
        self._chain_id = chain.chain_id
        # Disable sorting while populating; re-enabling at the end triggers
        # a single sort by the current header indicator.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        row_count = 1 + len(tokens)
        self.table.setRowCount(row_count)

        # Remember per-row Decimal balances so set_prices can multiply
        # without re-parsing the displayed text.
        self._balances: dict[tuple[int, str], Decimal] = {}

        # --- native row ---------------------------------------------------
        native_balance = wei_to_ether(native_wei)
        self._balances[(chain.chain_id, self.NATIVE_CONTRACT)] = native_balance
        sym = QTableWidgetItem(chain.symbol)
        sym.setData(Qt.UserRole, (chain.chain_id, self.NATIVE_CONTRACT))
        sym.setToolTip(f"Native {chain.symbol} on {chain.name}")
        bf = sym.font(); bf.setBold(True); sym.setFont(bf)
        native_pix = bundled_native_icon(chain.symbol)
        if native_pix is not None:
            sym.setIcon(QIcon(native_pix))
        bal = _NumericItem(f"{native_balance:.6g}", native_balance)
        bal.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bal.setFont(bf)
        val = _NumericItem("", Decimal(0))
        val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val.setFont(bf)
        name = QTableWidgetItem(chain.name)
        name.setFont(bf)
        self.table.setItem(0, 0, sym)
        self.table.setItem(0, 1, bal)
        self.table.setItem(0, 2, val)
        self.table.setItem(0, 3, name)

        # --- ERC-20 rows --------------------------------------------------
        for row, b in enumerate(tokens, start=1):
            key = (chain.chain_id, b.contract.lower())
            self._balances[key] = b.balance
            entry = list_entries.get(key)
            sym = QTableWidgetItem(b.symbol)
            sym.setData(Qt.UserRole, key)
            sym.setToolTip(b.contract)
            pix = self._icons.get(chain.chain_id, b.contract)
            if pix is not None:
                sym.setIcon(QIcon(pix))
            elif entry and entry.logo_uri:
                self._icons.request(chain.chain_id, b.contract, entry.logo_uri)
            bal = _NumericItem(f"{b.balance:.6g}", b.balance)
            bal.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val = _NumericItem("", Decimal(0))
            val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            name = QTableWidgetItem(b.name)
            self.table.setItem(row, 0, sym)
            self.table.setItem(row, 1, bal)
            self.table.setItem(row, 2, val)
            self.table.setItem(row, 3, name)

        self.table.setSortingEnabled(True)

    def show_cached(self, chain, cached: CachedWallet) -> None:
        """Render immediately from a cached wallet snapshot. Reuses the
        normal show_balances + set_prices code paths so display + sort +
        dust filter behave identically to a fresh fetch."""
        tokens: list[TokenBalance] = [
            TokenBalance(
                contract=t.contract, symbol=t.symbol, name=t.name,
                decimals=t.decimals, balance_raw=t.balance_raw,
            )
            for t in cached.tokens
        ]
        entries: dict = {}
        for t in cached.tokens:
            if t.logo_uri:
                entries[(chain.chain_id, t.contract.lower())] = TokenListEntry(
                    chain_id=chain.chain_id, address=t.contract.lower(),
                    symbol=t.symbol, name=t.name, decimals=t.decimals,
                    source="cache", logo_uri=t.logo_uri,
                )
        self.show_balances(chain, cached.native_balance_wei, tokens, entries)

        prices: dict = {}
        if cached.native_price_usd:
            prices[""] = Price(
                Decimal(cached.native_price_usd),
                cached.native_price_updated, "cache",
            )
        for t in cached.tokens:
            if t.price_usd:
                prices[t.contract.lower()] = Price(
                    Decimal(t.price_usd), t.price_updated, "cache",
                )
        self.set_prices(chain.chain_id, prices)

    def update_balances_if_set_unchanged(
        self,
        chain,
        native_wei: int,
        tokens: list[TokenBalance],
    ) -> bool:
        """If the displayed contract set matches the new fetch, update
        balance cells in place (no table rebuild → no flicker). Returns
        False to tell the caller "fall back to show_balances rebuild"
        when contracts changed (token added/removed)."""
        if self._chain_id != chain.chain_id:
            return False
        expected = {(chain.chain_id, self.NATIVE_CONTRACT)} | {
            (chain.chain_id, b.contract.lower()) for b in tokens
        }
        if set(self._balances.keys()) != expected:
            return False
        self.table.setSortingEnabled(False)
        native_balance = wei_to_ether(native_wei)
        self._balances[(chain.chain_id, self.NATIVE_CONTRACT)] = native_balance
        by_addr = {b.contract.lower(): b for b in tokens}
        for row in range(self.table.rowCount()):
            sym = self.table.item(row, 0)
            if sym is None:
                continue
            key = sym.data(Qt.UserRole)
            if not key:
                continue
            _, addr = key
            bal_cell = self.table.item(row, 1)
            if bal_cell is None:
                continue
            if addr == self.NATIVE_CONTRACT:
                value = native_balance
            else:
                b = by_addr.get(addr)
                if b is None:
                    continue
                value = b.balance
                self._balances[key] = value
            bal_cell.setText(f"{value:.6g}")
            if isinstance(bal_cell, _NumericItem):
                bal_cell.set_value(value)
        self.table.setSortingEnabled(True)
        return True

    def show_error(self, msg: str) -> None:
        self.table.setRowCount(0)
        self._chain_id = None
        self._balances = {}

    def show_message(self, msg: str) -> None:
        self.table.setRowCount(0)
        self._chain_id = None
        self._balances = {}

    def clear(self) -> None:
        self.table.setRowCount(0)
        self._chain_id = None
        self._balances = {}

    def _remember_prices(self, prices: dict) -> None:
        if not hasattr(self, "_prices_state"):
            self._prices_state: dict = {}
        for k, v in prices.items():
            self._prices_state[k] = v

    def reapply_prices(self) -> None:
        """Recompute the Value column using the most recent prices we have.

        Called after balance-only refreshes so the value cells aren't stale
        relative to the new balances."""
        if self._chain_id is None:
            return
        cached_prices = getattr(self, "_prices_state", {}) or {}
        if cached_prices:
            self.set_prices(self._chain_id, cached_prices)

    def set_prices(self, chain_id: int, prices: dict) -> None:
        """Populate the Value (USD) column from a {addr_lower: Price} dict
        and hide rows whose value falls below the dust threshold.

        Visibility rules:
        - Native row: always shown.
        - Force-shown ERC-20 (user override): always shown.
        - Priced ERC-20 with value < DUST_USD_THRESHOLD: hidden.
        - ERC-20 with no price quote: hidden (treated as zero — if it
          mattered, the user can force-show it).

        Sorting is suspended while we mutate cells, then re-enabled so the
        table re-sorts once by the current header indicator (Value desc by
        default; whatever the user clicked otherwise)."""
        if self._chain_id != chain_id:
            return
        self._remember_prices(prices)
        self.table.setSortingEnabled(False)
        for row in range(self.table.rowCount()):
            sym = self.table.item(row, 0)
            if sym is None:
                continue
            key = sym.data(Qt.UserRole)
            if not key:
                continue
            cid, addr = key
            is_native = (addr == self.NATIVE_CONTRACT)

            balance = self._balances.get(key)
            price = prices.get(addr)  # native lives under ""
            cell = self.table.item(row, 2)
            value: Decimal | None = None
            if cell is not None and balance is not None and price is not None:
                value = balance * price.price_usd
                cell.setText(_format_usd(value))
                if isinstance(cell, _NumericItem):
                    cell.set_value(value)

            # Dust hiding (display-time; doesn't touch the worker data).
            # Show only: native, user-force-shown, or priced above dust.
            if (is_native
                    or self._store.is_force_shown(cid, addr)
                    or (value is not None and value >= self.DUST_USD_THRESHOLD)):
                self.table.setRowHidden(row, False)
            else:
                self.table.setRowHidden(row, True)
        self.table.setSortingEnabled(True)

    # ---- icon refresh ---------------------------------------------------

    def _on_icon_ready(self, chain_id: int, contract: str) -> None:
        if self._chain_id != chain_id:
            return
        pix = self._icons.get(chain_id, contract)
        if pix is None:
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            cid, addr = item.data(Qt.UserRole) or (None, None)
            if cid == chain_id and addr == contract.lower():
                item.setIcon(QIcon(pix))
                break

    # ---- context menu ---------------------------------------------------

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        sym_item = self.table.item(item.row(), 0)
        if sym_item is None:
            return
        meta = sym_item.data(Qt.UserRole)
        if not meta:
            return
        cid, addr = meta
        if addr == self.NATIVE_CONTRACT:
            return  # native asset can't be hidden
        menu = QMenu(self)
        act_hide = menu.addAction(f"Hide {sym_item.text()}")
        act_copy = menu.addAction("Copy contract address")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_hide:
            self.hide_requested.emit(cid, addr)
        elif chosen is act_copy:
            QApplication.clipboard().setText(addr)


# --- Main window -------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, store, rpc):
        super().__init__()
        self.store = store
        self.rpc = rpc
        self.setWindowTitle("qeth — Ethereum wallet")
        self.resize(1200, 720)

        # Token discovery + curated whitelist (lists load in background).
        self._token_source = BlockscoutSource()
        self._token_lists = TokenLists()
        self._token_worker: TokenListWorker | None = None
        self._icon_cache = IconCache(self)
        self._price_source = DefiLlamaPrices()
        self._prices_worker: PricesWorker | None = None
        self._wallet_cache = WalletCache()
        # Stashed after a successful balance fetch so the post-price callback
        # has the data it needs to write the wallet cache.
        self._pending_save: dict | None = None

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._rebuild_tree()

        self._lists_loader = TokenListsLoader(self._token_lists)
        self._lists_loader.loaded.connect(self._on_lists_loaded)
        self._lists_loader.failed.connect(
            lambda e: self.token_panel.show_message(f"Token lists failed: {e}")
        )
        self.token_panel.show_message("Loading token lists…")
        self._lists_loader.start()
        self._refresh_status()

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        act_add = QAction(
            QIcon.fromTheme("document-new", self.style().standardIcon(QStyle.SP_FileIcon)),
            "Add account",
            self,
        )
        act_add.triggered.connect(self._add_ledger)
        tb.addAction(act_add)
        tb.addSeparator()

        self.act_copy = QAction(
            QIcon.fromTheme("edit-copy", self.style().standardIcon(QStyle.SP_DialogSaveButton)),
            "Copy address",
            self,
        )
        self.act_copy.setEnabled(False)
        self.act_copy.triggered.connect(self._copy_selected_address)
        tb.addAction(self.act_copy)
        tb.addSeparator()

        self.act_remove = QAction(
            QIcon.fromTheme("list-remove",
                            self.style().standardIcon(QStyle.SP_TrashIcon)),
            "Remove account",
            self,
        )
        self.act_remove.setEnabled(False)
        self.act_remove.triggered.connect(self._remove_selected_account)
        tb.addAction(self.act_remove)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        tb.addWidget(QLabel("Chain: "))
        self.chain_combo = QComboBox()
        self.chain_combo.setIconSize(QSize(18, 18))
        for c in self.store.chains:
            label = f"{c.name} ({c.chain_id})"
            pix = bundled_chain_icon(c.chain_id)
            if pix is not None:
                self.chain_combo.addItem(QIcon(pix), label, c.chain_id)
            else:
                self.chain_combo.addItem(label, c.chain_id)
        idx = self.chain_combo.findData(self.store.current_chain_id)
        if idx >= 0:
            self.chain_combo.setCurrentIndex(idx)
        self.chain_combo.currentIndexChanged.connect(self._on_chain_changed)
        tb.addWidget(self.chain_combo)

    def _build_central(self) -> None:
        outer = QSplitter(Qt.Horizontal)

        # Left half: tree on top, account details on bottom.
        left = QSplitter(Qt.Vertical)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Accounts"])
        self.tree.setRootIsDecorated(True)
        self.tree.setTextElideMode(Qt.ElideMiddle)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        left.addWidget(self.tree)

        self.details = DetailsPanel()
        self.details.set_default_requested.connect(self._set_default)
        details_wrap = QFrame()
        details_wrap.setFrameShape(QFrame.StyledPanel)
        dlay = QVBoxLayout(details_wrap)
        dlay.setContentsMargins(12, 12, 12, 12)
        dlay.addWidget(self.details)
        left.addWidget(details_wrap)
        left.setStretchFactor(0, 1)
        left.setStretchFactor(1, 1)
        left.setSizes([320, 400])

        outer.addWidget(left)

        # Right half: token list for the currently-selected account.
        self.token_panel = TokenListPanel(self._icon_cache, self.store)
        self.token_panel.hide_requested.connect(self._on_hide_token)
        outer.addWidget(self.token_panel)

        outer.setStretchFactor(0, 1)
        outer.setStretchFactor(1, 1)
        outer.setSizes([520, 680])

        self.setCentralWidget(outer)

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.rpc_label = QLabel()
        self.default_label = QLabel()
        sb.addWidget(self.rpc_label, 1)
        sb.addPermanentWidget(self.default_label)

    def _rebuild_tree(self) -> None:
        self.tree.clear()
        ledger_accts = [a for a in self.store.accounts if a.get("source") == "ledger"]
        ledger_root = QTreeWidgetItem([f"Ledger ({len(ledger_accts)})"])
        self.tree.addTopLevelItem(ledger_root)
        groups: dict[str, QTreeWidgetItem] = {}
        default_item: QTreeWidgetItem | None = None
        for a in ledger_accts:
            scheme = a.get("scheme", "Custom")
            grp = groups.get(scheme)
            if grp is None:
                grp = QTreeWidgetItem([scheme])
                ledger_root.addChild(grp)
                groups[scheme] = grp
            addr = a["address"]
            is_default = (
                self.store.default_account is not None
                and addr.lower() == self.store.default_account.lower()
            )
            label = f"[{addr}]" if is_default else f" {addr} "
            it = QTreeWidgetItem([label])
            it.setData(0, Qt.UserRole, addr)
            it.setFont(0, QFont("monospace"))
            grp.addChild(it)
            if is_default:
                default_item = it
        ledger_root.setExpanded(True)
        for g in groups.values():
            g.setExpanded(True)

        # Stubs for the future
        self.tree.addTopLevelItem(QTreeWidgetItem(["Hot wallet (0)"]))
        self.tree.addTopLevelItem(QTreeWidgetItem(["Watch only (0)"]))

        if default_item is not None:
            self.tree.setCurrentItem(default_item)

    def _refresh_status(self) -> None:
        err = self.rpc.error
        if err:
            self.rpc_label.setText(f"JSON-RPC: error — {err}")
        else:
            self.rpc_label.setText(
                f"JSON-RPC: http://{self.rpc.host}:{self.rpc.port}   (ws on same port)"
            )
        addr = self.store.default_account or "none"
        self.default_label.setText(
            f"Default: {addr}   •   {self.store.current_chain().name}"
        )

    def _selected_addresses(self) -> list[str]:
        out = []
        for it in self.tree.selectedItems():
            addr = it.data(0, Qt.UserRole)
            if addr:
                out.append(addr)
        return out

    def _on_tree_selection(self) -> None:
        addrs = self._selected_addresses()
        # Copy only makes sense for a single address; Remove handles many.
        self.act_copy.setEnabled(len(addrs) == 1)
        self.act_remove.setEnabled(len(addrs) >= 1)
        if len(addrs) == 1:
            acct = next((a for a in self.store.accounts if a["address"] == addrs[0]), None)
            if acct:
                self.details.show_account(
                    acct, is_default=(addrs[0] == self.store.default_account)
                )
                self._refresh_tokens(addrs[0])
                return
        self.details.clear()
        self.token_panel.clear()

    def _refresh_tokens(self, address: str) -> None:
        chain = self.store.current_chain()
        cached = self._wallet_cache.load(chain.chain_id, address)
        if cached is not None:
            # Immediate render from cache; no flicker while we refresh.
            self.token_panel.show_cached(chain, cached)
            # Independently refresh balances on-chain (multicall + native).
            # This works even when Blockscout/DefiLlama are down, so cached
            # balances stay current as long as the chain RPC works.
            bw = BalanceWorker(
                chain, address,
                [t.contract for t in cached.tokens],
            )
            bw.refreshed.connect(self._on_balance_refresh)
            bw.start()
            # Hold a ref so it isn't GC'd before finishing.
            self._balance_worker = bw

        if not self._token_lists.loaded:
            if cached is None:
                self.token_panel.show_message(
                    "Loading token lists… selection will refresh automatically"
                )
            return

        # Cancel any in-flight worker (best-effort; QThread won't truly stop).
        self._token_worker = TokenListWorker(
            chain, address, self._token_source, self._token_lists, self.store,
        )

        def on_fetched(native_wei: int, tokens: list) -> None:
            entries = {}
            for b in tokens:
                e = self._token_lists.get(chain.chain_id, b.contract)
                if e is not None:
                    entries[(chain.chain_id, b.contract.lower())] = e
            # If the displayed token set is unchanged (common case: revisit
            # an account with no new airdrops since last view) we can update
            # balance cells in place and avoid a table rebuild flicker.
            if not self.token_panel.update_balances_if_set_unchanged(
                chain, native_wei, tokens
            ):
                self.token_panel.show_balances(chain, native_wei, tokens, entries)
            # Stash for cache save after prices land.
            self._pending_save = {
                "chain": chain, "address": address, "native_wei": native_wei,
                "tokens": tokens, "entries": entries,
            }
            self._prices_worker = PricesWorker(
                self._price_source, chain,
                [b.contract for b in tokens],
                include_native=True,
            )
            self._prices_worker.prices_ready.connect(self._on_prices_ready)
            self._prices_worker.start()

        def on_failed(msg: str) -> None:
            # Blockscout failed; keep the cache view rather than wiping it.
            if cached is None:
                self.token_panel.show_error(msg)

        self._token_worker.fetched.connect(on_fetched)
        self._token_worker.failed.connect(on_failed)
        if cached is None:
            self.token_panel.show_loading(address)
        self._token_worker.start()

    def _on_balance_refresh(self, chain_id: int, native_wei, balances_raw: dict) -> None:
        """Apply on-chain balance refresh to the panel. Needs the cached
        token metadata (decimals, symbol, name) to construct TokenBalance
        objects for the in-place update."""
        chain = self.store.current_chain()
        if chain.chain_id != chain_id:
            return
        addrs = self._selected_addresses()
        if len(addrs) != 1:
            return
        cached = self._wallet_cache.load(chain_id, addrs[0])
        if cached is None:
            return
        tokens = []
        for t in cached.tokens:
            raw = balances_raw.get(t.contract.lower(), t.balance_raw)
            tokens.append(TokenBalance(
                contract=t.contract, symbol=t.symbol, name=t.name,
                decimals=t.decimals, balance_raw=int(raw),
            ))
        if self.token_panel.update_balances_if_set_unchanged(chain, native_wei, tokens):
            # Re-multiply by last-known prices so the Value column tracks
            # the new balances even before DefiLlama refreshes.
            self.token_panel.reapply_prices()

    def _on_prices_ready(self, chain_id: int, prices: dict) -> None:
        self.token_panel.set_prices(chain_id, prices)
        ps = self._pending_save
        if not ps or ps["chain"].chain_id != chain_id:
            return
        self._pending_save = None
        self._save_wallet_cache(
            ps["chain"], ps["address"], ps["native_wei"],
            ps["tokens"], prices, ps["entries"],
        )

    def _save_wallet_cache(
        self, chain, address: str, native_wei: int,
        tokens: list, prices: dict, entries: dict,
    ) -> None:
        import time
        from .wallet_cache import CachedToken
        now = int(time.time())
        cached = CachedWallet(
            chain_id=chain.chain_id,
            address=address.lower(),
            native_balance_wei=int(native_wei),
            native_balance_updated=now,
        )
        np = prices.get("")
        if np is not None:
            cached.native_price_usd = str(np.price_usd)
            cached.native_price_updated = np.timestamp or now

        # Only cache what the panel would actually display (passes dust /
        # force-show filter). Sort by USD value desc so the on-disk order
        # matches the displayed order.
        from decimal import Decimal as D
        kept: list[tuple[D, CachedToken]] = []
        for b in tokens:
            addr = b.contract.lower()
            price = prices.get(addr)
            entry = entries.get((chain.chain_id, addr))
            force = self.store.is_force_shown(chain.chain_id, addr)
            value = b.balance * price.price_usd if price else D(0)
            if not force and (price is None or value < TokenListPanel.DUST_USD_THRESHOLD):
                continue
            kept.append((value, CachedToken(
                contract=addr, symbol=b.symbol, name=b.name,
                decimals=b.decimals,
                logo_uri=entry.logo_uri if entry else None,
                balance_raw=int(b.balance_raw),
                price_usd=str(price.price_usd) if price else None,
                balance_updated=now,
                price_updated=price.timestamp if price else 0,
            )))
        kept.sort(key=lambda kv: kv[0], reverse=True)
        cached.tokens = [t for _, t in kept]
        self._wallet_cache.save(cached)

    def _on_hide_token(self, chain_id: int, contract: str) -> None:
        self.store.hide_token(chain_id, contract)
        # Re-fetch for the currently-selected account so the row disappears.
        addrs = self._selected_addresses()
        if len(addrs) == 1:
            self._refresh_tokens(addrs[0])

    def _on_lists_loaded(self) -> None:
        n = self._token_lists.count()
        self.statusBar().showMessage(
            f"Token lists loaded ({n} known tokens)", 3000
        )
        # If a single account is already selected, fetch its tokens now.
        addrs = self._selected_addresses()
        if len(addrs) == 1:
            self._refresh_tokens(addrs[0])
        else:
            self.token_panel.clear()

    def _copy_selected_address(self) -> None:
        addrs = self._selected_addresses()
        if len(addrs) != 1:
            return
        QApplication.clipboard().setText(addrs[0])
        self.statusBar().showMessage(f"Copied {addrs[0]} to clipboard", 3000)

    def _remove_selected_account(self) -> None:
        addrs = self._selected_addresses()
        if not addrs:
            return
        if len(addrs) == 1:
            prompt = f"Remove {addrs[0]} from this wallet?"
        else:
            preview = "\n".join(f"  • {a}" for a in addrs[:5])
            extra = f"\n  … and {len(addrs) - 5} more" if len(addrs) > 5 else ""
            prompt = f"Remove {len(addrs)} accounts from this wallet?\n\n{preview}{extra}"
        reply = QMessageBox.question(
            self,
            "Remove account" if len(addrs) == 1 else "Remove accounts",
            f"{prompt}\n\n"
            "Keys on your Ledger are untouched; this only forgets the "
            "addresses locally. You can re-add them via Scan at any time.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        removed = sum(1 for a in addrs if self.store.remove_account(a))
        if removed:
            self._rebuild_tree()
            self._refresh_status()
            self.statusBar().showMessage(f"Removed {removed} account(s)", 3000)

    def _add_ledger(self) -> None:
        dlg = AddLedgerDialog(self.store.current_chain(), self)
        if dlg.exec() != QDialog.Accepted:
            return
        scheme = dlg.scheme_combo.currentText()
        added = 0
        for d in dlg.selected_accounts():
            if self.store.add_account({
                "address": d.address,
                "path": d.path,
                "source": "ledger",
                "scheme": scheme,
                "label": "",
            }):
                added += 1
        self._rebuild_tree()
        self._refresh_status()
        if added:
            self.statusBar().showMessage(f"Added {added} account(s)", 3000)

    def _on_chain_changed(self, idx: int) -> None:
        cid = self.chain_combo.itemData(idx)
        if cid is not None:
            self.store.set_current_chain(int(cid))
            self._refresh_status()
            addrs = self._selected_addresses()
            if len(addrs) == 1:
                self._refresh_tokens(addrs[0])

    def _set_default(self, address: str) -> None:
        self.store.set_default_account(address)
        self._rebuild_tree()
        self._refresh_status()
        self._on_tree_selection()
