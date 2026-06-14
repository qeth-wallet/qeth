"""ENS plugin — a tab showing the ENS names an account owns as a tree.

Read-only v1 (see ``docs/ens-app.md``): keyless discovery via BENS, names →
owned subdomains → records (address, IPFS contenthash, text records) as tree
items with distinct icons, expiry status, caching, custom-name pinning, and
context actions (open in the ENS app, copy name / resolved address).

Isolated like every plugin: depends only on the ``Host`` protocol + the Qt-free
``qeth.ens_app`` data layer, mounted with one ``add_plugin`` line. ENS is
mainnet-only, so the plugin pins to chain 1 regardless of the viewing chain.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QHeaderView, QInputDialog, QMenu, QPushButton, QSizePolicy,
    QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..ens_app import (
    ENS_APP_URL, VERIFY_WAIT_S, EnsCache, EnsName, EnsNode, EnsRecords,
    OwnershipCheck, build_tree, expiry_status, fetch_name, lookup_owned_names,
    verified_read_records, verify_names,
)
from ..plugin import Plugin

log = logging.getLogger("qeth.plugins.ens")

ENS_CHAIN_ID = 1                       # ENS lives on Ethereum mainnet
_NAME_ROLE = Qt.ItemDataRole.UserRole          # stores the EnsName on a row
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1    # records-loaded flag
_VALUE_ROLE = Qt.ItemDataRole.UserRole + 2     # copyable value on a record row

# Verified-via-Helios markers. On the address column a leading glyph (not
# trailing) survives the tree's ElideMiddle, which keeps both ends visible.
_OWNED_TIP = (
    "Ownership proof-verified on-chain via a Helios light client — your "
    "address is the controller or registrant of this name."
)
_UNOWNED_TIP = (
    "⚠ On-chain state shows this name is NOT controlled by your address. "
    "The indexer that listed it may be stale or wrong."
)
_RESOLVED_TIP = "Resolved address proof-verified on-chain via a Helios light client."
_MISMATCH_TIP = (
    "⚠ The indexer's address differs from the proof-verified resolution — "
    "showing (and copying) the verified address."
)
_RECORD_TIP = "Record proof-verified on-chain via a Helios light client."
_WRAPPED_NOTE = "\nHeld via the ENS NameWrapper (ERC-1155)."

# Expiry-status → (column-1 text, colour). Theme-neutral fixed colours: this is
# a status chip, not palette-driven text.
_EXPIRY_STYLE = {
    "active":   (None,            None),
    "expiring": ("expiring soon", QColor(180, 95, 0)),     # amber
    "grace":    ("in grace",      QColor(176, 0, 32)),      # red
    "expired":  ("expired",       QColor(120, 120, 120)),   # grey
    "none":     (None,            None),
}


def _icon(theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
    """A themed icon, falling back to a built-in Qt standard icon so something
    always renders regardless of the user's icon theme."""
    ic = QIcon.fromTheme(theme_name)
    if not ic.isNull():
        return ic
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app.style().standardIcon(fallback)
    return QIcon()


def _record_rows(rec: EnsRecords) -> "list[tuple[str, str, str]]":
    """Flatten records to (icon-key, label, value) rows for the tree."""
    rows: list[tuple[str, str, str]] = []
    for coin, addr in rec.addresses.items():
        rows.append(("address", f"address ({coin})" if coin != "60" else "address", addr))
    if rec.contenthash:
        rows.append(("content", "content", rec.contenthash))
    for key, val in rec.texts.items():
        rows.append(("text", key, val))
    return rows


class EnsNamesWorker(QThread):
    """Discover the names owned by an address (BENS) + pull details for any
    custom-pinned names, off the Qt thread. Emits ``ready(address, names)``."""

    ready = Signal(str, object)        # (address, list[EnsName])

    def __init__(self, address: str, custom_names: "list[str]", parent=None):
        super().__init__(parent)
        self._address = address
        self._custom = list(custom_names)

    def run(self) -> None:
        names = lookup_owned_names(ENS_CHAIN_ID, self._address)
        have = {n.name.lower() for n in names}
        for cn in self._custom:
            if cn.lower() in have:
                continue
            n = fetch_name(ENS_CHAIN_ID, cn) or EnsName(cn, source="custom")
            names.append(n)
        self.ready.emit(self._address, names)


class EnsRecordsWorker(QThread):
    """Read one name's resolver records (lazy, on expand), Helios-verified when
    a sidecar is ready. Emits ``ready(name, records, verified)``."""

    ready = Signal(str, object, bool)        # (name, EnsRecords, verified)

    def __init__(self, chain, name: str, parent=None,
                 *, wait_s: float = VERIFY_WAIT_S):
        super().__init__(parent)
        self._chain = chain
        self._name = name
        self._wait_s = wait_s

    def run(self) -> None:
        rec, verified = verified_read_records(
            self._chain, self._name, wait_s=self._wait_s)
        self.ready.emit(self._name, rec, verified)


class EnsVerifyWorker(QThread):
    """Verify the displayed names against on-chain state through Helios — in two
    batched multicalls (ownership + resolved-address), not per-name. Emits
    ``ready(address, states, verified)`` where ``states`` is
    ``{name_lower: OwnershipCheck}``. ``verified`` is True only when a Helios
    sidecar proved the reads; otherwise ``states`` is empty and the rows stay
    unbadged (never blocked, never trusting an unverified re-read)."""

    ready = Signal(str, object, bool)        # (address, states, verified)

    def __init__(self, chain, address: str, names: "list[str]", parent=None,
                 *, wait_s: float = VERIFY_WAIT_S):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._names = list(names)
        self._wait_s = wait_s

    def run(self) -> None:
        states, verified = verify_names(
            self._chain, self._names, wait_s=self._wait_s)
        self.ready.emit(self._address, states, verified)


class EnsPanel(QWidget):
    """The tree widget: names → owned subdomains → records."""

    add_custom_requested = Signal()
    records_requested = Signal(str)    # name → load its records (lazy)

    COLS = ["Name", "Expires", "Resolves to"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items_by_name: dict[str, QTreeWidgetItem] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        # Resolved addresses are full 42-char strings shown in the stretch
        # column; let Qt middle-elide them as the tab narrows (same as the
        # wallet address list) instead of pre-shortening to 0x…tail.
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_menu)
        layout.addWidget(self.tree)

        self._domain_icon = _icon("emblem-web", QStyle.StandardPixmap.SP_DriveNetIcon)
        self._sub_icon = _icon("folder", QStyle.StandardPixmap.SP_DirIcon)
        self._rec_icons = {
            "address": _icon("avatar-default", QStyle.StandardPixmap.SP_FileIcon),
            "content": _icon("folder-remote", QStyle.StandardPixmap.SP_FileLinkIcon),
            "text": _icon("text-x-generic", QStyle.StandardPixmap.SP_FileIcon),
        }

    # --- rendering --------------------------------------------------------

    def populate(self, roots: "list[EnsNode]", now_ts: int) -> None:
        self.tree.clear()
        self._items_by_name.clear()
        for node in roots:
            self.tree.addTopLevelItem(self._build(node, now_ts, is_sub=False))

    def _build(self, node: EnsNode, now_ts: int, *, is_sub: bool) -> QTreeWidgetItem:
        n = node.name
        status = expiry_status(n.expiry_ts, now_ts)
        text, colour = _EXPIRY_STYLE.get(status, (None, None))
        exp_col = text or (_fmt_expiry(n.expiry_ts) if n.expiry_ts else "")
        item = QTreeWidgetItem([n.name, exp_col, n.resolved_address or ""])
        item.setIcon(0, self._sub_icon if is_sub else self._domain_icon)
        item.setData(0, _NAME_ROLE, n)
        item.setData(0, _LOADED_ROLE, False)
        if colour is not None:
            item.setForeground(1, QBrush(colour))
        if n.source == "custom":
            item.setToolTip(0, f"{n.name} — pinned")
        self._items_by_name[n.name.lower()] = item
        for child in node.children:
            item.addChild(self._build(child, now_ts, is_sub=True))
        # A name with no owned subdomains still needs to be expandable so the
        # user can pull its records — give it a lazy placeholder.
        if not node.children:
            item.addChild(QTreeWidgetItem(["…loading records"]))
        return item

    def add_records(self, name: str, rec: EnsRecords,
                    verified: bool = False) -> None:
        item = self._items_by_name.get(name.lower())
        if item is None:
            return
        item.setData(0, _LOADED_ROLE, True)
        # drop the "…loading records" placeholder (a childless leaf with no name)
        for i in range(item.childCount() - 1, -1, -1):
            ch = item.child(i)
            if ch.data(0, _NAME_ROLE) is None and ch.data(0, _VALUE_ROLE) is None:
                item.removeChild(ch)
        rows = _record_rows(rec)
        if not rows:
            note = QTreeWidgetItem(["no records", "", ""])
            note.setForeground(0, QBrush(QColor(120, 120, 120)))
            item.addChild(note)
            return
        for icon_key, label, value in rows:
            # ✓ prefix (not suffix) so it survives ElideMiddle. The raw value
            # — not the badged text — is stored for the copy action.
            shown = ("✓ " + value) if verified else value
            ch = QTreeWidgetItem([label, "", shown])
            ch.setIcon(0, self._rec_icons.get(icon_key, self._rec_icons["text"]))
            ch.setData(0, _VALUE_ROLE, value)
            ch.setToolTip(2, _RECORD_TIP if verified else value)
            item.addChild(ch)

    def mark_verified(self, states: "dict[str, OwnershipCheck]",
                      address: str) -> None:
        """Apply the batched on-chain verification to the rows: a ✓ / ⚠ on the
        Name column for ownership, and a ✓ (or a ⚠ + corrected value) on the
        Resolves-to column. Only called with proof-verified state, so trusting
        it over the indexer's hint on a mismatch is sound."""
        for name_l, st in states.items():
            item = self._items_by_name.get(name_l)
            if item is None:
                continue
            n = item.data(0, _NAME_ROLE)
            base = n.name if isinstance(n, EnsName) else item.text(0)
            if st.owned_by(address):
                item.setText(0, f"{base}  ✓")
                item.setToolTip(0, _OWNED_TIP + (_WRAPPED_NOTE if st.wrapped else ""))
            elif st.controller is not None or st.registrant is not None:
                item.setText(0, f"{base}  ⚠")
                item.setToolTip(0, _UNOWNED_TIP)
            if st.resolved_address:
                shown = n.resolved_address if isinstance(n, EnsName) else None
                if shown and shown.lower() == st.resolved_address.lower():
                    item.setText(2, "✓ " + st.resolved_address)
                    item.setToolTip(2, _RESOLVED_TIP)
                else:
                    # Trust the proof: show (and copy) the verified address.
                    if isinstance(n, EnsName):
                        n.resolved_address = st.resolved_address
                    glyph = "✓ " if not shown else "⚠ "
                    item.setText(2, glyph + st.resolved_address)
                    item.setToolTip(2, _RESOLVED_TIP if not shown else _MISMATCH_TIP)

    # --- interaction ------------------------------------------------------

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        n = item.data(0, _NAME_ROLE)
        if isinstance(n, EnsName) and not item.data(0, _LOADED_ROLE):
            item.setData(0, _LOADED_ROLE, True)   # guard against re-emit
            self.records_requested.emit(n.name)

    def _on_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self.tree)
        n = item.data(0, _NAME_ROLE)
        value = item.data(0, _VALUE_ROLE)
        if isinstance(n, EnsName):
            menu.addAction("Open in ENS app", lambda: QDesktopServices.openUrl(
                QUrl(ENS_APP_URL.format(name=n.name))))
            menu.addAction("Copy name", lambda: _clip(n.name))
            if n.resolved_address:
                menu.addAction("Copy resolved address",
                               lambda: _clip(n.resolved_address))
        elif value:
            menu.addAction("Copy value", lambda: _clip(str(value)))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))


def _clip(text: str) -> None:
    QApplication.clipboard().setText(text)


def _fmt_expiry(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class EnsPlugin(Plugin):
    name = "ENS"

    def __init__(self, store):
        super().__init__()
        self._store = store
        self._cache = EnsCache()
        self._panel: Optional[EnsPanel] = None
        self._loaded_for: Optional[str] = None
        self._add_btn: Optional[QPushButton] = None

    # --- plugin contract --------------------------------------------------

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = EnsPanel()
            self._panel.records_requested.connect(self._on_records_requested)
            self._panel.add_custom_requested.connect(self._on_add_custom)
        return self._panel

    def action_widgets(self) -> "list[QWidget]":
        if self._add_btn is None:
            # Match the Tokens pane's add button exactly: a flat 28×28
            # list-add ("+") icon button, not a framed text button.
            btn = QPushButton()
            btn.setIcon(QIcon.fromTheme(
                "list-add",
                btn.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder)))
            btn.setToolTip("Pin an ENS name to always show")
            btn.setFlat(True)
            btn.setMaximumSize(28, 28)
            btn.setIconSize(QSize(16, 16))
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(self._on_add_custom)
            self._add_btn = btn
        assert self._add_btn is not None
        return [self._add_btn]

    def on_account_changed(self, address: Optional[str]) -> None:
        self._load(address)

    def on_activated(self) -> None:
        if self.host is not None and self._loaded_for != self.host.selected_address:
            self._load(self.host.selected_address)

    # --- loading ----------------------------------------------------------

    def _mainnet(self):
        host = self.host
        if host is not None and hasattr(host, "chain_by_id"):
            ch = host.chain_by_id(ENS_CHAIN_ID)
            if ch is not None:
                return ch
        from ..chains import DEFAULT_CHAINS
        return next((c for c in DEFAULT_CHAINS if c.chain_id == ENS_CHAIN_ID), None)

    def _load(self, address: Optional[str]) -> None:
        if self._panel is None:
            return
        self._loaded_for = address
        if not address:
            self._panel.populate([], int(time.time()))
            return
        # Warm a mainnet Helios sidecar now so on-chain verification of the
        # names is ready (or close) by the time discovery returns. Cheap (one
        # Popen) and a no-op when Helios is absent/disabled.
        chain = self._mainnet()
        if chain is not None:
            try:
                from ..helios import prewarm
                prewarm(chain)
            except Exception:
                log.debug("helios prewarm failed", exc_info=True)
        cached = self._cache.load(ENS_CHAIN_ID, address)
        if cached is not None:
            self._render(cached)
        self._on_refresh()

    def _on_refresh(self) -> None:
        host = self.host
        addr = host.selected_address if host is not None else None
        if not addr:
            return
        worker = EnsNamesWorker(addr, sorted(self._store.custom_ens_names))
        worker.ready.connect(self._on_names_ready)
        self._start(worker)

    def _on_names_ready(self, address: str, names: "list[EnsName]") -> None:
        host = self.host
        if host is None or host.selected_address != address:
            return                                  # view moved on
        self._cache.save(ENS_CHAIN_ID, address, names)
        self._render(names)
        self._verify(address, [n.name for n in names])

    def _render(self, names: "list[EnsName]") -> None:
        if self._panel is not None:
            self._panel.populate(build_tree(names), int(time.time()))

    # --- verification (batched, Helios) -----------------------------------

    def _verify(self, address: str, names: "list[str]") -> None:
        chain = self._mainnet()
        if chain is None or not names:
            return
        worker = EnsVerifyWorker(chain, address, names)
        worker.ready.connect(self._on_verified)
        self._start(worker)

    def _on_verified(self, address: str, states: "dict[str, OwnershipCheck]",
                     verified: bool) -> None:
        host = self.host
        if not verified or self._panel is None:
            return
        if host is not None and host.selected_address != address:
            return                                  # view moved on
        self._panel.mark_verified(states, address)

    # --- records (lazy) ---------------------------------------------------

    def _on_records_requested(self, name: str) -> None:
        chain = self._mainnet()
        if chain is None:
            return
        worker = EnsRecordsWorker(chain, name)
        worker.ready.connect(self._on_records_ready)
        self._start(worker)

    def _on_records_ready(self, name: str, rec: EnsRecords,
                          verified: bool) -> None:
        if self._panel is not None:
            self._panel.add_records(name, rec, verified)

    # --- add custom -------------------------------------------------------

    def _on_add_custom(self) -> None:
        if self._panel is None:
            return
        text, ok = QInputDialog.getText(
            self._panel, "Add ENS name", "ENS name (e.g. vitalik.eth):")
        name = (text or "").strip().lower()
        if not ok or not name or "." not in name:
            return
        self._store.add_custom_ens_name(name)
        self._on_refresh()

    # --- worker lifetime --------------------------------------------------

    def _start(self, worker: QThread) -> None:
        host = self.host
        if host is not None and hasattr(host, "start_worker"):
            host.start_worker(worker)
        else:
            worker.start()
