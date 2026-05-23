import io
import logging

import segno

from PySide6.QtCore import Qt, QByteArray, QSize, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QStatusBar, QStyle, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from decimal import Decimal

from .chain import EthClient, wei_to_ether
from .icons import IconCache, bundled_chain_icon, bundled_native_icon
from .ledger import DiscoveredAccount, LedgerWorker, PATH_SCHEMES
from .prices import DefiLlamaPrices, Price, PriceSource
from .risk import GoPlusRisk, RiskCache
from .token_metadata import TokenMetadataCache
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
                 lists: TokenLists, store, show_all: bool = False, parent=None):
        super().__init__(parent)
        self.chain = chain
        self.address = address
        self.source = source
        self.lists = lists
        self.store = store
        self.show_all = show_all

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
                    known_or_pinned = (
                        self.lists.is_known(cid, b.contract)
                        or self.store.is_force_shown(cid, b.contract)
                    )
                    if not known_or_pinned:
                        # Random Blockscout discoveries don't surface in
                        # any mode. Spotlight is "show my trusted tokens
                        # regardless of value or hide-state", not "show
                        # every unknown contract Blockscout knows about".
                        # User can pin a contract explicitly via the +
                        # button to bring it in.
                        continue
                    if not self.show_all and self.store.is_hidden(cid, b.contract):
                        # Hide list applies in normal mode; spotlight
                        # ignores it (that's the whole point).
                        continue
                    tokens.append(b)
                tokens.sort(key=lambda x: x.balance, reverse=True)
            self.fetched.emit(native_wei, tokens)
        except Exception as e:
            self.failed.emit(str(e))


class RiskWorker(QThread):
    """Fetch GoPlus per-contract risk reports for any uncached contracts.

    Runs after the balance multicall and before the price fetch so the
    combined update can already factor risk into the visibility filter.
    Emits ``fetched(chain_id, {addr_lower: RiskReport})``."""

    fetched = Signal(int, object)
    failed = Signal(str)

    def __init__(self, source: GoPlusRisk, chain_id: int,
                 contracts: list[str], parent=None):
        super().__init__(parent)
        self.source = source
        self.chain_id = chain_id
        self.contracts = list(contracts)

    def run(self) -> None:
        try:
            reports = self.source.fetch(self.chain_id, self.contracts)
            self.fetched.emit(self.chain_id, reports)
        except Exception as e:
            self.failed.emit(str(e))


class MetadataWorker(QThread):
    """Fetch (name, symbol, decimals) on-chain via multicall for any
    contracts not already in the metadata cache. ERC-20 metadata is
    immutable, so once we have it for a contract, we never re-fetch."""

    fetched = Signal(int, object)   # chain_id, {contract_lower: {symbol,name,decimals}}
    failed = Signal(str)

    def __init__(self, chain, contracts: list[str], parent=None):
        super().__init__(parent)
        self.chain = chain
        self.contracts = list(contracts)

    def run(self) -> None:
        try:
            client = EthClient(self.chain)
            meta = client.multicall_erc20_metadata(self.contracts)
            self.fetched.emit(self.chain.chain_id, meta)
        except Exception as e:
            self.failed.emit(str(e))


class BalanceWorker(QThread):
    """Refresh balances on-chain via Multicall3 + eth_getBalance.

    Depends only on the user's configured chain RPC, so it produces
    fresh numbers even when Blockscout and DefiLlama are both down.
    Emits ``refreshed(chain_id, native_wei, {addr_lower: balance_raw})``.
    """

    # `dict` would make PySide6 marshal its values through qint64; some
    # ERC-20 raw balances (e.g. ~3.2e19 for ASF with 18 decimals) overflow.
    # Pass as plain object so the Python dict travels untouched.
    refreshed = Signal(int, object, object)
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

    # See BalanceWorker.refreshed: pass as object to skip Qt marshalling.
    prices_ready = Signal(int, object)

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


def _is_scam_via_lists(lists, chain_id: int, b: TokenBalance,
                       risk_cache=None) -> bool:
    risk = None
    if risk_cache is not None:
        risk = risk_cache.get(chain_id, b.contract)
    return lists.is_likely_scam(chain_id, b.contract, b.symbol, b.name, risk=risk)


def _format_usd(value: Decimal) -> str:
    if value <= 0:
        return ""
    if value < Decimal("0.01"):
        return "<$0.01"
    return f"${value:,.2f}"


# Map "e[+-]?N" suffixes to typographic ×10ⁿ notation, since balances on
# scam-airdrop tokens routinely land in the 10¹⁵+ range and "9.12e+10" reads
# noticeably worse than "9.12 × 10¹⁰".
_SUPERSCRIPT = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")


def _format_balance(value: Decimal) -> str:
    s = f"{value:.6g}"
    if "e" not in s and "E" not in s:
        return s
    mantissa, _, exp = s.lower().partition("e")
    exp = exp.lstrip("+")              # drop leading "+", keep "-"
    return f"{mantissa} × 10{exp.translate(_SUPERSCRIPT)}"


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

        # Bottom-right action row: + add custom, − remove (hide), pin
        # (force show), spotlight (show all). Theme icons with text
        # fallback for systems without the freedesktop icon names.
        action_row = QHBoxLayout()
        action_row.setContentsMargins(4, 2, 4, 4)
        action_row.addStretch(1)

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
            action_row.addWidget(b)
        v.addLayout(action_row)

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


# --- Main window -------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, store, rpc):
        super().__init__()
        self.store = store
        self.rpc = rpc
        self.setWindowTitle("qeth — Ethereum wallet")
        self.resize(1060, 720)
        # Override QMainWindow's inflated minimumSizeHint (it reports
        # ~950x565 even when child widgets only need ~370x500). Floor at
        # something a bit below what the QR + a usable table need.
        self.setMinimumSize(420, 360)

        # Token discovery + curated whitelist (lists load in background).
        self._token_source = BlockscoutSource()
        self._token_lists = TokenLists()
        self._token_worker: TokenListWorker | None = None
        self._icon_cache = IconCache(self)
        self._price_source = DefiLlamaPrices()
        self._wallet_cache = WalletCache()
        self._token_metadata = TokenMetadataCache()
        self._risk_source = GoPlusRisk()
        self._risk_cache = RiskCache()
        self._show_all = False
        # (chain_id, address_lower) the panel currently shows; used to
        # skip needless show_cached rebuilds on subsequent refresh calls
        # (e.g. from _on_lists_loaded firing after _on_tree_selection
        # already rendered the cache).
        self._displayed_view: tuple[int, str] | None = None
        # Active discovery (TokenList+Prices) jobs per view, so re-clicking
        # the same account doesn't stack duplicate Blockscout requests.
        self._discovery_in_flight: set[tuple[int, str]] = set()
        # Periodic refresh for prices / new tokens / balance changes from
        # external transactions. Fires every minute against the currently
        # displayed view; _refresh_tokens self-deduplicates via
        # _discovery_in_flight, so an unfinished run blocks the next tick.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(60_000)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start()
        # All in-flight workers. Held here so Python doesn't GC them while
        # they're still running — Qt's QThread destructor aborts (and kills
        # the process) if the thread is alive. Workers self-evict via their
        # `finished` signal so the set doesn't grow forever.
        self._active_workers: set[QThread] = set()

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._rebuild_tree()

        self._lists_loader = TokenListsLoader(self._token_lists)
        self._lists_loader.loaded.connect(self._on_lists_loaded)
        self._lists_loader.failed.connect(
            lambda e: self.token_panel.show_message(f"Token lists failed: {e}")
        )
        # Don't blank the panel with "Loading token lists…" here:
        # _rebuild_tree above has already invoked _refresh_tokens for the
        # default account, which renders the cached view immediately. The
        # "loading" message is only useful when there's no cache, and the
        # no-cache branch inside _refresh_tokens handles that case itself.
        self._lists_loader.start()

        # Restore prior window geometry (size + position + maximized state).
        # Done after the UI is fully built so the layout has a chance to
        # compute its hints; QByteArray.fromHex tolerates trailing nulls
        # and bad input — restoreGeometry returns False on garbage, which
        # we just ignore.
        if self.store.window_geometry:
            try:
                self.restoreGeometry(
                    QByteArray.fromHex(self.store.window_geometry.encode())
                )
            except Exception:
                pass
        # Splitter states — drag positions for outer (left↔right) and inner
        # (tree↕details) splits. Restored after geometry so the splitters
        # know the right total width to distribute.
        if self.store.splitter_state_main:
            try:
                self._splitter_outer.restoreState(
                    QByteArray.fromHex(self.store.splitter_state_main.encode())
                )
            except Exception:
                pass
        if self.store.splitter_state_left:
            try:
                self._splitter_left.restoreState(
                    QByteArray.fromHex(self.store.splitter_state_left.encode())
                )
            except Exception:
                pass

    def closeEvent(self, event):
        self.store.set_window_geometry(bytes(self.saveGeometry().toHex()).decode())
        self.store.set_splitter_states(
            bytes(self._splitter_outer.saveState().toHex()).decode(),
            bytes(self._splitter_left.saveState().toHex()).decode(),
        )
        super().closeEvent(event)
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
        self._splitter_outer = outer = QSplitter(Qt.Horizontal)

        # Left half: tree on top, account details on bottom.
        self._splitter_left = left = QSplitter(Qt.Vertical)

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
        # No bottom padding so DetailsPanel's button (now anchored at the
        # bottom of its own layout) lines up with the right panel's bottom.
        dlay.setContentsMargins(12, 12, 12, 0)
        dlay.addWidget(self.details)
        left.addWidget(details_wrap)
        left.setStretchFactor(0, 1)
        left.setStretchFactor(1, 1)
        left.setSizes([290, 365])

        outer.addWidget(left)

        # Right half: token list for the currently-selected account.
        self.token_panel = TokenListPanel(self._icon_cache, self.store)
        self.token_panel.hide_requested.connect(self._on_hide_token)
        self.token_panel.pin_requested.connect(self._on_pin_token)
        self.token_panel.add_custom_requested.connect(self._on_add_custom_token)
        self.token_panel.show_all_toggled.connect(self._on_show_all_toggled)
        outer.addWidget(self.token_panel)

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
        self._displayed_view = None

    def _refresh_tokens(self, address: str) -> None:
        chain = self.store.current_chain()
        view_key = (chain.chain_id, address.lower())
        is_new_view = self._displayed_view != view_key

        cached = self._wallet_cache.load(chain.chain_id, address)
        if cached is not None:
            if is_new_view:
                # Immediate render from cache; no flicker while we refresh.
                self.token_panel.show_cached(chain, cached)
                self._displayed_view = view_key
            if is_new_view:
                # Only kick off a multicall balance refresh once per view;
                # repeated _refresh_tokens calls for the same view (e.g.
                # _on_lists_loaded after _on_tree_selection) don't need
                # another round-trip.
                bw = BalanceWorker(
                    chain, address,
                    [t.contract for t in cached.tokens],
                )
                bw.refreshed.connect(self._on_balance_refresh)
                bw.failed.connect(
                    lambda msg: logging.getLogger("qeth.ui").warning(
                        "BalanceWorker failed: %s", msg
                    )
                )
                self._start_worker(bw)

        if not self._token_lists.loaded:
            if cached is None and is_new_view:
                self.token_panel.show_message(
                    "Loading token lists… selection will refresh automatically"
                )
                self._displayed_view = view_key
            return

        # Per-view "discovery in progress" guard. Avoids stacking duplicate
        # Blockscout/multicall/prices chains when _refresh_tokens fires
        # multiple times for the same view (e.g. _on_lists_loaded right
        # after _on_tree_selection on startup).
        if view_key in self._discovery_in_flight:
            return
        self._discovery_in_flight.add(view_key)

        # Per-call state captured by closure so concurrent jobs for
        # different views never trample each other's data.
        pv = {"chain": chain, "address": address, "view_key": view_key}

        def on_discovered(blockscout_native_wei, blockscout_tokens: list) -> None:
            # Discard Blockscout's balances — they read a few blocks behind
            # chain head. The contract list is the only thing we keep;
            # metadata (name/symbol/decimals) is fetched on-chain via
            # multicall and cached permanently (it's immutable), with
            # Blockscout's values used only as a one-shot fallback for
            # contracts whose on-chain metadata call reverts.
            # Always include force-shown contracts in the multicall set,
            # even if Blockscout didn't return them (user might have just
            # added a brand-new contract with zero balance).
            forced = {addr for (cid, addr) in self.store.shown_tokens
                      if cid == chain.chain_id}
            seen = set()
            contracts: list[str] = []
            for c in [b.contract for b in blockscout_tokens] + sorted(forced):
                cl = c.lower()
                if cl in seen:
                    continue
                seen.add(cl)
                contracts.append(c)
            blockscout_meta = {
                b.contract.lower(): (b.symbol, b.name, b.decimals)
                for b in blockscout_tokens
            }

            def build_metadata() -> dict:
                """Cached metadata first; fall back to Blockscout values
                for anything still missing (e.g. multicall just failed for
                that token). Always returns ``{addr_lower: (sym,name,dec)}``."""
                out: dict = {}
                for c in contracts:
                    al = c.lower()
                    m = self._token_metadata.get(chain.chain_id, al)
                    if m:
                        out[al] = (m["symbol"], m["name"], m["decimals"])
                    elif al in blockscout_meta:
                        out[al] = blockscout_meta[al]
                return out

            def kick_prices() -> None:
                pw = PricesWorker(
                    self._price_source, chain,
                    contracts, include_native=True,
                )
                pw.prices_ready.connect(
                    lambda c, p: self._on_combined_ready(pv, c, p)
                )
                self._start_worker(pw)

            def kick_risk_then_prices() -> None:
                """GoPlus first for any uncached non-whitelisted contracts
                (whitelisted ones can never be scams, no need to ask),
                then prices. Risk fetch is fast (~300 ms batched) and
                only happens once per token thanks to the disk cache."""
                needed_risk = self._risk_cache.missing(
                    chain.chain_id,
                    [c for c in contracts
                     if not self._token_lists.is_known(chain.chain_id, c)],
                )
                if not needed_risk:
                    kick_prices()
                    return

                def on_risk(cid: int, reports: dict) -> None:
                    if reports:
                        self._risk_cache.put_many(cid, reports)
                    kick_prices()

                def on_risk_fail(msg: str) -> None:
                    logging.getLogger("qeth.ui").warning(
                        "risk fetch failed: %s", msg
                    )
                    kick_prices()

                rw = RiskWorker(self._risk_source, chain.chain_id, needed_risk)
                rw.fetched.connect(on_risk)
                rw.failed.connect(on_risk_fail)
                self._start_worker(rw)

            def on_balances(cid: int, mc_native, mc_balances: dict) -> None:
                pv["native_wei"] = int(mc_native)
                pv["balances_raw"] = {k.lower(): int(v) for k, v in mc_balances.items()}
                pv["metadata"] = build_metadata()
                kick_risk_then_prices()

            def on_balances_fail(msg: str) -> None:
                logging.getLogger("qeth.ui").warning(
                    "post-discovery multicall failed: %s", msg
                )
                pv["native_wei"] = int(blockscout_native_wei)
                pv["balances_raw"] = {
                    b.contract.lower(): int(b.balance_raw) for b in blockscout_tokens
                }
                pv["metadata"] = build_metadata()
                kick_risk_then_prices()

            def kick_balance_multicall() -> None:
                bw = BalanceWorker(chain, address, contracts)
                bw.refreshed.connect(on_balances)
                bw.failed.connect(on_balances_fail)
                self._start_worker(bw)

            # Step 1: bring metadata cache up to date for any uncached
            # contracts. ERC-20 (name,symbol,decimals) are immutable, so
            # the cache hit rate climbs to ~100% after one visit per
            # token-set. Step 2: balance multicall + prices.
            missing_meta = self._token_metadata.missing(chain.chain_id, contracts)
            if not missing_meta:
                kick_balance_multicall()
                return

            def on_meta(cid: int, meta: dict) -> None:
                if meta:
                    self._token_metadata.put_many(chain.chain_id, meta)
                kick_balance_multicall()

            def on_meta_fail(msg: str) -> None:
                logging.getLogger("qeth.ui").warning(
                    "metadata multicall failed: %s", msg
                )
                kick_balance_multicall()

            mw = MetadataWorker(chain, missing_meta)
            mw.fetched.connect(on_meta)
            mw.failed.connect(on_meta_fail)
            self._start_worker(mw)

        def on_failed(msg: str) -> None:
            self._discovery_in_flight.discard(view_key)
            if self.token_panel._chain_id is None and self._displayed_view == view_key:
                self.token_panel.show_error(msg)

        worker = TokenListWorker(
            chain, address, self._token_source, self._token_lists, self.store,
            show_all=self._show_all,
        )
        worker.fetched.connect(on_discovered)
        worker.failed.connect(on_failed)
        if cached is None and is_new_view:
            self.token_panel.show_loading(address)
            self._displayed_view = view_key
        self._start_worker(worker)

    def _on_combined_ready(self, pv: dict, chain_id: int, prices) -> None:
        """TokenListWorker + PricesWorker both done. Apply visibility +
        sort once, then update the panel. Single visible update."""
        self._discovery_in_flight.discard(pv["view_key"])
        chain = pv["chain"]
        if chain.chain_id != chain_id:
            return
        # Drop stale results. If the user switched wallets while this
        # pipeline was running, pushing prices/balances from the previous
        # wallet would hide every row whose address doesn't happen to
        # appear in both wallets (the "tokens disappear and reappear"
        # flicker), then the current view's own pipeline would restore
        # them moments later.
        if self._displayed_view != pv["view_key"]:
            return

        address = pv["address"]
        native_wei = pv["native_wei"]
        metadata = pv["metadata"]
        balances_raw = pv["balances_raw"]

        # Tokens for display are constructed only from multicall results.
        # Anything multicall returned 0 for (Blockscout had a positive
        # balance at its read block but the user has since transferred away)
        # is silently dropped. Anything multicall didn't return at all
        # (call reverted) is dropped — better to omit than to invent.
        tokens: list[TokenBalance] = []
        for addr, raw in balances_raw.items():
            # Normally drop zero balances. Keep them when (a) the user
            # has pinned this token (then it should always be visible at
            # 0 too) or (b) spotlight mode is on (user explicitly wants
            # to see everything, including zero/undetermined-value rows).
            if raw == 0:
                if not (self._show_all
                        or self.store.is_force_shown(chain.chain_id, addr)):
                    continue
            meta = metadata.get(addr)
            if meta is None:
                continue
            sym, name, decimals = meta
            tokens.append(TokenBalance(
                contract=addr, symbol=sym, name=name,
                decimals=decimals, balance_raw=raw,
            ))

        entries = {
            (chain.chain_id, b.contract.lower()): e
            for b in tokens
            if (e := self._token_lists.get(chain.chain_id, b.contract)) is not None
        }

        visible = self._compute_visible_tokens(chain, tokens, prices)

        # Spotlight mode disables the dust filter at display time too —
        # otherwise rows kept by _compute_visible_tokens would still get
        # setRowHidden=True the moment set_prices applied dust check.
        apply_dust = not self._show_all
        if self.token_panel.contract_set_matches(chain, visible):
            self.token_panel.update_balances_if_set_unchanged(
                chain, native_wei, visible,
            )
            self.token_panel.set_prices(
                chain.chain_id, prices, apply_dust_filter=apply_dust,
            )
        else:
            self.token_panel.render_full(
                chain, native_wei, visible, entries, prices,
                apply_dust_filter=apply_dust,
            )

        # Cache only the normal-mode visible set (post-dust + force-show);
        # never persist the spotlight superset, otherwise next visit's
        # cached preview would briefly include dust tokens.
        cache_visible = (
            self._compute_visible_tokens(chain, tokens, prices, show_all=False)
            if self._show_all else visible
        )
        self._save_wallet_cache(chain, address, native_wei, cache_visible, prices, entries)

    def _compute_visible_tokens(self, chain, tokens: list, prices,
                                show_all: bool | None = None) -> list:
        """Apply dust + force-show filter and sort by USD value desc.

        Spotlight mode (``show_all``): worker already restricted the
        input to known-or-pinned, so the only thing the display layer
        needs to do is skip the dust check. The scam heuristic still
        runs at render time so pinned-but-scammy contracts get the
        alarm icon, but it doesn't *filter* anything here.
        """
        if show_all is None:
            show_all = self._show_all
        dust = TokenListPanel.DUST_USD_THRESHOLD
        out = []
        for b in tokens:
            addr = b.contract.lower()
            if show_all:
                out.append(b)
                continue
            if self.store.is_force_shown(chain.chain_id, addr):
                out.append(b)
                continue
            price = prices.get(addr)
            if price is None:
                continue
            if b.balance * price.price_usd < dust:
                continue
            out.append(b)

        def _value(b):
            p = prices.get(b.contract.lower())
            return b.balance * p.price_usd if p else Decimal(0)
        out.sort(key=_value, reverse=True)
        return out

    def _start_worker(self, worker: QThread) -> QThread:
        """Track a worker so it isn't garbage-collected while running."""
        self._active_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._active_workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return worker

    def _on_refresh_tick(self) -> None:
        """Periodic re-fetch for the currently-displayed account.

        Re-runs _refresh_tokens, which short-circuits on already-displayed
        views (no needless show_cached) and on already-running discovery
        chains (no stacked Blockscout requests). The net effect for the
        common steady state is: one Blockscout + one multicall + one
        DefiLlama request per minute, balance + price cells updated in
        place via update_balances_if_set_unchanged + set_prices."""
        addrs = self._selected_addresses()
        if len(addrs) == 1:
            self._refresh_tokens(addrs[0])

    def _on_balance_refresh(self, chain_id: int, native_wei, balances_raw: dict) -> None:
        """Fast in-place balance refresh for the cached set, ahead of the
        slower discovery+prices chain. Display only — cache writes happen
        in _save_wallet_cache after the combined update with prices."""
        chain = self.store.current_chain()
        if chain.chain_id != chain_id:
            return
        addrs = self._selected_addresses()
        if len(addrs) != 1:
            return
        cached = self._wallet_cache.load(chain_id, addrs[0])
        if cached is None:
            return

        nothing_changed = (
            int(native_wei) == cached.native_balance_wei
            and all(
                int(balances_raw.get(t.contract.lower(), t.balance_raw))
                == t.balance_raw
                for t in cached.tokens
            )
        )
        if nothing_changed:
            return

        tokens = [
            TokenBalance(
                contract=t.contract, symbol=t.symbol, name=t.name,
                decimals=t.decimals,
                balance_raw=int(balances_raw.get(t.contract.lower(), t.balance_raw)),
            )
            for t in cached.tokens
        ]
        if self.token_panel.update_balances_if_set_unchanged(chain, native_wei, tokens):
            # Re-multiply by last-known prices so the Value column tracks
            # the new balances even before DefiLlama refreshes.
            self.token_panel.reapply_prices()

    def _save_wallet_cache(
        self, chain, address: str, native_wei: int,
        tokens: list, prices: dict, entries: dict,
    ) -> None:
        """Persist the multicall-derived view. ``tokens`` is the visible
        set after dust/force-show filtering, sorted by USD value desc —
        whatever the panel actually rendered. No further filtering here."""
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

        for b in tokens:
            addr = b.contract.lower()
            price = prices.get(addr)
            entry = entries.get((chain.chain_id, addr))
            cached.tokens.append(CachedToken(
                contract=addr, symbol=b.symbol, name=b.name,
                decimals=b.decimals,
                logo_uri=entry.logo_uri if entry else None,
                balance_raw=int(b.balance_raw),
                price_usd=str(price.price_usd) if price else None,
                balance_updated=now,
                price_updated=price.timestamp if price else 0,
            ))
        self._wallet_cache.save(cached)

    def _on_hide_token(self, chain_id: int, contract: str) -> None:
        self.store.hide_token(chain_id, contract)
        self._invalidate_view_and_refresh()

    def _on_pin_token(self, chain_id: int, contract: str) -> None:
        self.store.force_show_token(chain_id, contract)
        self._invalidate_view_and_refresh()

    def _on_show_all_toggled(self, on: bool) -> None:
        self._show_all = on
        self._invalidate_view_and_refresh()

    def _on_add_custom_token(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        chain = self.store.current_chain()
        addr, ok = QInputDialog.getText(
            self, "Add custom token",
            f"Contract address on {chain.name} (0x… 40 hex chars):",
        )
        if not ok:
            return
        addr = (addr or "").strip()
        if not (addr.startswith("0x") and len(addr) == 42):
            QMessageBox.warning(self, "Invalid address",
                                "Expected a 0x-prefixed 40-character hex address.")
            return
        try:
            int(addr[2:], 16)
        except ValueError:
            QMessageBox.warning(self, "Invalid address",
                                "Address must be hexadecimal.")
            return

        try:
            meta = EthClient(chain).multicall_erc20_metadata([addr])
        except Exception as e:
            QMessageBox.warning(self, "Read failed",
                                f"Couldn't read ERC-20 metadata: {e}")
            return
        if not meta:
            QMessageBox.warning(
                self, "Not a token",
                "Contract didn't respond to ERC-20 metadata calls "
                "(name/symbol/decimals). It might not be an ERC-20.",
            )
            return
        self._token_metadata.put_many(chain.chain_id, meta)
        self.store.force_show_token(chain.chain_id, addr)

        m = next(iter(meta.values()))
        scam = self._token_lists.is_likely_scam(
            chain.chain_id, addr, m.get("symbol", ""), m.get("name", "")
        )
        if scam:
            QMessageBox.warning(
                self, "Heuristic warning",
                f"Added {m['symbol']!r} ({m['name']}). Heads up: it "
                "matches our scam heuristic (URL or impersonating a "
                "major symbol) and will be marked with an alarm icon. "
                "Pinned anyway since you added it explicitly.",
            )
        self._invalidate_view_and_refresh()

    def _invalidate_view_and_refresh(self) -> None:
        """Force the next _refresh_tokens to do a full discovery round
        rather than short-circuiting on the _discovery_in_flight or
        _displayed_view guards — used when the user changes filter state
        and we need the panel to re-render."""
        self._displayed_view = None
        self._discovery_in_flight.clear()
        addrs = self._selected_addresses()
        if len(addrs) == 1:
            self._refresh_tokens(addrs[0])

    def _on_lists_loaded(self) -> None:
        n = self._token_lists.count()
        self.statusBar().showMessage(
            f"Token lists loaded ({n} known tokens)", 3000
        )
        # Hand the lists + risk cache to the panel so it can run the
        # combined scam check for the alarm-icon decision.
        self.token_panel._token_lists = self._token_lists
        self.token_panel._risk_cache = self._risk_cache
        # If a single account is already selected, fetch its tokens now.
        addrs = self._selected_addresses()
        if len(addrs) == 1:
            self._refresh_tokens(addrs[0])
        else:
            self.token_panel.clear()
            self._displayed_view = None

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
