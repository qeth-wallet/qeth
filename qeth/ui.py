import datetime
import io
import logging

import segno

from PySide6.QtCore import Qt, QByteArray, QSize, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QStatusBar, QStyle, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from decimal import Decimal

from .chain import wei_to_ether
from .icons import IconCache, bundled_chain_icon, bundled_native_icon
from .ledger import DiscoveredAccount, LedgerWorker, PATH_SCHEMES
from .plugin import Slot
from .prices import Price
from .risk import RiskCache
from .tokenlists import TokenListEntry, TokenLists
from .tokens import TokenBalance
from .tokens_plugin import TokensPlugin
from .transactions import Transaction, TxDirection
from .transactions_plugin import TransactionsPlugin
from .wallet_cache import CachedWallet
from .wallets_plugin import WalletsPlugin


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

        # Workers tracked here so they aren't garbage-collected while still
        # running (Qt's QThread destructor aborts the process if it is).
        self._workers: set[QThread] = set()

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
        worker = LedgerWorker(
            self.scheme_combo.currentText(), n, chain=self._chain
        )
        worker.discovered.connect(self._on_found)
        worker.finished_ok.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        self._workers.add(worker)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

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
        # No bottom margin so the Set-as-default button can sit flush with
        # the bottom of the splitter (matches the right panel's bottom edge).
        v.setContentsMargins(9, 9, 9, 0)
        self.title = QLabel("Select an account on the left")
        f = self.title.font(); f.setPointSize(f.pointSize() + 2); f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)

        form = QFormLayout()
        mono = QFont("monospace")
        # Ignored size policy on the long monospace labels: their sizeHint
        # (full 42-char address etc.) shouldn't pin the panel's minimum
        # width, otherwise the whole window can't be shrunk down.
        self.address_lbl = QLabel("—"); self.address_lbl.setFont(mono)
        self.address_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.address_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.path_lbl = QLabel("—"); self.path_lbl.setFont(mono)
        self.path_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.source_lbl = QLabel("—")
        self.scheme_lbl = QLabel("—")
        form.addRow("Address:", self.address_lbl)
        form.addRow("Path:", self.path_lbl)
        form.addRow("Source:", self.source_lbl)
        form.addRow("Scheme:", self.scheme_lbl)
        v.addLayout(form)

        # Vertical breathing room above + below the QR — without these,
        # the form rows / button crowd right up against it and the panel
        # looks squeezed.
        v.addSpacing(12)
        self.qr_lbl = QLabel()
        self.qr_lbl.setAlignment(Qt.AlignCenter)
        self.qr_lbl.setFixedSize(220, 220)
        v.addWidget(self.qr_lbl, 0, Qt.AlignCenter)
        v.addSpacing(12)

        # Short label + tooltip rather than a wide button — keeps the
        # panel narrow-shrinkable. Same policy trick on the button itself.
        self.set_default_btn = QPushButton("Set as default")
        self.set_default_btn.setToolTip(
            "Make this the address dapps see (returned by eth_accounts)"
        )
        self.set_default_btn.setEnabled(False)
        # Pin the height. With QSizePolicy.Fixed Qt re-queries sizeHint()
        # every time the text changes — and "Default ✓" can come out a
        # touch shorter than "Set as default" depending on the theme. The
        # snapshot here is taken while the (longer) text is set, so the
        # disabled state never shrinks.
        self.set_default_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_default_btn.setMinimumHeight(self.set_default_btn.sizeHint().height())
        self.set_default_btn.clicked.connect(
            lambda: self._current and self.set_default_requested.emit(self._current)
        )
        # Stretch pushes the button to the very bottom of the panel.
        v.addStretch(1)
        v.addWidget(self.set_default_btn)
        self._current: str | None = None

    def show_account(self, account: dict, is_default: bool) -> None:
        self._current = account["address"]
        self.title.setText(account.get("label") or "Account")
        self.address_lbl.setText(account["address"])
        self.path_lbl.setText(account.get("path", "—"))
        self.source_lbl.setText(account.get("source", "—"))
        self.scheme_lbl.setText(account.get("scheme", "—"))
        self.set_default_btn.setEnabled(not is_default)
        self.set_default_btn.setText("Default ✓" if is_default else "Set as default")
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
        self.set_default_btn.setText("Set as default")


# --- Token list panel -------------------------------------------------------
#
# The six token-related QThread workers (TokenListsLoader, TokenListWorker,
# RiskWorker, MetadataWorker, BalanceWorker, PricesWorker) used to live
# here. They moved into qeth.tokens_plugin as part of the plugin refactor
# (step 3) — they're token-domain code, owned by TokensPlugin now.


def _is_scam_via_lists(lists, chain_id: int, b: TokenBalance,
                       risk_cache=None) -> bool:
    risk = None
    if risk_cache is not None:
        risk = risk_cache.get(chain_id, b.contract)
    return lists.is_likely_scam(chain_id, b.contract, b.symbol, b.name, risk=risk)


# Pure formatting helpers live in qeth.formatting so they can be unit-
# tested without dragging in PySide6. Aliased here under their old
# private names to keep the rest of this module unchanged.
from .formatting import format_balance as _format_balance
from .formatting import format_relative_time as _format_relative_time
from .formatting import format_usd as _format_usd
from .formatting import short_addr as _short_addr


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
    # User wants to add a custom token by contract address.
    add_custom_requested = Signal()
    # User wants to pin (force-show) the currently-selected token.
    pin_requested = Signal(int, str)
    # User toggled the "show all" view (no dust, no hide-list — only scams
    # still hidden).
    show_all_toggled = Signal(bool)

    NATIVE_CONTRACT = ""  # sentinel for the native row

    DUST_USD_THRESHOLD = Decimal("0.01")

    def __init__(self, icon_cache: IconCache, store, parent=None):
        super().__init__(parent)
        self._icons = icon_cache
        self._icons.icon_ready.connect(self._on_icon_ready)
        self._store = store

        v = QVBoxLayout(self)
        # No top margin so the table header aligns with the tree header on
        # the left side (which sits directly in the splitter, no wrapping).
        v.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Symbol", "Balance", "Value (USD)", "Name"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        # Disable focus rectangle on the table — many themes draw a 1px
        # focus border on the current cell which shifts contents on
        # hover/click. Selection still works without it.
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(False)
        # Pin padding + border explicitly for every state so no theme can
        # add an on-hover/on-selected border that shifts the text by 1px.
        # Selection still highlights the whole row (SelectRows + the rule
        # below). Hover style is set to match default so it produces no
        # visible change.
        self.table.setStyleSheet(
            "QTableView::item {"
            "  padding: 3px 6px;"
            "  border: 0;"
            "}"
            "QTableView::item:hover { background: transparent; }"
            "QTableView::item:selected,"
            "QTableView::item:selected:hover {"
            "  background: palette(highlight);"
            "  color: palette(highlighted-text);"
            "}"
        )
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

        # The +/-/★/👁 buttons are owned by this panel (so they can hook
        # into table selection and the panel's signals) but NOT added to
        # its layout — MainWindow places them on the shared bottom-right
        # action row alongside the chain selector, so we don't waste two
        # rows on what fits in one. ``action_widgets()`` exposes them.
        style = self.style()
        self.btn_add = QPushButton()
        self.btn_add.setIcon(QIcon.fromTheme("list-add",
                                             style.standardIcon(QStyle.SP_FileDialogNewFolder)))
        self.btn_add.setToolTip("Add a custom token by contract address")

        self.btn_hide = QPushButton()
        self.btn_hide.setIcon(QIcon.fromTheme("list-remove",
                                              style.standardIcon(QStyle.SP_TrashIcon)))
        self.btn_hide.setToolTip("Hide selected token from this wallet")
        self.btn_hide.setEnabled(False)

        self.btn_pin = QPushButton()
        _pin_icon = QIcon.fromTheme("emblem-favorite",
                                    QIcon.fromTheme("starred"))
        if _pin_icon.isNull() or not _pin_icon.availableSizes():
            self.btn_pin.setText("★")    # Unicode star, reliable on any system
        else:
            self.btn_pin.setIcon(_pin_icon)
        self.btn_pin.setToolTip(
            "Pin selected token: always show it, even at zero balance "
            "or below the dust threshold"
        )
        self.btn_pin.setEnabled(False)

        self.btn_show_all = QPushButton()
        _eye_icon = QIcon.fromTheme("view-visible",
                                    QIcon.fromTheme("eye-symbolic"))
        if _eye_icon.isNull() or not _eye_icon.availableSizes():
            self.btn_show_all.setText("👁")  # Unicode eye fallback
        else:
            self.btn_show_all.setIcon(_eye_icon)
        self.btn_show_all.setToolTip(
            "Show all tokens (including dust and hidden); suspected "
            "scams stay hidden"
        )
        self.btn_show_all.setCheckable(True)

        for b in (self.btn_add, self.btn_hide, self.btn_pin, self.btn_show_all):
            b.setFlat(True)
            b.setMaximumSize(28, 28)
            b.setIconSize(QSize(16, 16))
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.btn_add.clicked.connect(self.add_custom_requested.emit)
        self.btn_hide.clicked.connect(
            lambda: self._emit_for_selected(self.hide_requested)
        )
        self.btn_pin.clicked.connect(
            lambda: self._emit_for_selected(self.pin_requested)
        )
        self.btn_show_all.toggled.connect(self.show_all_toggled.emit)
        self.table.itemSelectionChanged.connect(self._update_action_buttons)

        # current chain (set by show_balances) — needed to scope icon lookups
        # and context-menu actions.
        self._chain_id: int | None = None
        # Set externally by MainWindow so we can mark scams with an alarm
        # icon and short-circuit `is_likely_scam` against the curated lists.
        self._token_lists: "TokenLists | None" = None
        # Same — MainWindow injects so the alarm icon also reflects GoPlus
        # high-risk verdicts (honeypot / hidden owner / >50% sell tax).
        self._risk_cache: "RiskCache | None" = None

    def action_widgets(self) -> list[QWidget]:
        """The +/-/★/👁 buttons, in display order. MainWindow mounts them
        on the shared bottom-right row beside the chain selector."""
        return [self.btn_add, self.btn_hide, self.btn_pin, self.btn_show_all]

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
        """Populate the table with the native asset on top, then ERC-20s.

        Caller is responsible for wrapping this (and any subsequent
        set_prices) in ``setUpdatesEnabled(False/True)`` when invoked
        during a refresh — otherwise the row-count change can produce a
        brief blank frame between rows being resized and cells being
        re-populated. See ``render_full`` for the safe one-shot helper.
        """
        self._chain_id = chain.chain_id
        # Disable sorting while populating; re-enabling at the end triggers
        # a single sort by the current header indicator.
        self.table.setSortingEnabled(False)
        # Set the new row count directly. If smaller than current, Qt
        # truncates from the bottom; if larger, it appends empty rows. Old
        # cells in surviving rows persist until we overwrite them just
        # below, avoiding the all-blank moment that setRowCount(0) would
        # cause. Callers that need to fully replace contents still get
        # correct results.
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
        bal = _NumericItem(_format_balance(native_balance), native_balance)
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
        alarm_icon = self.style().standardIcon(QStyle.SP_MessageBoxWarning)
        for row, b in enumerate(tokens, start=1):
            key = (chain.chain_id, b.contract.lower())
            self._balances[key] = b.balance
            entry = list_entries.get(key)
            sym = QTableWidgetItem(b.symbol)
            sym.setData(Qt.UserRole, key)
            sym.setToolTip(b.contract)
            # Mark suspected scams with an alarm. Most of the time these
            # don't reach the panel at all (filtered upstream); the case
            # that survives is "user force-shows a contract that fails
            # the heuristic" — exactly when a warning is most useful.
            scam = (
                self._token_lists is not None
                and _is_scam_via_lists(
                    self._token_lists, chain.chain_id, b,
                    risk_cache=self._risk_cache,
                )
            )
            if scam:
                sym.setIcon(alarm_icon)
                sym.setToolTip(b.contract + "\n⚠ Looks like a scam token")
            else:
                pix = self._icons.get(chain.chain_id, b.contract)
                if pix is not None:
                    sym.setIcon(QIcon(pix))
                elif entry and entry.logo_uri:
                    self._icons.request(chain.chain_id, b.contract, entry.logo_uri)
            bal = _NumericItem(_format_balance(b.balance), b.balance)
            bal.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val = _NumericItem("", Decimal(0))
            val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            name = QTableWidgetItem(b.name)
            self.table.setItem(row, 0, sym)
            self.table.setItem(row, 1, bal)
            self.table.setItem(row, 2, val)
            self.table.setItem(row, 3, name)

        self.table.setSortingEnabled(True)

    def render_full(self, chain, native_wei: int, tokens: list[TokenBalance],
                    entries: dict, prices: dict,
                    apply_dust_filter: bool = True) -> None:
        """Atomic full re-render: rows + balances + prices in one go,
        with paint events suspended so the user never sees an
        intermediate blank or pre-price-filter state.

        ``apply_dust_filter`` is forwarded to set_prices — spotlight
        mode passes False so zero-value / unpriced rows stay visible.
        """
        self.setUpdatesEnabled(False)
        try:
            self.show_balances(chain, native_wei, tokens, entries)
            self.set_prices(chain.chain_id, prices,
                            apply_dust_filter=apply_dust_filter)
        finally:
            self.setUpdatesEnabled(True)

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
        self.render_full(chain, cached.native_balance_wei, tokens, entries, prices)

    def contract_set_matches(self, chain, tokens: list[TokenBalance]) -> bool:
        """True if the panel currently displays exactly the contract set
        described by ``tokens`` (plus the native row). Lets callers decide
        between in-place updates and rebuilds without mutating cells."""
        if self._chain_id != chain.chain_id:
            return False
        expected = {(chain.chain_id, self.NATIVE_CONTRACT)} | {
            (chain.chain_id, b.contract.lower()) for b in tokens
        }
        return set(self._balances.keys()) == expected

    def update_balances_if_set_unchanged(
        self,
        chain,
        native_wei: int,
        tokens: list[TokenBalance],
    ) -> bool:
        """If the displayed contract set matches the new fetch, update
        balance cells in place. Only cells whose value actually differs
        are mutated, and the sort toggle is skipped entirely when nothing
        changed — both to prevent gratuitous repaints/reorders that the
        user perceives as flicker. Returns False to tell the caller
        "fall back to show_balances rebuild" when contracts changed."""
        if not self.contract_set_matches(chain, tokens):
            return False
        new_native = wei_to_ether(native_wei)
        by_addr = {b.contract.lower(): b for b in tokens}

        # First pass: collect what would change without mutating anything.
        changes: list[tuple[int, tuple[int, str], Decimal]] = []
        for row in range(self.table.rowCount()):
            sym = self.table.item(row, 0)
            if sym is None:
                continue
            key = sym.data(Qt.UserRole)
            if not key:
                continue
            _, addr = key
            if addr == self.NATIVE_CONTRACT:
                new_value = new_native
            else:
                b = by_addr.get(addr)
                if b is None:
                    continue
                new_value = b.balance
            if self._balances.get(key) != new_value:
                changes.append((row, key, new_value))

        if not changes:
            # Set matches, all balances identical to what's already shown.
            # Touch nothing, don't toggle sort.
            return True

        self.table.setSortingEnabled(False)
        for row, key, value in changes:
            self._balances[key] = value
            bal_cell = self.table.item(row, 1)
            if bal_cell is None:
                continue
            bal_cell.setText(_format_balance(value))
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
        relative to the new balances. Crucially: does NOT re-apply the
        dust filter. The dust check is only meaningful with fresh DefiLlama
        prices (which only arrive at combined-update time); re-running it
        here against stale cached prices would just oscillate borderline
        tokens between visible/hidden."""
        if self._chain_id is None:
            return
        cached_prices = getattr(self, "_prices_state", {}) or {}
        if cached_prices:
            self.set_prices(self._chain_id, cached_prices, apply_dust_filter=False)

    def set_prices(self, chain_id: int, prices: dict, apply_dust_filter: bool = True) -> None:
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
        # Short-circuit when the price values match the last call —
        # nothing in the table would change, so don't even toggle sort
        # (which causes Qt to repaint visible rows even when no order
        # change is needed).
        stored = getattr(self, "_prices_state", None) or {}
        if stored and set(stored.keys()) == set(prices.keys()) and all(
            stored[k].price_usd == prices[k].price_usd for k in prices
        ):
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

            if apply_dust_filter:
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

    # ---- action buttons --------------------------------------------------

    def _selected_token(self) -> tuple[int, str] | None:
        """``(chain_id, contract_lower)`` of the currently-selected ERC-20
        row, or None when nothing is selected / the native row is."""
        items = self.table.selectedItems()
        if not items:
            return None
        sym = self.table.item(items[0].row(), 0)
        if sym is None:
            return None
        key = sym.data(Qt.UserRole)
        if not key or key[1] == self.NATIVE_CONTRACT:
            return None
        return key

    def _emit_for_selected(self, sig) -> None:
        sel = self._selected_token()
        if sel:
            sig.emit(sel[0], sel[1])

    def _update_action_buttons(self) -> None:
        enabled = self._selected_token() is not None
        self.btn_hide.setEnabled(enabled)
        self.btn_pin.setEnabled(enabled)

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


# --- Transaction history panel + worker -------------------------------------

# Light decoding for the most common selectors. Anything not here renders
# as the raw 10-char selector — better than guessing wrong on a name.
KNOWN_SELECTORS: dict[str, str] = {
    "0xa9059cbb": "transfer",
    "0x23b872dd": "transferFrom",
    "0x095ea7b3": "approve",
    "0xd0e30db0": "deposit",
    "0x2e1a7d4d": "withdraw",
    "0x7ff36ab5": "swapExactETHForTokens",
    "0x18cbafe5": "swapExactTokensForETH",
    "0x38ed1739": "swapExactTokensForTokens",
    "0x5ae401dc": "multicall",
    "0xac9650d8": "multicall",
}


class TransactionListPanel(QWidget):
    """Right pane / Transactions tab: top-level txs for the selected
    account, newest first. Double-click opens the tx in the block
    explorer; right-click offers copy-hash / copy-counterparty."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["When", "Counterparty", "Value", "Method", "Status"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(False)
        # Same selection/hover normalization as TokenListPanel.
        self.table.setStyleSheet(
            "QTableView::item {"
            "  padding: 3px 6px;"
            "  border: 0;"
            "}"
            "QTableView::item:hover { background: transparent; }"
            "QTableView::item:selected,"
            "QTableView::item:selected:hover {"
            "  background: palette(highlight);"
            "  color: palette(highlighted-text);"
            "}"
        )
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellDoubleClicked.connect(self._open_in_explorer)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)

        # The empty-state / loading / error label sits stacked under the
        # table; we toggle visibility based on state.
        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setVisible(False)
        v.addWidget(self.status_lbl)

        # Set by MainWindow before render so we can build explorer URLs
        # and compute SENT/RECEIVED direction labels.
        self._chain = None
        self._viewer: str | None = None

    def set_context(self, chain, viewer_address: str) -> None:
        self._chain = chain
        self._viewer = viewer_address

    def show_loading(self) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setText("Loading transactions…")
        self.status_lbl.setVisible(True)

    def show_error(self, msg: str) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setText(f"Couldn't load transactions: {msg}")
        self.status_lbl.setVisible(True)

    def show_empty(self) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setText("No transactions yet for this account.")
        self.status_lbl.setVisible(True)

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setVisible(False)
        self._chain = None
        self._viewer = None

    def show_transactions(self, txs: list[Transaction]) -> None:
        if not txs:
            self.show_empty()
            return
        self.status_lbl.setVisible(False)
        self.table.setRowCount(len(txs))
        viewer = (self._viewer or "").lower()
        symbol = self._chain.symbol if self._chain else "ETH"
        now = int(datetime.datetime.now().timestamp())
        for row, tx in enumerate(txs):
            direction = tx.direction(viewer) if viewer else TxDirection.UNRELATED

            when = QTableWidgetItem(_format_relative_time(tx.timestamp, now))
            when.setToolTip(datetime.datetime.fromtimestamp(tx.timestamp)
                            .strftime("%Y-%m-%d %H:%M:%S"))
            when.setData(Qt.UserRole, tx.hash)

            if direction == TxDirection.SENT:
                arrow, counterparty = "→", tx.to_addr
            elif direction == TxDirection.RECEIVED:
                arrow, counterparty = "←", tx.from_addr
            elif direction == TxDirection.SELF:
                arrow, counterparty = "↻", tx.to_addr
            else:
                arrow, counterparty = " ", tx.to_addr or tx.from_addr
            cp = QTableWidgetItem(f"{arrow} {_short_addr(counterparty)}")
            cp.setFont(QFont("monospace"))
            cp.setToolTip(counterparty or "")
            cp.setData(Qt.UserRole, counterparty)

            if tx.value_wei:
                # Native amounts are wei → ether through Decimal (never
                # float — see CLAUDE.md on-chain math rule).
                ether = wei_to_ether(tx.value_wei)
                value_text = f"{ether:.6f} {symbol}"
            else:
                value_text = "—"
            val = QTableWidgetItem(value_text)
            val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            method_label = KNOWN_SELECTORS.get(tx.method_id, tx.method_id or "—")
            method = QTableWidgetItem(method_label)
            if tx.method_id and method_label != tx.method_id:
                method.setToolTip(tx.method_id)

            status = QTableWidgetItem("✓" if tx.success else "✗")
            status.setTextAlignment(Qt.AlignCenter)
            status.setToolTip("Success" if tx.success else "Reverted")

            self.table.setItem(row, 0, when)
            self.table.setItem(row, 1, cp)
            self.table.setItem(row, 2, val)
            self.table.setItem(row, 3, method)
            self.table.setItem(row, 4, status)

    def _selected_hash(self) -> str | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return self.table.item(items[0].row(), 0).data(Qt.UserRole)

    def _open_in_explorer(self, row: int, col: int) -> None:
        if self._chain is None or not self._chain.explorer:
            return
        h = self.table.item(row, 0).data(Qt.UserRole)
        if not h:
            return
        url = f"{self._chain.explorer.rstrip('/')}/tx/{h}"
        QDesktopServices.openUrl(QUrl(url))

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        h = self.table.item(row, 0).data(Qt.UserRole)
        cp = self.table.item(row, 1).data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction("Open in block explorer")
        act_open.setEnabled(bool(self._chain and self._chain.explorer and h))
        act_copy_hash = menu.addAction("Copy tx hash")
        act_copy_cp = menu.addAction("Copy counterparty address")
        act_copy_cp.setEnabled(bool(cp))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_open:
            self._open_in_explorer(row, 0)
        elif chosen is act_copy_hash and h:
            QApplication.clipboard().setText(h)
        elif chosen is act_copy_cp and cp:
            QApplication.clipboard().setText(cp)


# --- Main window -------------------------------------------------------------

class MainWindow(QMainWindow):
    """Top-level shell. Owns the store + RPC server, instantiates the
    three plugins (Wallets / Tokens / Transactions) into two slots,
    and wires cross-plugin signals. Almost no domain logic lives here
    anymore — each topic owns its own state and workers."""

    def __init__(self, store, rpc):
        super().__init__()
        self.store = store
        self.rpc = rpc
        self.setWindowTitle("qeth — Ethereum wallet")
        self.resize(1060, 720)
        # Override QMainWindow's inflated minimumSizeHint (it reports
        # ~950x565 even when child widgets only need ~370x500).
        self.setMinimumSize(420, 360)

        # Topic plugins. Each owns its sources/caches/workers/widgets.
        self.wallets_plugin = WalletsPlugin(self.store)
        self.tokens_plugin = TokensPlugin(self.store)
        self.transactions_plugin = TransactionsPlugin()

        # Workers tracked here so Python doesn't GC them while running
        # (Qt's QThread destructor aborts the process if the thread is
        # alive). Plugins register workers via host.start_worker(...);
        # they self-evict via the ``finished`` signal.
        self._active_workers: set[QThread] = set()

        self._build_central()
        self._build_statusbar()

        # Wallets is the source of the selection broadcast: when the
        # user picks an account, the plugin emits this signal and we
        # forward it to the right slot's mounted plugins (Tokens,
        # Transactions). Default-account changes refresh the status bar.
        self.wallets_plugin.selected_address_changed.connect(
            self.right_slot.broadcast_account_changed
        )
        self.wallets_plugin.default_account_changed.connect(self._refresh_status)
        self._refresh_status()

        # Restore prior window geometry + splitter states.
        if self.store.window_geometry:
            try:
                self.restoreGeometry(
                    QByteArray.fromHex(self.store.window_geometry.encode())
                )
            except Exception:
                pass
        if self.store.splitter_state_main:
            try:
                self._splitter_outer.restoreState(
                    QByteArray.fromHex(self.store.splitter_state_main.encode())
                )
            except Exception:
                pass
        if self.store.splitter_state_left:
            self.wallets_plugin.restore_splitter_state(
                self.store.splitter_state_left
            )

    def closeEvent(self, event):
        self.store.set_window_geometry(bytes(self.saveGeometry().toHex()).decode())
        self.store.set_splitter_states(
            bytes(self._splitter_outer.saveState().toHex()).decode(),
            self.wallets_plugin.splitter_state(),
        )
        super().closeEvent(event)

    def _build_chain_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.setIconSize(QSize(18, 18))
        for c in self.store.chains:
            label = f"{c.name} ({c.chain_id})"
            pix = bundled_chain_icon(c.chain_id)
            if pix is not None:
                combo.addItem(QIcon(pix), label, c.chain_id)
            else:
                combo.addItem(label, c.chain_id)
        idx = combo.findData(self.store.current_chain_id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(self._on_chain_changed)
        return combo

    def _build_central(self) -> None:
        self._splitter_outer = outer = QSplitter(Qt.Horizontal)

        # Left slot: Wallets only. Single-plugin → no tab bar visible.
        self.left_slot = Slot()
        self.left_slot.add_plugin(self.wallets_plugin, self)
        outer.addWidget(self.left_slot)

        # Right slot: Tokens + Transactions. Tab bar visible.
        self.right_slot = Slot()
        self.right_slot.add_plugin(self.tokens_plugin, self)
        self.right_slot.add_plugin(self.transactions_plugin, self)
        self.chain_combo = self._build_chain_combo()
        self.right_slot.add_shared_widget(self.chain_combo)
        outer.addWidget(self.right_slot)

        outer.setStretchFactor(0, 1)
        outer.setStretchFactor(1, 1)
        outer.setSizes([480, 580])

        self.setCentralWidget(outer)

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.rpc_label = QLabel()
        self.default_label = QLabel()
        sb.addWidget(self.rpc_label, 1)
        sb.addPermanentWidget(self.default_label)

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

    # --- Host protocol (consumed by plugins via plugin.attach) -----------

    @property
    def selected_address(self) -> "str | None":
        return self.wallets_plugin.selected_address

    def current_chain(self):
        return self.store.current_chain()

    def start_worker(self, worker: QThread) -> QThread:
        """Track a worker so Python doesn't GC it while running."""
        self._active_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._active_workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return worker

    def status_message(self, text: str, timeout_ms: int = 3000) -> None:
        self.statusBar().showMessage(text, timeout_ms)

    # --- transitional aliases (kept so existing tests / external code
    # that pokes at the panels directly keeps working).

    @property
    def token_panel(self):
        return self.tokens_plugin.widget()

    @property
    def tx_panel(self):
        return self.transactions_plugin.widget()

    @property
    def tree(self):
        return self.wallets_plugin._tree

    @property
    def details(self):
        return self.wallets_plugin._details

    @property
    def act_add(self):
        return self.wallets_plugin.act_add

    @property
    def act_copy(self):
        return self.wallets_plugin.act_copy

    @property
    def act_remove(self):
        return self.wallets_plugin.act_remove

    def _on_chain_changed(self, idx: int) -> None:
        cid = self.chain_combo.itemData(idx)
        if cid is not None:
            self.store.set_current_chain(int(cid))
            self._refresh_status()
            self.right_slot.broadcast_chain_changed()
