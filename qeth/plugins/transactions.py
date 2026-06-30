"""TransactionsPlugin — self-contained tx-history UI module.

Migrated out of MainWindow as step 2 of the plugin refactor. Owns its
data source, in-memory cache, in-flight set, and the QThread worker.
Lifecycle:

    on_account_changed  → render cached if any; trigger fetch when
                          this plugin is currently active.
    on_chain_changed    → same — current chain is part of the cache key.
    on_activated        → if no cache yet for the current (chain, addr),
                          fire a background fetch.

Lazy-loading is preserved: Blockscout is only hit when the plugin is
the active one (user opened the Transactions tab) AND we don't have
a cached page yet.
"""

from __future__ import annotations

import datetime
import html as _html
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast
from collections.abc import Callable

from eth_utils import to_checksum_address


def _escape_html(text: str) -> str:
    return _html.escape(text, quote=False)

from PySide6.QtCore import (
    QModelIndex, QObject, QPersistentModelIndex, QSize, Qt, QThread, QTimer,
    QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence,
    QPainter, QPalette, QPixmap, QResizeEvent, QStandardItem,
    QStandardItemModel, QTextDocument, QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCompleter, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QSizePolicy, QSpinBox, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from .. import QULONGLONG
from ..abi import (
    KNOWN_EVENT_NAMES, AnyAbiSource, BlockscoutAbiSource, EtherscanV2AbiSource,
    RoutedAbiSource, decode_call, decode_event,
)
from ..abi_cache import AbiCache
from ..contract_identity import (
    ContractIdentityCache, ContractIdentitySource, describe_identity,
)
from ..chain import EthClient, wei_to_ether
from ..live_watcher import LiveWatcher, PendingTx
from ..signing import ReplacementFloor, SignerError, SigningRequest
from ..formatting import format_balance, transfer_notice
from ..formatting import format_datetime as _format_datetime
from ..plugin import Plugin
from ..dialog import Dialog
from ..transactions import (
    BlockscoutTransactionSource, EtherscanV2TransactionSource,
    RoutedTransactionSource, Transaction, TransactionSource,
)
from ..transactions_cache import TransactionCache, merge_txs
from ..activity_cache import ActivityCache
from ..token_metadata import TokenMetadataCache
from ..tx_activity import (
    Activity, AssetLeg, fetch_activities, transfer_legs_from_logs,
)
from ..icons import (
    IconCache, bundled_native_icon, notification_icon, smooth_scaled,
)
from .tx_summary import (
    _ICON, Coin, TxSummary, coins_content_width, paint_summary,
)


def _copy_noun(value: str) -> str:
    """Name the thing a link label holds, for its "Copy …" menu item.
    0x-addresses → Address, 32-byte hashes → Hash, anything else (e.g. a
    dapp origin URL) → Link."""
    v = value.strip()
    if v.startswith(("0x", "0X")):
        if len(v) == 42:
            return "Address"
        if len(v) == 66:
            return "Hash"
    return "Link"


_COINS_ROLE = Qt.ItemDataRole.UserRole + 1   # stores the row's TxSummary


class _CoinsIconDelegate(QStyledItemDelegate):
    """Paint the coins column by drawing the moved-assets row straight onto the
    view's painter — coin logos blitted, the flow arrow as *vector*.

    Nothing is rasterised into an icon and then rescaled by the style, so the
    arrow is crisp and pixel-identical in every row at any DPI. (The default
    delegate hands a pixmap to the QStyle, and some style/Qt combos rescale the
    per-row decoration — the solid coin discs hide it but the thin arrow shows
    it as size/sharpness differences.) Background, selection and focus still
    come from the base delegate; we only add the drawing.
    """

    _HPAD = 6   # matches the cell stylesheet's horizontal padding

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        # Draw the cell background ourselves. The table's QTableView::item
        # stylesheet suppresses the style's selection fill on a custom-delegate
        # cell, so super().paint() leaves a selected coins cell un-highlighted
        # while the rest of the row is highlighted. Match the row: Highlight
        # when selected (Active/Inactive to track focus), else the alternating
        # / base colour.
        pal = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            grp = (QPalette.ColorGroup.Active
                   if option.state & QStyle.StateFlag.State_Active
                   else QPalette.ColorGroup.Inactive)
            painter.fillRect(option.rect, pal.brush(grp, QPalette.ColorRole.Highlight))
        elif option.features & QStyleOptionViewItem.ViewItemFeature.Alternate:
            painter.fillRect(option.rect,
                             pal.brush(QPalette.ColorGroup.Normal,
                                       QPalette.ColorRole.AlternateBase))
        else:
            painter.fillRect(option.rect,
                             pal.brush(QPalette.ColorGroup.Normal,
                                       QPalette.ColorRole.Base))
        summary = index.data(_COINS_ROLE)
        if summary is None:
            return
        fg = pal.color(
            QPalette.ColorRole.HighlightedText if selected
            else QPalette.ColorRole.Text)
        r = option.rect
        top = r.y() + (r.height() - _ICON) // 2
        painter.save()
        paint_summary(painter, summary, fg, r.x() + self._HPAD, top)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex | QPersistentModelIndex) -> QSize:
        base = super().sizeHint(option, index)
        summary = index.data(_COINS_ROLE)
        if summary is None:
            return base
        w = coins_content_width(summary) + 2 * self._HPAD
        return QSize(max(w, base.width()), max(base.height(), _ICON + 6))


def _set_coin_pixmap(label: QLabel | None, src: QPixmap, size: int = 20) -> None:
    """Put ``src`` on a fixed-size icon ``label``, scaled smoothly to ``size``
    logical px at the label's device pixel ratio.

    Replaces ``QLabel.setScaledContents(True)``, which scales the pixmap with
    nearest-neighbour and renders coin logos blocky on low-res screens.
    """
    if label is None or src.isNull():
        return
    label.setPixmap(smooth_scaled(src, size, label.devicePixelRatioF()))


def _install_copy_menu(label: QLabel, value: str, url: str | None) -> None:
    """Give a hyperlink QLabel a useful right-click menu.

    Qt's default rich-text menu offers "Copy" (for *selected text*, so
    it sits disabled) and "Copy Link Location" (the explorer URL) — but
    not the one thing the user wants: the address/hash itself. Replace
    it with a working "Copy Address/Hash/Link" that copies ``value``
    verbatim, plus "Open in Browser" when there's a ``url``."""
    from PySide6.QtWidgets import QMenu

    label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    noun = _copy_noun(value)
    copy_icon = QIcon.fromTheme("edit-copy")
    open_icon = QIcon.fromTheme(
        "applications-internet", QIcon.fromTheme("internet-web-browser")
    )

    def _show(pos):
        menu = QMenu(label)
        menu.addAction(copy_icon, f"Copy {noun}").triggered.connect(
            lambda: QApplication.clipboard().setText(value)
        )
        if url:
            menu.addAction(open_icon, "Open in Browser").triggered.connect(
                lambda: QDesktopServices.openUrl(QUrl(url))
            )
        menu.exec(label.mapToGlobal(pos))

    label.customContextMenuRequested.connect(_show)


def _confirmed_from_receipt(old: Transaction, receipt: dict) -> Transaction:
    """Build a confirmed Transaction by merging an
    ``eth_getTransactionReceipt`` payload into the prior pending
    record. Hex fields parsed; ``effectiveGasPrice`` becomes the
    canonical gas_price (Geth fills it from the EIP-1559 base+tip
    math, so the cached number reflects what the wallet actually
    paid)."""
    from dataclasses import replace

    def _hex(v, default=0):
        if v is None:
            return default
        if isinstance(v, int):
            return v
        return int(v, 16)

    status_v = receipt.get("status")
    success = (_hex(status_v) == 1) if status_v is not None else True
    return replace(
        old,
        block_number=_hex(receipt.get("blockNumber")),
        gas_used=_hex(receipt.get("gasUsed")),
        gas_price_wei=_hex(
            receipt.get("effectiveGasPrice"), default=old.gas_price_wei,
        ),
        success=success,
        pending=False,
        raw_signed=None,   # confirmed — no need to keep it for re-broadcast
    )


# ---- pending-tx polling -------------------------------------------------


class PendingProbeWorker(QThread):
    """Diagnose one pending (qeth-broadcast) tx on a worker thread:

      1. receipt present           → confirmed
      2. else nonce already spent   → dropped (a *different* tx took this
         (tx.nonce < latest mined)     nonce, so this hash can never
                                       confirm — replacement / user re-sent)
      3. else nonce still open      → still_pending; and, when asked,
                                       re-broadcast the raw signed bytes
                                       (RPCs like DRPC sometimes ack a tx
                                       they never actually propagate).

    The nonce check is the reliable death signal: mined-nonce is
    consistent across a load-balanced RPC's backend nodes, unlike a
    mempool query. Re-broadcast is idempotent — "already known" /
    "nonce too low" are swallowed — so re-sending a still-open tx every
    tick is safe."""

    confirmed = Signal(object, str, object)   # (chain, hash, receipt dict)
    dropped = Signal(object, str)             # (chain, hash) nonce consumed
    still_pending = Signal(object, str)
    failed = Signal(object, str, str)

    def __init__(self, chain, tx_hash: str, from_addr: str, nonce: int,
                 raw_signed: str | None, rebroadcast: bool, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._tx_hash = tx_hash
        self._from = from_addr
        self._nonce = nonce
        self._raw = raw_signed
        self._rebroadcast = rebroadcast

    def run(self) -> None:
        client = EthClient(self._chain)
        try:
            receipt = client.rpc(
                "eth_getTransactionReceipt", [self._tx_hash],
            )
        except Exception as e:
            # The probe RPC itself is flaky (e.g. DRPC 408 timeouts — the
            # exact condition a dropped tx ends up in). Don't give up
            # without re-broadcasting: that's the whole point of the
            # watcher, and re-broadcast is idempotent + safe (a spent or
            # already-known tx is rejected harmlessly). Then surface the
            # failure so the user knows the RPC is struggling.
            self._try_rebroadcast(client)
            self.failed.emit(self._chain, self._tx_hash, str(e))
            return
        if receipt is not None:
            self.confirmed.emit(self._chain, self._tx_hash, receipt)
            return
        # No receipt yet — is the nonce already spent by another tx?
        # from_addr is stored lowercased; web3.py rejects non-checksum
        # addresses, so checksum it before the nonce lookup.
        try:
            latest = client.get_transaction_count(
                to_checksum_address(self._from), "latest",
            )
        except Exception as e:
            self._try_rebroadcast(client)
            self.failed.emit(self._chain, self._tx_hash, str(e))
            return
        if self._nonce < latest:
            self.dropped.emit(self._chain, self._tx_hash)
            return
        # Nonce still open: genuinely unconfirmed. Re-push the raw bytes
        # in case the RPC silently dropped it.
        self._try_rebroadcast(client)
        self.still_pending.emit(self._chain, self._tx_hash)

    def _try_rebroadcast(self, client) -> None:
        if not (self._rebroadcast and self._raw):
            return
        try:
            client.send_raw_transaction(self._raw)
        except Exception as e:
            # "already known" / "nonce too low" / "known transaction" are
            # expected and harmless — already in a mempool or just mined.
            # A network error here just means this push didn't land; the
            # next tick tries again.
            log.debug("re-broadcast of %s: %s", self._tx_hash, e)


class PendingTxWatcher(QObject):
    """Periodic sweep of the plugin's cache for ``tx.pending=True``
    entries. For each, spawns a ``ReceiptWorker``; on confirmation
    the plugin updates the cached Transaction in place. Restarts
    are safe — pending entries survive in the disk cache, so the
    next launch picks them up on the first tick."""

    POLL_INTERVAL_MS = 10_000
    # Keep re-broadcasting a still-open pending tx for up to this many
    # ticks (~5 min at 10s), then stop re-sending (but keep watching for
    # a receipt or a nonce-consumed drop). Past this it's almost
    # certainly stuck for a reason re-broadcasting won't fix (e.g. gas
    # too low), surfaced via a one-time warning.
    REBROADCAST_MAX_ATTEMPTS = 30

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # Avoid double-polling the same hash inside a tick or across
        # overlapping ticks — receipt fetches can take longer than
        # the interval on a slow RPC endpoint.
        self._in_flight_hashes: set[str] = set()
        # Per-hash re-broadcast attempt counter (capped) + a one-time
        # "gave up re-broadcasting" warning guard.
        self._rebroadcast_attempts: dict[str, int] = {}
        self._capped_warned: set[str] = set()

    def start(self) -> None:
        if self._timer.isActive():
            return
        self._timer.start(self.POLL_INTERVAL_MS)
        # One immediate tick so app-restart pending txs get checked
        # right away rather than after a full interval.
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        # Refresh the ws live watcher's pending snapshot from the cache —
        # picks up app-restart pending txs and prunes confirmed/dropped ones
        # (no-op when the live watcher is off).
        self._plugin._rebuild_live_snapshot()
        host = self._plugin.host
        if host is None:
            return
        chain_lookup = getattr(host, "chain_by_id", None)
        if not callable(chain_lookup):
            return
        for (chain_id, _addr_lower), txs in list(self._plugin._cache.items()):
            chain = chain_lookup(chain_id)
            if chain is None:
                continue
            for tx in txs:
                if not tx.pending or tx.hash in self._in_flight_hashes:
                    continue
                self._spawn_worker(chain, tx)

    def _spawn_worker(self, chain, tx) -> None:
        attempts = self._rebroadcast_attempts.get(tx.hash, 0)
        has_raw = tx.raw_signed is not None
        do_rebroadcast = has_raw and attempts < self.REBROADCAST_MAX_ATTEMPTS
        if (has_raw and not do_rebroadcast
                and tx.hash not in self._capped_warned):
            self._capped_warned.add(tx.hash)
            log.warning(
                "tx %s still pending after %d re-broadcasts — giving up on "
                "re-broadcast (likely stuck on gas); still watching.",
                tx.hash, self.REBROADCAST_MAX_ATTEMPTS,
            )
        worker = PendingProbeWorker(
            chain, tx.hash, tx.from_addr, tx.nonce, tx.raw_signed,
            do_rebroadcast,
        )
        self._in_flight_hashes.add(tx.hash)
        if do_rebroadcast:
            self._rebroadcast_attempts[tx.hash] = attempts + 1
        worker.confirmed.connect(self._on_confirmed)
        worker.dropped.connect(self._on_dropped)
        worker.still_pending.connect(self._on_still_pending)
        worker.failed.connect(self._on_failed)
        self._plugin.host.start_worker(worker)

    def _forget(self, tx_hash: str) -> None:
        self._in_flight_hashes.discard(tx_hash)
        self._rebroadcast_attempts.pop(tx_hash, None)
        self._capped_warned.discard(tx_hash)

    def _on_confirmed(self, chain, tx_hash: str, receipt) -> None:
        self._forget(tx_hash)
        self._plugin._on_receipt_confirmed(chain, tx_hash, receipt)

    def _on_dropped(self, chain, tx_hash: str) -> None:
        self._forget(tx_hash)
        self._plugin._on_tx_dropped(chain, tx_hash)

    def _on_still_pending(self, _chain, tx_hash: str) -> None:
        self._in_flight_hashes.discard(tx_hash)
        # Contradicting reading → reset the plugin's tentative drop count
        # (DROP_CONFIRM_READINGS counts consecutive readings, not cumulative).
        self._plugin._on_tx_still_pending(_chain, tx_hash)

    def _on_failed(self, _chain, tx_hash: str, msg: str) -> None:
        self._in_flight_hashes.discard(tx_hash)
        log.warning("PendingProbeWorker for %s failed: %s", tx_hash, msg)


log = logging.getLogger("qeth.plugin.transactions")


def _make_identity_get_code(store):
    """A keyless ``eth_getCode`` probe for ContractIdentitySource, resolving
    chain_id → the store's Chain → EthClient. Lets an EOA recipient be
    identified with no Etherscan key (the key is only needed for a contract's
    name/verified/deployer). Returns the code hex, or None when the chain
    is unknown or the read fails (the source then leaves the row bare)."""
    def _get_code(chain_id: int, address: str) -> str | None:
        chain = next((c for c in store.chains if c.chain_id == chain_id), None)
        if chain is None:
            return None
        return EthClient(chain).rpc("eth_getCode", [address, "latest"])
    return _get_code


def _build_pending_snapshot(
    cache: dict[tuple[int, str], list],
    chain_lookup: Callable[[int], Any],
) -> dict[int, tuple[Any, list[PendingTx]]]:
    """Pure: scan the tx cache for pending entries and group them per chain
    as ``{chain_id: (Chain, [PendingTx, ...])}`` — the immutable snapshot the
    LiveWatcher reads from its asyncio thread. Pending txs for the same chain
    but different accounts are merged. Chains the host can't resolve are
    skipped."""
    snap: dict[int, tuple[Any, list[PendingTx]]] = {}
    for (chain_id, _addr), txs in cache.items():
        pend = [PendingTx(t.hash, t.from_addr, t.nonce, t.raw_signed)
                for t in txs if t.pending]
        if not pend:
            continue
        entry = snap.get(chain_id)
        if entry is not None:
            snap[chain_id] = (entry[0], entry[1] + pend)
            continue
        chain = chain_lookup(chain_id)
        if chain is not None:
            snap[chain_id] = (chain, pend)
    return snap


# --- decoded-call renderer ------------------------------------------------
#
# Colour choices balance light and dark themes — moderate saturation
# so both modes stay readable. Function name is bold + default text
# colour (always contrasts against the background); types and values
# get distinct colours so the eye can scan them quickly.
_TYPE_COLOR = "#0066cc"     # cool blue
_VALUE_COLOR = "#22863a"    # green
_COMMENT_COLOR = "#999999"  # mid-grey for trailing "# X SYMBOL" notes


# Functions on the canonical ERC-20 surface (plus the widespread
# burn/mint extensions). When the called contract is a known token,
# every uintN argument to one of these is a value denominated in the
# token's decimals — there's no other meaning for an integer arg on
# these signatures. Param names vary across ABIs ("_value", "amount",
# "wad", "rawAmount", …) so we key on the function rather than the
# arg name; the whitelist is what tells us "any number here is a
# token amount".
_ERC20_AMOUNT_FUNCTIONS = frozenset({
    "transfer", "transferFrom", "approve",
    "increaseAllowance", "decreaseAllowance",
    "burn", "burnFrom", "mint",
})

# 2**256 − 1 — the canonical "infinite approval" sentinel. Anything
# this large isn't a real amount worth converting to a decimal.
_UINT256_MAX = (1 << 256) - 1

# Monospace families we prefer for the decoded-call view, in order.
# All ship a Bold style — that's what made the function name fail to
# stand out when Qt resolved generic ``monospace`` to a Regular-only
# family on Linux. The list also covers macOS (Menlo / SF Mono /
# Monaco) and Windows (Consolas / Courier New) so the picker lands
# on a platform-native choice without us having to special-case OS.
_MONO_FAMILY_PREFERENCES = (
    # Linux desktop defaults
    "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono",
    # Cross-platform popular dev fonts (Adobe / GitHub / Mozilla)
    "Source Code Pro", "Hack", "Fira Code", "Cascadia Code",
    # macOS built-ins (Menlo is the default since 10.6; SF Mono and
    # Monaco are also shipped)
    "Menlo", "SF Mono", "Monaco",
    # Windows built-ins
    "Consolas", "Courier New",
)


def _pick_mono_font() -> QFont:
    installed = set(QFontDatabase.families())
    for family in _MONO_FAMILY_PREFERENCES:
        if family not in installed:
            continue
        if any("bold" in s.lower() for s in QFontDatabase.styles(family)):
            f = QFont(family)
            f.setFixedPitch(True)
            return f
    # Last resort — generic alias. Bold may render as faux-bold (or
    # not at all) depending on what the resolver picks, but the
    # rest of the dialog still looks fine.
    f = QFont("monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    return f


def _format_token_amount(raw: int, decimals: int, symbol: str) -> str:
    """Render an ERC-20 amount as ``"5000 crvUSD"`` etc. Uses Decimal
    end-to-end so the 18-place precision of wei-scale values survives
    division — ``raw / 10**decimals`` as floats silently corrupts the
    last few digits."""
    if raw == _UINT256_MAX:
        return f"unlimited {symbol}"
    from decimal import Decimal
    if decimals <= 0:
        return f"{raw} {symbol}"
    scaled = Decimal(raw) / (Decimal(10) ** decimals)
    # normalize() collapses trailing zeros (Decimal("5000.000") →
    # Decimal("5E+3")), then "f" formatting expands the exponent so
    # the output is the plain "5000" the user expects rather than
    # scientific notation.
    text = format(scaled.normalize(), "f")
    return f"{text} {symbol}"


def _render_decoded(text_edit, decoded: dict,
                    token_context: dict | None = None,
                    known_addresses=None) -> None:
    """Render a decoded call into ``text_edit`` as Python-style
    annotated text. Top-level args render as

        register(
            registration: tuple = {
                label: string = qeth,
                secret: bytes32 = 0x99…,
                …
            },
        )

    with the function name bold, types in blue and values in green.
    Struct args expand recursively with deepening indentation; leaf
    args go on one line.

    Built as a single HTML string. We can't use ``<pre>`` — Qt's
    HTML engine renders ``<pre>`` in its own default UI font
    (``Ubuntu`` on Linux desktops), ignoring the QTextEdit's base
    font, so columns would misalign. Instead we wrap in a ``<div>``
    with ``white-space: pre`` (preserves newlines and indentation)
    and an explicit ``font-family`` set to the family we picked —
    one that we've checked is installed AND ships a Bold style, so
    ``<b>`` actually renders bold (a Bold-less family would render
    the bold span visually identical to the surrounding text, which
    is the trap we hit before)."""
    mono = _pick_mono_font()
    text_edit.setFont(mono)

    fn_name = decoded.get("function") or "?"
    args = decoded.get("args") or []
    # Token-amount annotation only kicks in at top level (struct
    # fields are passed token_context=None below). The function
    # must be one of the canonical ERC-20 amount-carrying functions
    # AND the contract must be on the curated token list — both
    # together is what guarantees "uint here means token amount".
    use_amounts = (
        token_context is not None
        and fn_name in _ERC20_AMOUNT_FUNCTIONS
    )

    # ``white-space: pre-wrap`` preserves our newlines + indentation
    # but still lets very long single tokens (e.g. raw uint256 values
    # printed in decimal) wrap onto the next line at word boundaries,
    # so the dialog never grows a horizontal scrollbar.
    # ``word-break: break-all`` extends that to wrap inside hex
    # strings (which have no spaces and would otherwise overflow).
    # Decoded from the 4-byte signature DB (no contract ABI) — flag it,
    # since parameter names are positional and the signature could be a
    # hash collision.
    note_html = ""
    if decoded.get("via_signature"):
        note_html = (
            '<div style="color:gray; font-style:italic;">'
            "# decoded via the 4-byte signature database — parameter "
            "names unavailable</div>"
        )
    parts = [
        note_html
        + f'<div style="white-space: pre-wrap; word-break: break-all; '
        f"font-family: '{mono.family()}', monospace;\">"
        f"<b>{_escape_html(fn_name)}</b>(\n"
    ]
    for i, arg in enumerate(args):
        parts.append(_arg_html(
            arg, indent=1,
            last=(i == len(args) - 1),
            token_context=token_context if use_amounts else None,
            known_addresses=known_addresses,
        ))
    parts.append(")</div>")
    text_edit.setHtml("".join(parts))


def _arg_html(arg: dict, *, indent: int, last: bool,
              token_context: dict | None = None,
              known_addresses=None) -> str:
    """Serialise one ``arg`` node (leaf, struct branch, or array
    branch) to an HTML fragment. ``last`` controls the trailing
    punctuation: non-final entries get a comma, the last in any
    group just gets a newline (Python-style without trailing
    commas). Positional nodes (empty ``name`` — array elements)
    skip the ``name: type =`` prefix and render the value alone."""
    pad = "    " * indent
    tail = "\n" if last else ",\n"
    name = arg.get("name") or ""
    type_ = arg.get("type") or ""
    type_span = (
        f'<span style="color:{_TYPE_COLOR};">'
        f"{_escape_html(type_)}</span>"
    )
    # Named arg → "name: type = …"; positional element of an array
    # → just the value at the current indent, no annotation.
    if name:
        head = f"{pad}{_escape_html(name)}: {type_span} = "
    else:
        head = pad

    children = arg.get("children")
    if children is not None:
        # Tuple uses { }, array uses [ ]. Empty containers stay on
        # one line; populated ones break to multi-line so long
        # arrays don't trigger horizontal scroll.
        open_b, close_b = ("[", "]") if type_.endswith("]") else ("{", "}")
        if not children:
            return head + open_b + close_b + tail
        inner = "".join(
            _arg_html(child, indent=indent + 1,
                       last=(j == len(children) - 1),
                       known_addresses=known_addresses)
            for j, child in enumerate(children)
        )
        return head + open_b + "\n" + inner + f"{pad}{close_b}{tail}"

    value = arg.get("value")
    value_text = "" if value is None else str(value)
    inner = _escape_html(value_text)
    # Bold + italic an address argument that belongs to one of the
    # user's own wallets — so a self-send / approval-to-self stands out
    # in the decoded call.
    if (type_ == "address" and known_addresses
            and value_text.lower() in known_addresses):
        inner = f"<b><i>{inner}</i></b>"
    value_span = (
        f'<span style="color:{_VALUE_COLOR};">'
        f"{inner}</span>"
    )
    # Trailing "  # X SYMBOL" comment for ERC-20 amounts. Only uintN
    # leaves on a function the caller marked as amount-carrying —
    # see _render_decoded for that gating.
    comment_html = ""
    if (token_context is not None
            and type_.startswith("uint")
            and value is not None):
        try:
            raw = int(str(value))
        except ValueError:
            raw = None
        if raw is not None:
            text = _format_token_amount(
                raw,
                token_context["decimals"],
                token_context["symbol"],
            )
            comment_html = (
                f'  <span style="color:{_COMMENT_COLOR};">'
                f"# {_escape_html(text)}</span>"
            )
    return head + value_span + comment_html + tail


def _is_full_history(txs: list[Transaction]) -> bool:
    """True iff ``txs`` already represents the entire sent history of
    a wallet — used to decide whether the next refresh can early-exit
    on hash overlap, or whether it has to walk every page.

    Sent nonces are 0-based and strictly monotonic per sender, so the
    cache is complete iff nonce 0 is present AND every value between
    0 and max(nonce) appears. Returns False for empty input — an
    empty cache could be a brand-new wallet OR a never-fetched one,
    and a re-walk costs at most one Blockscout call to confirm."""
    if not txs:
        return False
    nonces = {t.nonce for t in txs}
    return 0 in nonces and len(nonces) == max(nonces) + 1


class NonceCheckWorker(QThread):
    """Fetch the sender's on-chain transaction count (its next nonce).
    When that exceeds the highest nonce in our recorded history, a tx was
    sent from another wallet client — we never saw it, but the nonce gives
    it away even though the explorer-backed history hasn't been re-fetched.
    Cheap: one ``eth_getTransactionCount`` per poll. ``count`` is ``None``
    on error (the poll is just skipped)."""

    checked = Signal(object, object)   # (key, count | None)

    def __init__(self, chain, address: str, key, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._key = key

    def run(self) -> None:
        try:
            # "latest", not "pending": we want mined sends only — a
            # pending count would include our own just-broadcast tx and
            # trigger a spurious re-fetch. web3 wants a checksum address.
            count = EthClient(self._chain).get_transaction_count(
                to_checksum_address(self._address), "latest",
            )
        except Exception as e:
            log.warning("nonce check failed for %s: %s", self._address, e)
            self.checked.emit(self._key, None)
            return
        self.checked.emit(self._key, count)


class TransactionsWorker(QThread):
    """Fetch ONE page of (sent) transactions from Blockscout.

    Single-page-per-fetch is what enables the "load on scroll" UX:
    the plugin kicks one worker on tab open (page 1), then one more
    per scroll-to-bottom (page 2, 3, …). Auto-walking the entire
    history at once is too aggressive for accounts with thousands of
    txs (e.g. the 0x7a16… test address has 17 000+ sent).

    ``sent_only`` filters out received entries before the signal —
    their nonces are the *sender's* and would break the nonce-desc
    sort. ``has_more`` distinguishes "this was a normal full page"
    from "Blockscout returned fewer than we asked, so we've reached
    the end" — lets the plugin stop fetching without trying another
    empty round-trip."""

    # Object signal carries Python objects (avoids qint64 marshalling).
    # Chain id travels as ``"qulonglong"`` — dapp-added chains can have ids
    # above qint32 (e.g. Palm = 11297108109), where Signal(int) overflows.
    fetched = Signal(QULONGLONG, str, int, object, bool, object)
    # (chain_id, addr_lower, page_idx, list[Transaction], has_more)
    failed = Signal(str)

    # A sparse-sent account needs many pages to fill the view, and Blockscout
    # is slow/flaky for high-activity addresses — so retry a page a couple of
    # times before failing, rather than letting one transient blip abort the
    # whole walk (which left just the 3 sent txs from page 1 for a busy
    # receive-heavy account whose 657 sent txs are sparse among received ones).
    MAX_ATTEMPTS = 3
    RETRY_BACKOFF_S = 0.6

    def __init__(self, source: TransactionSource, chain, address: str,
                 page: int = 1, page_size: int = 100,
                 sent_only: bool = True, before_block=None, parent=None):
        super().__init__(parent)
        self.source = source
        self.chain = chain
        self.address = address
        self.page = page
        self.page_size = page_size
        self.sent_only = sent_only
        self.before_block = before_block

    def run(self) -> None:
        viewer = self.address.lower()
        raw = None
        last_err: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                raw = self.source.list_transactions(
                    self.chain, self.address,
                    page=self.page, limit=self.page_size,
                    before_block=self.before_block,
                )
                last_err = None
                break
            except Exception as e:
                # One transient blip on a deep page must not abort the walk —
                # retry with a short backoff before failing (see the
                # MAX_ATTEMPTS note above).
                last_err = e
                if attempt < self.MAX_ATTEMPTS - 1:
                    time.sleep(self.RETRY_BACKOFF_S * (attempt + 1))
        if raw is None:
            self.failed.emit(str(last_err) if last_err else "no result")
            return
        # A partial page means Blockscout has nothing more — used
        # by the plugin to flag the (chain, addr) as exhausted.
        has_more = len(raw) >= self.page_size
        # The paging cursor must follow the RAW oldest block (incl. received
        # txs), not the sent-filtered page below — or a receive-heavy account's
        # sparse sent txs stall the older-walk on a received-only window.
        raw_oldest = min((t.block_number for t in raw), default=None)
        page = raw
        if self.sent_only:
            page = [t for t in raw if t.from_addr.lower() == viewer]
        self.fetched.emit(
            self.chain.chain_id, viewer, self.page, page, has_more, raw_oldest,
        )


class TxActivityWorker(QThread):
    """Off-thread build of per-tx Activity (verb + assets moved) for a
    page of txs — one batched tokentx/internal fetch + ABI-decoded verbs,
    sharing the disk ABI cache. Best-effort: on failure those rows just
    keep showing the hash. Emits ``{tx_hash: Activity}``."""

    loaded = Signal(QULONGLONG, str, object)   # (chain_id, addr_lower, dict[str, Activity])

    def __init__(self, chain, address: str, txs: list[Transaction],
                 abi_cache: AbiCache, abi_source: AnyAbiSource | None = None,
                 parent=None):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._txs = txs
        self._abi_cache = abi_cache
        self._abi_source = abi_source

    def run(self) -> None:
        cid = self._chain.chain_id
        addr = self._address.lower()

        def emit(batch: dict) -> None:
            # One signal per batch: pass 1 paints coins + known/placeholder
            # verbs, then each cold callee's resolve refines its rows — so
            # the panel fills in progressively instead of all at the end.
            if batch:
                self.loaded.emit(cid, addr, dict(batch))

        try:
            fetch_activities(self._chain, self._address, self._txs,
                             abi_source=self._abi_source,
                             abi_cache=self._abi_cache, on_batch=emit)
        except Exception as e:
            log.debug("activity build failed: %s", e)


class ReceiptScanWorker(QThread):
    """Background RPC pass for the rare confirmed txs the tokentx index
    returned no coins for. Pulls each receipt and hands its event logs
    back, so the activity can be filled in from the Transfer events the
    indexer missed. Best-effort, sequential, capped — these are rare."""

    found = Signal(QULONGLONG, str, object)   # chain_id, tx_hash, receipt logs

    def __init__(self, chain, hashes: list[str], parent: QObject | None = None):
        super().__init__(parent)
        self._chain = chain
        self._hashes = hashes

    def run(self) -> None:
        client = EthClient(self._chain)
        cid = self._chain.chain_id
        for h in self._hashes:
            try:
                receipt = client.rpc("eth_getTransactionReceipt", [h])
            except Exception as e:
                log.debug("receipt scan failed for %s: %s", h, e)
                continue
            logs = receipt.get("logs") if hasattr(receipt, "get") else None
            if logs:
                self.found.emit(cid, h, logs)


# Sentinel fork floor meaning "fork at the freshest state (head)" — returned
# by fork_floor_block when the wallet has an in-flight sent tx. Above any real
# block number, so _latest_block's min(floor, head) clamps it to the live head.
_FORK_FLOOR_HEAD = (1 << 63) - 1


class TransactionsPlugin(Plugin):
    name = "Transactions"

    # Fires when a tracked pending tx mines (chain, tx_hash, receipt). Lets
    # sibling plugins react to their own broadcasts confirming — the ENS plugin
    # re-reads a name's records the moment its record/subdomain write lands,
    # rather than guessing with a timer.
    tx_confirmed = Signal(object, str, object)

    # How many cached rows to materialise into the table on first
    # open, and how many more to reveal on each scroll-to-bottom
    # while the in-memory cache still has unrevealed entries below
    # the displayed window. 200 ≈ several viewports of buffer; on
    # a wallet with thousands of cached txs the initial open drops
    # from ~900 ms to under 50 ms.
    INITIAL_VISIBLE = 200
    # How many *new* rows a network page must yield before we stop
    # walking through Blockscout's pagination on the load-older /
    # initial-walk paths. Independent of INITIAL_VISIBLE — this is
    # about the network round-trip budget, not the render budget.
    INITIAL_BATCH = 50
    # How often to poll the current account's on-chain nonce to catch
    # txs sent from another wallet client (NonceCheckWorker). One cheap
    # RPC call; on a hit it re-fetches page 1 to pull the new tx in.
    NONCE_POLL_INTERVAL_MS = 30_000
    # A pending tx looks "dropped" when its nonce is spent but we can't fetch
    # its receipt. That single reading is unreliable behind a load-balanced RPC
    # (a backend can miss a mined tx's receipt), so only believe it after this
    # many consecutive readings across watcher ticks — a mined tx's receipt
    # propagates and confirms within a tick or two, clearing the count.
    DROP_CONFIRM_READINGS = 3

    def __init__(
        self,
        source: TransactionSource | None = None,
        disk_cache: TransactionCache | None = None,
        abi_source: AnyAbiSource | None = None,
        abi_cache: AbiCache | None = None,
        store=None,
    ):
        super().__init__()
        # Source / cache injection both let tests pass fakes.
        # Default source: prefer Etherscan v2 when the store has a
        # key, fall back to Blockscout. When no store is given
        # (tests / standalone construction), behaviour matches the
        # original bare-Blockscout setup.
        if source is None:
            blockscout = BlockscoutTransactionSource()
            if store is not None:
                source = RoutedTransactionSource(
                    EtherscanV2TransactionSource(
                        lambda: store.etherscan_api_key,
                    ),
                    blockscout,
                )
            else:
                source = blockscout
        self._source: TransactionSource = source
        self._disk_cache = disk_cache if disk_cache is not None else TransactionCache()
        # ABI machinery for the details dialog. Lazy fetch + disk-
        # cache so each contract address is looked up at most once.
        # Prefer Etherscan v2 (reliable, proxy-aware) when the store has a
        # key, fall back to Blockscout — mirrors the tx-source routing and
        # fixes Polygon, whose Blockscout instance is flaky.
        if abi_source is None:
            blockscout_abi = BlockscoutAbiSource()
            if store is not None:
                abi_source = RoutedAbiSource(
                    EtherscanV2AbiSource(lambda: store.etherscan_api_key),
                    blockscout_abi,
                )
            else:
                abi_source = blockscout_abi
        self._abi_source = abi_source
        self._abi_cache = abi_cache if abi_cache is not None else AbiCache()
        # Contract-identity machinery (name / verified / deployer / age),
        # shown on the To: row of the details + signing dialogs. Same
        # Etherscan-v2 key as the ABI source; immutable facts → cached
        # permanently on disk. None when there's no store/key (tests).
        self._identity_source: ContractIdentitySource | None = (
            ContractIdentitySource(
                lambda: store.etherscan_api_key,
                get_code=_make_identity_get_code(store))
            if store is not None else None
        )
        self._identity_cache = ContractIdentityCache()
        # In-memory cache, keyed by (chain_id, address_lower). Hydrated
        # lazily from the disk cache on first ``on_account_changed`` for
        # a (chain, addr) — that's what prevents the empty → populated
        # flicker on startup.
        self._cache: dict[tuple[int, str], list[Transaction]] = {}
        # Per-hash count of consecutive "looks dropped" readings — a single one
        # is unreliable behind a load-balanced RPC, so we wait for repeats
        # before flipping a tx to the terminal dropped state.
        self._drop_readings: dict[str, int] = {}
        # Active fetches — prevents duplicate Blockscout calls when
        # on_activated / on_account_changed / on_chain_changed all fire
        # close together (e.g. user clicks a new account while the tab
        # is open). Also coalesces repeated scroll-to-bottom triggers.
        self._in_flight: set[tuple[int, str]] = set()
        # Per-key paging state for the load-on-scroll UX.
        # next_page = page index to fetch on the next scroll-to-bottom.
        # exhausted = we've fetched the last page (a partial page came
        # back, OR the cached set now includes nonce 0) — further
        # network scrolls are ignored for this account.
        # displayed_count = how many of the cached rows are currently
        # rendered on the table. Bounded growth via INITIAL_BATCH +
        # scroll-driven appends keeps rendering O(visible), not
        # O(cache) — critical for accounts with thousands of cached
        # entries where rebuilding the whole table freezes the UI.
        self._exhausted: set[tuple[int, str]] = set()
        self._displayed_count: dict[tuple[int, str], int] = {}
        # Which (chain, addr) the panel's table currently shows. Used
        # to skip the show_transactions rebuild when on_activated fires
        # for the same view we already painted — Qt then preserves the
        # scrollbar and the user's scrolled-in batches stay intact.
        self._rendered_for: tuple[int, str] | None = None
        # tx hash → (out ERC-20 contracts, in ERC-20 contracts) learned from
        # a receipt or pre-broadcast simulation, folded into the activity so
        # a swap's coins show before Blockscout indexes the transfers.
        self._known_legs: dict[str, tuple[list[str], list[str]]] = {}
        # (chain_id, contract) → ticker, harvested from Blockscout-built
        # activities (their tokentx carries tokenSymbol). Lets a receipt/sim
        # leg show the right symbol (e.g. Gnosis "EURe" → "E") even when the
        # curated token lists don't know the token, instead of a bare "?".
        self._symbol_cache: dict[tuple[int, str], str] = {}
        # On-chain (name, symbol, decimals) cache, shared on disk with the
        # tokens plugin. The authoritative, token-list-INDEPENDENT symbol
        # source: a token absent from every curated list still has an on-chain
        # symbol() — read it here so the coins column shows "PEPE", not "?".
        # _meta_inflight dedupes the on-chain multicall per contract.
        self._token_meta = TokenMetadataCache()
        self._meta_inflight: set[str] = set()
        # Resolved activities persisted to disk — a confirmed tx's verb +
        # coins never change, so a return visit to a chain paints from here
        # instantly instead of refetching (and receipt coins survive a
        # restart).
        self._activity_cache = ActivityCache()
        # Hashes we've already RPC-receipt-scanned for the rare tokentx
        # misses, so we don't re-scan within a session.
        self._receipt_checked: set[str] = set()
        # The widget is built lazily so the plugin can be instantiated
        # outside a Qt event loop (useful in pure-Python imports).
        self._panel: TransactionListPanel | None = None
        # Wired in attach(); polls for receipts of broadcast txs whose
        # hashes are sitting in cache with pending=True.
        self._pending_watcher: PendingTxWatcher | None = None
        # Optional ws live watcher (QETH_LIVE_WS): confirms pending txs on the
        # block they mine in, alongside the polling watcher (the floor).
        self._live_watcher: LiveWatcher | None = None
        # Immutable per-chain pending-tx snapshot the LiveWatcher reads from
        # its asyncio thread; rebuilt + atomically swapped on the main thread.
        self._live_snapshot: dict[int, tuple[Any, list[PendingTx]]] = {}
        # The (chain, account) currently on screen, whose ERC-20 Transfer
        # logs the LiveWatcher subscribes to for live balances. Atomic tuple
        # swap on the main thread; read from the asyncio thread.
        self._live_account: tuple[Any, str] | None = None
        # Keys with a NonceCheckWorker in flight (coalesce polls).
        self._nonce_in_flight: set[tuple[int, str]] = set()
        self._nonce_timer: QTimer | None = None

    # --- Plugin contract ----------------------------------------------------

    def attach(self, host) -> None:
        super().attach(host)
        self._pending_watcher = PendingTxWatcher(self, parent=self)
        self._pending_watcher.start()
        # ws live watcher (on by default; set QETH_LIVE_WS=0 to disable):
        # subscribes to newHeads per chain-with-a-pending-tx (confirms on the
        # mining block) and the on-screen account's ERC-20 Transfer logs (live
        # token balances). The polling watcher above stays the floor — covers
        # ws-down chains, brand-new txs, and native balances — so this is
        # purely an accelerator; confirmed / dropped / balance_dirty route
        # into idempotent handlers. aboutToQuit joins the asyncio thread.
        if os.environ.get("QETH_LIVE_WS", "1").strip().lower() not in (
                "0", "false", "no", "off", ""):
            self._live_watcher = LiveWatcher(
                self._live_chains_provider,
                self._live_pending_provider,
                self._live_account_provider,
                parent=self,
            )
            self._live_watcher.confirmed.connect(self._on_receipt_confirmed)
            self._live_watcher.dropped.connect(self._on_tx_dropped)
            self._live_watcher.still_pending.connect(self._on_tx_still_pending)
            self._live_watcher.link_state.connect(self._on_ws_link_state)
            self._live_watcher.balance_dirty.connect(self._on_balance_dirty)
            self._live_watcher.native_balance.connect(self._on_native_balance)
            self._live_watcher.transfer_seen.connect(self._on_transfer_seen)
            self._update_live_account()
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._live_watcher.stop)
            self._live_watcher.start()
            log.info("ws live watcher enabled")
        # Catch txs sent from another wallet client: the explorer-backed
        # history only refreshes on tab/account/chain change (and the
        # _is_full_history short-circuit means a "complete" cache never
        # re-fetches), so an external send is otherwise invisible until
        # something forces a refetch. Polling the nonce gives it away.
        self._nonce_timer = QTimer(self)
        self._nonce_timer.setInterval(self.NONCE_POLL_INTERVAL_MS)
        self._nonce_timer.timeout.connect(self._poll_external_nonce)
        self._nonce_timer.start()
        # Let the ABI source resolve proxy implementations from the chain
        # when Blockscout v2 is down (notably polygon.blockscout.com, which
        # 500s — so proxy contracts like USDC otherwise decode as a bare
        # stub). Shared with the Send/Sign dialogs (same source instance).
        if self._abi_source is not None and hasattr(
                self._abi_source, "set_storage_reader"):
            self._abi_source.set_storage_reader(self._abi_read_storage)

    # --- ws live watcher wiring -------------------------------------------

    def _live_chains_provider(self) -> list[Any]:
        """Chains to watch live = those with a pending tx. Read from the
        asyncio thread; returns the current immutable snapshot's Chains."""
        return [chain for chain, _ in self._live_snapshot.values()]

    def _live_pending_provider(self, chain_id: int) -> list[PendingTx]:
        """Pending txs for a chain, for the live probe. Asyncio-thread read
        of the immutable snapshot (built on the main thread)."""
        entry = self._live_snapshot.get(chain_id)
        return entry[1] if entry is not None else []

    def pending_nonce_floor(self, chain_id: int, address: str) -> int | None:
        """One past the highest nonce among the txs WE broadcast from
        ``address`` and are still tracking as pending — the nonce a brand-new
        send must use so it doesn't collide with one of ours that's still in
        flight. ``None`` when nothing is in flight (the mined count is then
        authoritative). We're the sole signer for the account, so this is
        exact and needs no trust in the node's flaky "pending" view."""
        addr = address.lower()
        nonces = [
            t.nonce for t in self._live_pending_provider(chain_id)
            if t.from_addr.lower() == addr and t.nonce is not None
        ]
        return max(nonces) + 1 if nonces else None

    def fork_floor_block(self, chain_id: int, address: str) -> int | None:
        """The block a verified preview must not fork BEFORE, so an
        approve-then-swap sees the approval. The latest block this wallet's
        own sent activity may have touched on ``chain_id``:

          - an in-flight (pending, not dropped) sent tx → ``_FORK_FLOOR_HEAD``,
            a sentinel meaning "fork at the freshest state (head)". The tx may
            have *just mined* before our receipt watcher flipped it to
            confirmed (the ~30 s window that hid an approval from the next
            swap) — forking behind the head would still miss it, so demand
            head; ``_latest_block`` clamps the sentinel down to the live head.
          - else the highest CONFIRMED sent-tx block.
          - else ``None`` (the fork uses its per-chain lag alone).

        Only txs we *sent* (``from == address``) count — those are the ones
        whose omission would make a follow-up call falsely revert."""
        addr = address.lower()
        txs = self._cache.get((chain_id, addr))
        if txs is None:
            txs = self._disk_cache.load(chain_id, addr) or []
        sent = [t for t in txs if (t.from_addr or "").lower() == addr]
        if any(t.pending and not getattr(t, "dropped", False) for t in sent):
            return _FORK_FLOOR_HEAD
        blocks = [t.block_number for t in sent if t.block_number]
        return max(blocks) if blocks else None

    def _rebuild_live_snapshot(self) -> None:
        """Main-thread rebuild of the pending snapshot the LiveWatcher reads,
        assigning a fresh dict (atomic swap → no lock). Cheap no-op when the
        live watcher is disabled."""
        if self._live_watcher is None:
            return
        host = self.host
        chain_lookup = getattr(host, "chain_by_id", None) if host else None
        if not callable(chain_lookup):
            self._live_snapshot = {}
            return
        self._live_snapshot = _build_pending_snapshot(self._cache, chain_lookup)

    def _on_ws_link_state(self, chain, connected: bool) -> None:
        log.debug("ws %s: %s", chain.name, "up" if connected else "down")
        tokens = getattr(self.host, "tokens_plugin", None) if self.host else None
        if tokens is not None and hasattr(tokens, "on_ws_link_state"):
            try:
                tokens.on_ws_link_state(chain, connected)
            except Exception:
                log.exception("on_ws_link_state relay failed")

    def _live_account_provider(self) -> tuple[Any, str] | None:
        """The on-screen (chain, account) for the LiveWatcher's Transfer-log
        subscription. Asyncio-thread read of the atomically-swapped snapshot
        built on the main thread by _update_live_account."""
        return self._live_account

    def _update_live_account(self) -> None:
        """Recompute the (chain, account) whose Transfer logs we want live,
        from the current view. Main-thread only (atomic tuple swap → no
        lock); cheap no-op when the live watcher is off. Called on attach
        and on every account / chain change."""
        if self._live_watcher is None:
            return
        host = self.host
        chain = host.current_chain() if host is not None else None
        addr = getattr(host, "selected_address", None) if host else None
        self._live_account = (
            (chain, addr.lower()) if (chain is not None and addr) else None)

    def _on_balance_dirty(self, chain, account: str, token: str) -> None:
        """A ws Transfer touched the account (LiveWatcher.balance_dirty).
        Relay it to TokensPlugin, which re-reads that balance — mirrors the
        _on_receipt_confirmed → note_receipt_logs cross-plugin hop."""
        tokens = getattr(self.host, "tokens_plugin", None) if self.host else None
        if tokens is not None and hasattr(tokens, "on_balance_dirty"):
            try:
                tokens.on_balance_dirty(chain, account, token)
            except Exception:
                log.exception("on_balance_dirty failed")

    def _on_native_balance(self, chain, account: str, native_wei) -> None:
        """The on-screen account's native balance, read over the live ws every
        ~minute (LiveWatcher.native_balance) — the inbound-ETH counterpart to
        _on_balance_dirty (a plain ETH send fires no Transfer log). Relay to
        TokensPlugin for a lightweight native-only apply."""
        tokens = getattr(self.host, "tokens_plugin", None) if self.host else None
        if tokens is not None and hasattr(tokens, "on_native_balance"):
            try:
                tokens.on_native_balance(chain, account, native_wei)
            except Exception:
                log.exception("on_native_balance failed")

    def _on_transfer_seen(
        self, chain, account: str, token: str, counterparty: str,
        outgoing: bool, raw_value,
    ) -> None:
        """A ws Transfer touching the account, decoded (LiveWatcher). Relay to
        TokensPlugin, which formats the amount with the token's symbol/decimals
        and raises the sent/received desktop notification."""
        tokens = getattr(self.host, "tokens_plugin", None) if self.host else None
        if tokens is not None and hasattr(tokens, "on_transfer_seen"):
            try:
                tokens.on_transfer_seen(
                    chain, account, token, counterparty, outgoing, raw_value)
            except Exception:
                log.exception("on_transfer_seen failed")

    def _abi_read_storage(self, chain_id: int, address: str, slot: str):
        """``eth_getStorageAt`` for the ABI source's proxy-slot probing.
        Runs in the AbiFetchWorker thread, off the UI thread."""
        host = self.host
        lookup = getattr(host, "chain_by_id", None) if host else None
        chain = lookup(chain_id) if lookup else None
        if chain is None:
            return None
        return EthClient(chain).rpc(
            "eth_getStorageAt",
            [to_checksum_address(address), slot, "latest"],
        )

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = TransactionListPanel()
            self._panel.scrolled_to_bottom.connect(self._on_scroll_bottom)
            self._panel.tx_details_requested.connect(self._show_tx_details)
            self._panel.replace_requested.connect(self._on_replace_requested)
        return self._panel

    def _show_tx_details(self, tx: Transaction) -> None:
        if self.host is None or self._panel is None:
            return
        chain = self.host.current_chain()
        # Pull token-annotation deps from the host. They're optional on
        # the dialog (so it stays unit-testable without a TokensPlugin),
        # but in the running app the host always supplies them.
        token_info = getattr(self.host, "token_info", None)
        icon_cache_fn = getattr(self.host, "icon_cache", None)
        icon_cache = icon_cache_fn() if callable(icon_cache_fn) else None
        price_lookup = getattr(self.host, "native_price_usd", None)
        native_price_usd = (
            price_lookup(chain.chain_id, self.host.selected_address)
            if callable(price_lookup) else None
        )
        addrs_fn = getattr(self.host, "account_addresses", None)
        known_addresses = addrs_fn() if callable(addrs_fn) else []
        dialog = TransactionDetailsDialog(
            tx, chain,
            abi_source=self._abi_source,
            abi_cache=self._abi_cache,
            identity_source=self._identity_source,
            identity_cache=self._identity_cache,
            tx_cache=self._disk_cache,
            start_worker=self.host.start_worker,
            token_info=token_info,
            icon_cache=icon_cache,
            native_price_usd=native_price_usd,
            known_addresses=known_addresses,
            parent=self._panel,
        )
        dialog.replace_requested.connect(self._on_replace_requested)
        dialog.show()

    def _on_replace_requested(self, tx: Transaction, cancel: bool) -> None:
        """Speed up / Cancel was picked (from the details dialog or a row
        right-click). Hand off to the host, which opens the replace dialog
        through the normal sign+broadcast flow."""
        opener = getattr(self.host, "open_replace_tx", None)
        if callable(opener):
            opener(tx, cancel)

    def action_widgets(self):
        return self._panel.action_widgets() if self._panel is not None else []

    # --- pending-tx integration ---------------------------------------------

    def add_pending(self, tx_hash: str, req: SigningRequest, chain,
                    raw_signed: str | None = None) -> None:
        """Called by MainWindow right after a successful broadcast.
        Synthesises a ``Transaction(pending=True)`` from the finalised
        request + broadcast hash, prepends it to the cache for
        (chain, from_addr), persists, and re-renders if the panel is
        currently showing that view. The pending entry's confirmed
        fields (block_number, gas_used, success, gas_price_wei) get
        filled in by ``PendingTxWatcher`` when the receipt lands."""
        import time
        addr_lower = req.from_addr.lower()
        key = (chain.chain_id, addr_lower)
        gas_price_wei = (req.max_fee_per_gas
                          if chain.eip1559 else req.gas_price) or 0
        method_id = req.data[:10] if (req.data and len(req.data) >= 10) else ""
        pending = Transaction(
            chain_id=chain.chain_id,
            hash=tx_hash,
            block_number=0,
            timestamp=int(time.time()),
            nonce=req.nonce or 0,
            from_addr=addr_lower,
            to_addr=(req.to_addr.lower() if req.to_addr else None),
            value_wei=req.value_wei,
            gas_used=0,
            gas_price_wei=gas_price_wei,
            method_id=method_id,
            input_data=req.data or "0x",
            success=True,            # placeholder until the receipt lands
            pending=True,
            raw_signed=raw_signed,
        )
        # Hydrate the in-memory cache from disk if this is the first
        # time we touch this view this session — otherwise we'd
        # overwrite the file with just the pending entry on save.
        if key not in self._cache:
            disk = self._disk_cache.load(chain.chain_id, addr_lower)
            self._cache[key] = list(disk) if disk else []
        self._cache[key] = merge_txs([pending], self._cache[key])
        self._disk_cache.save(chain.chain_id, addr_lower, self._cache[key])
        # If the panel is currently showing this view, prepend the
        # single new pending row instead of rebuilding the whole
        # table — full repaints on big caches are exactly the
        # freeze we're trying to avoid.
        if self._panel is not None and self._rendered_for == key:
            self._panel.prepend_transactions([pending])
            self._displayed_count[key] = (
                self._displayed_count.get(key, 0) + 1
            )
        # Make sure the watcher is running — it's idempotent if
        # already started.
        if self._pending_watcher is not None:
            self._pending_watcher.start()
        # Surface the new pending tx to the ws live watcher right away so it
        # can confirm on the next block rather than after a poll refresh.
        self._rebuild_live_snapshot()

    def _on_receipt_confirmed(self, chain, tx_hash: str, receipt) -> None:
        """ReceiptWorker → PendingTxWatcher → here. Find the pending
        cached entry for this (chain, hash) and replace it with the
        confirmed form built from the receipt. No-op if the entry
        isn't in cache (e.g. already overwritten by a Blockscout
        refresh — the new entry wins).

        Also forwards the receipt to TokensPlugin so its Transfer-
        event scan can pick up any ERC-20 contract that touched
        one of our wallets — this is the only way to learn about
        the receive-side of a swap before Blockscout indexes it
        (3+ minutes lag on busy chains)."""
        chain_id = chain.chain_id
        # Confirmed → cancel any tentative drop-readings for this hash.
        self._drop_readings.pop(tx_hash, None)
        # Let sibling plugins react to this exact tx confirming. Emitted
        # unconditionally (even when no pending row matches — e.g. a Blockscout
        # race already swapped it) so a one-shot listener never misses it.
        self.tx_confirmed.emit(chain, tx_hash, receipt)
        # Forward to TokensPlugin regardless of whether we still
        # have a matching pending entry — the tokens scan only
        # cares about the logs, not the local cache state.
        if self.host is not None:
            tokens_plugin = getattr(self.host, "tokens_plugin", None)
            if tokens_plugin is not None:
                try:
                    tokens_plugin.note_receipt_logs(chain, receipt)
                except Exception:
                    log.exception("note_receipt_logs failed")
        for key, txs in list(self._cache.items()):
            if key[0] != chain_id:
                continue
            for i, t in enumerate(txs):
                if t.hash != tx_hash:
                    continue
                if not t.pending:
                    return    # already confirmed (race with Blockscout)
                txs[i] = _confirmed_from_receipt(t, receipt)
                self._disk_cache.save(chain_id, key[1], txs)
                # Repaint just the one row whose hash we updated;
                # rebuilding the whole table here used to freeze
                # the UI on big caches even though we only swap
                # pending → confirmed on a single entry. If the
                # row is beyond the currently-visible window (not
                # yet revealed), there's nothing on screen to
                # update — the next reveal will paint the new
                # state from the cache.
                if self._panel is not None and self._rendered_for == key:
                    self._panel.update_tx_by_hash(txs[i])
                # Fold the receipt's ERC-20 Transfer events into the row's
                # activity now — so a swap/transfer shows its coins the
                # instant it confirms, instead of waiting for Blockscout to
                # index the transfers (or an app restart).
                logs = receipt.get("logs") if hasattr(receipt, "get") else None
                self.note_transfer_legs(chain_id, tx_hash, logs, key[1])
                self._maybe_notify_native_sent(chain, txs[i])
                return

    def _maybe_notify_native_sent(self, chain, tx) -> None:
        """Desktop-notify a confirmed native (ETH/xDAI/…) send of ours. Token
        sends are notified from the ws Transfer-log path; native value carries
        no log, so it rides the confirmed tx (which has value_wei + recipient).
        Skips zero-value calls (plain contract interactions) and reverts."""
        if int(getattr(tx, "value_wei", 0) or 0) <= 0 or not getattr(
                tx, "success", False):
            return
        host = self.host
        notify = getattr(host, "notify", None) if host is not None else None
        if not callable(notify):
            return
        amount = format_balance(wei_to_ether(int(tx.value_wei)))
        title, body = transfer_notice(
            True, amount, chain.symbol,
            counterparty=tx.to_addr, chain_name=chain.name)
        icon = notification_icon(bundled_native_icon(chain.symbol), True)
        notify(title, body, icon)

    def _on_tx_still_pending(self, _chain, tx_hash: str) -> None:
        """A probe saw the tx still open — a contradicting reading, so cancel
        any tentative drop count. Without this reset the
        ``DROP_CONFIRM_READINGS`` guard counts *cumulative* readings over the
        whole session instead of *consecutive* ones, and a flappy
        load-balanced RPC (occasionally serving a stale backend) still falsely
        drops a pending tx — just more slowly."""
        self._drop_readings.pop(tx_hash, None)

    def _on_tx_dropped(self, chain, tx_hash: str) -> None:
        """PendingProbeWorker → here. The tx's nonce was consumed by a
        different tx, so this hash will never confirm. Flip the cached
        entry to the terminal ``dropped`` state (no longer pending, not
        a revert), drop the stored raw bytes, persist, and repaint the
        one row if it's on screen.

        Guarded by a CONSECUTIVE-reading count: a single "nonce spent + no
        receipt" reading is unreliable behind an RPC load balancer (a backend
        can return a null receipt for a tx that IS mined). Only act once the
        reading repeats ``DROP_CONFIRM_READINGS`` times across ticks — by then
        a mined tx's receipt has propagated and confirmed instead. A
        contradicting still-pending / confirmed reading resets the count
        (``_on_tx_still_pending`` / ``_on_receipt_confirmed``)."""
        seen = self._drop_readings.get(tx_hash, 0) + 1
        self._drop_readings[tx_hash] = seen
        if seen < self.DROP_CONFIRM_READINGS:
            log.debug(
                "tx %s looks dropped (%d/%d readings) — re-checking before "
                "believing it", tx_hash, seen, self.DROP_CONFIRM_READINGS,
            )
            return
        self._drop_readings.pop(tx_hash, None)
        from dataclasses import replace
        chain_id = chain.chain_id
        for key, txs in list(self._cache.items()):
            if key[0] != chain_id:
                continue
            for i, t in enumerate(txs):
                if t.hash != tx_hash:
                    continue
                if not t.pending:
                    return
                txs[i] = replace(
                    t, pending=False, dropped=True, raw_signed=None,
                )
                self._disk_cache.save(chain_id, key[1], txs)
                log.info(
                    "tx %s dropped — nonce %d already consumed by another tx",
                    tx_hash, t.nonce,
                )
                if self._panel is not None and self._rendered_for == key:
                    self._panel.update_tx_by_hash(txs[i])
                return

    # --- persistence shim ---------------------------------------------------

    def header_state(self) -> str:
        if self._panel is None:
            return ""
        return self._panel.header_state()

    def restore_header_state(self, state_hex: str) -> None:
        if self._panel is not None:
            self._panel.restore_header_state(state_hex)

    # --- lifecycle hooks ----------------------------------------------------

    def on_account_changed(self, address: str | None) -> None:
        self._update_live_account()   # re-target the live Transfer-log sub
        if address is None:
            if self._panel is not None:
                self._panel.clear()
            # Drop the rendered-view marker too. Without this, drag-
            # multi-selecting accounts in the wallet tree (which
            # emits None because there's no single "current") clears
            # the panel but leaves _rendered_for pointing at the
            # last-shown key — clicking back to that same account
            # then sees view_changed=False in _refresh and never
            # re-renders, so the panel stays empty.
            self._rendered_for = None
            return
        self._refresh(address)

    def on_chain_changed(self) -> None:
        self._update_live_account()   # re-target the live Transfer-log sub
        addr = self.host.selected_address if self.host else None
        if addr is not None:
            self._refresh(addr)

    def on_activated(self) -> None:
        addr = self.host.selected_address if self.host else None
        if addr is not None:
            # Force the fetch path even if the cache is empty — this
            # is the user explicitly opening the tab.
            self._refresh(addr, force_fetch=True)

    # --- external-send detection -------------------------------------------

    def _poll_external_nonce(self) -> None:
        """Timer tick: check the current account's on-chain nonce against
        our history. Only the displayed account, one in-flight at a time."""
        if self.host is None:
            return
        address = self.host.selected_address
        if not address:
            return
        chain = self.host.current_chain()
        key = (chain.chain_id, address.lower())
        if key in self._nonce_in_flight:
            return
        self._nonce_in_flight.add(key)
        worker = NonceCheckWorker(chain, address, key)
        worker.checked.connect(self._on_external_nonce)
        self.host.start_worker(worker)

    def _on_external_nonce(self, key, count) -> None:
        """``count`` = the chain's tx-sent count (next nonce), so the last
        sent nonce is ``count - 1``. If that's beyond the highest nonce we
        hold, a tx was sent elsewhere — drop the ``exhausted`` flag (our
        'full history' assumption is now stale) and re-fetch page 1, which
        merges + prepends the new row."""
        self._nonce_in_flight.discard(key)
        if count is None or self.host is None:
            return
        addr = self.host.selected_address
        if addr is None:
            return
        if key != (self.host.current_chain().chain_id, addr.lower()):
            return   # account/chain moved on since the poll started
        cached = self._cache.get(key) or []
        our_max = max((t.nonce for t in cached), default=-1)
        if count - 1 > our_max:
            self._exhausted.discard(key)
            self._fetch_page(key, addr, page=1)

    # --- core --------------------------------------------------------------

    def _is_active(self) -> bool:
        """True when this plugin is currently the active tab in its
        slot. Used to decide whether to actually hit Blockscout —
        switching accounts while the user is on the Tokens tab should
        just invalidate our cache view, not trigger network calls."""
        panel = self._panel
        if panel is None:
            return False
        # When the slot is single-plugin, the widget is always shown.
        # When multi-plugin, only the active one is visible.
        return panel.isVisible()

    def _kick_activities(self, chain, address: str,
                         txs: list[Transaction]) -> None:
        """Build Activity (verb + assets moved) for ``txs`` off-thread,
        sharing the disk ABI cache; feed it back to the panel if it's
        still showing this view. Called by the panel for newly-shown rows."""
        if self.host is None or self._panel is None or not txs:
            return
        key = (chain.chain_id, address.lower())
        worker = TxActivityWorker(chain, address, list(txs), self._abi_cache,
                                  self._abi_source)
        worker.loaded.connect(
            lambda _cid, _addr, acts, k=key: self._on_activities(k, acts))
        self.host.start_worker(worker)

    def _on_activities(self, key, acts) -> None:
        # Remember every real ticker these (Blockscout-built) activities
        # carry, so a later receipt/sim leg for the same token reads "EURe"
        # not "?". Harvest before merging.
        for a in acts.values():
            for leg in (*a.out, *a.inn):
                if leg.contract and leg.symbol and leg.symbol != "?":
                    self._symbol_cache[(key[0], leg.contract)] = leg.symbol
        merged = {h: self._fill_symbols(key[0], self._with_known_legs(key[0], h, a))
                  for h, a in acts.items()}
        self._activity_cache.update(key[0], key[1], merged)
        if self._panel is not None and self._rendered_for == key:
            self._panel.set_activities(merged)
        self._scan_coinless(key, merged)
        # Tokens still showing "?" aren't in any list or cache — read their
        # on-chain symbol() so the coins column names them (repaints on arrival).
        self._kick_token_meta(key[0], self._unknown_symbol_contracts(key[0], merged))

    def _scan_coinless(self, key: tuple[int, str],
                       acts: dict[str, Activity]) -> None:
        """Queue an RPC receipt scan for any just-resolved txs that came
        back with no coins — the tokentx index occasionally drops a tx's
        transfers, and the receipt still has them. Rare, so best-effort,
        deduped, and capped; results fold in via note_transfer_legs."""
        if self.host is None:
            return
        hashes = [h for h, a in acts.items()
                  if not a.out and not a.inn and not a.muted
                  and h not in self._receipt_checked][:30]
        if not hashes:
            return
        chain = self.host.current_chain()
        if chain.chain_id != key[0]:
            return
        self._receipt_checked.update(hashes)
        worker = ReceiptScanWorker(chain, hashes)
        worker.found.connect(
            lambda cid, h, logs, viewer=key[1]:
            self.note_transfer_legs(cid, h, logs, viewer))
        self.host.start_worker(worker)

    def _with_known_legs(self, chain_id: int, tx_hash: str,
                         activity: Activity) -> Activity:
        """Fold in ERC-20 legs learned from a receipt / simulation that
        Blockscout's tokentx hasn't indexed yet (so a just-confirmed or
        still-pending swap shows its coins immediately, not after a
        restart). Idempotent — contracts the Activity already lists are
        skipped, and the native ETH legs are preserved."""
        extra = self._known_legs.get(tx_hash)
        if not extra:
            return activity
        out_c, in_c = extra
        have = {leg.contract for leg in (*activity.out, *activity.inn)
                if leg.contract}

        def make(contract: str) -> AssetLeg:
            info = self.host.token_info(chain_id, contract) if self.host else None
            sym = (getattr(info, "symbol", None)
                   or self._symbol_cache.get((chain_id, contract))
                   or "?")
            return AssetLeg(sym, contract)

        new_out = activity.out + tuple(
            make(c) for c in out_c if c not in have)
        new_in = activity.inn + tuple(
            make(c) for c in in_c if c not in have)
        if new_out == activity.out and new_in == activity.inn:
            return activity
        from dataclasses import replace
        return replace(activity, out=new_out, inn=new_in)

    # --- symbol resolution for non-listed tokens ---------------------------

    def _best_symbol(self, chain_id: int, contract: str) -> str | None:
        """The real ticker for ``contract``, list-independent: curated token
        list → on-chain metadata cache → symbols harvested from Blockscout
        tokentx. None when we genuinely don't know it yet (caller fetches)."""
        info = self.host.token_info(chain_id, contract) if self.host else None
        sym = getattr(info, "symbol", None)
        if sym:
            return sym
        meta = self._token_meta.get(chain_id, contract)
        if meta and meta.get("symbol"):
            return meta["symbol"]
        return self._symbol_cache.get((chain_id, contract.lower()))

    def _fill_symbols(self, chain_id: int, activity: Activity) -> Activity:
        """Rewrite any leg whose symbol is unknown ("?"/empty) with the token's
        real on-chain symbol when we can resolve it — so a token outside every
        curated list shows its ticker instead of a bare "?"."""
        def fix(legs: tuple[AssetLeg, ...]) -> tuple[tuple[AssetLeg, ...], bool]:
            out: list[AssetLeg] = []
            changed = False
            for leg in legs:
                if leg.contract and leg.symbol in ("", "?"):
                    s = self._best_symbol(chain_id, leg.contract)
                    if s and s != leg.symbol:
                        out.append(AssetLeg(s, leg.contract))
                        changed = True
                        continue
                out.append(leg)
            return tuple(out), changed

        new_out, c1 = fix(activity.out)
        new_in, c2 = fix(activity.inn)
        if not (c1 or c2):
            return activity
        from dataclasses import replace
        return replace(activity, out=new_out, inn=new_in)

    def _unknown_symbol_contracts(self, chain_id: int,
                                  acts: dict[str, Activity]) -> set[str]:
        """Contracts still showing "?" after a fill — their on-chain symbol
        isn't cached anywhere yet, so they're the ones worth a multicall."""
        out: set[str] = set()
        for a in acts.values():
            for leg in (*a.out, *a.inn):
                if (leg.contract and leg.symbol in ("", "?")
                        and self._best_symbol(chain_id, leg.contract) is None):
                    out.add(leg.contract.lower())
        return out

    def _kick_token_meta(self, chain_id: int, contracts: set[str]) -> None:
        """Fetch on-chain (symbol, decimals) for unknown-ticker contracts so
        even tokens we've never held — and old txs whose token was never
        cached — resolve. Deduped per contract; skips ones already cached."""
        if self.host is None or not contracts:
            return
        chain = self.host.current_chain()
        if chain.chain_id != chain_id:
            return
        todo = [c for c in self._token_meta.missing(chain_id, list(contracts))
                if c not in self._meta_inflight]
        if not todo:
            return
        self._meta_inflight.update(todo)
        from .tokens import MetadataWorker
        worker = MetadataWorker(chain, todo)
        worker.fetched.connect(self._on_token_meta)
        worker.failed.connect(
            lambda _e, cs=todo: self._meta_inflight.difference_update(cs))
        self.host.start_worker(worker)

    def _on_token_meta(self, chain_id, meta) -> None:
        """On-chain metadata landed: cache it, harvest the symbols, and repaint
        just the rows whose coins were waiting on one of these tickers."""
        chain_id = int(chain_id)
        meta = meta or {}
        self._token_meta.put_many(chain_id, meta)
        fetched = {c.lower() for c in meta}
        for c, m in meta.items():
            s = (m or {}).get("symbol")
            if s:
                self._symbol_cache[(chain_id, c.lower())] = s
        self._meta_inflight.difference_update(fetched)
        if self._panel is None or self._rendered_for is None:
            return
        key = self._rendered_for
        if key[0] != chain_id:
            return
        cached = self._activity_cache.load(chain_id, key[1])
        affected = {
            h: self._fill_symbols(chain_id, a)
            for h, a in cached.items()
            if any(leg.contract in fetched for leg in (*a.out, *a.inn))
        }
        if affected:
            self._activity_cache.update(chain_id, key[1], affected)
            self._panel.set_activities(affected)

    def note_transfer_legs(self, chain_id: int, tx_hash: str,
                           logs: object, viewer: str) -> None:
        """Record the ERC-20 contracts a tx's logs moved through ``viewer``
        (from a receipt or a pre-broadcast simulation) and repaint that
        row's activity with the coins folded in."""
        out_c, in_c = transfer_legs_from_logs(logs, viewer)
        if not out_c and not in_c:
            return
        self._known_legs[tx_hash] = (out_c, in_c)
        key = (chain_id, viewer.lower())
        panel = self._panel
        on_view = panel is not None and self._rendered_for == key
        # Base activity from the live panel if we're on this view, else the
        # disk cache — so a background receipt scan persists its coins even
        # if the user has navigated away (a pending tx with no resolved
        # activity yet stays in _known_legs and folds in once it resolves).
        base = (panel.activity_for(tx_hash)
                if on_view and panel is not None else None)
        if base is None:
            base = self._activity_cache.load(chain_id, key[1]).get(tx_hash)
        if base is None:
            return
        merged = {tx_hash: self._fill_symbols(
            chain_id, self._with_known_legs(chain_id, tx_hash, base))}
        self._activity_cache.update(chain_id, key[1], merged)
        if on_view and panel is not None:
            panel.set_activities(merged)
        self._kick_token_meta(
            chain_id, self._unknown_symbol_contracts(chain_id, merged))

    def _refresh(self, address: str, force_fetch: bool = False) -> None:
        """Render cached transactions immediately (if any) and kick a
        background fetch when the plugin is currently visible and
        we're not already refreshing this (chain, address) view.
        ``force_fetch=True`` skips the visibility gate — used by
        ``on_activated`` so an explicit tab open always refreshes."""
        if self.host is None or self._panel is None:
            # Plugin not yet attached, or widget not built — nothing
            # to do, the next activation will pick up state.
            return
        chain = self.host.current_chain()
        key = (chain.chain_id, address.lower())

        self._panel.set_context(chain, address)
        # Wire the Activity column's deps (shared icon cache + token-logo
        # lookup) and the off-thread Activity builder. All idempotent.
        ic_fn = getattr(self.host, "icon_cache", None)
        ti_fn = getattr(self.host, "token_info", None)
        self._panel.set_icon_deps(ic_fn() if callable(ic_fn) else None,
                                  ti_fn if callable(ti_fn) else None)
        self._panel.set_activity_kicker(self._kick_activities)
        cached = self._cache.get(key)
        if cached is None:
            # First time this (chain, addr) is seen this session — try
            # the disk cache. Confirmed txs don't change, so cached
            # bytes from a prior run are always safe to render. Also
            # drop any received txs that an earlier (pre-filter) build
            # may have written to disk — keeping them would break the
            # nonce-monotonic sort.
            disk = self._disk_cache.load(chain.chain_id, address)
            if disk:
                addr_l = address.lower()
                disk = [t for t in disk if t.from_addr.lower() == addr_l]
                self._cache[key] = disk
                cached = disk
                # No page cursor to seed: "load older" resumes from the
                # cache's oldest block (_oldest_block), so a big cache picks
                # up exactly where it left off with no page-walk.
        # Re-render only when the panel currently shows a *different*
        # view. If it's the same (chain, addr) we already painted
        # (e.g. user just toggled away to Tokens and back), leaving
        # the table alone preserves both content AND the scroll
        # position — exactly what tabs in a browser do.
        view_changed = self._rendered_for != key
        if view_changed and cached is not None:
            # Render only the first INITIAL_VISIBLE rows. The rest
            # of the cache stays in memory and is revealed
            # incrementally by ``_on_scroll_bottom`` before any
            # Blockscout call is made; once the cache is exhausted
            # we fall through to the network path. Rendering 4000+
            # rows up-front froze the main thread for ~900 ms on
            # busy wallets even with updates suspended — at 200
            # rows it's well under 50 ms.
            cap = min(self.INITIAL_VISIBLE, len(cached))
            self._displayed_count[key] = cap
            # Seed last session's resolved activities so the rows paint
            # their verb + coins immediately; only the still-unknown ones
            # get a worker. Re-scan any cached-coinless rows by receipt too,
            # so a tokentx miss persisted last session gets a second chance
            # without forcing a full re-resolve.
            primed = {h: self._fill_symbols(chain.chain_id, a)
                      for h, a in self._activity_cache.load(
                          chain.chain_id, address).items()}
            self._panel.prime_activities(primed)
            self._scan_coinless(key, primed)
            self._panel.show_transactions(cached[:cap])
            self._rendered_for = key
            # Old caches (and old txs) may carry "?" for tokens never held —
            # read their on-chain symbol so they resolve on this visit too.
            self._kick_token_meta(
                chain.chain_id, self._unknown_symbol_contracts(
                    chain.chain_id, primed))
        elif view_changed and (force_fetch or self._is_active()):
            self._displayed_count[key] = 0
            self._panel.show_loading()
            self._rendered_for = key

        if not (force_fetch or self._is_active()):
            return
        if not self._source.supports(chain):
            self._panel.show_error(
                f"Transactions aren't available for {chain.name}."
            )
            return
        # Check the on-chain nonce *now*, not just on the 30s timer — so
        # opening / switching to an account immediately catches a tx sent
        # from another client, even when the cache looks complete and the
        # short-circuit below would otherwise skip the fetch.
        self._poll_external_nonce()
        # If we already hold the wallet's full sent history (nonce 0
        # present + contiguous), there's nothing newer to refresh and
        # nothing older to scroll for. Skip the network call. (The nonce
        # check above re-opens this if a send happened elsewhere.)
        if _is_full_history(cached or []):
            self._exhausted.add(key)
            return
        # Always (re-)fetch page 1 on open: cheapest way to pick up
        # txs the user might have sent from another wallet client
        # since the last visit. Older pages come from scroll.
        self._fetch_page(key, address, page=1)

    def _oldest_block(self, key) -> int | None:
        """Block of the oldest loaded tx for this view — the cursor for
        paging older (explorer ``endblock``), which sidesteps the
        ``page × offset ≤ 10000`` window."""
        txs = self._cache.get(key) or []
        return min((t.block_number for t in txs), default=None)

    def _fetch_page(self, key, address: str, page: int,
                    walk_on_overlap: bool = False,
                    before_block=None) -> None:
        """Kick a single-page fetch. No-op if a fetch for this key is
        already in flight or if the history is known to be exhausted.

        ``walk_on_overlap`` distinguishes the two fetch reasons:
          - False (default): "refresh newest" — fetch page 1 to pick up
            anything new, but if the page returns only entries already
            cached, stop. Used on _refresh (tab activation / account
            select). Tab switching costs at most one HTTP call.
          - True: "load older" — the user has scrolled past the cache
            and wants more history. If the page returns only overlap
            (typical when resuming an interrupted backfill), advance
            to the next page until we find genuinely new older data.
        """
        if self.host is None or self._panel is None:
            return
        if key in self._in_flight or key in self._exhausted:
            return
        chain = self.host.current_chain()
        self._in_flight.add(key)
        worker = TransactionsWorker(
            self._source, chain, address, page=page, before_block=before_block,
        )
        worker.fetched.connect(
            lambda c, a, p, t, m, ro, w=walk_on_overlap, bb=before_block:
                self._on_page_fetched(c, a, p, t, m, walk_on_overlap=w,
                                      raw_oldest=ro, requested_before=bb)
        )
        worker.failed.connect(
            lambda msg, k=key: self._on_failed(k, msg)
        )
        self.host.start_worker(worker)

    def _on_scroll_bottom(self) -> None:
        """User reached the bottom. Two paths:

        1. ``displayed_count < len(cache)`` — we initially rendered
           only a window; reveal the next INITIAL_VISIBLE rows
           from the in-memory cache (no network call).
        2. Otherwise — the cache is fully visible. Fetch older
           pages from Blockscout, walking through cached overlap.
        """
        if self.host is None or self._panel is None:
            return
        addr = self.host.selected_address
        if not addr:
            return
        chain = self.host.current_chain()
        key = (chain.chain_id, addr.lower())
        cached = self._cache.get(key) or []
        shown = self._displayed_count.get(key, 0)
        if shown < len(cached):
            more = cached[shown:shown + self.INITIAL_VISIBLE]
            self._panel.append_transactions(more)
            self._displayed_count[key] = shown + len(more)
            return
        # Cache fully shown → page older via the block cursor (no 10k cap).
        self._fetch_page(key, addr, page=1, walk_on_overlap=True,
                         before_block=self._oldest_block(key))

    def _on_page_fetched(self, chain_id: int, address_lower: str,
                         page_idx: int, page: list, has_more: bool,
                         walk_on_overlap: bool = False,
                         raw_oldest: int | None = None,
                         requested_before: int | None = None) -> None:
        """One page arrived. Merge it into the cache, persist, advance
        the paging cursor, and incrementally update the visible table
        — never rebuilding it from the full cache (which can be tens
        of thousands of rows). ``walk_on_overlap`` mirrors the flag
        set by _fetch_page: True for scroll-driven calls (keep walking
        through cached overlap until new data lands), False for the
        cheap refresh-newest path."""
        key = (chain_id, address_lower)
        self._in_flight.discard(key)
        if self._panel is None:
            return
        existing = self._cache.get(key)
        if existing is None:
            # Seed from disk before merging. A background fetch (the 30s nonce
            # poll detecting an external send) can land before this view is
            # ever rendered; without seeding, this page would *replace* the
            # full on-disk history with just itself in memory and shadow it for
            # the rest of the session — the render path only loads disk when
            # the key is absent from memory. A just-sent pending row then
            # merged into that stub showed up as the only row in the list.
            existing = self._disk_cache.load(chain_id, address_lower) or []
        existing_hashes = {t.hash for t in existing}
        merged = merge_txs(page, existing)
        self._cache[key] = merged
        # Save only when the merge actually changed something. A page-1
        # refresh of an already-complete view otherwise re-serialized the
        # ENTIRE cache on the main thread on every tab open / account switch —
        # ~150 ms of json.dumps for a 12.7 MB busy-wallet cache. Value
        # equality (frozen dataclasses, deterministic merge order) catches
        # both new rows and updated fields (pending→confirmed, reorg fixes).
        if merged != existing:
            self._disk_cache.save(chain_id, address_lower, merged)

        new_rows = [t for t in page if t.hash not in existing_hashes]
        oldest = min((t.block_number for t in merged), default=None)
        # The older-walk cursor follows the RAW oldest block fetched (incl.
        # received txs the sent filter strips) so a receive-heavy account
        # doesn't stall on a window that held only received txs. Fall back to
        # the sent oldest when the worker reported none (e.g. unit tests).
        cursor = raw_oldest if raw_oldest is not None else oldest
        # "Advanced" = the raw cursor went strictly below what we asked for;
        # if it didn't (one block heavier than a page) and nothing new
        # surfaced, the walk has genuinely bottomed out.
        cursor_advanced = raw_oldest is not None and (
            requested_before is None or raw_oldest < requested_before)

        if not has_more or _is_full_history(merged):
            self._exhausted.add(key)
        elif walk_on_overlap:
            # Paging older via the block cursor — advance by the RAW oldest
            # block. Keep walking until a full INITIAL_BATCH of new older rows
            # surfaces or the page runs short (has_more=False, above). A
            # received-only window still advances the cursor, so the walk
            # marches past it instead of giving up on a sparse-sent account.
            if not cursor_advanced and not new_rows:
                self._exhausted.add(key)
            elif len(new_rows) < self.INITIAL_BATCH:
                self._fetch_page(key, address_lower, page=1,
                                 walk_on_overlap=True, before_block=cursor)
        elif len(merged) < self.INITIAL_BATCH:
            # Fresh, sparse, OR partially-cached account (< INITIAL_BATCH rows).
            # Page 1 may just re-confirm the few rows we hold — a receive-heavy
            # account's sent txs are sparse, and an interrupted earlier load can
            # leave a stub in cache — so do NOT require this page to have
            # *progressed* (it won't have, when it only overlaps that stub).
            # Walk older from the oldest row we hold to backfill the view; the
            # walk stops itself once no new older rows surface, and a genuinely
            # full account is already caught by the has_more / full-history check
            # above, so a large cache still skips this.
            self._fetch_page(key, address_lower, page=1,
                             walk_on_overlap=True, before_block=oldest)

        # Only touch the panel if the user is still on this view.
        if (self.host is None
                or self.host.selected_address is None
                or self.host.selected_address.lower() != address_lower
                or self.host.current_chain().chain_id != chain_id):
            return
        if not new_rows:
            # Empty page + empty cache = brand-new account with
            # zero history. Flip the panel from "Loading…" to the
            # empty-state message rather than leaving it spinning
            # forever.
            if not merged:
                self._panel.show_empty()
                self._displayed_count[key] = 0
            return
        # Newer entries (refresh case, nonce above old top) prepend;
        # older entries (scroll case) append. Both grow the visible
        # window without re-rendering the rest of the table.
        shown = self._displayed_count.get(key, 0)
        top_nonce = existing[0].nonce if existing else -1
        newer = [t for t in new_rows if t.nonce > top_nonce]
        older = [t for t in new_rows if t.nonce <= top_nonce]
        if newer:
            newer.sort(key=lambda t: t.nonce, reverse=True)
            self._panel.prepend_transactions(newer)
            shown += len(newer)
        # Append older rows only if the user has already scrolled
        # past the existing window — otherwise they'd appear between
        # the displayed top section and the not-yet-revealed cache
        # entries below, which would look weird. We just save them
        # to the cache and let the next scroll-to-bottom reveal them.
        if older and shown >= len(existing):
            older.sort(key=lambda t: t.nonce, reverse=True)
            self._panel.append_transactions(older)
            shown += len(older)
        self._displayed_count[key] = shown

    def _on_failed(self, key: tuple[int, str], msg: str) -> None:
        self._in_flight.discard(key)
        log.warning("transactions fetch failed for %s/%s: %s",
                    key[0], key[1], msg)
        if self.host is None or self._panel is None:
            return
        addr = self.host.selected_address
        if addr is None or addr.lower() != key[1]:
            return
        if self.host.current_chain().chain_id != key[0]:
            return
        if not self._is_active():
            return
        self._panel.show_error(msg)


# --- panel ----------------------------------------------------------------


def _speedup_icon() -> QIcon:
    """Fast-forward glyph for the Speed-up action (theme, with fallback)."""
    return QIcon.fromTheme("media-seek-forward", QIcon.fromTheme("go-up"))


def _cancel_tx_icon() -> QIcon:
    """Stop glyph for the Cancel-transaction action."""
    return QIcon.fromTheme("process-stop", QIcon.fromTheme("dialog-cancel"))


# Activity-table columns: Status | Nonce | gap | Time | gap | Verb | Coins.
# The two empty gap columns Stretch, so a wide window "justifies" the row —
# Nonce hard left, Time centred, Activity + coins hard right — instead of
# leaving all the dead space trailing on the right.
(_C_STATUS, _C_NONCE, _C_GAP1, _C_TIME,
 _C_GAP2, _C_VERB, _C_COINS) = range(7)
_N_COLS = 7


class TransactionListPanel(QWidget):
    """Right pane / Transactions tab: top-level txs for the selected
    account, newest first.

    Signals:
      scrolled_to_bottom      — user reached the bottom; load more.
      tx_details_requested    — user double-clicked or hit
                                "Show details…" in the context menu.
                                Plugin opens TransactionDetailsDialog
                                with ABI decoding.
    """

    scrolled_to_bottom = Signal()
    tx_details_requested = Signal(object)   # Transaction instance
    replace_requested = Signal(object, bool)  # (Transaction, cancel)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        # Status / Nonce / Time / Hash. The Status column has an empty
        # label — the ✓/✗ glyph speaks for itself, and dropping the word
        # "Status" lets the column be tight against the left edge.
        self.table = QTableWidget(0, _N_COLS)
        self.table.setHorizontalHeaderLabels(
            ["", "Nonce", "", "Time", "", "Activity", ""])
        # "Activity" is split into two plain columns so each renders with
        # standard cell painting (no custom delegate, no whole-line image —
        # both broke selection/hover/scroll or font rendering under the
        # user's theme): col 3 is the decoded **verb as text** (theme font,
        # native selection colour); col 4 is a small **coins-only icon**
        # (assets moved, out → in) with no text inside it. Until a row's
        # activity resolves (or on a chain with no source) both stay empty.
        self._activities: dict[str, Activity] = {}
        self._icons: IconCache | None = None
        self._token_info: Callable[..., object] | None = None
        # Plugin-supplied callback (chain, address, txs) → fetch their
        # Activity off-thread; the panel calls it for newly-shown rows.
        self._activity_kicker: Callable[..., None] | None = None
        # Lazy coin-icon arrivals are coalesced: collect the contracts whose
        # logo just downloaded and repaint each affected row ONCE on a short
        # timer, instead of re-rendering every shared-coin row per arrival.
        self._icon_dirty: set[str] = set()
        self._icon_timer = QTimer(self)
        self._icon_timer.setSingleShot(True)
        self._icon_timer.setInterval(60)
        self._icon_timer.timeout.connect(self._flush_icon_updates)
        # Icon area: 16px tall keeps the Status check at its native theme
        # size (a taller iconSize re-picked/scaled the glyph). 180px wide
        # fits up to 4+arrow+4 coins; only the stretched coins column uses
        # the width — Status is Fixed and text columns carry no icon.
        self.table.setIconSize(QSize(180, 16))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        # Cell padding + no hover highlight (same as TokenListPanel). Safe
        # again now the Activity column is a plain icon cell rather than a
        # custom delegate: the ``:hover`` rule no longer blanks it and the
        # row selection highlight reaches it normally.
        self.table.setStyleSheet(
            "QTableView::item {"
            "  padding: 3px 6px;"
            "  border: 0;"
            "}"
            "QTableView::item:hover { background: transparent; }"
        )
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        # Enter / Return on the focused transactions table opens
        # the details dialog for the highlighted row — same as
        # double-click.
        self.table.installEventFilter(self)
        # ElideRight on the verb (the only stretchable text column): when
        # the window narrows, a long method name is truncated from the end
        # ("increase_unlock_ti…") rather than the middle. Short-text cells
        # (Nonce/Time, ResizeToContents) always fit, so this only bites the
        # verb column.
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        # Scroll-to-bottom drives the load-more UX.
        self.table.verticalScrollBar().valueChanged.connect(
            self._on_scroll_change
        )
        h = self.table.horizontalHeader()
        # Status / Nonce / Time auto-fit content (no user-drag — there's
        # nothing meaningful to widen them to). Hash stretches to fill
        # the remaining space; its rendered text is the short
        # 0x1234…abcd form, so the wider cell looks padded rather than
        # full-bleed.
        # Status is Fixed-width (not ResizeToContents) so its 16px icon
        # column stays tight regardless of iconSize. Nonce/Time/Verb fit
        # their text. The coins column stretches to fill — its icon is
        # left-aligned, so the coins sit right after the verb with the
        # spare space trailing.
        h.setSectionResizeMode(_C_STATUS, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(_C_STATUS, 34)
        h.setSectionResizeMode(_C_NONCE, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_C_TIME, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_C_COINS, QHeaderView.ResizeMode.ResizeToContents)
        # Draw the coins icon 1:1 so the style can't rescale it per row (which
        # renders the thin flow arrow at inconsistent sizes). See the delegate.
        self.table.setItemDelegateForColumn(_C_COINS, _CoinsIconDelegate(self.table))
        # The two empty gap columns soak up the slack on a wide window so
        # the row justifies (Nonce left, Time centre, Activity+coins right)
        # instead of pooling whitespace on the right; they shrink to the
        # minimum as the window narrows.
        h.setSectionResizeMode(_C_GAP1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(_C_GAP2, QHeaderView.ResizeMode.Stretch)
        # Verb is sized by hand (_fit_verb_column): just wide enough for its
        # text so the coins sit right after it, but capped to the room left
        # after the gaps' minimum, so it elides long names when the window
        # narrows rather than overflowing. Coins are content-sized.
        h.setSectionResizeMode(_C_VERB, QHeaderView.ResizeMode.Fixed)
        h.setMinimumSectionSize(24)
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Widest verb text seen so far (px) — the verb column's natural
        # width; tracked incrementally so the fit never scans all rows.
        self._max_verb_px = 0
        v.addWidget(self.table, 1)

        # The empty-state / loading / error label sits stacked under the
        # table; we toggle visibility based on state.
        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_lbl.setVisible(False)
        v.addWidget(self.status_lbl)

        # Action buttons — same flat 28×28 style as the Tokens panel,
        # mounted by Slot on the shared bottom-right row beside the
        # chain selector. Mirrors the row-level right-click menu so
        # every action is reachable both ways.
        style = self.style()
        self.btn_details = QPushButton()
        self.btn_details.setIcon(QIcon.fromTheme(
            "document-properties",
            style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView),
        ))
        self.btn_details.setToolTip("Details")
        self.btn_details.setEnabled(False)

        self.btn_explorer = QPushButton()
        # The freedesktop "internet/web browser" icons. Some themes
        # ship one, some the other; chain them and finally fall back
        # to a Unicode globe so the button always carries *some*
        # signifier even on a stripped-down system.
        _browser_icon = QIcon.fromTheme(
            "applications-internet",
            QIcon.fromTheme("internet-web-browser"),
        )
        if _browser_icon.isNull() or not _browser_icon.availableSizes():
            self.btn_explorer.setText("🌐")
        else:
            self.btn_explorer.setIcon(_browser_icon)
        self.btn_explorer.setToolTip("Open in explorer")
        self.btn_explorer.setEnabled(False)

        self.btn_copy_hash = QPushButton()
        self.btn_copy_hash.setIcon(QIcon.fromTheme(
            "edit-copy",
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton),
        ))
        self.btn_copy_hash.setToolTip("Copy hash")
        self.btn_copy_hash.setEnabled(False)

        for b in (self.btn_details, self.btn_explorer, self.btn_copy_hash):
            b.setFlat(True)
            b.setMaximumSize(28, 28)
            b.setIconSize(QSize(16, 16))
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.btn_details.clicked.connect(self._details_for_selected)
        self.btn_explorer.clicked.connect(self._explorer_for_selected)
        self.btn_copy_hash.clicked.connect(self._copy_hash_for_selected)
        # Ctrl+C copies the selected transaction's hash, scoped to the
        # table so it only fires when this tab has focus.
        copy_act = QAction(self.table)
        copy_act.setShortcut(QKeySequence.StandardKey.Copy)
        copy_act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_act.triggered.connect(self._copy_hash_for_selected)
        self.table.addAction(copy_act)
        self.table.itemSelectionChanged.connect(self._update_action_buttons)

        # Set by MainWindow before render so we can build explorer URLs
        # and compute SENT/RECEIVED direction labels.
        self._chain = None
        self._viewer: str | None = None

    def action_widgets(self) -> list[QWidget]:
        """Buttons the slot mounts on its shared bottom-right row."""
        return [self.btn_details, self.btn_explorer, self.btn_copy_hash]

    def _selected_tx(self) -> Transaction | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self._tx_at(rows[0].row())

    def _details_for_selected(self) -> None:
        tx = self._selected_tx()
        if tx is not None:
            self.tx_details_requested.emit(tx)

    def _explorer_for_selected(self) -> None:
        tx = self._selected_tx()
        if tx is not None:
            self._open_in_explorer(tx)

    def _copy_hash_for_selected(self) -> None:
        tx = self._selected_tx()
        if tx is not None:
            QApplication.clipboard().setText(tx.hash)

    def _update_action_buttons(self) -> None:
        tx = self._selected_tx()
        has_tx = tx is not None
        self.btn_details.setEnabled(has_tx)
        self.btn_copy_hash.setEnabled(has_tx)
        self.btn_explorer.setEnabled(
            has_tx and self._chain is not None and bool(self._chain.explorer)
        )

    def set_context(self, chain, viewer_address: str) -> None:
        self._chain = chain
        self._viewer = viewer_address
        self._update_action_buttons()

    # --- Activity column (verb + coins moved) -------------------------------

    def activity_for(self, tx_hash: str) -> Activity | None:
        """The Activity currently held for ``tx_hash`` (if its worker has
        resolved), so the plugin can fold late-arriving receipt coins in."""
        return self._activities.get(tx_hash)

    def prime_activities(self, mapping: dict) -> None:
        """Seed the activity map from the disk cache *before* the rows are
        built, so _populate_row paints them with no worker round-trip and
        _request_activities only kicks the ones still missing."""
        self._activities.update(mapping)

    def set_icon_deps(self, icon_cache: IconCache | None,
                      token_info: object) -> None:
        """Wire the shared icon cache (+ optional token_info logo lookup)
        the Activity column uses to paint coins. Connected once; lazy icon
        arrivals repaint the rows that reference them."""
        if self._icons is icon_cache:
            return
        self._icons = icon_cache
        self._token_info = token_info if callable(token_info) else None
        if icon_cache is not None:
            icon_cache.icon_ready.connect(self._on_icon_ready)

    def set_activities(self, mapping: dict) -> None:
        """Merge a freshly-built ``{hash: Activity}`` and fill the rows that
        just gained one (verb text + coins icon)."""
        if not mapping:
            return
        self._activities.update(mapping)
        for row in range(self.table.rowCount()):
            tx = self._tx_at(row)
            if tx is None or tx.hash not in mapping:
                continue
            self._apply_activity(row, mapping[tx.hash])
        self._fit_verb_column()

    def _apply_activity(self, row: int, activity: Activity) -> None:
        """Write an Activity into its two cells: the verb (col 3, text) and
        the moved-coins icon (col 4)."""
        summary = self._build_summary(activity)
        verb = self.table.item(row, _C_VERB)
        if verb is not None:
            verb.setText(summary.verb)
            self._max_verb_px = max(
                self._max_verb_px,
                self.table.fontMetrics().horizontalAdvance(summary.verb))
            if summary.muted:
                verb.setForeground(self.table.palette().color(
                    QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text))
        coins = self.table.item(row, _C_COINS)
        if coins is not None:
            coins.setData(_COINS_ROLE, summary)
            coins.setToolTip(self._coins_tooltip(summary))

    @staticmethod
    def _coins_tooltip(summary: TxSummary) -> str:
        """Name the moved assets for the coins column — it's icon-only, so a
        hover is the only way to tell USDC from USDT (issue #17)."""
        out = ", ".join(c.symbol for c in summary.out)
        inn = ", ".join(c.symbol for c in summary.inn)
        if not summary.show_arrow:          # approval: the approved token only
            return f"Approved {out}" if out else ""
        if out and inn:
            return f"{out} → {inn}"
        if out:
            return f"Sent {out}"
        if inn:
            return f"Received {inn}"
        return ""

    def _fit_verb_column(self) -> None:
        """Size the verb column to its text, but never wider than the room
        left after the fixed columns *and the two gaps' minimum* — so the
        coins always fit, the verb elides when squeezed, no horizontal
        scrollbar appears, and on a wide window the leftover goes to the
        gaps (justifying the row) rather than to the verb."""
        t = self.table
        if t.columnCount() < _N_COLS:
            return
        hdr = t.horizontalHeader()
        others = sum(t.columnWidth(i)
                     for i in (_C_STATUS, _C_NONCE, _C_TIME, _C_COINS))
        gaps_min = 2 * hdr.minimumSectionSize()
        avail = t.viewport().width() - others - gaps_min
        # text + the cell's L/R padding (stylesheet 6px each) + a little
        # slack so the widest verb doesn't elide when there's room — but
        # never narrower than the "Activity" header itself, so before any
        # activity has resolved the header reads cleanly.
        head = t.horizontalHeaderItem(_C_VERB)
        header_w = hdr.fontMetrics().horizontalAdvance(
            head.text() if head else "Activity") + 28
        natural = max(self._max_verb_px + 24, header_w)
        t.setColumnWidth(_C_VERB, max(24, min(natural, max(24, avail))))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_verb_column()

    def _build_summary(self, activity: Activity) -> TxSummary:
        return TxSummary(
            activity.verb,
            tuple(self._coin(leg) for leg in activity.out),
            tuple(self._coin(leg) for leg in activity.inn),
            show_arrow=activity.show_arrow,
            muted=activity.muted,
        )

    def _coin(self, leg) -> Coin:
        if leg.contract is None:                      # native coin
            return Coin(leg.symbol, bundled_native_icon(leg.symbol))
        chain_id = self._chain.chain_id if self._chain is not None else 0
        pix = self._icons.get(chain_id, leg.contract) if self._icons is not None else None
        if pix is None and self._icons is not None and self._token_info is not None:
            entry = self._token_info(chain_id, leg.contract)
            url = getattr(entry, "logo_uri", None)
            if url:
                self._icons.request(chain_id, leg.contract, url)   # lazy; repaints on arrival
        return Coin(leg.symbol, pix)

    def _on_icon_ready(self, chain_id: int, contract: str) -> None:
        # Don't repaint per arrival — a burst of logos (e.g. a swap's two
        # tokens, or the Tokens panel warming the shared cache) would each
        # sweep the whole list. Coalesce and flush once.
        self._icon_dirty.add(contract.lower())
        self._icon_timer.start()

    def _flush_icon_updates(self) -> None:
        dirty = self._icon_dirty
        self._icon_dirty = set()
        if not dirty:
            return
        for row in range(self.table.rowCount()):
            tx = self._tx_at(row)
            act = self._activities.get(tx.hash) if tx is not None else None
            if act is None:
                continue
            if any((leg.contract or "") in dirty
                   for leg in (*act.out, *act.inn)):
                # Only the coins cell depends on the logo; leave the verb
                # cell untouched so just the picture refreshes, not the row.
                # Re-store the (rebuilt, now logo-bearing) summary; the
                # delegate repaints it.
                coins = self.table.item(row, _C_COINS)
                if coins is not None:
                    coins.setData(_COINS_ROLE, self._build_summary(act))

    def set_activity_kicker(self, fn: Callable[..., None] | None) -> None:
        self._activity_kicker = fn

    def _request_activities(self, txs: list[Transaction]) -> None:
        """Ask the plugin to build Activity for any of ``txs`` we don't
        have yet. Called from the render methods so every path (initial,
        append, prepend) enriches the rows it just added."""
        if self._activity_kicker is None or self._chain is None or not self._viewer:
            return
        pending = [t for t in txs if t.hash not in self._activities]
        if pending:
            self._activity_kicker(self._chain, self._viewer, pending)

    def _on_scroll_change(self, value: int) -> None:
        """Emit ``scrolled_to_bottom`` when the user reaches the
        bottom of the table. The 4-pixel slack matches Qt's default
        item-view fuzz so a kinetic scroll that stops a hair short
        still triggers."""
        bar = self.table.verticalScrollBar()
        if bar.maximum() > 0 and value >= bar.maximum() - 4:
            self.scrolled_to_bottom.emit()

    def header_state(self) -> str:
        """Hex-encoded QHeaderView.saveState() — captures column widths,
        order, and any sort indicator. MainWindow persists this on close
        and restores on startup."""
        return bytes(
            self.table.horizontalHeader().saveState().toHex().data()
        ).decode()

    def restore_header_state(self, state_hex: str) -> None:
        if not state_hex:
            return
        try:
            from PySide6.QtCore import QByteArray
            self.table.horizontalHeader().restoreState(
                QByteArray.fromHex(state_hex.encode())
            )
        except Exception:
            pass

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
        self._activities.clear()
        self._max_verb_px = 0
        self._chain = None
        self._viewer = None

    def show_transactions(self, txs: list[Transaction]) -> None:
        if not txs:
            self.show_empty()
            return
        self.status_lbl.setVisible(False)
        self._max_verb_px = 0   # this view's verbs re-measure from scratch
        h = self.table.horizontalHeader()
        col_count = self.table.columnCount()
        # ResizeToContents columns re-measure all rows of that column
        # on every setItem when an item is being REPLACED (not on the
        # initial add to an empty table). With 2000+ rows that's
        # O(N²) — ~35 s for a single bulk repopulate triggered by a
        # pending-tx confirmation. Switch to Fixed during populate
        # and restore the user's resize modes after, so the per-item
        # cost stays O(1).
        prior_modes = [h.sectionResizeMode(i) for i in range(col_count)]
        for i in range(col_count):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        # Same idea for signals: itemSelectionChanged fires on every
        # setItem at the selected row, and the slot walks the model
        # to read selected_tx — death by a thousand cuts on a big
        # bulk update. Block + flush once at the end.
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(txs))
            for row, tx in enumerate(txs):
                self._populate_row(row, tx)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.blockSignals(False)
            for i, mode in enumerate(prior_modes):
                h.setSectionResizeMode(i, mode)
        self._request_activities(txs)
        self._fit_verb_column()

    def append_transactions(self, txs: list[Transaction]) -> None:
        """Add rows at the bottom of the existing list (older entries
        for our nonce-desc sort). No setRowCount-on-the-whole-cache —
        only the new rows get materialized."""
        if not txs:
            return
        self.status_lbl.setVisible(False)
        start = self.table.rowCount()
        self.table.setRowCount(start + len(txs))
        for offset, tx in enumerate(txs):
            self._populate_row(start + offset, tx)
        self._request_activities(txs)
        self._fit_verb_column()

    def prepend_transactions(self, txs: list[Transaction]) -> None:
        """Add rows at the top of the existing list (newer entries —
        used when a page-1 refresh discovers txs the user sent from
        another wallet client). ``txs`` is expected newest-first;
        we insertRow in reverse so each ends up at row 0 in order."""
        if not txs:
            return
        self.status_lbl.setVisible(False)
        for tx in reversed(txs):
            self.table.insertRow(0)
            self._populate_row(0, tx)
        self._request_activities(txs)
        self._fit_verb_column()
        # insertRow(0) can leave the view's *current* index on the new
        # (0,0) status cell, drawing a stray focus outline on the icon
        # until the next full rebuild (confirm / tab switch). Clear the
        # current index — any real row selection is preserved.
        sm = self.table.selectionModel()
        if sm is not None:
            sm.clearCurrentIndex()

    def update_tx_by_hash(self, tx: Transaction) -> bool:
        """Repaint a single row in place when its tx has been
        updated (typically pending → confirmed via receipt).
        Returns True if a matching row was found. Cheaper than a
        full show_transactions when the cache is large — no
        rebuild, no header re-measurement."""
        for row in range(self.table.rowCount()):
            existing = self._tx_at(row)
            if existing is not None and existing.hash == tx.hash:
                self._populate_row(row, tx)
                return True
        return False

    def _populate_row(self, row: int, tx: Transaction) -> None:
        """Render one tx into ``row``. Shared by show / append /
        prepend so the cell shape stays consistent across paths.
        The full Transaction is stored on the Hash cell's UserRole
        so handlers (explorer, details dialog) can recover it."""
        # (themed icon name, Unicode glyph fallback, tooltip). The glyph
        # is used only when the icon theme lacks the named icon, so a
        # status cell never renders blank.
        if tx.pending:
            icon_name, glyph, tip = "content-loading", "⏳", "Pending"
        elif getattr(tx, "dropped", False):
            icon_name, glyph, tip = (
                "user-trash", "⊘",
                "Dropped — replaced by another tx at this nonce",
            )
        elif tx.success:
            icon_name, glyph, tip = "dialog-ok", "✓", "Success"
        else:
            icon_name, glyph, tip = "dialog-error", "✗", "Reverted"
        status = QTableWidgetItem()
        icon = QIcon.fromTheme(icon_name)
        if icon.isNull():
            status.setText(glyph)
        else:
            status.setIcon(icon)
        status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setToolTip(tip)

        nonce = QTableWidgetItem(str(tx.nonce))
        nonce.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        time_item = QTableWidgetItem(_format_datetime(tx.timestamp))

        # Activity = two cells: the verb (text) and the moved-coins icon,
        # separated from Nonce/Time by the stretch gap columns. The verb
        # cell also carries the full hash on its tooltip and the Transaction
        # on UserRole, for the copy / explorer / details handlers (and
        # _tx_at). Both stay empty until the Activity resolves.
        verb_item = QTableWidgetItem()
        verb_item.setToolTip(tx.hash)
        verb_item.setData(Qt.ItemDataRole.UserRole, tx)
        coins_item = QTableWidgetItem()

        self.table.setItem(row, _C_STATUS, status)
        self.table.setItem(row, _C_NONCE, nonce)
        self.table.setItem(row, _C_TIME, time_item)
        self.table.setItem(row, _C_VERB, verb_item)
        self.table.setItem(row, _C_COINS, coins_item)

        activity = self._activities.get(tx.hash)
        if activity is not None:
            self._apply_activity(row, activity)

    def _tx_at(self, row: int) -> Transaction | None:
        item = self.table.item(row, _C_VERB)
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, Transaction) else None

    def _on_double_click(self, row: int, _col: int) -> None:
        tx = self._tx_at(row)
        if tx is not None:
            self.tx_details_requested.emit(tx)

    def eventFilter(self, obj, event):  # noqa: N802 — Qt method name
        from PySide6.QtCore import QEvent
        if (obj is self.table
                and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            row = self.table.currentRow()
            tx = self._tx_at(row) if row >= 0 else None
            if tx is not None:
                self.tx_details_requested.emit(tx)
            return True
        return super().eventFilter(obj, event)

    def _open_in_explorer(self, tx: Transaction) -> None:
        if self._chain is None or not self._chain.explorer:
            return
        url = f"{self._chain.explorer.rstrip('/')}/tx/{tx.hash}"
        QDesktopServices.openUrl(QUrl(url))

    def _on_context_menu(self, pos) -> None:
        # Resolve the row, not the item: the justify gap columns hold no
        # QTableWidgetItem, so itemAt() returns None there and a right-click
        # on the empty space between columns opened nothing. indexAt() gives
        # a valid index for any cell in the row.
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        tx = self._tx_at(index.row())
        if tx is None:
            return
        menu = QMenu(self)
        # Reuse the panel buttons' icons so the menu matches the toolbar.
        act_details = menu.addAction(
            self.btn_details.icon(), "Show Transaction Details…")
        act_open = menu.addAction(
            self.btn_explorer.icon(), "Open in Block Explorer")
        act_open.setEnabled(bool(self._chain and self._chain.explorer))
        act_copy_hash = menu.addAction(
            self.btn_copy_hash.icon(), "Copy Tx Hash")
        # Speed up / Cancel only for a still-pending tx we broadcast.
        act_speedup = act_cancel = None
        if tx.pending and tx.raw_signed:
            menu.addSeparator()
            act_speedup = menu.addAction(
                _speedup_icon(), "Speed up (bump gas)…")
            act_cancel = menu.addAction(
                _cancel_tx_icon(), "Cancel transaction…")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_details:
            self.tx_details_requested.emit(tx)
        elif chosen is act_open:
            self._open_in_explorer(tx)
        elif chosen is act_copy_hash:
            QApplication.clipboard().setText(tx.hash)
        elif act_speedup is not None and chosen is act_speedup:
            self.replace_requested.emit(tx, False)
        elif act_cancel is not None and chosen is act_cancel:
            self.replace_requested.emit(tx, True)


# --- transaction details dialog + ABI fetch worker ------------------------


class AbiFetchWorker(QThread):
    """Look up the ABI for a contract address. Checks the disk cache
    first (positive hits and the unverified-sentinel both short-
    circuit the HTTP call) and falls back to a Blockscout fetch.

    Emits ``ready(abi)`` where ``abi`` is the parsed list of fragments,
    ``False`` for known-unverified, or ``None`` on transient errors."""

    ready = Signal(object)

    def __init__(self, source: AnyAbiSource, cache: AbiCache,
                 chain_id: int, address: str, parent=None):
        super().__init__(parent)
        self.source = source
        self.cache = cache
        self.chain_id = chain_id
        self.address = address

    def run(self) -> None:
        cached = self.cache.load(self.chain_id, self.address)
        if cached is not None:
            self.ready.emit(cached)
            return
        try:
            abi = self.source.fetch(self.chain_id, self.address)
        except Exception as e:
            log.warning("ABI fetch failed for %s/%s: %s",
                        self.chain_id, self.address, e)
            self.ready.emit(None)
            return
        # Persist verified ABIs AND the negative sentinel — both save
        # the next dialog the round-trip.
        self.cache.save(self.chain_id, self.address, abi)
        self.ready.emit(abi)


class ContractIdentityWorker(QThread):
    """Resolve a contract's identity (name / verified / deployer / age)
    off the UI thread and emit a rendered ``IdentityBadge``. Disk-cache
    first (instant + offline on re-open); on a miss, fetch and persist —
    the facts are immutable. Emits ``ready(IdentityBadge | None)``; None
    means nothing useful to show (unsupported chain + uncached, or a
    transient error — the To: row just stays bare)."""

    ready = Signal(object)

    def __init__(self, source: ContractIdentitySource | None,
                 cache: ContractIdentityCache, chain_id: int, address: str,
                 my_addresses, tx_cache: TransactionCache | None = None,
                 mode: str = "interact", parent=None):
        super().__init__(parent)
        self._source = source
        self._cache = cache
        self._chain_id = chain_id
        self._address = address
        self._my = list(my_addresses or [])
        self._tx_cache = tx_cache
        # "interact" = times you've *called* this contract (tx.to == it);
        # "send" = times you've *sent value here* (native OR token transfer
        # whose recipient is this address — the Send dialog's case).
        self._mode = mode

    def run(self) -> None:
        idy = self._cache.load(self._chain_id, self._address)
        if idy is None and self._source is not None:
            try:
                idy = self._source.fetch(self._chain_id, self._address)
            except Exception as e:
                log.warning("identity fetch failed for %s/%s: %s",
                            self._chain_id, self._address, e)
            # A ``partial`` (keyless) contract identity is a stub — don't
            # persist it, so adding an Etherscan key later fetches the full
            # one instead of hitting a cached half-identity forever.
            if idy is not None and not idy.partial:
                self._cache.save(self._chain_id, idy)
        if idy is None:
            self.ready.emit(None)
            return
        count = (self._cache.deployer_contract_count(self._chain_id, idy.deployer)
                 if idy.deployer else 0)
        # Familiarity count from the local tx-history cache (no network).
        interactions = None
        if self._tx_cache is not None:
            if self._mode == "send":
                interactions = self._tx_cache.sent_to_count(
                    self._chain_id, self._address, self._my)
            else:
                interactions = self._tx_cache.interaction_count(
                    self._chain_id, self._address, self._my)
        badge = describe_identity(
            idy, my_addresses=self._my, deployer_count=count,
            interaction_count=interactions, context=self._mode, now_ts=time.time())
        self.ready.emit(badge)


class SignatureFetchWorker(QThread):
    """Last-resort decode when no contract ABI matched: look the call's
    4-byte selector up in 4byte.directory and decode the calldata from
    the signature, like a block explorer. Emits ``ready(decoded|None)``."""

    ready = Signal(object)

    def __init__(self, input_data: str, parent=None):
        super().__init__(parent)
        self._input_data = input_data

    def run(self) -> None:
        from ..abi import decode_via_4byte
        try:
            self.ready.emit(decode_via_4byte(self._input_data))
        except Exception as e:
            log.warning("4byte decode failed: %s", e)
            self.ready.emit(None)


class LogsFetchWorker(QThread):
    """Fetch a confirmed tx's receipt and emit its event logs (the raw
    list of {address, topics, data} dicts). ``ready([])`` on any error
    or a still-pending receipt — the events section just stays empty."""

    ready = Signal(object)   # list of log dicts

    def __init__(self, chain, tx_hash: str, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._tx_hash = tx_hash

    def run(self) -> None:
        try:
            receipt = EthClient(self._chain).rpc(
                "eth_getTransactionReceipt", [self._tx_hash],
            )
        except Exception as e:
            log.warning("logs fetch failed for %s: %s", self._tx_hash, e)
            self.ready.emit([])
            return
        logs = (receipt or {}).get("logs") or []
        # Normalise each log to plain dicts/strings so decode_event (and
        # the worker→main-thread signal) don't carry web3 AttributeDicts.
        out = []
        for lg in logs:
            out.append({
                "address": lg.get("address"),
                "topics": [
                    t.hex() if hasattr(t, "hex") else t
                    for t in (lg.get("topics") or [])
                ],
                "data": (lg.get("data").hex()
                          if hasattr(lg.get("data"), "hex")
                          else lg.get("data")) or "0x",
            })
        self.ready.emit(out)


class SimulateWorker(QThread):
    """Run a not-yet-broadcast tx through local simulation off the
    main thread and emit the event logs it would produce — the same
    ``{address, topics, data}`` shape ``LogsFetchWorker`` emits, so the
    two feed ``_EventsView`` interchangeably. ``ready(None)`` when no
    simulation route works or the tx reverts (the pane shows a
    placeholder)."""

    ready = Signal(object)   # list of log dicts, or None

    def __init__(self, chain, from_addr, to_addr, data, value,
                 floor_block=None, parent=None):
        super().__init__(parent)
        self._args = (chain, from_addr, to_addr, data, value)
        self._floor_block = floor_block

    def run(self) -> None:
        from ..simulate import simulate_logs
        self.ready.emit(simulate_logs(*self._args, floor_block=self._floor_block))


class _EventsView(QWidget):
    """Reusable events pane: decode logs (from a receipt or a local
    simulation) and render them Python-style — default view keeps
    Transfer/Approval events touching one of our wallets, a toggle shows
    everything, own addresses are bold+italic, known-token contracts are
    prefixed with their logo + symbol, and fungible amounts get the
    human-readable ``# 5000 USDC`` comment. Non-Transfer/Approval events
    are named via their contract's ABI, fetched lazily on "Show all".

    The host feeds logs with ``set_logs`` (or a placeholder string with
    ``set_placeholder``); everything else is self-contained."""

    def __init__(self, *, chain, known_addresses, token_info, icon_cache,
                 abi_source, abi_cache, start_worker, parent=None):
        super().__init__(parent)
        self.chain = chain
        self._known_addresses = {a.lower() for a in (known_addresses or ())}
        self._token_info = token_info
        self._icon_cache = icon_cache
        self._abi_source = abi_source
        self._abi_cache = abi_cache
        self._start_worker = start_worker
        self._logs: list = []
        self._show_all_events = False
        self._event_abis: dict = {}
        self._abi_inflight: set = set()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(QLabel("Events:"))
        # Shown when the logs came from a simulation over proof-verified
        # state (Helios sidecar) — see set_verified. A self-consistent
        # (bg, fg) success-green pill, same theme-safe approach as the
        # _IDENTITY_TINT pills: carrying both colors reads on any
        # palette, where a lone gray drowned on light themes.
        self.verified_lbl = QLabel("✓ verified")
        self.verified_lbl.setStyleSheet(
            "background:#d1e7dd; color:#0f5132; "
            "padding:1px 6px; border-radius:4px;")
        self.verified_lbl.setToolTip("Cryptographically verified simulation")
        self.verified_lbl.hide()
        header.addWidget(self.verified_lbl)
        header.addStretch(1)
        self.show_all_events_btn = QPushButton("Show &all events")
        self.show_all_events_btn.setCheckable(True)
        self.show_all_events_btn.setEnabled(False)
        self.show_all_events_btn.toggled.connect(self._on_show_all_events)
        header.addWidget(self.show_all_events_btn)
        lay.addLayout(header)
        self.events_view = QTextEdit()
        self.events_view.setReadOnly(True)
        self.events_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.events_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.events_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        lay.addWidget(self.events_view, 1)

        # Spinner for the "busy" placeholder (simulating / loading). A
        # braille cycle reads as motion in any monospace-ish font and
        # needs no color, so it's theme-safe; QTimer-driven, parented to
        # this widget so it dies with the pane.
        self._spin_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spin_i = 0
        self._spin_text = ""
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(90)
        self._spin_timer.timeout.connect(self._tick_spinner)

    def _tick_spinner(self) -> None:
        self._spin_i = (self._spin_i + 1) % len(self._spin_frames)
        self.events_view.setPlainText(
            f"{self._spin_frames[self._spin_i]}  {self._spin_text}")

    def set_busy(self, text: str) -> None:
        """An animated 'working…' placeholder (a spinner + ``text``).
        Cleared by the next ``set_placeholder`` / ``set_logs``."""
        self._spin_text = text
        self.show_all_events_btn.setEnabled(False)
        self.verified_lbl.hide()
        self._spin_i = 0
        self.events_view.setPlainText(f"{self._spin_frames[0]}  {text}")
        self._spin_timer.start()

    def set_placeholder(self, text: str) -> None:
        self._spin_timer.stop()
        self.events_view.setPlainText(text)
        self.show_all_events_btn.setEnabled(False)
        self.verified_lbl.hide()

    def set_verified(self, on: bool) -> None:
        """Show/hide the '⚡ verified' badge. Only the simulation path
        sets it (receipt-log views never call this)."""
        self.verified_lbl.setVisible(on)

    def set_logs(self, logs) -> None:
        self._spin_timer.stop()
        self._logs = logs or []
        self.show_all_events_btn.setEnabled(bool(self._logs))
        if self._show_all_events:
            self._ensure_event_abis()
        self._render_events_view()

    def _on_show_all_events(self, checked: bool) -> None:
        self._show_all_events = checked
        if checked:
            self._ensure_event_abis()
        self._render_events_view()

    def _ensure_event_abis(self) -> None:
        """For each contract whose event we couldn't name from a known
        signature, grab its ABI — from the local cache if present, else
        a lazy Blockscout fetch. Events re-render as ABIs land."""
        for lg in self._logs:
            if decode_event(lg) is not None:
                continue   # Transfer/Approval-family — no ABI needed
            addr = (lg.get("address") or "").lower()
            if (not addr or addr in self._event_abis
                    or addr in self._abi_inflight):
                continue
            cached = self._abi_cache.load(self.chain.chain_id, addr)
            if cached is not None:
                self._event_abis[addr] = cached if cached else None
                continue
            self._abi_inflight.add(addr)
            worker = AbiFetchWorker(
                self._abi_source, self._abi_cache, self.chain.chain_id, addr,
            )
            worker.ready.connect(
                lambda abi, a=addr: self._on_event_abi_ready(a, abi)
            )
            self._start_worker(worker)

    def _on_event_abi_ready(self, addr: str, abi) -> None:
        self._abi_inflight.discard(addr)
        self._event_abis[addr] = abi if abi else None
        if self._show_all_events:
            self._render_events_view()

    def _event_touches_ours(self, decoded: dict) -> bool:
        for a in decoded.get("args") or []:
            if (a.get("type") == "address"
                    and str(a.get("value", "")).lower() in self._known_addresses):
                return True
        return False

    def _token_prefix_html(self, doc, contract: str) -> str:
        if self._token_info is None or not contract:
            return ""
        entry = self._token_info(self.chain.chain_id, contract)
        if entry is None:
            return ""
        img = ""
        if self._icon_cache is not None:
            pix = self._icon_cache.get(self.chain.chain_id, contract)
            if pix is not None and not pix.isNull():
                url = f"tok:{contract.lower()}"
                doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(url), pix)
                img = f'<img src="{url}" width="14" height="14"> '
        return f'{img}<b>{_escape_html(entry.symbol)}</b> '

    def _render_events_view(self) -> None:
        rendered: list[tuple] = []   # (decoded_or_None, raw_log)
        for lg in self._logs:
            abi = self._event_abis.get((lg.get("address") or "").lower())
            decoded = decode_event(lg, abi)
            if self._show_all_events:
                rendered.append((decoded, lg))
            elif (decoded is not None
                    and decoded["event"] in KNOWN_EVENT_NAMES
                    and self._event_touches_ours(decoded)):
                rendered.append((decoded, lg))
        if not rendered:
            self.events_view.setPlainText(
                "(no events) — showing all may reveal more"
                if not self._show_all_events else "(no events in this transaction)"
            )
            return
        doc = self.events_view.document()
        # Bold-capable monospace family so <b> on names/own addresses
        # renders bold (the generic "monospace" alias is Regular-only on
        # Linux and would fall back to faux/no-bold).
        mono = _pick_mono_font()
        self.events_view.setFont(mono)
        parts = [
            f'<div style="white-space: pre-wrap; word-break: break-all; '
            f"font-family: '{mono.family()}', monospace;\">"
        ]
        for i, (decoded, lg) in enumerate(rendered):
            parts.append(self._event_html(decoded, lg, doc))
            if i != len(rendered) - 1:
                parts.append("\n")
        parts.append("</div>")
        self.events_view.setHtml("".join(parts))

    def _event_html(self, decoded, lg, doc) -> str:
        contract = (decoded or lg).get("contract") or lg.get("address") or "?"
        contract_span = (
            f'<span style="color:{_TYPE_COLOR};">{_escape_html(contract)}</span>'
        )
        prefix = self._token_prefix_html(doc, contract)
        if decoded is None:
            topic0 = (lg.get("topics") or ["?"])[0]
            return (
                f"{prefix}{contract_span}.<b>(unknown event)</b>("
                f"topic0 = {_escape_html(str(topic0))}\n)\n"
            )
        head = f"{prefix}{contract_span}.<b>{_escape_html(decoded['event'])}</b>(\n"
        args = decoded.get("args") or []
        token_context = self._event_token_context(contract, decoded)
        body = "".join(
            _arg_html(a, indent=1, last=(j == len(args) - 1),
                       token_context=token_context,
                       known_addresses=self._known_addresses)
            for j, a in enumerate(args)
        )
        return head + body + ")\n"

    def _event_token_context(self, contract: str, decoded: dict):
        if (self._token_info is None
                or decoded.get("event") not in ("Transfer", "Approval")):
            return None
        if not any(a.get("name") == "value" for a in decoded.get("args") or []):
            return None   # ERC-721 (tokenId) — not an amount
        entry = self._token_info(self.chain.chain_id, contract)
        decimals = getattr(entry, "decimals", None) if entry else None
        if decimals is None:
            return None
        return {"symbol": entry.symbol, "decimals": decimals}


class _EventPreviewMixin:
    """Adds an 'Events' tab — backed by local revm simulation — to a
    signing dialog (Send / dapp-Sign). The dialog wraps its existing
    widgets in a 'Details' tab, then calls ``_init_event_preview(tabs)``
    to append the Events tab.

    Simulation is lazy and cached: it runs the first time the Events tab
    is shown and re-runs only when the tx params change (so the Send
    dialog re-simulates after the user edits recipient/amount, but
    toggling tabs without edits doesn't refork the chain). Subclasses
    provide ``_sim_params()`` → ``(from, to, data, value)`` (or raise
    ``SignerError`` / return None when the inputs aren't a valid tx yet).

    Requires the host to have ``chain``, ``_token_info``, ``_icon_cache``,
    ``_abi_source``, ``_abi_cache`` and ``_start_worker`` already set."""

    if TYPE_CHECKING:
        # Members the concrete dialog (a QDialog) provides — this mixin is
        # never instantiated standalone. Declared so the type checker
        # resolves the references in the methods below.
        from PySide6.QtCore import SignalInstance
        chain: Any
        finished: SignalInstance
        _token_info: Any
        _icon_cache: Any
        _abi_source: AnyAbiSource | None
        _abi_cache: AbiCache
        _start_worker: Any
        _sim_floor_provider: Callable[[int, str], int | None] | None

        def _sim_params(self) -> Any: ...
        def fontMetrics(self) -> Any: ...   # noqa: N802 — the host QWidget's

    # How long the UI waits for a simulation before giving up. The fast
    # path (eth_simulateV1) returns in well under this; the local fork
    # can take far longer on a slow / rate-limited endpoint (e.g. Arbitrum
    # on DRPC, which has no simulateV1), and a single fork attempt can't
    # be interrupted — so we stop *waiting* and let the worker finish in
    # the background (it releases the GIL; the UI stays responsive) and
    # self-evict via the host's worker set.
    SIM_TIMEOUT_MS = 35_000

    def _init_event_preview(self, tabs, *, known_addresses,
                            sim_floor_provider=None) -> None:
        self._sim_tabs = tabs
        self._sim_key = None
        self._sim_worker: SimulateWorker | None = None
        self._sim_done = True   # no sim in flight
        # (chain_id, from_addr) -> this wallet's latest confirmed block, so a
        # verified preview never forks before our own recent tx. None when the
        # host doesn't wire it (the fork just uses the per-chain lag alone).
        self._sim_floor_provider = sim_floor_provider
        self._events = _EventsView(
            chain=self.chain, known_addresses=known_addresses,
            token_info=self._token_info, icon_cache=self._icon_cache,
            abi_source=self._abi_source, abi_cache=self._abi_cache,
            start_worker=self._start_worker,
        )
        self._events.set_placeholder(
            "(switch to this tab to simulate the transaction locally)"
        )
        self._events_page = QWidget()
        lay = QVBoxLayout(self._events_page)
        # Match the Details page's in-frame left/right padding (font-derived).
        _pad = self.fontMetrics().height() // 2
        lay.setContentsMargins(_pad, 6, _pad, _pad)
        lay.addWidget(self._events, 1)
        tabs.addTab(self._events_page, "&Events")
        tabs.currentChanged.connect(self._on_sim_tab_changed)
        # Parented to the dialog so it can't fire after the dialog dies.
        # The mixin is always mixed into a QDialog (a QObject).
        self._sim_timer = QTimer(cast(QObject, self))
        self._sim_timer.setSingleShot(True)
        self._sim_timer.timeout.connect(self._on_sim_timeout)
        # A prominent, always-visible warning the host places above its
        # Confirm button (outside the tabs), shown red when the preview
        # predicts a revert — warn-only, the user can still send. Hidden
        # otherwise. Word-wrapped + selectable; no setSizePolicy (that resets
        # a wrapped QLabel's heightForWidth and clips it).
        self._revert_banner = QLabel()
        self._revert_banner.setWordWrap(True)
        self._revert_banner.setVisible(False)
        self._revert_banner.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        # Debounce proactive re-runs (dialog open, Send-dialog edits) so a
        # predicted revert warns before the user opens the Events tab without
        # forking on every keystroke.
        self._sim_debounce = QTimer(cast(QObject, self))
        self._sim_debounce.setSingleShot(True)
        self._sim_debounce.timeout.connect(self._maybe_simulate)
        # The simulation worker is tracked by the host, not this dialog, so
        # it outlives a closed dialog (these dialogs are non-modal). Detach
        # its signal on close so a late `ready` can't call into a deleted
        # dialog.
        self.finished.connect(self._detach_sim)

    # Debounce for proactive re-runs (open / Send-dialog edits).
    SIM_DEBOUNCE_MS = 400

    @property
    def _logs(self) -> list:
        """The most recent simulated event logs (empty until a sim lands).
        Exposed on the dialog so the host's post-broadcast leg-folding
        (``ui._on_tx_broadcast`` → ``note_transfer_legs``) can show a swap's
        coins in the pending row before the receipt/indexer catch up — the
        logs actually live on the inner ``_EventsView``."""
        events = getattr(self, "_events", None)
        return list(events._logs) if events is not None else []

    def revert_banner(self) -> QLabel:
        """The red 'will revert' warning label — the host adds it to its own
        layout above the Confirm button so it shows from any tab."""
        return self._revert_banner

    def request_simulation(self) -> None:
        """Kick the preview proactively (debounced), so a predicted revert
        warns before the user opens the Events tab or edits further. Idempotent
        — ``_maybe_simulate`` no-ops when the tx params haven't changed."""
        self._sim_debounce.start(self.SIM_DEBOUNCE_MS)

    def _show_revert_warning(self, note) -> None:
        bg, fg = _IDENTITY_TINT["warn"]
        self._revert_banner.setStyleSheet(
            f"background:{bg}; color:{fg}; padding:6px 10px; border-radius:4px;")
        text = "⚠ This transaction is expected to revert"
        reason = (note.reason or "").strip()
        if reason:
            text += f": {reason}"
        if getattr(note, "verified", False):
            text += "  (verified)"
        self._revert_banner.setText(text)
        self._revert_banner.setVisible(True)

    def _clear_revert_warning(self) -> None:
        self._revert_banner.setVisible(False)

    def _on_sim_tab_changed(self, idx: int) -> None:
        if self._sim_tabs.widget(idx) is self._events_page:
            self._maybe_simulate()

    def _sim_blocked_text(self) -> str:
        return "(enter a valid recipient and amount to preview events)"

    def _maybe_simulate(self) -> None:
        try:
            params = self._sim_params()
        except SignerError:
            params = None
        if params is None or not params[1]:   # no/invalid `to` address
            self._events.set_placeholder(self._sim_blocked_text())
            self._sim_key = None
            self._clear_revert_warning()
            return
        if params == self._sim_key:
            return   # already simulated (or in flight) for this exact tx
        self._sim_key = params
        # Drop any stale warning from the previous params while the new run
        # is in flight; _on_sim_ready re-raises it if this tx also reverts.
        self._clear_revert_warning()
        from ..simulate import simulation_available
        if not simulation_available(self.chain):
            self._events.set_placeholder(self._no_simulation_text())
            return
        self._events.set_busy("simulating…")
        self._sim_done = False
        self._detach_sim()                 # drop any prior in-flight worker
        from_addr, to_addr, data, value = params
        floor_block = None
        if self._sim_floor_provider is not None:
            floor_block = self._sim_floor_provider(self.chain.chain_id, from_addr)
        worker = SimulateWorker(self.chain, from_addr, to_addr, data, value,
                                floor_block=floor_block)
        self._sim_worker = worker
        worker.ready.connect(
            lambda logs, k=params: self._on_sim_ready(k, logs)
        )
        self._start_worker(worker)
        self._sim_timer.start(self.SIM_TIMEOUT_MS)

    def _no_simulation_text(self) -> str:
        return ("(no simulation available — this RPC has no eth_simulateV1 "
                "and the optional 'py-evm' package isn't installed)")

    def _detach_sim(self, *_args) -> None:
        """Stop waiting on the current worker (it keeps running in the
        background and self-evicts). Called on a new sim, on resolve, and
        on dialog close — the last is what keeps a late `ready` from
        reaching a deleted dialog."""
        self._sim_timer.stop()
        w = self._sim_worker
        self._sim_worker = None
        if w is not None:
            try:
                w.ready.disconnect()
            except (RuntimeError, TypeError):
                pass

    def _on_sim_timeout(self) -> None:
        if self._sim_done:
            return
        self._sim_done = True
        self._detach_sim()
        self._events.set_placeholder(
            "(simulation timed out — this RPC is too slow to fork and "
            "doesn't support eth_simulateV1; previews need a faster RPC)"
        )

    def _on_sim_ready(self, key, logs) -> None:
        if key != self._sim_key or self._sim_done:
            return   # superseded by a newer sim, or already timed out
        self._sim_done = True
        self._detach_sim()
        from ..simulate import RevertNote, SimulationNote, VerifiedLogs
        if isinstance(logs, RevertNote):
            # Definitive revert — warn in red above Confirm (warn-only, send
            # stays enabled) and mirror the reason in the Events tab.
            self._show_revert_warning(logs)
            self._events.set_verified(logs.verified)
            self._events.set_placeholder(
                f"(reverts: {logs.reason})" if logs.reason else "(reverts)"
            )
            return
        self._clear_revert_warning()
        if isinstance(logs, SimulationNote):
            # Ran fine, but an (empty) events list would mislead — e.g.
            # calldata to a code-less target that the chain's node may
            # handle natively (TAC system contracts).
            self._events.set_placeholder(logs.text)
            return
        self._events.set_verified(isinstance(logs, VerifiedLogs))
        if logs is None:
            # No definitive answer — the worker may have just *learned* this
            # endpoint can't simulate (no eth_simulateV1, no py-evm), or a
            # transient failure. Not a revert (that's RevertNote now), so don't
            # claim one.
            from ..simulate import simulation_available
            if not simulation_available(self.chain):
                self._events.set_placeholder(self._no_simulation_text())
            else:
                self._events.set_placeholder(
                    "(couldn't simulate this transaction — try again)"
                )
            return
        self._events.set_logs(logs)


# Self-consistent (bg, fg) pairs for the Contract: identity row. Each
# carries its own background, so it reads on any palette — same theme-safe
# approach as the wallet-label sticky pill (feedback_theme_safe_colors).
_IDENTITY_TINT = {
    "warn":    ("#f8d7da", "#842029"),   # unverified — stands out
    "caution": ("#fff3cd", "#664d03"),   # verified, but deployed recently
}


def _style_identity_label(label: QLabel, badge) -> None:
    """Render an IdentityBadge into ``label``: a tinted pill for warn /
    caution, muted plain text for a neutral EOA, normal text otherwise."""
    label.setText(badge.text)
    tint = _IDENTITY_TINT.get(badge.level)
    if tint:
        bg, fg = tint
        label.setStyleSheet(
            f"background:{bg}; color:{fg}; padding:1px 6px; border-radius:4px;")
        label.setEnabled(True)
    else:
        label.setStyleSheet("")
        label.setEnabled(badge.level != "info")   # grey out a plain EOA


def _make_identity_row(*, to_addr: str | None, chain,
                       identity_source: ContractIdentitySource | None,
                       identity_cache: ContractIdentityCache,
                       my_addresses, start_worker,
                       tx_cache: TransactionCache | None = None):
    """Build the Contract:-row label and a ``kick()`` that fills it via a
    background ContractIdentityWorker. Returns ``(None, None)`` when there's
    no recipient to identify."""
    if not to_addr:
        return None, None
    label = QLabel("")
    # Word wrap on, and DON'T override the size policy — a wrapped QLabel
    # ships hasHeightForWidth()=True, which is what makes the form row grow
    # to fit multiple lines. Replacing the policy resets that flag and the
    # 2nd/3rd line gets clipped (the row keeps the 1-line sizeHint height).
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def _apply(badge) -> None:
        if badge is not None:
            _style_identity_label(label, badge)
        else:
            # Unsupported chain / transient error — drop the placeholder
            # back to blank rather than leave it stuck on "identifying…".
            label.clear()
            label.setStyleSheet("")
            label.setEnabled(True)

    def kick() -> None:
        # Skip entirely when there's nothing to go on (no source/key AND
        # not already cached) — leaves the row blank rather than spinning.
        if (identity_source is None
                and identity_cache.load(chain.chain_id, to_addr) is None):
            return
        # The fetch is several explorer round-trips; without a placeholder
        # the row sits blank the whole time, indistinguishable from "no
        # info". Show a muted "identifying…" so a slow fetch reads as
        # loading. (A cache hit resolves in ~ms, so the flash is invisible.)
        label.setText("identifying…")
        label.setEnabled(False)
        worker = ContractIdentityWorker(
            identity_source, identity_cache, chain.chain_id, to_addr,
            my_addresses, tx_cache=tx_cache)
        worker.ready.connect(_apply)
        start_worker(worker)

    return label, kick


class TransactionDetailsDialog(Dialog):
    """Modal-ish dialog showing the full tx record.

    Calldata decoding runs asynchronously: the dialog opens with a
    "(decoding…)" placeholder, kicks an AbiFetchWorker, and fills in
    the function name + arguments when the worker returns. The
    explorer link button is always available regardless of ABI state.
    """

    # Emitted when the user picks Speed up / Cancel on a pending tx.
    # (Transaction, cancel: bool) → the plugin routes it to the host.
    replace_requested = Signal(object, bool)

    def __init__(self, tx: Transaction, chain, *,
                 abi_source: AnyAbiSource | None,
                 abi_cache: AbiCache,
                 start_worker,
                 token_info=None,
                 icon_cache=None,
                 native_price_usd=None,
                 known_addresses=None,
                 identity_source: ContractIdentitySource | None = None,
                 identity_cache: ContractIdentityCache | None = None,
                 tx_cache: TransactionCache | None = None,
                 parent=None):
        super().__init__(parent)
        self.tx = tx
        self.chain = chain
        self._abi_source = abi_source
        self._abi_cache = abi_cache
        self._identity_source = identity_source
        self._identity_cache = (
            identity_cache if identity_cache is not None
            else ContractIdentityCache())
        self._tx_cache = tx_cache
        self._start_worker = start_worker
        self._known_addresses = {a.lower() for a in (known_addresses or ())}
        # Optional dependencies for ERC-20 annotation on the "To:" row.
        # Plugin passes both when available; tests can leave them None
        # and the dialog falls back to a plain address line.
        self._token_info = token_info
        self._icon_cache = icon_cache
        # Decimal USD-per-native price for the actual-fee annotation.
        # None when there's no cached price for this (chain, from_addr)
        # — the fee row then omits the dollar parenthetical.
        self._native_price_usd = native_price_usd
        self._to_icon_label: QLabel | None = None
        self._to_addr_lower: str | None = None

        self.setWindowTitle(f"Transaction {tx.hash[:10]}…")
        self.resize(720, 660)

        # Use the regular text colour for links rather than
        # QPalette.ColorRole.Link — that role inherits a low-contrast cyan in a
        # lot of common themes (Breeze/Kvantum/qt6ct fallbacks pick
        # ``#2adfff``-ish), so links end up nearly invisible on the
        # dialog background. Black-on-white (or white-on-dark) plus
        # the underline carries the "this is a link" signal without
        # needing a colour that's guaranteed to contrast.
        self._link_color = self.palette().color(QPalette.ColorRole.WindowText).name()

        # Outer layout: comfortable margins so the contents don't bump
        # against the window chrome.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(8)

        # Two tabs: "Details" (record + decoded call) and "Events" (the
        # receipt's logs), so the events list gets a full pane of space
        # instead of being squeezed under the decoded call.
        # Left/right padding inside the tab frame so content doesn't sit flush
        # against its border (font-derived, matching the window edge margin).
        _pad = self.fontMetrics().height() // 2
        tabs = QTabWidget()
        details_page = QWidget()
        details_layout = QVBoxLayout(details_page)
        details_layout.setContentsMargins(_pad, 8, _pad, _pad)
        details_layout.setSpacing(8)
        events_page = QWidget()
        events_layout = QVBoxLayout(events_page)
        events_layout.setContentsMargins(_pad, 8, _pad, _pad)
        events_layout.setSpacing(6)
        tabs.addTab(details_page, "&Details")
        tabs.addTab(events_page, "&Events")
        outer.addWidget(tabs, 1)

        # Header fields go in a QFormLayout — labels left-aligned, the
        # value column starts at the widest label's width so values
        # line up cleanly underneath each other.
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(6)
        details_layout.addLayout(form)

        mono = QFont("monospace")
        self._mono_font = mono

        if tx.pending:
            status_text = "⏳ Pending"
        elif getattr(tx, "dropped", False):
            status_text = "⊘ Dropped (replaced at this nonce)"
        elif tx.success:
            status_text = "✓ Success"
        else:
            status_text = "✗ Reverted"
        form.addRow("Status:", self._value_label(status_text))
        form.addRow("Nonce:", self._value_label(str(tx.nonce)))
        dt = datetime.datetime.fromtimestamp(tx.timestamp)
        form.addRow("Date:", self._value_label(dt.strftime("%c")))
        form.addRow("Timestamp:", self._value_label(f"{tx.timestamp} (unix)"))
        form.addRow("Block:", self._value_label(str(tx.block_number)))
        form.addRow("Hash:",
                    self._link_label(tx.hash,
                                     self._explorer_url("tx", tx.hash),
                                     monospace=True))
        from_cs = to_checksum_address(tx.from_addr)
        form.addRow("From:",
                    self._link_label(from_cs,
                                     self._explorer_url("address", from_cs),
                                     monospace=True))
        form.addRow(
            "To:", self._build_to_row(tx.to_addr, tx.from_addr, chain, mono),
        )
        _id_label, _id_kick = _make_identity_row(
            to_addr=tx.to_addr, chain=chain,
            identity_source=self._identity_source,
            identity_cache=self._identity_cache,
            my_addresses=known_addresses or [],
            start_worker=self._start_worker, tx_cache=self._tx_cache)
        if _id_label is not None and _id_kick is not None:
            form.addRow("Contract:", _id_label)
            _id_kick()
        # Value rendered through wei_to_ether (Decimal) — never float.
        if tx.value_wei:
            ether = wei_to_ether(tx.value_wei)
            value_text = f"{ether} {chain.symbol}  ({tx.value_wei} wei)"
        else:
            value_text = "0"
        form.addRow("Value:", self._value_label(value_text))
        form.addRow("Method ID:",
                    self._value_label(
                        tx.method_id or "(none — plain transfer)",
                        monospace=True))

        # Gas details — skipped for still-pending txs since gas_used
        # and effectiveGasPrice are only filled in once the receipt
        # arrives. The pending row's gas_price_wei reflects the user's
        # signed maxFeePerGas, not what the chain will actually
        # charge, so showing it as the realised rate would be wrong.
        if not tx.pending and tx.gas_used > 0:
            form.addRow("Gas used:",
                        self._value_label(f"{tx.gas_used:,}"))
            gwei = wei_to_ether(tx.gas_price_wei) * Decimal(10**9)
            form.addRow("Gas price:", self._value_label(f"{gwei} gwei"))
            fee_wei = tx.gas_used * tx.gas_price_wei
            fee_ether = wei_to_ether(fee_wei)
            fee_text = f"{fee_ether} {chain.symbol}"
            if self._native_price_usd is not None:
                usd = fee_ether * self._native_price_usd
                fee_text += f"  ({_format_usd(usd)})"
            form.addRow("Fee paid:", self._value_label(fee_text))

        # Decoded call sits below the form: label on its own line,
        # then the QTextEdit (read-only, with the call rendered as
        # HTML via setHtml) underneath claiming leftover vertical
        # space.
        details_layout.addWidget(QLabel("Decoded call:"))
        self.decoded_view = QTextEdit()
        self.decoded_view.setReadOnly(True)
        self.decoded_view.setFont(mono)
        # No horizontal scroll — wrap to widget width, and break
        # inside long unbroken tokens too (decimal uint256 values
        # and hex blobs have no spaces and would otherwise overflow).
        self.decoded_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.decoded_view.setWordWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        self.decoded_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        details_layout.addWidget(self.decoded_view, 1)

        # Events tab — the receipt's logs, decoded. Default view is
        # Transfer/Approval events touching one of the user's wallets;
        # the toggle reveals everything else.
        self._events = _EventsView(
            chain=chain, known_addresses=known_addresses,
            token_info=self._token_info, icon_cache=self._icon_cache,
            abi_source=self._abi_source, abi_cache=self._abi_cache,
            start_worker=self._start_worker,
        )
        events_layout.addWidget(self._events, 1)

        # Buttons row: Explorer + Close.
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        explorer_btn = QPushButton("&Open in Block Explorer")
        # Same browser icon as the Transactions list's external-
        # link button so "go to the explorer" reads identically
        # wherever the user encounters it.
        _explorer_icon = QIcon.fromTheme(
            "applications-internet",
            QIcon.fromTheme("internet-web-browser"),
        )
        if not _explorer_icon.isNull() and _explorer_icon.availableSizes():
            explorer_btn.setIcon(_explorer_icon)
        explorer_btn.setEnabled(bool(chain.explorer))
        explorer_btn.clicked.connect(self._open_explorer)
        buttons.addButton(explorer_btn, QDialogButtonBox.ButtonRole.ActionRole)
        # Speed up / Cancel — only for a still-pending tx we broadcast (we
        # need its signed bytes to recover the original fees).
        if tx.pending and tx.raw_signed:
            speedup_btn = QPushButton("&Speed up")
            speedup_btn.setIcon(_speedup_icon())
            speedup_btn.setToolTip("Replace with higher gas")
            speedup_btn.clicked.connect(lambda: self._emit_replace(False))
            buttons.addButton(speedup_btn, QDialogButtonBox.ButtonRole.ActionRole)
            cancel_btn = QPushButton("&Cancel tx")
            cancel_btn.setIcon(_cancel_tx_icon())
            cancel_btn.setToolTip("Cancel — 0-value self-send")
            cancel_btn.clicked.connect(lambda: self._emit_replace(True))
            buttons.addButton(cancel_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Start ABI fetch + decode (only when there's calldata + an ABI source).
        if (tx.input_data and tx.input_data not in ("0x", "0X") and tx.to_addr
                and self._abi_source is not None):
            self.decoded_view.setPlainText("(decoding…)")
            worker = AbiFetchWorker(
                self._abi_source, self._abi_cache,
                chain.chain_id, tx.to_addr,
            )
            worker.ready.connect(self._on_abi_ready)
            self._start_worker(worker)
        elif not tx.to_addr:
            self.decoded_view.setPlainText("(contract creation — no method call)")
        else:
            self.decoded_view.setPlainText("(plain value transfer — no calldata)")

        # Events only exist on a mined tx — pending/dropped have no
        # receipt yet, so skip the fetch for those.
        if tx.pending or getattr(tx, "dropped", False):
            self._events.set_placeholder("(no events — transaction not mined)")
        else:
            self._events.set_busy("loading events…")
            logs_worker = LogsFetchWorker(chain, tx.hash)
            logs_worker.ready.connect(self._events.set_logs)
            self._start_worker(logs_worker)

    def _on_abi_ready(self, abi) -> None:
        decoded = None
        if isinstance(abi, list):
            decoded = decode_call(
                abi, self.tx.input_data, address=self.tx.to_addr,
            )
        if decoded is not None:
            self._render_decoded_call(decoded)
            return
        # No contract ABI decoded the call (unverified contract, a proxy
        # whose impl isn't verified, or a fallback). Fall back to the
        # 4-byte signature DB like an explorer.
        self._abi_state = abi
        self.decoded_view.setPlainText("(decoding via signature database…)")
        worker = SignatureFetchWorker(self.tx.input_data)
        worker.ready.connect(self._on_signature_ready)
        self._start_worker(worker)

    def _on_signature_ready(self, decoded) -> None:
        if decoded is not None:
            self._render_decoded_call(decoded)
            return
        abi = getattr(self, "_abi_state", None)
        if abi is False:
            msg = ("(contract source is not verified, and the call's "
                   "selector isn't in the 4-byte database)")
        elif abi is None:
            msg = "(failed to fetch ABI — try again later)"
        else:
            msg = ("(this calldata didn't match the contract's ABI, and "
                   "its selector isn't in the 4-byte database — possibly "
                   "a fallback or proxy call)")
        self.decoded_view.setPlainText(msg)

    def _render_decoded_call(self, decoded) -> None:
        # If the called contract is on the curated whitelist, pass
        # its (symbol, decimals) so the renderer can annotate token-
        # amount uints with the human-readable "# 5000 crvUSD" form.
        token_context = None
        if self._token_info is not None and self.tx.to_addr:
            entry = self._token_info(self.chain.chain_id, self.tx.to_addr)
            if entry is not None:
                token_context = {
                    "symbol": entry.symbol,
                    "decimals": entry.decimals,
                }
        _render_decoded(
            self.decoded_view, decoded, token_context,
            known_addresses=self._known_addresses,
        )

    def _emit_replace(self, cancel: bool) -> None:
        self.replace_requested.emit(self.tx, cancel)
        self.accept()

    def _open_explorer(self) -> None:
        url = self._explorer_url("tx", self.tx.hash)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _value_label(self, text: str, *, monospace: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if monospace:
            lbl.setFont(self._mono_font)
        # Wrap long values (full hashes etc.) so they don't push the
        # dialog width past the user's screen.
        lbl.setWordWrap(True)
        return lbl

    def _link_label(self, text: str, url: str | None, *,
                    monospace: bool = False) -> QLabel:
        """Address / hash label that's a hyperlink when an explorer
        URL is available, plain selectable text when the chain has
        no explorer configured."""
        if not url:
            return self._value_label(text, monospace=monospace)
        style = f"color: {self._link_color}; text-decoration: underline;"
        if monospace:
            style += " font-family: monospace;"
        html = (
            f'<a href="{_escape_html(url)}" style="{style}">'
            f"{_escape_html(text)}</a>"
        )
        lbl = QLabel(html)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setOpenExternalLinks(True)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        lbl.setWordWrap(True)
        _install_copy_menu(lbl, text, url)
        return lbl

    def _explorer_url(self, kind: str, addr: str,
                       *, ref_addr: str | None = None) -> str | None:
        """Build an Etherscan-family URL: ``tx``/``address``/``token``.
        ``token`` with ``ref_addr`` appends ``?a=<ref>`` — Etherscan's
        convention for filtering the token page to a specific holder
        (so clicking a token in the To row jumps straight to the
        sender's transfer history for that token)."""
        if not self.chain.explorer or not addr:
            return None
        base = self.chain.explorer.rstrip("/")
        if kind == "tx":
            return f"{base}/tx/{addr}"
        if kind == "address":
            return f"{base}/address/{addr}"
        if kind == "token":
            url = f"{base}/token/{addr}"
            if ref_addr:
                url += f"?a={ref_addr}"
            return url
        return None

    # --- "To:" row composition ------------------------------------------

    def _build_to_row(self, to_addr: str | None, from_addr: str,
                      chain, mono: QFont) -> QWidget:
        """The To: cell. Plain address text when the recipient isn't a
        known ERC-20; address with a leading icon + symbol when it is.
        Both the tx-details dialog and the sign-tx dialog call this
        with their (to_addr, from_addr) pair — the from_addr is used
        as the ``a=`` parameter on the token-page URL so the link
        lands on the user's transfer history rather than the bare
        contract page."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        if not to_addr:
            label = QLabel("(contract creation)")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setFont(mono)
            row.addWidget(label)
            row.addStretch(1)
            return container

        # Display in EIP-55 mixed case from here down. Blockscout
        # returns addresses lower-cased; checksumming on display lets
        # the eye spot typos / wrong-case copies. Downstream uses
        # (explorer URLs, the icon cache lookup) are all case-
        # insensitive, so the rebinding is safe.
        addr = to_checksum_address(to_addr)
        from_cs = to_checksum_address(from_addr)

        entry = (self._token_info(chain.chain_id, addr)
                 if self._token_info is not None else None)

        if entry is not None:
            # Icon on the left, then "SYMBOL (0xaddr…)" rendered as
            # rich text in a single QLabel so symbol and address sit
            # flush against the parentheses. The address is a link to
            # the token's page filtered by the sender — Etherscan's
            # /token/<token>?a=<holder> pattern — so the click lands
            # on the user's transfer history for that token rather
            # than the generic contract page.
            self._to_addr_lower = addr.lower()
            self._to_icon_label = QLabel()
            # Icon dims match what TokenListPanel uses for inline rows.
            self._to_icon_label.setFixedSize(20, 20)
            row.addWidget(self._to_icon_label)

            token_url = self._explorer_url(
                "token", addr, ref_addr=from_cs,
            )
            if token_url:
                addr_html = (
                    f'<a href="{_escape_html(token_url)}" '
                    f'style="color: {self._link_color}; '
                    f'text-decoration: underline; '
                    f'font-family: monospace;">'
                    f"{_escape_html(addr)}</a>"
                )
            else:
                addr_html = (
                    f'<span style="font-family: monospace;">'
                    f"{_escape_html(addr)}</span>"
                )
            label = QLabel(
                f"{_escape_html(entry.symbol)} ({addr_html})"
            )
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setOpenExternalLinks(True)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.TextSelectableByMouse
            )
            _install_copy_menu(label, addr, token_url)
            row.addWidget(label, 1)

            if self._icon_cache is not None:
                pix = self._icon_cache.get(chain.chain_id, addr)
                if pix is not None and not pix.isNull():
                    _set_coin_pixmap(self._to_icon_label, pix)
                else:
                    self._icon_cache.icon_ready.connect(self._on_to_icon_ready)
                    self._icon_cache.request(
                        chain.chain_id, addr, entry.logo_uri,
                    )
        else:
            row.addWidget(
                self._link_label(addr,
                                 self._explorer_url("address", addr),
                                 monospace=True),
                1,
            )

        return container

    def _on_to_icon_ready(self, chain_id: int, contract: str) -> None:
        if (self._to_icon_label is None
                or self._to_addr_lower is None
                or self._icon_cache is None):
            return
        if chain_id != self.chain.chain_id or contract != self._to_addr_lower:
            return
        pix = self._icon_cache.get(chain_id, contract)
        if pix is not None and not pix.isNull():
            _set_coin_pixmap(self._to_icon_label, pix)


# --- Sign-transaction dialog + gas-suggestion worker ----------------------


_WEI_PER_GWEI = 10 ** 9
# Spinbox upper bounds. 30 M gas is the current Ethereum block limit;
# 100k gwei is ~$300/gas at $3000 ETH — well beyond any realistic fee.
_GAS_LIMIT_MAX = 30_000_000
_GWEI_MAX = 100_000.0


def _wei_to_gwei(wei: int) -> float:
    """Convert wei → gwei for spinbox display. Using float for the
    spinbox's native value; we always recompute back to wei via
    Decimal at submission time so display rounding doesn't corrupt
    the on-chain value."""
    from decimal import Decimal
    return float(Decimal(wei) / Decimal(_WEI_PER_GWEI))


def _gwei_decimals(wei: int) -> int:
    """Spinbox decimals needed so ``wei`` survives the gwei round-trip.
    QDoubleSpinBox rounds its *stored* value to ``decimals`` — not just
    the display — so at the default 4 anything under 10⁵ wei quantizes
    to 0.0 and would be signed as a 0 tip (which Gnosis, base fee a few
    hundred kwei, rejects outright: "EffectivePriorityFeePerGas too low
    0 < 1"). 4 decimals when they suffice; up to 9 (= 1 wei) when not."""
    d = 4
    while d < 9 and wei % 10 ** (9 - d):
        d += 1
    return d


def _set_gwei(sp: QDoubleSpinBox, wei: int) -> None:
    """Populate a gwei spinbox from a wei amount, widening its precision
    first so tiny Gnosis/L2 fees aren't quantized to zero."""
    sp.setDecimals(_gwei_decimals(wei))
    sp.setValue(_wei_to_gwei(wei))


def _gwei_to_wei(gwei: float) -> int:
    """gwei → wei via Decimal so 1.23 gwei doesn't drift to
    1229999999.9999 in float."""
    from decimal import Decimal
    return int(Decimal(str(gwei)) * Decimal(_WEI_PER_GWEI))


def _format_usd(usd) -> str:
    """Adaptive USD precision so layer-2 fees in the sub-cent range
    don't all read as ``0.00 USD``. ≥ $1 → 2 decimals; $0.01..$1 →
    4 decimals; below that → 6 decimals."""
    from decimal import Decimal
    usd = Decimal(usd)
    if usd >= 1:
        return f"{usd:.2f} USD"
    if usd >= Decimal("0.01"):
        return f"{usd:.4f} USD"
    return f"{usd:.6f} USD"


# A chain's absolute minimum priority fee. Gnosis (Nethermind/Erigon)
# rejects a tx whose EffectivePriorityFeePerGas is < 1 wei outright
# ("FeeTooLow … 0 < 1"). Its base fee is a few wei, so baseFee×5% underflows
# to 0, and eth_maxPriorityFeePerGas returns 0 while the chain is idle —
# leaving a 0 tip the node bounces. This floor keeps it above the threshold.
# It binds only when every other signal is already 0, so it never overpays
# where a real fee market exists (and the cost is negligible regardless).
_MIN_PRIORITY_FEE_WEI = 1


def apply_gas_policy(
    *,
    estimated_gas: int,
    eip1559: bool,
    base_fee_wei: int,
    gas_price_wei: int,
    req: SigningRequest,
    max_priority_fee_wei: int = 0,
) -> dict:
    """Pure function: turn raw chain readings (gas estimate, current
    base fee or gas price, node-suggested tip) into the suggested
    gas/fee values per project policy.

    Policy:

      gas limit                = max(estimate × 1.5, dapp gas)
      EIP-1559 chain, baseFee > 0:
        maxPriorityFeePerGas   = max(baseFee × 0.05, node tip, 1 wei)
        maxFeePerGas           = max(baseFee × 2, baseFee + tip)  (≥ dapp's)
      EIP-1559 chain, baseFee == 0 (BSC-style):
        maxPriorityFeePerGas   = max(gasPrice, node tip, 1 wei)
        maxFeePerGas           = tip × 2  (≥ dapp's)
      Legacy chain:
        gasPrice               = current × 1.35  (≥ dapp's)

    The ``node tip`` floor (``max_priority_fee_wei``, from
    ``eth_maxPriorityFeePerGas``) is what makes this work on OP-stack
    L2s: there the base fee is a few hundred wei, so ``baseFee × 0.05``
    underflows to a ~zero tip and the tx is underpriced. The node's
    suggested tip (≈0.001 gwei) is the real floor. On Ethereum the node
    value is unreliable (often single-digit wei) but ``baseFee × 0.05``
    dominates, so ``max()`` picks the right signal on every chain.

    Dapp-supplied gas limit and maxFeePerGas act as a floor — we
    never silently lower these because they're safety buffers (a
    time-sensitive tx that explicitly overpays the maxFee still
    needs that ceiling to land). The priority fee is different:
    it's the tip the user actually pays, and dapps in the wild
    set it conservatively-high "just in case" (some set 2 gwei
    on Ethereum even when baseFee is 0.1 gwei — 20× the chain
    cost). Always override to 5 % of baseFee; the user can still
    bump it via the priority-fee spinner in the dialog if they
    want faster inclusion."""
    target_gas = (estimated_gas * 3) // 2
    if req.gas is not None and req.gas > target_gas:
        target_gas = req.gas
    out: dict = {"gas": target_gas, "estimated_gas": estimated_gas,
                  "eip1559": eip1559}
    if eip1559:
        if base_fee_wei > 0:
            # Ethereum-flavoured: baseFee already covers the bulk
            # of the cost, so a 5 % tip is a reasonable hint to
            # validators…
            ref = base_fee_wei
            priority = (ref * 5) // 100
            # …but on OP-stack L2s baseFee is ~0, so 5 % underflows the
            # tip; floor it at the node's own suggested minimum, and below
            # that at the chain hard minimum (Gnosis idle: both are 0).
            priority = max(priority, max_priority_fee_wei, _MIN_PRIORITY_FEE_WEI)
            # 2× base fee for spike headroom, but never below base+tip —
            # that's what binds when the base fee is negligible.
            max_fee = max(ref * 2, ref + priority)
        else:
            # baseFee == 0 (BNB Smart Chain and friends): the
            # network's reported gas_price IS the mandatory
            # minimum priority fee. Taking 5 % of it lands the tx
            # under the chain's accept threshold — BSC rejects
            # outright with "gas tip cap below minimum needed".
            # Use gas_price (floored at the node tip) as the priority,
            # doubled as the max_fee ceiling.
            ref = gas_price_wei
            priority = max(ref, max_priority_fee_wei, _MIN_PRIORITY_FEE_WEI)
            max_fee = priority * 2
        if (req.max_fee_per_gas is not None
                and req.max_fee_per_gas > max_fee):
            max_fee = req.max_fee_per_gas
        # Note: NO dapp floor on priority. See docstring.
        out["max_fee_per_gas"] = max_fee
        out["max_priority_fee_per_gas"] = priority
        out["base_fee"] = ref
    else:
        suggested = (gas_price_wei * 135) // 100
        if req.gas_price is not None and req.gas_price > suggested:
            suggested = req.gas_price
        out["gas_price"] = suggested
        out["base_fee"] = gas_price_wei
    return out


class GasSuggestionWorker(QThread):
    """Pulls gas estimate + current fee state + pending nonce, then
    runs the readings through ``apply_gas_policy`` and emits the
    suggested dict. IO surface only — the math is in the pure
    function above so it can be tested without a chain."""

    suggested = Signal(object)  # dict
    failed = Signal(str)

    def __init__(self, chain, req: SigningRequest,
                 nonce_floor: int | None = None, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._req = req
        # One past our own highest in-flight nonce (or None). The node's
        # "pending" count lags a just-broadcast tx, so without this floor a
        # back-to-back send reuses the nonce and the two txs replace each other.
        self._nonce_floor = nonce_floor

    def run(self) -> None:
        try:
            client = EthClient(self._chain)
            tx_for_estimate = {
                "from": self._req.from_addr,
                "value": hex(self._req.value_wei),
                "data": self._req.data,
            }
            if self._req.to_addr:
                tx_for_estimate["to"] = self._req.to_addr
            try:
                estimated = client.estimate_gas(tx_for_estimate)
            except Exception as e:
                # Estimation can fail (reverting tx, missing
                # allowance, etc.); fall back to a generous default
                # so the user can still confirm — they can edit the
                # gas limit before submission.
                log.warning("estimate_gas failed: %s", e)
                estimated = 21_000 if self._req.data in ("", "0x") else 250_000

            max_priority = 0
            if self._chain.eip1559:
                latest = client.rpc("eth_getBlockByNumber", ["latest", False])
                base_fee_hex = (latest or {}).get("baseFeePerGas")
                base_fee = int(base_fee_hex, 16) if base_fee_hex else 0
                gas_price = client.gas_price() if base_fee == 0 else 0
                # The chain's own suggested tip — the floor that keeps
                # OP-stack L2s (negligible base fee) from being underpriced.
                try:
                    max_priority = client.max_priority_fee()
                except Exception as e:
                    log.warning("max_priority_fee failed: %s", e)
            else:
                base_fee = 0
                gas_price = client.gas_price()

            out = apply_gas_policy(
                estimated_gas=estimated,
                eip1559=bool(self._chain.eip1559),
                base_fee_wei=base_fee,
                gas_price_wei=gas_price,
                req=self._req,
                max_priority_fee_wei=max_priority,
            )
            # Nonce: don't trust the node's "pending" count — RPCs behind a
            # load balancer report it inconsistently. The MINED ("latest")
            # count is the reliable cold-start baseline; our own in-flight
            # nonces (nonce_floor, from the txs WE broadcast and still track —
            # nobody else signs for this account) carry it forward so
            # back-to-back sends each get a fresh, increasing nonce.
            mined = client.get_transaction_count(self._req.from_addr, "latest")
            out["nonce"] = (
                max(mined, self._nonce_floor)
                if self._nonce_floor is not None else mined
            )
            self.suggested.emit(out)
        except Exception as e:
            log.exception("gas suggestion failed")
            self.failed.emit(str(e))


class _CollapsibleSection(QWidget):
    """A disclosure triangle + label that shows/hides a content area.

    Progressive disclosure (GNOME HIG): advanced controls stay tucked
    away behind a header the user can expand, instead of crowding the
    dialog. Collapsed by default. The header is a flat (border-less)
    QToolButton with a rotating arrow so it reads as an expander rather
    than a button."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._toggle = QToolButton()
        self._toggle.setText(title)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setAutoRaise(True)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.toggled.connect(self._on_toggled)
        lay.addWidget(self._toggle)
        self._content = QWidget()
        self._content.setVisible(False)
        lay.addWidget(self._content)

    def set_content_layout(self, layout) -> None:
        self._content.setLayout(layout)

    def _on_toggled(self, expanded: bool) -> None:
        self._toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._content.setVisible(expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._toggle.setChecked(expanded)

    def is_expanded(self) -> bool:
        return self._toggle.isChecked()


class _TxComposerDialog(_EventPreviewMixin, Dialog):
    """Reusable transaction-composer shell shared by the user-driven
    ``SendTokenDialog`` (and, in later phases, the dapp ``SignTransactionDialog``
    and the ENS write composers).

    Owns the common machinery: the tabbed Details/Events shell, the
    Network/From header, the decoded-call view, the collapsible gas
    section + spinners, the fee summary, the Confirm button + signing
    flow, the address-book picker, and the debounced gas re-estimate
    scaffold. Subclasses fill the hooks below to supply their own
    input rows + request construction; everything else is inherited.

    Hooks (overridable):
      ``_build_request()``        – build the SigningRequest (abstract).
      ``_build_header_rows(form, outer)`` – add subclass header rows.
      ``_build_extra_summary_rows(summary)`` – add extra fee-summary rows.
      ``_refresh_decoded_view()`` – render the decoded-call preview.
      ``_inputs_valid()``         – are the editable inputs a valid tx?
      ``_set_inputs_enabled(b)``  – enable/disable the editable inputs.
      ``_update_extra_totals(fee_wei)`` – extra summary lines (Total…).
      ``_update_state()``         – re-evaluate Confirm + preview.
      ``_on_gas_ready()``         – one-shot reaction to the first estimate.
      ``_wire_inputs()``          – connect subclass input signals.
      ``_reestimate_gas()``       – re-run the gas probe (debounced)."""

    # User clicked Confirm. The dialog stays open; the host does the
    # signing on a worker and calls accept() only on broadcast.
    sign_requested = Signal()

    def __init__(self, chain, from_addr: str, *,
                 title: str,
                 confirm_text: str,
                 confirm_icon_names: tuple[str, ...] = (),
                 confirm_fallback: QStyle.StandardPixmap =
                     QStyle.StandardPixmap.SP_ArrowUp,
                 abi_source: AnyAbiSource | None,
                 abi_cache: AbiCache,
                 start_worker,
                 token_info=None,
                 icon_cache=None,
                 native_price_usd=None,
                 known_addresses=None,
                 address_book=None,
                 identity_source: ContractIdentitySource | None = None,
                 identity_cache: ContractIdentityCache | None = None,
                 tx_cache: TransactionCache | None = None,
                 nonce_floor_provider:
                     Callable[[int, str], int | None] | None = None,
                 sim_floor_provider:
                     Callable[[int, str], int | None] | None = None,
                 base_fee_text: str = "(fetching…)",
                 resize_to: tuple[int, int] = (720, 640),
                 parent=None):
        super().__init__(parent)
        # --- common state -------------------------------------------------
        # Address book = (address, label) of the user's OWN wallets only —
        # the recipient autocomplete + own-wallet label. Scoped to wallets
        # the user added, so the picker can't suggest a foreign address.
        self._address_book = {
            a.lower(): (lbl or "") for a, lbl in (address_book or ())
        }
        self.chain = chain
        self._from_addr = to_checksum_address(from_addr)
        self._abi_source = abi_source
        self._abi_cache = abi_cache
        self._identity_source = identity_source
        self._identity_cache = (
            identity_cache if identity_cache is not None
            else ContractIdentityCache())
        self._tx_cache = tx_cache
        self._nonce_floor_provider = nonce_floor_provider
        self._start_worker = start_worker
        self._token_info = token_info
        self._icon_cache = icon_cache
        self._native_price_usd = native_price_usd
        # Addresses the user owns (lowercased), so subclasses can flag when
        # the recipient is one of their own wallets. Empty set = no hints.
        self._known_addresses = {a.lower() for a in (known_addresses or ())}
        self._known_addresses_list = list(known_addresses or ())
        # Gas state, populated by _on_gas_suggested.
        self._gas_ready = False
        self._base_fee_wei = 0
        self._estimated_gas = 0
        self._suggested_nonce: int | None = None
        # Replace-mode knobs — None for Send; SignTransactionDialog (later
        # phase) clamps the fee up to a floor and pins the nonce. None here
        # makes _on_gas_suggested behave exactly like Send's original.
        self._fixed_nonce: int | None = None
        self._fee_floor: ReplacementFloor | None = None
        # Token-icon async update target (used by _build_token_header_row /
        # _build_to_row); only relevant for ERC-20 token rows.
        self._to_icon_label: QLabel | None = None
        self._to_addr_lower: str | None = None
        # Address-book picker field — set by _make_address_field.
        self._book_completer: QCompleter | None = None
        self._book_field: QLineEdit | None = None

        self.setWindowTitle(title)
        self.resize(*resize_to)
        self._link_color = self.palette().color(
            QPalette.ColorRole.WindowText).name()

        # Tabbed shell: the existing detail widgets live on a "Details"
        # page; the mixin appends an "Events" page that previews the tx via
        # local simulation. Buttons sit below the tabs, on the dialog's own
        # (root) layout, so they're shared across tabs.
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(8)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        # Left/right padding INSIDE the tab frame so content doesn't sit flush
        # against its border (the same breathing room the window gets at its
        # edge). Font-derived, matching the dialog's edge margin.
        _pad = self.fontMetrics().height() // 2
        _details_page = QWidget()
        outer = QVBoxLayout(_details_page)
        outer.setContentsMargins(_pad, 6, _pad, _pad)
        outer.setSpacing(8)
        self._tabs.addTab(_details_page, "&Details")

        header = QFormLayout()
        header.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        header.setHorizontalSpacing(16)
        header.setVerticalSpacing(6)
        outer.addLayout(header)

        mono = QFont("monospace")
        self._mono_font = mono

        header.addRow("Network:",
                      self._value_label(f"{chain.name} ({chain.chain_id})"))
        from_url = self._explorer_url("address", self._from_addr)
        header.addRow(
            "From:",
            self._link_label(self._from_addr, from_url, monospace=True),
        )
        # Subclass-specific header rows (recipient / amount / identity …).
        self._build_header_rows(header, outer)

        # Decoded preview of the call about to be signed. Set to Expanding
        # so it absorbs vertical space rather than leaving large gaps.
        outer.addSpacing(4)
        outer.addWidget(QLabel("Decoded call:"))
        self.decoded_view = QTextEdit()
        self.decoded_view.setReadOnly(True)
        self.decoded_view.setFont(mono)
        self.decoded_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.decoded_view.setWordWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        self.decoded_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        outer.addWidget(self.decoded_view, 1)

        # Gas section — editable controls behind a collapsed "Gas settings"
        # expander; the fee summary stays visible.
        outer.addSpacing(4)
        self._build_gas_section(outer, base_fee_text=base_fee_text)

        # Always-visible fee summary.
        summary = QFormLayout()
        summary.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        summary.setHorizontalSpacing(16)
        self.max_total_lbl = self._value_label("—")
        summary.addRow("Expected fee:", self.max_total_lbl)
        self._build_extra_summary_rows(summary)
        outer.addLayout(summary)

        # --- buttons -------------------------------------------------
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.confirm_btn = self.buttons.addButton(
            confirm_text, QDialogButtonBox.ButtonRole.AcceptRole,
        )
        icon = self._confirm_icon(confirm_icon_names, confirm_fallback)
        if not icon.isNull() and icon.availableSizes():
            self.confirm_btn.setIcon(icon)
        self.confirm_btn.setEnabled(False)
        self.buttons.rejected.connect(self.reject)
        # Emit a request signal rather than accept()ing here — the host runs
        # signing on a worker while the dialog stays visible.
        self.confirm_btn.clicked.connect(self.sign_requested.emit)

        # --- events preview tab (lazy local simulation) --------------
        self._init_event_preview(self._tabs, known_addresses=known_addresses,
                                 sim_floor_provider=sim_floor_provider)
        # Run the preview up front so a predicted revert warns before the user
        # opens the Events tab.
        self.request_simulation()

        root.addWidget(self.revert_banner())
        root.addWidget(self.buttons)

        # Recompute the "Expected fee" line whenever the user touches an
        # input that affects it: gas limit and either (1559) priority tip or
        # (legacy) gas price. spin_max_fee is excluded — it caps the upper
        # bound but doesn't change the expected effective rate (base + tip).
        for sp in (self.spin_gas, self.spin_priority, self.spin_gas_price):
            if sp is not None:
                sp.valueChanged.connect(self._update_max_total)

        # Debounced gas re-estimate scaffold — subclasses decide when to
        # start the timer (Send: on a valid recipient).
        self._reestimate_timer = QTimer(self)
        self._reestimate_timer.setSingleShot(True)
        self._reestimate_timer.setInterval(400)
        self._reestimate_timer.timeout.connect(self._reestimate_gas)
        self._last_estimated_recipient: str | None = None

        # Subclass input wiring (recipient/amount edits, etc.).
        self._wire_inputs()
        # Render an empty-state preview right away.
        self._refresh_decoded_view()

    # --- construction helpers --------------------------------------

    @staticmethod
    def _confirm_icon(names: tuple[str, ...],
                      fallback: QStyle.StandardPixmap) -> QIcon:
        """Resolve the Confirm button icon: try each theme name in order,
        falling back through the remaining names and finally to a standard
        pixmap (so it always renders something)."""
        icon = QApplication.style().standardIcon(fallback)
        for name in reversed(names):
            icon = QIcon.fromTheme(name, icon)
        return icon

    def _build_gas_section(self, outer, *, base_fee_text: str) -> None:
        """Build the collapsible "Gas settings" section: gas-limit spinner,
        the one applicable fee-mode spinner(s), and the base-fee label."""
        self._gas_section = _CollapsibleSection("Gas settings")
        gas_form = QFormLayout()
        gas_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        gas_form.setHorizontalSpacing(16)

        self.spin_gas = QSpinBox()
        self.spin_gas.setRange(21_000, _GAS_LIMIT_MAX)
        self.spin_gas.setSingleStep(1_000)
        self.spin_gas.setSuffix(" gas")
        self.spin_gas.setEnabled(False)
        gas_form.addRow("Gas limit:", self.spin_gas)

        # Exactly one fee mode is populated; the other three stay None.
        self.spin_max_fee: QDoubleSpinBox | None
        self.spin_priority: QDoubleSpinBox | None
        self.spin_gas_price: QDoubleSpinBox | None
        if self.chain.eip1559:
            self.spin_max_fee = QDoubleSpinBox()
            self.spin_max_fee.setRange(0.0, _GWEI_MAX)
            self.spin_max_fee.setDecimals(4)
            self.spin_max_fee.setSingleStep(0.5)
            self.spin_max_fee.setSuffix(" gwei")
            self.spin_max_fee.setEnabled(False)
            gas_form.addRow("Max fee / gas:", self.spin_max_fee)

            self.spin_priority = QDoubleSpinBox()
            self.spin_priority.setRange(0.0, _GWEI_MAX)
            self.spin_priority.setDecimals(4)
            self.spin_priority.setSingleStep(0.1)
            self.spin_priority.setSuffix(" gwei")
            self.spin_priority.setEnabled(False)
            gas_form.addRow("Max priority / gas:", self.spin_priority)
            self.spin_gas_price = None
        else:
            self.spin_max_fee = None
            self.spin_priority = None
            self.spin_gas_price = QDoubleSpinBox()
            self.spin_gas_price.setRange(0.0, _GWEI_MAX)
            self.spin_gas_price.setDecimals(4)
            self.spin_gas_price.setSingleStep(0.5)
            self.spin_gas_price.setSuffix(" gwei")
            self.spin_gas_price.setEnabled(False)
            gas_form.addRow("Gas price:", self.spin_gas_price)

        self.base_fee_lbl = self._value_label(base_fee_text)
        gas_form.addRow("Network base fee:", self.base_fee_lbl)
        self._gas_section.set_content_layout(gas_form)
        outer.addWidget(self._gas_section)

    # --- shared widget helpers -------------------------------------

    def _value_label(self, text: str, *, monospace: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if monospace:
            lbl.setFont(self._mono_font)
        lbl.setWordWrap(True)
        return lbl

    def _link_label(self, text: str, url: str | None, *,
                    monospace: bool = False) -> QLabel:
        if not url:
            return self._value_label(text, monospace=monospace)
        style = f"color: {self._link_color}; text-decoration: underline;"
        if monospace:
            style += " font-family: monospace;"
        html = (
            f'<a href="{_escape_html(url)}" style="{style}">'
            f"{_escape_html(text)}</a>"
        )
        lbl = QLabel(html)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setOpenExternalLinks(True)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        lbl.setWordWrap(True)
        _install_copy_menu(lbl, text, url)
        return lbl

    def _explorer_url(self, kind: str, addr: str,
                       *, ref_addr: str | None = None) -> str | None:
        if not self.chain.explorer or not addr:
            return None
        base = self.chain.explorer.rstrip("/")
        if kind == "tx":
            return f"{base}/tx/{addr}"
        if kind == "address":
            return f"{base}/address/{addr}"
        if kind == "token":
            url = f"{base}/token/{addr}"
            if ref_addr:
                url += f"?a={ref_addr}"
            return url
        return None

    def _build_token_header_row(self, asset: dict, mono: QFont) -> QWidget:
        """Icon + "SYMBOL (linked-contract-addr)" — same treatment
        the rest of the app uses for known ERC-20s."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        addr = to_checksum_address(asset["contract"])
        from_cs = self._from_addr

        self._to_addr_lower = addr.lower()
        self._to_icon_label = QLabel()
        self._to_icon_label.setFixedSize(20, 20)
        row.addWidget(self._to_icon_label)

        token_url = self._explorer_url("token", addr, ref_addr=from_cs)
        if token_url:
            addr_html = (
                f'<a href="{_escape_html(token_url)}" '
                f'style="color: {self._link_color}; '
                f'text-decoration: underline; '
                f'font-family: monospace;">'
                f"{_escape_html(addr)}</a>"
            )
        else:
            addr_html = (
                f'<span style="font-family: monospace;">'
                f"{_escape_html(addr)}</span>"
            )
        label = QLabel(f"{_escape_html(asset['symbol'])} ({addr_html})")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        _install_copy_menu(label, addr, token_url)
        row.addWidget(label, 1)

        if self._icon_cache is not None:
            pix = self._icon_cache.get(self.chain.chain_id, addr)
            if pix is not None and not pix.isNull():
                _set_coin_pixmap(self._to_icon_label, pix)
            elif asset.get("logo_uri"):
                self._icon_cache.icon_ready.connect(self._on_to_icon_ready)
                self._icon_cache.request(
                    self.chain.chain_id, addr, asset["logo_uri"],
                )
        return container

    def _on_to_icon_ready(self, chain_id: int, contract: str) -> None:
        if (self._to_icon_label is None
                or self._to_addr_lower is None
                or self._icon_cache is None):
            return
        if chain_id != self.chain.chain_id or contract != self._to_addr_lower:
            return
        pix = self._icon_cache.get(chain_id, contract)
        if pix is not None and not pix.isNull():
            _set_coin_pixmap(self._to_icon_label, pix)

    def _build_to_row(self, to_addr: str | None, from_addr: str,
                      chain, mono: QFont) -> QWidget:
        """Render a "To:" field for a *fixed* destination address (the dapp
        Sign flow): contract-creation placeholder, a known ERC-20's
        icon + symbol + linked-contract row, or a plain linked address.
        Async icon updates land in ``_on_to_icon_ready``."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        if not to_addr:
            label = QLabel("(contract creation)")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setFont(mono)
            row.addWidget(label)
            row.addStretch(1)
            return container

        addr = to_checksum_address(to_addr)
        from_cs = to_checksum_address(from_addr)
        entry = (self._token_info(chain.chain_id, addr)
                 if self._token_info is not None else None)

        if entry is not None:
            self._to_addr_lower = addr.lower()
            self._to_icon_label = QLabel()
            self._to_icon_label.setFixedSize(20, 20)
            row.addWidget(self._to_icon_label)

            token_url = self._explorer_url(
                "token", addr, ref_addr=from_cs,
            )
            if token_url:
                addr_html = (
                    f'<a href="{_escape_html(token_url)}" '
                    f'style="color: {self._link_color}; '
                    f'text-decoration: underline; '
                    f'font-family: monospace;">'
                    f"{_escape_html(addr)}</a>"
                )
            else:
                addr_html = (
                    f'<span style="font-family: monospace;">'
                    f"{_escape_html(addr)}</span>"
                )
            label = QLabel(
                f"{_escape_html(entry.symbol)} ({addr_html})"
            )
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setOpenExternalLinks(True)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.TextSelectableByMouse
            )
            _install_copy_menu(label, addr, token_url)
            row.addWidget(label, 1)

            if self._icon_cache is not None:
                pix = self._icon_cache.get(chain.chain_id, addr)
                if pix is not None and not pix.isNull():
                    _set_coin_pixmap(self._to_icon_label, pix)
                else:
                    self._icon_cache.icon_ready.connect(self._on_to_icon_ready)
                    self._icon_cache.request(
                        chain.chain_id, addr, entry.logo_uri,
                    )
        else:
            row.addWidget(
                self._link_label(addr,
                                 self._explorer_url("address", addr),
                                 monospace=True),
                1,
            )
        return container

    # --- address-book picker ---------------------------------------

    def _make_address_field(self, initial: str = "") -> tuple[QWidget, QLineEdit]:
        """A monospace address QLineEdit + a ▾ button that pops the
        address-book completer over the user's own wallets. Returns
        ``(container_widget, line_edit)``. Sets ``self._book_completer`` /
        ``self._book_field`` so ``_show_book_popup`` drives this field."""
        edit = QLineEdit()
        if initial:
            edit.setText(initial)
        edit.setFont(self._mono_font)
        self._book_field = edit
        completer = self._build_book_completer()
        self._book_completer = completer
        if completer is not None:
            edit.setCompleter(completer)
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)                       # button sits flush
        lay.addWidget(edit, 1)
        if completer is not None:
            book_btn = QToolButton()
            book_btn.setText("▾")
            book_btn.setToolTip("Pick from your wallets")
            # Match the input field's height so it doesn't stick up above it.
            book_btn.setFixedHeight(edit.sizeHint().height())
            book_btn.clicked.connect(self._show_book_popup)
            lay.addWidget(book_btn)
        return container, edit

    def _build_book_completer(self) -> QCompleter | None:
        """An autocomplete over the user's own wallets — search by label or
        address, contains-style. None when the book is empty."""
        if not self._address_book:
            return None
        model = QStandardItemModel(self)
        for low, label in sorted(self._address_book.items(),
                                 key=lambda kv: (kv[1] or "￿").lower()):
            addr = to_checksum_address(low)
            disp = f"{label} — {addr}" if label else addr
            item = QStandardItem(disp)
            item.setData(addr, Qt.ItemDataRole.UserRole)
            item.setEditable(False)
            model.appendRow(item)
        completer = _AddressBookCompleter(model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        return completer

    def _show_book_popup(self) -> None:
        if self._book_completer is None or self._book_field is None:
            return
        self._book_field.setFocus()
        self._book_completer.setCompletionPrefix("")
        self._book_completer.complete()

    # --- gas suggestion + fee summary ------------------------------

    def _kick_gas(self, probe: SigningRequest) -> None:
        """Run GasSuggestionWorker for ``probe`` and wire its result onto the
        shared spinners. Applies the host's nonce floor, if any."""
        floor = (self._nonce_floor_provider(self.chain.chain_id, self._from_addr)
                 if self._nonce_floor_provider is not None else None)
        gas_worker = GasSuggestionWorker(self.chain, probe, nonce_floor=floor)
        gas_worker.suggested.connect(self._on_gas_suggested)
        gas_worker.failed.connect(self._on_gas_failed)
        self._start_worker(gas_worker)

    def _on_gas_suggested(self, info: dict) -> None:
        self._base_fee_wei = int(info.get("base_fee") or 0)
        self._estimated_gas = int(info.get("estimated_gas") or 0)
        self.spin_gas.setValue(info["gas"])
        self.spin_gas.setEnabled(True)
        floor = self._fee_floor
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            max_fee = info["max_fee_per_gas"]
            prio = info["max_priority_fee_per_gas"]
            if floor is not None:   # replace-mode: never price below the bump
                if floor.max_fee_per_gas:
                    max_fee = max(max_fee, floor.max_fee_per_gas)
                if floor.max_priority_fee_per_gas:
                    prio = max(prio, floor.max_priority_fee_per_gas)
            _set_gwei(self.spin_max_fee, max_fee)
            self.spin_max_fee.setEnabled(True)
            _set_gwei(self.spin_priority, prio)
            self.spin_priority.setEnabled(True)
        else:
            assert self.spin_gas_price is not None
            gp = info["gas_price"]
            if floor is not None and floor.gas_price:
                gp = max(gp, floor.gas_price)
            _set_gwei(self.spin_gas_price, gp)
            self.spin_gas_price.setEnabled(True)
        self.base_fee_lbl.setText(
            f"{_wei_to_gwei(self._base_fee_wei):.4f} gwei"
        )
        # Replace-mode locks the nonce to the pending tx's; otherwise use
        # the chain's next-nonce from the suggestion.
        self._suggested_nonce = (
            self._fixed_nonce if self._fixed_nonce is not None
            else info.get("nonce"))
        self._gas_ready = True
        self._on_gas_ready()
        self._update_state()

    def _on_gas_failed(self, msg: str) -> None:
        self.base_fee_lbl.setText(f"(failed: {msg})")

    def _update_max_total(self) -> None:
        if not self._gas_ready:
            return
        gas = min(self._estimated_gas or self.spin_gas.value(),
                  self.spin_gas.value())
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            effective = (
                self._base_fee_wei + _gwei_to_wei(self.spin_priority.value())
            )
        else:
            assert self.spin_gas_price is not None
            effective = _gwei_to_wei(self.spin_gas_price.value())
        fee_wei = gas * effective
        fee_text = f"≈ {wei_to_ether(fee_wei)} {self.chain.symbol}"
        if self._native_price_usd is not None:
            usd = wei_to_ether(fee_wei) * self._native_price_usd
            fee_text += f"  ({_format_usd(usd)})"
        self.max_total_lbl.setText(fee_text)
        self._update_extra_totals(fee_wei)

    def set_signing_in_progress(self, busy: bool) -> None:
        """Lock / unlock the dialog while the host runs the
        sign-and-broadcast worker. Symmetric across all composer
        subclasses so the host's ``_begin_sign`` flow can drive any of
        them identically."""
        ok_to_enable = not busy and self._gas_ready and self._inputs_valid()
        self.confirm_btn.setEnabled(ok_to_enable)
        for btn in self.buttons.buttons():
            if btn is not self.confirm_btn:
                btn.setEnabled(not busy)
        self.spin_gas.setEnabled(not busy and self._gas_ready)
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            self.spin_max_fee.setEnabled(not busy and self._gas_ready)
            self.spin_priority.setEnabled(not busy and self._gas_ready)
        else:
            assert self.spin_gas_price is not None
            self.spin_gas_price.setEnabled(not busy and self._gas_ready)
        self._set_inputs_enabled(not busy)

    def _update_state(self) -> None:
        ok = self._gas_ready and self._inputs_valid()
        self.confirm_btn.setEnabled(ok)
        self._refresh_decoded_view()
        self._update_max_total()

    # --- request construction --------------------------------------

    def _sim_params(self):
        """Tx params for the events preview, derived from ``_build_request``.
        Raises SignerError (caught by the mixin) until the inputs are a
        valid tx, so the preview shows a 'fill these in' note."""
        req = self._build_request()
        return (req.from_addr, req.to_addr, req.data or "0x",
                int(req.value_wei or 0))

    def finalised_request(self) -> SigningRequest:
        """The SigningRequest with all gas / fee / nonce fields filled from
        the dialog's current state — ready to hand to a Signer."""
        if not self._gas_ready:
            raise SignerError("gas suggestion did not complete")
        from dataclasses import replace
        base = self._build_request()  # raises SignerError if invalid
        kwargs: dict = {
            "gas": self.spin_gas.value(),
            "nonce": self._suggested_nonce,
        }
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            kwargs["max_fee_per_gas"] = _gwei_to_wei(self.spin_max_fee.value())
            kwargs["max_priority_fee_per_gas"] = _gwei_to_wei(
                self.spin_priority.value()
            )
            kwargs["gas_price"] = None
        else:
            assert self.spin_gas_price is not None
            kwargs["gas_price"] = _gwei_to_wei(self.spin_gas_price.value())
            kwargs["max_fee_per_gas"] = None
            kwargs["max_priority_fee_per_gas"] = None
        return replace(base, **kwargs)

    # --- hooks (overridable by subclasses) -------------------------

    def _build_request(self) -> SigningRequest:
        raise NotImplementedError

    def _build_header_rows(self, header: QFormLayout, outer) -> None:
        ...

    def _build_extra_summary_rows(self, summary: QFormLayout) -> None:
        ...

    def _refresh_decoded_view(self) -> None:
        ...

    def _inputs_valid(self) -> bool:
        return True

    def _set_inputs_enabled(self, enabled: bool) -> None:
        ...

    def _update_extra_totals(self, fee_wei: int) -> None:
        ...

    def _on_gas_ready(self) -> None:
        ...

    def _wire_inputs(self) -> None:
        ...

    def _reestimate_gas(self) -> None:
        ...


class SignTransactionDialog(_TxComposerDialog):
    """Confirmation dialog for an incoming ``eth_sendTransaction``
    from the Frame RPC (and the speed-up / cancel replace flow).

    A thin subclass of ``_TxComposerDialog``: the shell, gas/fee
    machinery, decoded view, fee summary, signing flow and the
    replace-mode fee-floor / fixed-nonce clamp all live on the base.
    This class supplies the fixed-``req`` request, its header rows
    (Requested-by / To / Contract / Value) and the *async* decoded
    call (arbitrary dapp calldata, so it fetches the contract ABI
    with a 4-byte signature-DB fallback rather than a synthetic tree).

    The dialog is driven asynchronously by the host: clicking
    "Confirm and Sign" emits ``sign_requested`` (rather than
    closing the dialog). The host runs signing on a worker; on
    success it calls ``accept()``; on failure it pops an error
    parented to the still-open dialog and calls
    ``set_signing_in_progress(False)`` so the user can retry."""

    def __init__(self, req: SigningRequest, chain, *,
                 abi_source: AnyAbiSource | None,
                 abi_cache: AbiCache,
                 start_worker,
                 token_info=None,
                 icon_cache=None,
                 native_price_usd=None,
                 known_addresses=None,
                 identity_source: ContractIdentitySource | None = None,
                 identity_cache: ContractIdentityCache | None = None,
                 tx_cache: TransactionCache | None = None,
                 fixed_nonce: int | None = None,
                 fee_floor: ReplacementFloor | None = None,
                 replace_label: str | None = None,
                 nonce_floor_provider:
                     Callable[[int, str], int | None] | None = None,
                 sim_floor_provider:
                     Callable[[int, str], int | None] | None = None,
                 parent=None):
        # Sign-specific state the base __init__ touches while it runs our
        # header/decoded hooks — must be set before super().__init__().
        self.req = req
        self._replace_label = replace_label
        # The decode is one-shot (req is fixed); guard so the base calling
        # _refresh_decoded_view() again from _update_state (post gas) doesn't
        # re-kick the AbiFetchWorker and stomp a completed decode.
        self._decode_kicked = False
        super().__init__(
            chain,
            to_checksum_address(req.from_addr) if req.from_addr else req.from_addr,
            title=replace_label or "Sign Transaction",
            confirm_text="&Replace and Sign" if replace_label
                         else "&Confirm and Sign",
            # Checkmark icon — universal "approve"; same resolution chain as
            # before (emblem-ok › dialog-ok-apply › SP_DialogApplyButton).
            confirm_icon_names=("emblem-ok", "dialog-ok-apply"),
            confirm_fallback=QStyle.StandardPixmap.SP_DialogApplyButton,
            abi_source=abi_source, abi_cache=abi_cache,
            start_worker=start_worker, token_info=token_info,
            icon_cache=icon_cache, native_price_usd=native_price_usd,
            known_addresses=known_addresses,
            identity_source=identity_source, identity_cache=identity_cache,
            tx_cache=tx_cache, nonce_floor_provider=nonce_floor_provider,
            sim_floor_provider=sim_floor_provider,
            parent=parent,
        )
        # Replace-mode (speed up / cancel a pending tx): lock the nonce to the
        # pending tx's and clamp the suggested fees up to a floor so the node
        # accepts the same-nonce replacement. The base reset these to None
        # during __init__; set them now, before the gas suggestion lands.
        self._fixed_nonce = fixed_nonce
        self._fee_floor = fee_floor
        # The request is fixed, so estimate gas immediately (Send waits for a
        # recipient; here we have everything).
        self._kick_gas(self.req)

    # --- header rows (base hook) -----------------------------------

    def _build_header_rows(self, header: QFormLayout, outer) -> None:
        req = self.req
        mono = self._mono_font
        # "Requested by:" — the caller's Origin (typically the dapp URL).
        # Only shown when the RPC layer captured one; local flows leave this
        # None and the row is hidden. Inserted above the base's Network/From
        # rows so it stays at the top of the header, as before.
        if req.origin:
            header.insertRow(
                0, "Requested by:", self._link_label(req.origin, req.origin),
            )
        header.addRow(
            "To:", self._build_to_row(req.to_addr, req.from_addr, self.chain, mono),
        )
        _id_label, _id_kick = _make_identity_row(
            to_addr=req.to_addr, chain=self.chain,
            identity_source=self._identity_source,
            identity_cache=self._identity_cache,
            my_addresses=self._known_addresses_list,
            start_worker=self._start_worker, tx_cache=self._tx_cache)
        if _id_label is not None and _id_kick is not None:
            header.addRow("Contract:", _id_label)
            _id_kick()
        if req.value_wei > 0:
            ether = wei_to_ether(req.value_wei)
            header.addRow(
                "Value:",
                self._value_label(
                    f"{ether} {self.chain.symbol}  ({req.value_wei} wei)"),
            )
        else:
            header.addRow("Value:", self._value_label("0"))

    # --- decoded call (base hook) — async, Sign-specific -----------

    def _refresh_decoded_view(self) -> None:
        """Kick the background decode of the dapp's (arbitrary) calldata.
        One-shot: the request is fixed, so re-entry (the base calls this
        again from _update_state once gas lands) is a no-op."""
        if self._decode_kicked:
            return
        self._decode_kicked = True
        req = self.req
        if (req.data and req.data not in ("0x", "0X") and req.to_addr
                and self._abi_source is not None):
            self.decoded_view.setPlainText("(decoding…)")
            worker = AbiFetchWorker(
                self._abi_source, self._abi_cache,
                self.chain.chain_id, req.to_addr,
            )
            worker.ready.connect(self._on_abi_ready)
            self._start_worker(worker)
        elif not req.to_addr:
            self.decoded_view.setPlainText("(contract creation — no method call)")
        else:
            self.decoded_view.setPlainText("(plain value transfer — no calldata)")

    def _sim_blocked_text(self) -> str:
        return "(contract creation — no events to preview)"

    def _on_abi_ready(self, abi) -> None:
        decoded = None
        if isinstance(abi, list):
            decoded = decode_call(abi, self.req.data, address=self.req.to_addr)
        if decoded is not None:
            self._render_decoded_call(decoded)
            return
        # No contract ABI decoded it — fall back to the 4-byte signature DB
        # (unverified contract / proxy / fallback), like an explorer.
        self._abi_state = abi
        self.decoded_view.setPlainText("(decoding via signature database…)")
        worker = SignatureFetchWorker(self.req.data)
        worker.ready.connect(self._on_signature_ready)
        self._start_worker(worker)

    def _on_signature_ready(self, decoded) -> None:
        if decoded is not None:
            self._render_decoded_call(decoded)
            return
        abi = getattr(self, "_abi_state", None)
        if abi is False:
            msg = ("(contract source is not verified, and the call's "
                   "selector isn't in the 4-byte database)")
        elif abi is None:
            msg = "(failed to fetch ABI — try again later)"
        else:
            msg = ("(this calldata didn't match the contract's ABI, and "
                   "its selector isn't in the 4-byte database)")
        self.decoded_view.setPlainText(msg)

    def _render_decoded_call(self, decoded) -> None:
        token_context = None
        if self._token_info is not None and self.req.to_addr:
            entry = self._token_info(self.chain.chain_id, self.req.to_addr)
            if entry is not None:
                token_context = {
                    "symbol": entry.symbol,
                    "decimals": entry.decimals,
                }
        _render_decoded(
            self.decoded_view, decoded, token_context,
            known_addresses=self._known_addresses,
        )

    # --- request construction (base hook) --------------------------

    def _build_request(self) -> SigningRequest:
        return self.req


def _erc20_transfer_calldata(recipient: str, amount_raw: int) -> str:
    """Encode an ERC-20 ``transfer(address,uint256)`` call. Returned
    as ``"0x"``-prefixed hex so it slots straight into a
    SigningRequest.data field."""
    from eth_abi import encode
    selector = bytes.fromhex("a9059cbb")
    encoded = encode(["address", "uint256"], [recipient, amount_raw])
    return "0x" + (selector + encoded).hex()


class _AddressBookCompleter(QCompleter):
    """Recipient autocomplete over the user's wallets. The popup shows (and
    matches) "label — 0x…" so you can search by label OR address, but when a
    row is chosen we insert ONLY the bare address (held in UserRole) — the
    recipient field must end up with a valid address, not the display text.
    pathFromIndex() is the hook QLineEdit calls to decide the inserted text,
    so this is race-free (unlike overriding the text after activation)."""

    def pathFromIndex(
        self, index: QModelIndex | QPersistentModelIndex,
    ) -> str:
        addr = index.data(Qt.ItemDataRole.UserRole)
        return addr if addr else super().pathFromIndex(index)




class SendTokenDialog(_TxComposerDialog):
    """User-driven counterpart to ``SignTransactionDialog``. Same
    overall shape (gas controls, expected fee, signing flow) but
    the recipient + amount are *editable*: the user types them
    here rather than receiving them from a dapp. On Confirm the
    dialog builds the SigningRequest and emits ``sign_requested``;
    the host runs the same worker pipeline as for RPC-driven
    requests.

    A thin subclass of ``_TxComposerDialog``: the shell, gas/fee
    machinery, decoded view, signing flow and address-book picker all
    live on the base; this class supplies the editable recipient/amount
    rows + the ERC-20/native request construction via the base hooks."""

    def __init__(self, asset: dict, chain, from_addr: str, *,
                 abi_source: AnyAbiSource | None,
                 abi_cache: AbiCache,
                 start_worker,
                 token_info=None,
                 icon_cache=None,
                 native_price_usd=None,
                 known_addresses=None,
                 address_book=None,
                 identity_source: ContractIdentitySource | None = None,
                 identity_cache: ContractIdentityCache | None = None,
                 tx_cache: TransactionCache | None = None,
                 nonce_floor_provider:
                     Callable[[int, str], int | None] | None = None,
                 sim_floor_provider:
                     Callable[[int, str], int | None] | None = None,
                 parent=None):
        # Send-specific state that the base __init__ touches while it runs
        # our header/decoded hooks — must be set before super().__init__().
        self._asset = asset
        # Contract-identity row for the typed recipient (created in the
        # header rows below; the worker handler reads _identity_last_addr).
        self._identity_label: QLabel | None = None
        self._identity_last_addr: str | None = None
        self._recipient_hint = ""  # "", "own", or "token"
        # ENS forward-resolution state — read by _parsed_recipient(), which
        # the base calls from _refresh_decoded_view during construction.
        self._ens_input = ""
        self._ens_resolved: str | None = None
        super().__init__(
            chain, from_addr,
            title=f"Send {asset['symbol']}",
            confirm_text="&Send",
            # Same mail-send icon as the toolbar Send button on the tokens
            # panel — same meaning across all the places a send launches.
            confirm_icon_names=("mail-send", "document-send"),
            confirm_fallback=QStyle.StandardPixmap.SP_ArrowUp,
            abi_source=abi_source, abi_cache=abi_cache,
            start_worker=start_worker, token_info=token_info,
            icon_cache=icon_cache, native_price_usd=native_price_usd,
            known_addresses=known_addresses, address_book=address_book,
            identity_source=identity_source, identity_cache=identity_cache,
            tx_cache=tx_cache, nonce_floor_provider=nonce_floor_provider,
            sim_floor_provider=sim_floor_provider,
            base_fee_text="(enter recipient to estimate)",
            parent=parent,
        )

    # --- header rows (base hook) -----------------------------------

    def _build_header_rows(self, header: QFormLayout, outer) -> None:
        asset = self._asset
        mono = self._mono_font
        if asset["is_native"]:
            header.addRow("Asset:", self._value_label(asset["symbol"]))
        else:
            # Token row uses the same icon + symbol + linked-contract
            # treatment as the rest of the app for consistency. The
            # contract is what the tx's To: field is set to (not the
            # recipient); the recipient is collected via the field below.
            header.addRow("Token:", self._build_token_header_row(asset, mono))

        # Recipient — editable. Validated only on Confirm so the user
        # can paste partial text without seeing intermediate errors. The
        # ▾ button pops the address-book completer over the user's wallets.
        to_row, self.recipient_edit = self._make_address_field()
        self.recipient_edit.setPlaceholderText("0x… address or name.eth")
        header.addRow("&To:", to_row)
        # ENS: when the recipient is a name (name.eth), forward-resolve it
        # and show the 0x address here (highlighted) so the user verifies
        # the actual destination before signing. Hidden for a plain address.
        self._ens_label = QLabel("")
        self._ens_label.setWordWrap(True)
        self._ens_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addRow(self._ens_label)
        self._ens_form = header
        self._ens_form.setRowVisible(self._ens_label, False)
        # Identity of the typed recipient (when it resolves to a contract):
        # name / verified / age / deployer — filled async, see
        # _update_recipient_identity.
        self._identity_label = QLabel("")
        # Keep the wrapped label's default size policy — see _make_identity_row.
        self._identity_label.setWordWrap(True)
        self._identity_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        # "Identity:", not "Contract:" — in the Send dialog this row describes
        # the typed RECIPIENT (usually an EOA: "Regular account … sent here
        # N×"), not a contract. The tx's actual contract is the "Token:" row
        # above. (The sign/details dialogs keep "Contract:" — there the row
        # genuinely describes tx.to.)
        header.addRow("Identity:", self._identity_label)

        # Amount + Max + balance label.
        amount_box = QWidget()
        amount_row = QHBoxLayout(amount_box)
        amount_row.setContentsMargins(0, 0, 0, 0)
        amount_row.setSpacing(8)
        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText(f"Amount in {asset['symbol']}")
        amount_row.addWidget(self.amount_edit, 1)
        self.max_btn = QPushButton("Ma&x")
        self.max_btn.clicked.connect(self._on_max_clicked)
        # For native sends Max must deduct a gas reserve — disable
        # it until GasSuggestionWorker has populated _gas_ready so
        # we never drop the full balance into the field (chain
        # rejects with "insufficient funds for gas * price + value"
        # at broadcast time). For ERC-20s gas is paid in the native
        # asset, so Max is independent of gas readiness.
        if asset["is_native"]:
            self.max_btn.setEnabled(False)
            self.max_btn.setToolTip("Waiting for gas estimate…")
        amount_row.addWidget(self.max_btn)
        # Buddy the mnemonic to the inner line-edit, not the row
        # container, so Alt+A lands the cursor in the amount field.
        amount_lbl = QLabel("&Amount:")
        amount_lbl.setBuddy(self.amount_edit)
        header.addRow(amount_lbl, amount_box)

        balance_dec = (
            Decimal(asset["balance_raw"]) / (Decimal(10) ** asset["decimals"])
        )
        self.balance_lbl = self._value_label(
            f"{balance_dec} {asset['symbol']}"
        )
        header.addRow("Balance:", self.balance_lbl)

        # Live USD value of the typed amount (token or native). Updated on
        # every keystroke; blank when there's no price or no valid amount.
        self._value_usd_lbl = self._value_label("")
        header.addRow("Value:", self._value_usd_lbl)

    def _build_extra_summary_rows(self, summary: QFormLayout) -> None:
        # When sending native ETH, the value moves out of the wallet too —
        # so a "Total to send" line that combines fee + value is genuinely
        # useful. For ERC-20s it'd just duplicate the Amount field, so we
        # only show it for native sends.
        self.total_lbl: QLabel | None
        if self._asset["is_native"]:
            self.total_lbl = self._value_label("—")
            summary.addRow("Total to send:", self.total_lbl)
        else:
            self.total_lbl = None

    def _wire_inputs(self) -> None:
        # Recipient/amount changes re-evaluate the Confirm button enable
        # state, the previews, and (for native) the Total line.
        self.recipient_edit.textChanged.connect(self._update_ens)
        self.recipient_edit.textChanged.connect(self._update_recipient_identity)
        self.recipient_edit.textChanged.connect(self._update_state)
        self.recipient_edit.textChanged.connect(self._update_recipient_hint)
        self.recipient_edit.textChanged.connect(self.request_simulation)
        self.amount_edit.textChanged.connect(self._update_state)
        self.amount_edit.textChanged.connect(self._update_usd_value)
        self.amount_edit.textChanged.connect(self.request_simulation)
        # Estimate gas only against the recipient the user has actually
        # typed — never against a placeholder. ERC-20 transfer cost depends
        # heavily on the recipient's storage slot (cold vs warm, zero vs
        # non-zero balance) and a placeholder is just wrong in some
        # direction. Until a valid address lands the spinners stay
        # un-populated and the Send button stays disabled. Debounced so each
        # keystroke doesn't fire an RPC call (the timer lives on the base).
        self.recipient_edit.textChanged.connect(
            lambda _t: self._reestimate_timer.start()
        )

    # --- token header icon update ---------------------------------

    # (_build_token_header_row / _on_to_icon_ready live on the base.)

    # --- input handling --------------------------------------------

    def _update_recipient_identity(self) -> None:
        """Resolve the typed recipient's contract identity once it's a
        valid, changed address. The worker hits the disk cache first, so
        re-typing a seen address is instant + offline; a stale in-flight
        result (recipient changed meanwhile) is dropped in the handler."""
        label = self._identity_label
        if label is None:
            return
        # Identify the ACTUAL destination — the resolved address for an ENS
        # name, or the typed 0x address. _parsed_recipient() returns None
        # until an ENS name resolves, so the row stays blank meanwhile.
        resolved = self._parsed_recipient()
        if resolved is None:
            label.setText("")
            label.setStyleSheet("")
            self._identity_last_addr = None
            return
        addr = resolved.lower()
        if addr == self._identity_last_addr:
            return
        self._identity_last_addr = addr
        # One of the user's own wallets → show its label, no contract lookup.
        if addr in self._address_book:
            lbl = self._address_book[addr]
            label.setText(f"Your wallet — {lbl}" if lbl else "Your wallet")
            label.setStyleSheet(            # theme-safe green pill
                "background:#d1e7dd; color:#0f5132; padding:1px 6px;"
                " border-radius:4px;")
            return
        label.setText("…")
        label.setStyleSheet("")
        worker = ContractIdentityWorker(
            self._identity_source, self._identity_cache, self.chain.chain_id,
            addr, self._known_addresses_list, tx_cache=self._tx_cache,
            mode="send")
        worker.ready.connect(
            lambda badge, a=addr: self._on_identity_ready(a, badge))
        self._start_worker(worker)

    def _on_identity_ready(self, addr: str, badge) -> None:
        if self._identity_label is None or addr != self._identity_last_addr:
            return   # stale: recipient changed while this lookup was in flight
        if badge is None:
            self._identity_label.setText("")
            self._identity_label.setStyleSheet("")
        else:
            _style_identity_label(self._identity_label, badge)

    def _asset_price_usd(self) -> Decimal | None:
        """USD price for the asset being sent: the native price the host
        passed in for native sends, or the token's cached price for ERC-20s.
        ``None`` when no price is known (we then show no value rather than
        a misleading $0)."""
        if self._asset.get("is_native"):
            return self._native_price_usd
        p = self._asset.get("price_usd")
        if not p:
            return None
        try:
            return Decimal(str(p))
        except (InvalidOperation, ValueError, TypeError):
            return None

    def _update_usd_value(self) -> None:
        """Show ``≈ <usd>`` for the amount currently typed. Blank when the
        amount is empty/invalid or no price is available."""
        price = self._asset_price_usd()
        text = self.amount_edit.text().strip().replace(",", "")
        if price is None or not text:
            self._value_usd_lbl.setText("")
            return
        try:
            amount = Decimal(text)
        except (InvalidOperation, ValueError):
            self._value_usd_lbl.setText("")
            return
        if amount < 0:
            self._value_usd_lbl.setText("")
            return
        self._value_usd_lbl.setText("≈ " + _format_usd(amount * price))

    def _on_max_clicked(self) -> None:
        """For ERC-20s the full balance is sendable — gas is paid
        in the native asset. For NATIVE sends, deduct the **upper
        bound** on what the chain may charge: ``gas × maxFeePerGas``
        (EIP-1559) or ``gas × gasPrice`` (legacy). maxFeePerGas is
        the hard ceiling the user has authorised, so even if
        baseFee spikes between Max click and broadcast we won't
        pay more than this. Bumped 50 % on top to absorb (a) gas
        estimate undershoot, (b) the user nudging the fee spinners
        up before sending, (c) basefee bursts past the suggested
        max. The user explicitly preferred 'too much margin' over
        'too little' here — leaving dust in the wallet is fine,
        an 'insufficient funds' reject is not. The Max button is
        disabled until ``_gas_ready`` so this branch is the only
        code path for native."""
        raw = self._asset["balance_raw"]
        if self._asset["is_native"] and self._gas_ready:
            gas = max(
                self._estimated_gas or self.spin_gas.value(),
                self.spin_gas.value(),
            )
            if self.chain.eip1559:
                assert self.spin_max_fee is not None and self.spin_priority is not None
                ceiling = _gwei_to_wei(self.spin_max_fee.value())
            else:
                assert self.spin_gas_price is not None
                ceiling = _gwei_to_wei(self.spin_gas_price.value())
            gas_cost = (gas * ceiling * 3) // 2
            raw = max(0, raw - gas_cost)
        bal = (
            Decimal(raw) / (Decimal(10) ** self._asset["decimals"])
        )
        self.amount_edit.setText(format(bal, "f"))

    @staticmethod
    def _looks_like_ens(text: str) -> bool:
        # A name to forward-resolve: has a dot, isn't a 0x address. web3
        # decides whether it actually resolves; this just gates the lookup.
        return bool(text) and "." in text and not text.startswith("0x")

    def _update_ens(self) -> None:
        """When the recipient is an ENS name, forward-resolve it (on
        mainnet) and reveal the resolved 0x address row. The resolution is
        the source of truth for _parsed_recipient(), so Send stays disabled
        until a name resolves."""
        text = self.recipient_edit.text().strip()
        if not self._looks_like_ens(text):
            if self._ens_input:        # was a name, now a plain address/empty
                self._ens_input = ""
                self._ens_resolved = None
                self._ens_form.setRowVisible(self._ens_label, False)
            return
        if text.lower() == self._ens_input:
            return                     # already resolving / resolved this name
        self._ens_input = text.lower()
        self._ens_resolved = None
        self._ens_label.setStyleSheet("")
        self._ens_label.setText(f"resolving {text} …")
        self._ens_form.setRowVisible(self._ens_label, True)
        from ..chains import DEFAULT_CHAINS
        from ..ens import EnsResolveWorker
        mainnet = next((c for c in DEFAULT_CHAINS if c.chain_id == 1), None)
        if mainnet is None:
            return
        worker = EnsResolveWorker(mainnet, text)
        worker.resolved.connect(
            lambda _nm, addr, verified, key=text.lower():
            self._on_ens_resolved(key, addr, verified))
        self._start_worker(worker)

    def _on_ens_resolved(
        self, key: str, address: str, verified: bool = False,
    ) -> None:
        if key != self._ens_input:
            return                     # recipient changed while in flight
        if address:
            self._ens_resolved = to_checksum_address(address)
            if self._recipient_is_token(self._ens_resolved):
                # Danger dominates the reassurance: the name → address mapping
                # may be verified, but the destination is a TOKEN CONTRACT —
                # sending here almost always burns the funds. A green
                # "✓ verified" pill right next to a token address reads as
                # "safe to send", so for a token we show a red warning that
                # names the token instead (the mapping's verified status moves
                # to the tooltip).
                entry = self._resolved_token_entry(self._ens_resolved)
                sym = getattr(entry, "symbol", "") if entry is not None else ""
                tag = f"⚠ {sym} token contract" if sym else "⚠ token contract"
                self._ens_label.setText(f"↳ {self._ens_resolved}  {tag}")
                self._ens_label.setStyleSheet(
                    "background:#f8d7da; color:#842029; padding:1px 6px;"
                    " border-radius:4px;")
                self._ens_label.setToolTip("⚠ Token contract — funds may burn")
            elif verified:
                # Green pill == proof-verified through Helios (same meaning as
                # the Events tab badge). Neutral style otherwise, so green
                # never overclaims a name we only trusted from a remote RPC.
                self._ens_label.setText(f"↳ {self._ens_resolved}  ✓ verified")
                self._ens_label.setStyleSheet(
                    "background:#d1e7dd; color:#0f5132; padding:1px 6px;"
                    " border-radius:4px;")
                self._ens_label.setToolTip("Cryptographically verified")
            else:
                self._ens_label.setText(f"↳ {self._ens_resolved}")
                self._ens_label.setStyleSheet("")
                self._ens_label.setToolTip("Unverified (no Helios)")
        else:
            self._ens_resolved = None
            self._ens_label.setText("⚠ name not found")
            self._ens_label.setStyleSheet(
                "background:#f8d7da; color:#842029; padding:1px 6px;"
                " border-radius:4px;")
        # The resolved address feeds the Send gate, calldata, gas estimate
        # and the contract-identity row — re-run them all.
        self._update_state()
        self._update_recipient_hint()
        self._update_recipient_identity()
        self._reestimate_timer.start()

    def _parsed_recipient(self) -> str | None:
        text = self.recipient_edit.text().strip()
        if text.startswith("0x") and len(text) == 42:
            try:
                return to_checksum_address(text)
            except Exception:
                return None
        # An ENS name is a valid recipient only once it has resolved.
        if text and text.lower() == self._ens_input and self._ens_resolved:
            return self._ens_resolved
        return None

    def _resolved_token_entry(self, recipient: str):
        """The curated-list token entry for ``recipient`` (has ``.symbol`` /
        ``.name``), or None — used to name the token in the recipient warning."""
        if self._token_info is None:
            return None
        try:
            return self._token_info(self.chain.chain_id, recipient)
        except Exception:
            return None

    def _recipient_is_token(self, recipient: str) -> bool:
        """True when the recipient address is itself a token contract —
        either the token we're about to send, or any token on the
        curated lists (so it catches tokens the user doesn't hold).
        Sending tokens/ETH to a token contract almost always burns the
        funds, hence the red flag."""
        rl = recipient.lower()
        contract = self._asset.get("contract")
        if contract and contract.lower() == rl:
            return True
        return self._resolved_token_entry(recipient) is not None

    def _update_recipient_hint(self) -> None:
        """Tint the recipient field to flag two situations, each set as
        a self-consistent (background, text) colour pair so it stays
        legible in any palette — a hardcoded background left to the
        palette's default text would wash out under a dark theme:

          - red: sending to a token contract (almost always a mistake
            that burns the funds) — wins over the green hint;
          - green: sending to one of the user's own wallets.

        Cleared to the default style otherwise."""
        recipient = self._parsed_recipient()
        if recipient is None:
            hint = ""
        elif self._recipient_is_token(recipient):
            hint = "token"
        elif recipient.lower() in self._known_addresses:
            hint = "own"
        else:
            hint = ""
        if hint == self._recipient_hint:
            return  # no change — avoid restyling on every keystroke
        self._recipient_hint = hint
        if hint == "token":
            self.recipient_edit.setStyleSheet(
                "QLineEdit { background-color: #f6d4d7; color: #7f1d1d; }"
            )
            self.recipient_edit.setToolTip("⚠ Token contract — funds may burn")
        elif hint == "own":
            self.recipient_edit.setStyleSheet(
                "QLineEdit { background-color: #d7f0db; color: #14532d; }"
            )
            self.recipient_edit.setToolTip("Your own wallet")
        else:
            self.recipient_edit.setStyleSheet("")
            self.recipient_edit.setToolTip("")

    def _parsed_amount_raw_unchecked(self) -> int | None:
        """Parse the amount field WITHOUT comparing against the
        cached balance. The calldata preview uses this so that
        typing always updates the rendered transfer(_to, _value) —
        even when the cached balance is stale (e.g. right after a
        dapp swap before our balances refresh). The Send button
        still gates on the balance via ``_parsed_amount_raw``."""
        text = self.amount_edit.text().strip()
        if not text:
            return None
        try:
            amount = Decimal(text)
        except Exception:
            return None
        if amount <= 0:
            return None
        amount_raw = int(amount * (Decimal(10) ** self._asset["decimals"]))
        return amount_raw if amount_raw > 0 else None

    def _parsed_amount_raw(self) -> int | None:
        """Strict parse — used to enable/disable the Send button.
        Returns None if the typed amount exceeds the wallet-cached
        balance. The cap protects against the obvious mistake but
        is not authoritative (cache can lag the chain by minutes)."""
        raw = self._parsed_amount_raw_unchecked()
        if raw is None or raw > self._asset["balance_raw"]:
            return None
        return raw

    # --- base hooks: validity, inputs, totals, decoded preview ----

    def _inputs_valid(self) -> bool:
        return (self._parsed_recipient() is not None
                and self._parsed_amount_raw() is not None)

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self.recipient_edit.setEnabled(enabled)
        self.amount_edit.setEnabled(enabled)
        self.max_btn.setEnabled(enabled)

    def _on_gas_ready(self) -> None:
        # Native Max needs a gas reserve, so it's gated on the first
        # estimate landing; ERC-20 Max is always enabled.
        if self._asset["is_native"]:
            self.max_btn.setEnabled(True)
            self.max_btn.setToolTip("")

    def _update_extra_totals(self, fee_wei: int) -> None:
        # Total to send (native only) = fee + amount value.
        if self.total_lbl is None:
            return
        amount_raw = self._parsed_amount_raw() or 0
        total_wei = fee_wei + amount_raw
        text = f"{wei_to_ether(total_wei)} {self.chain.symbol}"
        if self._native_price_usd is not None:
            usd = wei_to_ether(total_wei) * self._native_price_usd
            text += f"  ({_format_usd(usd)})"
        self.total_lbl.setText(text)

    def _refresh_decoded_view(self) -> None:
        """Live preview of the call. For an ERC-20 send, render the
        transfer(_to, _value) tree (same shape ``_render_decoded``
        uses for incoming dapp txs, complete with the ``# X SYMBOL``
        token-amount annotation). For native sends, show a fixed
        placeholder — there's no calldata to decode."""
        if self._asset["is_native"]:
            self.decoded_view.setPlainText(
                "(plain value transfer — no calldata)"
            )
            return
        recipient = self._parsed_recipient()
        # Unchecked variant: the preview should reflect whatever
        # the user typed, even if it exceeds our cached balance
        # snapshot (the cache lags the chain — typically after a
        # dapp swap). The Send button still gates on the strict
        # variant below in ``_update_state``.
        amount_raw = self._parsed_amount_raw_unchecked()
        # Use sentinels for missing inputs so the user sees the
        # shape of the call as they type, rather than an empty box.
        recipient_str = recipient if recipient is not None else "0x…"
        amount_str = str(amount_raw) if amount_raw is not None else "?"
        decoded = {
            "function": "transfer",
            "args": [
                {"name": "_to", "type": "address", "value": recipient_str},
                {"name": "_value", "type": "uint256", "value": amount_str},
            ],
        }
        token_context = None
        if amount_raw is not None:
            token_context = {
                "symbol": self._asset["symbol"],
                "decimals": self._asset["decimals"],
            }
        _render_decoded(
            self.decoded_view, decoded, token_context,
            known_addresses=self._known_addresses,
        )

    def _reestimate_gas(self) -> None:
        """Re-run gas estimation against the recipient the user has
        actually typed. Called debounced from recipient_edit's
        textChanged. Skips when the recipient is invalid (still
        typing) or unchanged since the last estimate (no
        duplicate calls)."""
        recipient = self._parsed_recipient()
        if recipient is None:
            return
        if recipient == self._last_estimated_recipient:
            return
        self._last_estimated_recipient = recipient
        # Use the placeholder amount (1 unit) — amount barely
        # affects the storage-cost calculus; recipient is what
        # matters. The Confirm-time finalised request still uses
        # the user's actual amount, so this only influences the
        # gas LIMIT we set.
        if self._asset["is_native"]:
            probe = SigningRequest(
                chain_id=self.chain.chain_id,
                from_addr=self._from_addr,
                to_addr=recipient,
                value_wei=1,
                data="0x",
            )
        else:
            probe = SigningRequest(
                chain_id=self.chain.chain_id,
                from_addr=self._from_addr,
                to_addr=to_checksum_address(self._asset["contract"]),
                value_wei=0,
                data=_erc20_transfer_calldata(recipient, 1),
            )
        self._kick_gas(probe)

    # --- request construction (base hook) --------------------------

    def _build_request(self) -> SigningRequest:
        """Construct the SigningRequest from the current widget
        state. Raises SignerError if recipient or amount aren't
        valid (the dialog's gas-estimate path checks recipient
        independently via ``_parsed_recipient``)."""
        recipient = self._parsed_recipient()
        amount_raw = self._parsed_amount_raw()
        if recipient is None or amount_raw is None:
            raise SignerError("Recipient or amount missing")
        if self._asset["is_native"]:
            return SigningRequest(
                chain_id=self.chain.chain_id,
                from_addr=self._from_addr,
                to_addr=recipient,
                value_wei=amount_raw,
                data="0x",
            )
        return SigningRequest(
            chain_id=self.chain.chain_id,
            from_addr=self._from_addr,
            to_addr=to_checksum_address(self._asset["contract"]),
            value_wei=0,
            data=_erc20_transfer_calldata(recipient, amount_raw),
        )
