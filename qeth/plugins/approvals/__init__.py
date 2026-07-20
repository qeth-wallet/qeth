"""Approvals plugin — progressive full-history scan → live tree of allowances.

A ScanWorker pages the selected account's WHOLE tx history (explorer, block-
cursor walk, resumable from the tx cache), emits each fetched batch for the
plugin to merge into the tx cache on the MAIN thread (TransactionCache has no
lock), and every few pages checks the newly discovered approve() (token,
spender) pairs via multicall — so the tree fills in as the scan runs. A bottom
progress bar tracks it and can be stopped. Side effect: when the scan completes,
the account's full history is cached (the Transactions tab stops refetching).

Modify/revoke actions land in later commits; this file is the read-only scan +
tree.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, ClassVar

from eth_utils import to_checksum_address
from PySide6.QtCore import QEvent, QRect, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QProgressBar, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ... import QULONGLONG
from ...chain import EthClient
from ...formatting import format_balance, format_usd, short_addr
from ...plugin import Plugin
from ...token_metadata import TokenMetadataCache
from ...transactions import (
    ApprovalLogSource, BlockscoutTransactionSource,
    EtherscanV2TransactionSource, RoutedTransactionSource,
    fetch_contract_display_name,
)
from ..transactions import _encode_approve
from .discovery import (
    ApprovalRow, approval_pairs_from_log_rows, approval_pairs_from_logs,
    approve_pairs_in, fetch_allowances,
)
from .revoke_queue import RevokeQueue

if TYPE_CHECKING:
    from decimal import Decimal

    from ...transactions import Transaction

log = logging.getLogger(__name__)

_ROW_ROLE = Qt.ItemDataRole.UserRole          # leaf: ApprovalRow
_TOKEN_ROLE = Qt.ItemDataRole.UserRole + 1    # token node: token address (lower)
_USD_SORT_ROLE = Qt.ItemDataRole.UserRole + 2  # float: USD exposure (∞ = unlimited)
_RISK_ROLE = Qt.ItemDataRole.UserRole + 3     # token node: "$X at risk" pill text ("" = none)
_MIN_COL_W = 48                                # neither column shrinks below this
_COL_GAP = 16                                  # breathing room left of the (right-aligned) amount
_SOFT_NAME_WORKERS = 8                          # concurrent spender contract-name lookups

# The "at risk" pill: a self-consistent (bg, fg) pair — like the account-tree
# label pills — so it reads on any palette. A distinct amber/orange (not the
# accounts' yellow/blue) marks the USD value the token's approvals could drain.
_RISK_BG, _RISK_FG = "#ffe0b2", "#7a3d00"


class _RiskPillDelegate(QStyledItemDelegate):
    """Paints a right-aligned "at risk" pill on a token node's Allowance cell
    (column 1, otherwise empty on token nodes), mirroring the account-tree label
    pills. Everything else renders normally via the base delegate."""

    def paint(self, painter: QPainter, option, index) -> None:
        super().paint(painter, option, index)
        if index.column() != 1:
            return
        text = index.data(_RISK_ROLE)
        if not text:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fm = option.fontMetrics
        pad = 6
        w = fm.horizontalAdvance(str(text)) + 2 * pad
        h = fm.height() + 2
        r = option.rect
        pill = QRect(r.right() - w - 4, r.top() + (r.height() - h) // 2, w, h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_RISK_BG))
        painter.drawRoundedRect(pill, 4, 4)
        painter.setPen(QColor(_RISK_FG))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, str(text))
        painter.restore()


def _icon(names: tuple[str, ...], fallback: QStyle.StandardPixmap) -> QIcon:
    """A themed icon from the first of ``names`` the icon theme provides, with a
    built-in Qt standard icon as a last resort so something always renders
    (icon-name coverage is patchy across themes)."""
    for name in names:
        ic = QIcon.fromTheme(name)
        if not ic.isNull():
            return ic
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app.style().standardIcon(fallback)
    return QIcon()


# Action → (theme-name candidates, Qt standard-icon fallback). Shared by the
# buttons and the context menu so both carry the same glyph (house standard).
_IC_MODIFY = (("document-edit", "edit-rename", "accessories-text-editor"),
              QStyle.StandardPixmap.SP_FileDialogDetailedView)
_IC_REVOKE = (("edit-delete", "list-remove", "user-trash"),
              QStyle.StandardPixmap.SP_TrashIcon)
_IC_COPY = (("edit-copy",), QStyle.StandardPixmap.SP_FileDialogContentsView)
# Same primary name + order as the Transactions tab's explorer button, so both
# resolve to the same themed icon.
_IC_EXPLORER = (("applications-internet", "internet-web-browser", "web-browser"),
                QStyle.StandardPixmap.SP_ComputerIcon)
_IC_REFRESH = (("view-refresh", "reload"), QStyle.StandardPixmap.SP_BrowserReload)
_IC_STOP = (("media-playback-stop", "process-stop"),
            QStyle.StandardPixmap.SP_MediaStop)
_IC_SELECT_ALL = (("edit-select-all", "select-all", "checkbox"),
                  QStyle.StandardPixmap.SP_FileDialogListView)

# At/above this an allowance reads as "unlimited" — the same threshold the
# approve dialog's Unlimited toggle uses (2**255 is half the uint256 space,
# far past any honest cap, so it captures 2**256-1 and its near-max sentinels
# that would otherwise render as a 70-plus-digit number).
_UNLIMITED_MIN = 2 ** 255


_UNLIMITED = "∞"                               # near-max sentinel → infinity sign


def _format_allowance(raw: int, decimals: int) -> str:
    """Compact, symbol-free allowance for the tree's Allowance column (the
    token symbol is already the parent branch). Near-max sentinels collapse to
    the infinity sign; everything else goes through ``format_balance`` so a
    large but finite cap shows as ``1.23 × 10¹⁰`` rather than a
    horizontally-scrolling run of digits."""
    if raw >= _UNLIMITED_MIN:
        return _UNLIMITED
    from decimal import Decimal
    scaled = Decimal(raw) / (Decimal(10) ** decimals) if decimals > 0 else Decimal(raw)
    # Format via float so %g is clean in BOTH directions — a Decimal keeps its
    # trailing zeros ("9.12000 × 10¹⁰") and turns round thousands scientific
    # ("1.5 × 10³"); float gives "9.12 × 10¹⁰" and "1500". 6 sig figs fits float.
    return format_balance(float(scaled))


def _row_usd(r: ApprovalRow):
    """USD value of the allowance cap (``amount × unit price``), or None for an
    unlimited or unpriced allowance. Derived from ``allowance`` each call so it
    stays correct after a reconcile edits the amount in place."""
    if r.price_usd is None or r.allowance >= _UNLIMITED_MIN:
        return None
    from decimal import Decimal
    scaled = (Decimal(r.allowance) / (Decimal(10) ** r.decimals)
              if r.decimals > 0 else Decimal(r.allowance))
    return scaled * r.price_usd


def _token_risk_usd(r: ApprovalRow):
    """USD value of the holder's WALLET BALANCE of this token — the amount its
    approvals could actually drain (a spender's transferFrom is bounded by what
    you hold). None when unpriced, not held, or below a displayable cent."""
    if r.price_usd is None or r.token_balance <= 0:
        return None
    from decimal import Decimal
    scaled = (Decimal(r.token_balance) / (Decimal(10) ** r.decimals)
              if r.decimals > 0 else Decimal(r.token_balance))
    usd = scaled * r.price_usd
    return usd if usd >= Decimal("0.005") else None


def _risk_tag(r: ApprovalRow) -> str:
    """The token node's pill text, or "" when there's nothing at risk."""
    usd = _token_risk_usd(r)
    return f"{format_usd(usd)} at risk" if usd is not None else ""


def _row_sort_value(r: ApprovalRow) -> float:
    """Numeric exposure key for sorting: unlimited outranks everything (∞), a
    priced cap sorts by its USD value, an unpriced finite cap sorts as 0."""
    if r.allowance >= _UNLIMITED_MIN:
        return float("inf")
    usd = _row_usd(r)
    return float(usd) if usd is not None else 0.0


def _allowance_cell(r: ApprovalRow) -> str:
    """Allowance column text: just the compact token amount. (USD is not shown —
    it read as clutter — but the priced value still drives the by-exposure sort
    via ``_row_sort_value``.)"""
    return _format_allowance(r.allowance, r.decimals)


class _ApprovalItem(QTreeWidgetItem):
    """Tree item that sorts the Allowance column by a numeric USD role (stored in
    ``_USD_SORT_ROLE``) instead of its display text, and the identity column by
    casefolded text — the same trick the ENS tree uses for its expiry column."""

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, QTreeWidgetItem):
            return NotImplemented
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        if col == 1:
            a = self.data(1, _USD_SORT_ROLE)
            b = other.data(1, _USD_SORT_ROLE)
            return (a if a is not None else 0.0) < (b if b is not None else 0.0)
        return self.text(0).casefold() < other.text(0).casefold()


# --- worker ---------------------------------------------------------------

class ScanWorker(QThread):
    """Discovers approvals from the account's ERC-20 ``Approval`` event logs
    (``ApprovalLogSource``, windowed by block), then patches the recent tail a
    logs indexer may lag behind by reading only the account's newest txs.
    Interruptible between windows (the Stop button)."""

    batch_fetched = Signal(QULONGLONG, str, object)      # cid, addr_l, [Transaction] (NEW rows)
    rows_ready = Signal(QULONGLONG, str, object)         # cid, addr_l, [ApprovalRow]
    pairs_zeroed = Signal(QULONGLONG, str, object)       # cid, addr_l, {(token, spender)} read==0
    progress = Signal(QULONGLONG, str, object, object)   # cid, addr_l, pairs_seen, total(0=busy)
    scan_done = Signal(QULONGLONG, str, bool, object)    # cid, addr_l, complete, logs_head

    PAGE = 100                    # tx-tail page size
    LOG_PAGE = 1000               # explorer logs-API row cap per window
    RECENT_TX_PAGES = 5           # cap on the lag-patch tail (never the full history)
    MAX_ATTEMPTS = 3
    # Per-scan budget for the per-address contract-name lookups (the ERC-20-name
    # multicall is batched/free and never capped) — bounds cold-scan fan-out on a
    # huge account; the residual shows the bare address and is retried next scan
    # (seeding carries the wins forward). Lookups run concurrently, so this many
    # is seconds, not minutes.
    SOFT_NAME_HTTP_CAP = 120

    def __init__(self, chain, address: str, log_source, tx_source, snapshot,
                 metadata_cache, *, from_block=0, label_source=None,
                 price_source=None, known_pairs=None, known_soft_labels=None,
                 get_api_key=None, client_factory=None, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._log_source = log_source          # ApprovalLogSource (bulk discovery)
        self._tx_source = tx_source            # RoutedTransactionSource (recent-tail patch)
        self._snapshot = list(snapshot)
        self._meta = metadata_cache
        self._from_block = int(from_block or 0)   # incremental cursor (cache last_block)
        self._label_source = label_source
        self._price_source = price_source
        self._get_api_key = get_api_key           # Etherscan key for the ABI-name fallback
        self._known_pairs = set(known_pairs or ())   # cached pairs to re-check
        self._priced: dict[str, Decimal | None] = {}   # token -> unit price (memo)
        self._balances: dict[str, int] = {}            # token -> holder balance (memo)
        # Seeded from the cache's already-resolved names, so the per-scan budget
        # is spent only on STILL-UNNAMED spenders (progressive coverage across
        # scans) and a resolved name is never re-fetched or overwritten with "".
        self._soft_labels: dict[str, str] = dict(known_soft_labels or {})
        self._softname_budget = self.SOFT_NAME_HTTP_CAP
        self._client_factory = client_factory or EthClient
        self._head = 0               # chain head, for block-% progress

    # Progress is split across the scan's two real cost centres (measured
    # ~equal on a heavy account): DISCOVERY (the log-window walk) fills the
    # first _DISCOVERY_FRAC of the bar; VERIFICATION (the allowance multicalls)
    # fills the rest, batched, so the bar keeps moving through the part that
    # used to sit at ~99%.
    _DISCOVERY_FRAC = 0.4
    VERIFY_CHUNK = 150

    def run(self) -> None:
        cid = self._chain.chain_id
        addr_l = self._address.lower()
        try:
            client = self._client_factory(self._chain)
            try:
                self._head = client.get_block_number()
            except Exception:
                self._head = 0                           # unknown → block-% is busy
            self._progress(cid, addr_l, 0.0)

            # 1. Discovery: window the account's Approval logs from the
            #    incremental cursor. logs_head tracks how far the indexer has
            #    served (the cache's next resume point); progress rides the
            #    block range covered, scaled into [0, _DISCOVERY_FRAC].
            all_pairs = self._known_pairs | set(
                approve_pairs_in(self._snapshot, self._address))
            logs_head = self._from_block
            window_from = self._from_block
            while not self.isInterruptionRequested():
                rows = self._fetch_logs(window_from)
                if rows is None:                         # persistent log-source failure
                    self.scan_done.emit(cid, addr_l, False, logs_head)
                    return
                if not rows:
                    break
                pairs, max_block = approval_pairs_from_log_rows(rows, self._address)
                all_pairs |= pairs
                if max_block > logs_head:
                    logs_head = max_block
                self._progress(
                    cid, addr_l, self._DISCOVERY_FRAC * self._block_frac(logs_head))
                if len(rows) < self.LOG_PAGE:            # short window = end of range
                    break
                # Advance past the window's highest block. +1 skips re-fetching
                # the boundary block; a single block holding a full window still
                # makes progress via window_from+1.
                nxt = max_block + 1 if max_block > window_from else window_from + 1
                if nxt <= window_from:                   # no-progress guard
                    break
                window_from = nxt

            # 2. Lag patch: a logs indexer can trail chain head. Read only the
            #    account's txs NEWER than logs_head (a bounded recent window, not
            #    the full history) and union their approve pairs — catching a
            #    fresh approval the indexer hasn't surfaced yet.
            self._patch_recent_tail(cid, addr_l, logs_head, all_pairs)
            self._progress(cid, addr_l, self._DISCOVERY_FRAC)

            # 3. Verification: read allowance() for every candidate in batches,
            #    emitting rows + progress per batch so the bar climbs smoothly
            #    from _DISCOVERY_FRAC to 1.0 through the multicall reads.
            todo = sorted(all_pairs)
            total = len(todo) or 1
            checked: set[tuple[str, str]] = set()
            for i in range(0, len(todo), self.VERIFY_CHUNK):
                if self.isInterruptionRequested():
                    break
                self._emit_rows(client, cid, addr_l,
                                set(todo[i:i + self.VERIFY_CHUNK]), checked)
                done = min(i + self.VERIFY_CHUNK, len(todo))
                self._progress(cid, addr_l, self._DISCOVERY_FRAC
                               + (1.0 - self._DISCOVERY_FRAC) * done / total)

            self.scan_done.emit(
                cid, addr_l, not self.isInterruptionRequested(), logs_head)
        except Exception:
            log.debug("approvals scan failed", exc_info=True)
            self.scan_done.emit(cid, addr_l, False, self._from_block)

    def _block_frac(self, cursor: int) -> float:
        """Fraction of the [from_block, head] block range the log cursor has
        covered (0.0 when the head is unknown)."""
        head = getattr(self, "_head", 0)
        if head and head > self._from_block:
            span = head - self._from_block
            return max(0.0, min(1.0, (min(int(cursor), head) - self._from_block) / span))
        return 0.0

    def _progress(self, cid: int, addr_l: str, frac: float) -> None:
        # Determinate 0-100 once the head is known (verification is always
        # count-based, so it's determinate regardless); indeterminate only while
        # the head is unknown AND we're still in discovery.
        if getattr(self, "_head", 0) or frac > self._DISCOVERY_FRAC:
            self.progress.emit(cid, addr_l, int(100 * frac), 100)
        else:
            self.progress.emit(cid, addr_l, 0, 0)

    def _fetch_logs(self, from_block: int) -> list[dict] | None:
        import time
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                return self._log_source.fetch(
                    self._chain, self._address, from_block=from_block)
            except Exception:
                if attempt < self.MAX_ATTEMPTS - 1:
                    time.sleep(0.5 * (attempt + 1))
        return None

    def _patch_recent_tail(self, cid: int, addr_l: str, logs_head: int,
                           all_pairs: set[tuple[str, str]]) -> None:
        """Walk the account's txs newest-first only down to ``logs_head`` (the
        gap the logs indexer may lag), bounded by ``RECENT_TX_PAGES``. Best
        effort: a source failure just skips the patch — the log walk already has
        the bulk. New txs are merged into the tx cache via ``batch_fetched``."""
        seen = {t.hash for t in self._snapshot}
        cursor: int | None = None
        for _ in range(self.RECENT_TX_PAGES):
            if self.isInterruptionRequested():
                return
            raw = self._fetch_tx_page(cursor)
            if not raw:
                return
            new = [t for t in raw if t.hash not in seen]
            if new:
                seen.update(t.hash for t in new)
                self.batch_fetched.emit(cid, addr_l, new)
                all_pairs |= approve_pairs_in(new, self._address)
            raw_oldest = min(t.block_number for t in raw)
            if raw_oldest <= logs_head:                  # covered the indexer gap
                return
            if len(raw) < self.PAGE:                     # start of history
                return
            if cursor is not None and raw_oldest >= cursor:   # no-progress guard
                return
            cursor = raw_oldest

    def _fetch_tx_page(self, cursor: int | None) -> list[Transaction] | None:
        try:
            return self._tx_source.list_transactions(
                self._chain, self._address, page=1, limit=self.PAGE,
                before_block=cursor)
        except Exception:
            return None

    def _emit_rows(self, client, cid: int, addr_l: str,
                   all_pairs: set[tuple[str, str]],
                   checked: set[tuple[str, str]]) -> None:
        todo = all_pairs - checked
        if not todo:
            return
        found, read = fetch_allowances(client, self._address, todo)
        # Mark only DEFINITIVELY-read pairs as checked, so a pair whose call
        # failed this pass is retried in a later batch rather than silently
        # dropped. Pairs read as exactly zero are reported as zeroed so the
        # plugin can prune them — but a failed read never prunes a real cap.
        checked.update(read)
        zeroed = read - set(found)
        if zeroed:
            self.pairs_zeroed.emit(cid, addr_l, zeroed)
        if not found:
            return
        tokens = sorted({t for (t, _s) in found})
        missing = self._meta.missing(cid, tokens)
        if missing:
            try:
                self._meta.put_many(cid, client.multicall_erc20_metadata(missing))
            except Exception:
                log.debug("approvals metadata read failed", exc_info=True)
        spenders = sorted({s for (_t, s) in found})
        labels = self._fetch_labels(cid, spenders)
        soft = self._fetch_soft_labels(
            client, cid, [s for s in spenders if not labels.get(s)])
        self._fetch_balances(client, tokens)
        # Price finite-allowance tokens (for the cap USD sort) AND every token
        # actually HELD (for the "at risk" tag — most held tokens have an
        # unlimited cap, so pricing can't be limited to finite caps).
        finite = {t for (t, _s), v in found.items() if v < _UNLIMITED_MIN}
        held = {t for t in tokens if self._balances.get(t, 0) > 0}
        prices = self._fetch_prices(sorted(finite | held))
        rows = []
        for (token, spender), value in found.items():
            m = self._meta.get(cid, token) or {}
            rows.append(ApprovalRow(
                token=token, spender=to_checksum_address(spender), allowance=value,
                symbol=m.get("symbol") or "", name=m.get("name") or "",
                decimals=int(m.get("decimals") or 18),
                spender_label=labels.get(spender, ""),
                spender_soft_label=(
                    "" if labels.get(spender) else soft.get(spender, "")),
                price_usd=prices.get(token),
                token_balance=self._balances.get(token, 0)))
        self.rows_ready.emit(cid, addr_l, rows)

    def _fetch_balances(self, client, tokens: list[str]) -> None:
        """Read the holder's ``balanceOf`` for tokens not yet seen (memoized).
        Best-effort — a failure just leaves those tokens without an at-risk tag.
        Absent = unknown, so a reverted read stays out of the memo (not 0)."""
        need = [t for t in tokens if t not in self._balances]
        if not need:
            return
        try:
            self._balances.update(
                client.multicall_erc20_balances(need, self._address))
        except Exception:
            log.debug("approvals balance read failed", exc_info=True)

    def _fetch_prices(self, tokens: list[str]) -> dict[str, Decimal | None]:
        """USD unit prices for ``tokens`` (memoized, so streaming batches don't
        re-quote). Best-effort — an outage just leaves the caps unpriced."""
        if self._price_source is None or not tokens:
            return {}
        need = [t for t in tokens if t not in self._priced]
        if need:
            quotes: dict = {}
            try:
                quotes = self._price_source.fetch(self._chain, need)
            except Exception:
                log.debug("approvals price fetch failed", exc_info=True)
            for t in need:
                q = quotes.get(t)
                self._priced[t] = q.price_usd if q is not None else None
        return {t: self._priced.get(t) for t in tokens}

    def _fetch_labels(self, cid: int, spenders: list[str]) -> dict[str, str]:
        """Keyless public name-tags for the spender contracts ("Uniswap:
        Universal Router", …), so a leaf reads as WHO it approved, not a bare
        address. Resilient — one bad fetch just leaves the addresses bare."""
        if self._label_source is None or not spenders:
            return {}
        try:
            return self._label_source.fetch_labels(cid, spenders)
        except Exception:
            log.debug("approvals label fetch failed", exc_info=True)
            return {}

    def _fetch_soft_labels(self, client, cid: int,
                           spenders: list[str]) -> dict[str, str]:
        """Best-effort SELF-REPORTED names for spenders with no public name-tag:
        the spender's own ERC-20 name (keyless multicall), then — for the
        residual — its verified ABI contract name (Blockscout, then Etherscan,
        proxy-resolved). Memoized so streaming batches don't refetch; the
        per-address contract-name lookups are budgeted (``SOFT_NAME_HTTP_CAP``)
        so a huge account doesn't fan out unboundedly, and run CONCURRENTLY —
        they're independent explorer round-trips, and serialising them is what
        made a big account's names crawl in over minutes. Italic (lower-trust)."""
        need = [s for s in spenders if s not in self._soft_labels]
        if need:
            try:
                meta = client.multicall_erc20_metadata(need)
            except Exception:
                meta = {}
                log.debug("approvals soft-name metadata read failed", exc_info=True)
            residual = []
            for s in need:
                m = meta.get(s)
                if m:                                    # the spender IS an ERC-20
                    self._soft_labels[s] = m.get("name") or m.get("symbol") or ""
                else:
                    residual.append(s)
            # Spend the per-scan budget, resolve those concurrently; the rest are
            # left bare this scan and retried next (seeding carries the wins).
            take = residual[:max(0, self._softname_budget)]
            self._softname_budget -= len(take)
            for s in residual[len(take):]:
                self._soft_labels[s] = ""

            def _one(s: str) -> str:
                try:
                    return fetch_contract_display_name(
                        cid, s, get_api_key=self._get_api_key)
                except Exception:
                    log.debug("approvals contract-name fetch failed", exc_info=True)
                    return ""

            if take:
                with ThreadPoolExecutor(max_workers=_SOFT_NAME_WORKERS) as ex:
                    for s, name in zip(take, ex.map(_one, take)):
                        self._soft_labels[s] = name
            if len(residual) > len(take):
                log.debug("approvals: %d spender contract-name lookups skipped "
                          "(per-scan budget)", len(residual) - len(take))
        return {s: self._soft_labels.get(s, "") for s in spenders}


class ReconcileWorker(QThread):
    """Re-reads ``allowance(owner, spender)`` for a specific set of pairs after
    a modify/revoke mines, so the tree reflects the on-chain truth (a reverted
    revoke keeps its old value; a successful one reads 0). Reports a pair's value
    only when its call SUCCEEDED — a pair whose read reverted/failed is OMITTED,
    so ``_on_reconciled`` leaves that leaf as-is rather than falsely removing a
    real cap on a transient glitch."""

    reconciled = Signal(QULONGLONG, str, object)   # cid, addr_l, {(token, spender): value}

    def __init__(self, chain, owner: str, pairs, *, client_factory=None, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._owner = owner
        self._pairs = list(pairs)
        self._client_factory = client_factory or EthClient

    def run(self) -> None:
        cid = self._chain.chain_id
        addr_l = self._owner.lower()
        try:
            client = self._client_factory(self._chain)
            found, read = fetch_allowances(client, self._owner, self._pairs)
        except Exception:
            log.debug("approvals reconcile failed", exc_info=True)
            return                              # leave leaves as-is; no false removals
        # Only pairs actually read carry a value: found>0 as read, the rest of
        # `read` as 0 (definitively revoked). Pairs whose call failed are absent
        # → the plugin leaves them untouched.
        values: dict[tuple[str, str], int] = {p: 0 for p in read}
        values.update(found)
        self.reconciled.emit(cid, addr_l, values)


# --- panel ----------------------------------------------------------------

class ApprovalsPanel(QWidget):
    modify_requested = Signal(object)      # ApprovalRow   (commit 2)
    revoke_requested = Signal(object)      # [ApprovalRow] (commit 3)
    refresh_requested = Signal()
    stop_requested = Signal()
    copied = Signal(str)

    # Two columns only: the identity (token symbol / spender name-or-address)
    # and the allowance. The full spender address isn't a column — it lives in
    # the leaf tooltip + the Copy action — so the tree never needs the width a
    # 42-char address would force. The identity column middle-elides (the
    # Accounts-tab trick) and the horizontal scrollbar is switched off, so a
    # long name or address truncates gracefully instead of scrolling.
    COLS: ClassVar[list[str]] = ["Token / Spender", "Allowance"]

    def __init__(self, host=None, parent=None):
        super().__init__(parent)
        self._host = host
        self._token_items: dict[str, QTreeWidgetItem] = {}
        self._hovered: QTreeWidgetItem | None = None
        self._sort_col = 0
        self._sort_order = Qt.SortOrder.AscendingOrder    # token A→Z by default
        self._col0_frac: float | None = None              # user's split ratio
        self._syncing = False                             # re-entrancy guard
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setItemDelegate(_RiskPillDelegate(self.tree))   # "$X at risk" pill
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        # Hover / selection reveals a named spender's ACTUAL address (so it can
        # be eyeballed / checked on the explorer without losing the name label).
        self.tree.setMouseTracking(True)
        self.tree.itemEntered.connect(self._on_item_entered)
        self.tree.viewport().installEventFilter(self)
        self.tree.installEventFilter(self)           # +/−/* selection keys
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        hh = self.tree.header()
        # QTreeView defaults stretchLastSection=True, which force-stretched the
        # last (Allowance) column — stranding the amount with dead space no drag
        # could reclaim. Off, with BOTH columns Interactive + a manual split
        # (see _layout_columns / _on_section_resized): the divider drags, the two
        # columns always sum to the viewport, and Allowance starts content-wide.
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setMinimumSectionSize(_MIN_COL_W)
        hh.setSectionsClickable(True)
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(self._sort_col, self._sort_order)
        hh.sectionClicked.connect(self._on_header_clicked)
        hh.sectionResized.connect(self._on_section_resized)
        v.addWidget(self.tree, 1)

        self.status_lbl = QLabel("")
        self.status_lbl.setVisible(False)
        v.addWidget(self.status_lbl)

        # Scan progress: a bar with a small media-player-style Stop sign to its
        # right, shown only while scanning (hidden as one unit).
        self._scan_bar = QWidget()
        bar = QHBoxLayout(self._scan_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        # No "%p%" text (it inflated the bar's box and threw the button off);
        # just the bar. The bar keeps its natural height and the Stop button
        # fills to exactly that (Ignored vertical), so they line up under any
        # theme without a per-theme fixed pixel height.
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Fixed)
        bar.addWidget(self.progress, 1)
        self.btn_stop = QToolButton()
        self.btn_stop.setIcon(_icon(*_IC_STOP))
        self.btn_stop.setToolTip("Stop scanning")
        self.btn_stop.setAutoRaise(True)
        self.btn_stop.setSizePolicy(QSizePolicy.Policy.Fixed,
                                    QSizePolicy.Policy.Ignored)
        bar.addWidget(self.btn_stop)
        self._scan_bar.setVisible(False)
        v.addWidget(self._scan_bar)

        # One morphing primary button, so the action row stays narrow enough
        # that the pane (and the eliding identity column) can size down: it's
        # "Modify" for a single selected row and turns into "Revoke (N)" the
        # moment any boxes are checked. (The context menu still offers both
        # per-row — parity is kept there.)
        self._ic_modify = _icon(*_IC_MODIFY)
        self._ic_revoke = _icon(*_IC_REVOKE)
        self._action_mode = "modify"
        self.btn_action = QPushButton("&Modify")
        self.btn_action.setIcon(self._ic_modify)
        self.btn_action.clicked.connect(self._on_action_clicked)
        # Lock the width to max(Modify, Revoke) so the icon buttons to its right
        # don't jump under the cursor when it flips Modify ↔ Revoke (N).
        self.btn_action.setText("&Revoke (000)")
        self.btn_action.setIcon(self._ic_revoke)
        w_revoke = self.btn_action.sizeHint().width()
        self.btn_action.setText("&Modify")
        self.btn_action.setIcon(self._ic_modify)
        self.btn_action.setMinimumWidth(max(w_revoke, self.btn_action.sizeHint().width()))
        # Icon-only buttons render frameless (flat), like a toolbar.
        self.btn_select_all = QPushButton()
        self.btn_select_all.setIcon(_icon(*_IC_SELECT_ALL))
        self.btn_select_all.setToolTip("Check / uncheck all  ( +  −  ∗ )")
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_copy = QPushButton()
        self.btn_copy.setIcon(_icon(*_IC_COPY))
        self.btn_copy.setToolTip("Copy spender address")
        self.btn_copy.clicked.connect(self._copy_spender)
        self.btn_explorer = QPushButton()
        self.btn_explorer.setIcon(_icon(*_IC_EXPLORER))
        self.btn_explorer.setToolTip("Open spender in the block explorer")
        self.btn_explorer.clicked.connect(self._open_selected_in_explorer)
        for b in (self.btn_select_all, self.btn_copy, self.btn_explorer):
            b.setFlat(True)

        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemSelectionChanged.connect(self._refresh_reveal)
        self.tree.itemChanged.connect(self._update_buttons)
        self._update_buttons()

        if self._host is not None:
            self._host.icon_cache().icon_ready.connect(self._on_icon_ready)

    def action_widgets(self) -> list[QWidget]:
        return [self.btn_action, self.btn_select_all, self.btn_copy,
                self.btn_explorer]

    # --- scan lifecycle ---------------------------------------------------
    def begin_scan(self) -> None:
        self.clear()
        # Start determinate at 0% (not the busy animation) so the bar reads as
        # "started, nothing yet" rather than "undefined"; the worker switches it
        # to indeterminate only if the chain head is unavailable.
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._scan_bar.setVisible(True)              # bar + Stop as one unit
        self._set_status("")

    def set_progress(self, seen: int, total: int) -> None:
        if total and total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(seen, total))
        else:
            self.progress.setRange(0, 0)

    def finish_scan(self, complete: bool) -> None:
        self._scan_bar.setVisible(False)
        if self.tree.topLevelItemCount() == 0:
            self._set_status("No active approvals found"
                             if complete else "Scan stopped — no approvals found so far")
        elif not complete:
            self._set_status("Scan stopped — showing what was found so far")
        else:
            self._set_status("")

    def clear(self) -> None:
        self.tree.clear()
        self._token_items.clear()
        self._set_status("")
        self._update_buttons()

    # --- population -------------------------------------------------------
    def append_rows(self, rows: list[ApprovalRow]) -> None:
        self.tree.blockSignals(True)                 # populate without churning buttons
        for r in rows:
            self._add_row(r)
        self.tree.blockSignals(False)
        self._apply_sort()                           # recompute sums, sort, expandAll
        self._layout_columns()                       # keep the split filling the width
        self._update_buttons()
        self._refresh_reveal()

    def _all_leaves(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            out.extend(node.child(ci) for ci in range(node.childCount()))
        return out

    def checked_leaves(self) -> list[ApprovalRow]:
        """Every spender leaf the user has ticked (via its own box or its token
        node, which auto-checks the subtree)."""
        out: list[ApprovalRow] = []
        for leaf in self._all_leaves():
            if leaf.checkState(0) == Qt.CheckState.Checked:
                r = leaf.data(0, _ROW_ROLE)
                if isinstance(r, ApprovalRow):
                    out.append(r)
        return out

    def _set_all_checked(self, state: Qt.CheckState) -> None:
        self.tree.blockSignals(True)
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is not None:                      # setting the token node
                node.setCheckState(0, state)          # auto-propagates to its leaves
        self.tree.blockSignals(False)
        self._update_buttons()

    def _select_all(self) -> None:                    # + key
        self._set_all_checked(Qt.CheckState.Checked)

    def _deselect_all(self) -> None:                  # − key
        self._set_all_checked(Qt.CheckState.Unchecked)

    def _invert_selection(self) -> None:              # * key
        self.tree.blockSignals(True)
        for leaf in self._all_leaves():
            leaf.setCheckState(0, Qt.CheckState.Unchecked
                               if leaf.checkState(0) == Qt.CheckState.Checked
                               else Qt.CheckState.Checked)
        self.tree.blockSignals(False)
        self._update_buttons()

    def _toggle_select_all(self) -> None:
        """The button: check every leaf, or uncheck them all if already checked
        — so a whole account's caps can be batch-revoked in one go."""
        leaves = self._all_leaves()
        if not leaves:
            return
        if all(le.checkState(0) == Qt.CheckState.Checked for le in leaves):
            self._deselect_all()
        else:
            self._select_all()

    def _token_node(self, r: ApprovalRow) -> QTreeWidgetItem:
        node = self._token_items.get(r.token)
        if node is not None:
            return node
        node = _ApprovalItem(self.tree)
        node.setText(0, f"{r.symbol} ({short_addr(r.token)})" if r.symbol
                     else short_addr(r.token))
        node.setData(0, _TOKEN_ROLE, r.token)
        node.setToolTip(0, f"{r.name or r.symbol}\n{r.token}")
        node.setFlags(node.flags() | Qt.ItemFlag.ItemIsUserCheckable
                      | Qt.ItemFlag.ItemIsAutoTristate)
        node.setCheckState(0, Qt.CheckState.Unchecked)
        self._token_items[r.token] = node
        self._apply_token_icon(node, r.token)
        return node

    @staticmethod
    def _leaf_display(r: ApprovalRow, reveal: bool) -> tuple[str, bool]:
        """(column-0 text, italic?) for a spender leaf. ``reveal`` (hover /
        selection) always shows the plain address (regular). Otherwise, in
        order: a definitive public name-tag (regular) › a self-reported soft
        name (ITALIC — forgeable, so we flag lower confidence) › the bare
        address. ElideMiddle truncates a long value to fit."""
        if reveal:
            return r.spender, False
        if r.spender_label:
            return r.spender_label, False
        if r.spender_soft_label:
            return r.spender_soft_label, True
        return r.spender, False

    @staticmethod
    def _set_italic(leaf: QTreeWidgetItem, italic: bool) -> None:
        f = leaf.font(0)
        if f.italic() != italic:
            f.setItalic(italic)
            leaf.setFont(0, f)

    @staticmethod
    def _leaf_tooltip(r: ApprovalRow) -> str:
        if r.spender_label:
            return f"{r.spender_label}\n{r.spender}"
        if r.spender_soft_label:
            return (f"{r.spender_soft_label} — self-reported name (unverified, "
                    f"could be spoofed)\n{r.spender}")
        return r.spender

    def _add_row(self, r: ApprovalRow) -> None:
        # Upsert: a fresh scan re-emits pairs already shown (from the cache) —
        # refresh the existing leaf in place rather than duplicating it.
        node = self._token_node(r)
        self._set_token_risk(node, r)                # "$X at risk" pill on the token line
        existing = self._leaf_for(r.token, r.spender)
        if existing is not None:
            self._fill_leaf(existing, r)
            return
        leaf = _ApprovalItem(node)
        leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        leaf.setCheckState(0, Qt.CheckState.Unchecked)
        self._fill_leaf(leaf, r)

    @staticmethod
    def _set_token_risk(node: QTreeWidgetItem, r: ApprovalRow) -> None:
        # All of a token's rows carry the same balance/price, so any refreshes
        # the pill. Only set when there's a value, so an unpriced/not-held token
        # (or a cache row lacking a balance) leaves the pill off rather than
        # clearing a value a sibling row already set.
        tag = _risk_tag(r)
        if tag:
            node.setData(1, _RISK_ROLE, tag)

    def _fill_leaf(self, leaf: QTreeWidgetItem, r: ApprovalRow) -> None:
        text, italic = self._leaf_display(r, reveal=leaf is self._hovered)
        leaf.setText(0, text)
        self._set_italic(leaf, italic)
        leaf.setText(1, _allowance_cell(r))
        leaf.setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        leaf.setData(1, _USD_SORT_ROLE, _row_sort_value(r))
        leaf.setToolTip(0, self._leaf_tooltip(r))
        leaf.setData(0, _ROW_ROLE, r)

    def all_rows(self) -> list[ApprovalRow]:
        """Every displayed ApprovalRow (for persisting the cache)."""
        out: list[ApprovalRow] = []
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            for ci in range(node.childCount()):
                r = node.child(ci).data(0, _ROW_ROLE)
                if isinstance(r, ApprovalRow):
                    out.append(r)
        return out

    def prune_zeroed(self, pairs: set[tuple[str, str]]) -> None:
        """Drop leaves whose (token, spender) a fresh scan read as DEFINITIVELY
        zero (revoked). Only these are removed — a cached cap the scan couldn't
        re-read (a transient failure) is left displayed, never dropped on a
        hiccup."""
        if not pairs:
            return
        self.tree.blockSignals(True)
        for ti in range(self.tree.topLevelItemCount() - 1, -1, -1):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            for ci in range(node.childCount() - 1, -1, -1):
                leaf = node.child(ci)
                r = leaf.data(0, _ROW_ROLE)
                if (isinstance(r, ApprovalRow)
                        and (r.token.lower(), r.spender.lower()) in pairs):
                    if leaf is self._hovered:
                        self._hovered = None
                    node.removeChild(leaf)
            if node.childCount() == 0:
                self._token_items.pop(node.data(0, _TOKEN_ROLE), None)
                self.tree.takeTopLevelItem(ti)
        self.tree.blockSignals(False)
        self._update_buttons()

    # --- sorting (manual: live setSortingEnabled would re-sort on hover) ---
    def _on_header_clicked(self, col: int) -> None:
        if col == self._sort_col:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder)
        else:
            self._sort_col = col
            # Allowance defaults to highest-exposure-first; identity to A→Z.
            self._sort_order = (Qt.SortOrder.DescendingOrder if col == 1
                                else Qt.SortOrder.AscendingOrder)
        self._apply_sort()

    def _apply_sort(self) -> None:
        self._recompute_token_totals()
        self.tree.header().setSortIndicator(self._sort_col, self._sort_order)
        self.tree.sortItems(self._sort_col, self._sort_order)
        self.tree.expandAll()

    def _recompute_token_totals(self) -> None:
        """Each token node's Allowance-sort key = its USD VALUE AT RISK (wallet
        balance × price) — the amount its approvals could actually drain — so
        sorting by allowance ranks the tokens holding real money to the top.
        A summed allowance cap would just be ∞ for every token with any unlimited
        approval (nearly all), tying them uselessly. Not-held / unpriced tokens
        sort as 0 (bottom). Spender leaves keep sorting by their own cap."""
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            risk = 0.0
            for ci in range(node.childCount()):
                r = node.child(ci).data(0, _ROW_ROLE)      # all children share the token
                if isinstance(r, ApprovalRow):
                    usd = _token_risk_usd(r)
                    risk = float(usd) if usd is not None else 0.0
                    break
            node.setData(1, _USD_SORT_ROLE, risk)

    # --- hover / selection address reveal ---------------------------------
    def _on_item_entered(self, item: QTreeWidgetItem, column: int) -> None:
        self._hovered = item
        self._refresh_reveal()

    def eventFilter(self, obj, event) -> bool:
        # + / − / * = select-all / none / invert (both main-row and keypad — the
        # key code is the same, only a KeypadModifier differs, which we ignore).
        if obj is self.tree.viewport():
            et = event.type()
            if et == QEvent.Type.Leave and self._hovered is not None:
                self._hovered = None
                self._refresh_reveal()
            elif et == QEvent.Type.Resize:
                self._layout_columns()               # keep the split filling the width
        elif obj is self.tree and event.type() == QEvent.Type.KeyPress:
            handler = {
                Qt.Key.Key_Plus: self._select_all,
                Qt.Key.Key_Minus: self._deselect_all,
                Qt.Key.Key_Asterisk: self._invert_selection,
            }.get(event.key())
            if handler is not None and self._all_leaves():
                handler()
                return True                          # consume, don't let the tree see it
        return super().eventFilter(obj, event)

    # --- column split (two Interactive columns that always fill the width) --
    def _layout_columns(self, *, refit: bool = False) -> None:
        vp = self.tree.viewport().width()
        if vp <= 2 * _MIN_COL_W:
            return
        if refit or self._col0_frac is None:          # first fill: allowance fits content
            self.tree.resizeColumnToContents(1)
            c1 = self.tree.columnWidth(1) + _COL_GAP  # + a gap before the amount
            c1 = min(max(c1, _MIN_COL_W), vp - _MIN_COL_W)
            self._col0_frac = (vp - c1) / vp
        c0 = max(_MIN_COL_W, min(int(vp * self._col0_frac), vp - _MIN_COL_W))
        self._syncing = True
        self.tree.setColumnWidth(0, c0)
        self.tree.setColumnWidth(1, vp - c0)
        self._syncing = False

    def _on_section_resized(self, idx: int, _old: int, new: int) -> None:
        if self._syncing:
            return                                    # our own setColumnWidth
        vp = self.tree.viewport().width()
        if vp <= 2 * _MIN_COL_W:
            return
        c0 = new if idx == 0 else vp - new            # divider drag → both adjust
        c0 = max(_MIN_COL_W, min(c0, vp - _MIN_COL_W))
        self._col0_frac = c0 / vp
        self._syncing = True
        self.tree.setColumnWidth(0, c0)
        self.tree.setColumnWidth(1, vp - c0)
        self._syncing = False

    def _refresh_reveal(self) -> None:
        """Rewrite each named leaf's column-0 text so the hovered / selected one
        shows its (regular-weight) address and the rest show their name — a
        name-tag regular, a soft name italic. A bare-address leaf has nothing to
        toggle and is skipped."""
        hovered = self._hovered
        current = self.tree.currentItem()
        self.tree.blockSignals(True)
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            for ci in range(node.childCount()):
                leaf = node.child(ci)
                r = leaf.data(0, _ROW_ROLE)
                if not isinstance(r, ApprovalRow) or not (
                        r.spender_label or r.spender_soft_label):
                    continue
                reveal = leaf is hovered or (leaf is current and leaf.isSelected())
                text, italic = self._leaf_display(r, reveal)
                if leaf.text(0) != text:
                    leaf.setText(0, text)
                self._set_italic(leaf, italic)
        self.tree.blockSignals(False)

    def _apply_token_icon(self, node: QTreeWidgetItem, token: str) -> None:
        """Set the token node's coin icon from the shared cache; kick a
        background fetch (repaints via ``icon_ready``) when it's not cached."""
        if self._host is None:
            return
        cid = self._host.current_chain().chain_id
        cache = self._host.icon_cache()
        pix = cache.get(cid, token)
        if pix is not None:
            node.setIcon(0, QIcon(pix))
            return
        info = self._host.token_info(cid, token)
        url = getattr(info, "logo_uri", None)
        if url:
            cache.request(cid, token, url)

    def _on_icon_ready(self, cid: int, contract: str) -> None:
        node = self._token_items.get(contract.lower())
        if node is None or self._host is None:
            return
        pix = self._host.icon_cache().get(cid, contract)
        if pix is not None:
            node.setIcon(0, QIcon(pix))

    def _set_status(self, text: str) -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setVisible(bool(text))

    # --- optimistic updates (from broadcast / reconcile) ------------------
    def _leaf_for(self, token: str, spender: str) -> QTreeWidgetItem | None:
        node = self._token_items.get(token.lower())
        if node is None:
            return None
        sp = spender.lower()
        for i in range(node.childCount()):
            leaf = node.child(i)
            r = leaf.data(0, _ROW_ROLE)
            if isinstance(r, ApprovalRow) and r.spender.lower() == sp:
                return leaf
        return None

    def mark_pending(self, token: str, spender: str) -> None:
        """Show the leaf as in-flight while its modify/revoke tx confirms."""
        leaf = self._leaf_for(token, spender)
        if leaf is not None:
            leaf.setText(1, "pending…")
            leaf.setDisabled(True)

    def update_allowance(self, token: str, spender: str, value: int) -> None:
        """Re-render a leaf's allowance from an authoritative re-read."""
        leaf = self._leaf_for(token, spender)
        if leaf is None:
            return
        r = leaf.data(0, _ROW_ROLE)
        if isinstance(r, ApprovalRow):
            r.allowance = value
            leaf.setText(1, _allowance_cell(r))
            leaf.setData(1, _USD_SORT_ROLE, _row_sort_value(r))
        leaf.setDisabled(False)

    def remove_leaf(self, token: str, spender: str) -> None:
        """Drop a spender leaf (allowance now zero); drop the token node too when
        it has no spenders left."""
        node = self._token_items.get(token.lower())
        leaf = self._leaf_for(token, spender)
        if node is None or leaf is None:
            return
        node.removeChild(leaf)
        if node.childCount() == 0:
            idx = self.tree.indexOfTopLevelItem(node)
            if idx >= 0:
                self.tree.takeTopLevelItem(idx)
            self._token_items.pop(token.lower(), None)
        self._update_buttons()

    # --- selection / actions ---------------------------------------------
    def _selected_leaf(self) -> ApprovalRow | None:
        it = self.tree.currentItem()
        if it is None:
            return None
        data = it.data(0, _ROW_ROLE)
        return data if isinstance(data, ApprovalRow) else None

    def _update_buttons(self) -> None:
        has_leaf = self._selected_leaf() is not None
        n_checked = len(self.checked_leaves())
        self.btn_copy.setEnabled(has_leaf)
        self.btn_explorer.setEnabled(has_leaf and self._explorer_base() is not None)
        self.btn_select_all.setEnabled(bool(self._all_leaves()))
        # Checked boxes → batch Revoke; else a selected row → Modify; else off.
        if n_checked > 0:
            self._action_mode = "revoke"
            self.btn_action.setText(f"&Revoke ({n_checked})")
            self.btn_action.setIcon(self._ic_revoke)
            self.btn_action.setToolTip("Set the checked allowances to zero")
            self.btn_action.setEnabled(True)
        else:
            self._action_mode = "modify"
            self.btn_action.setText("&Modify")
            self.btn_action.setIcon(self._ic_modify)
            self.btn_action.setToolTip("Set a new allowance for the selected spender")
            self.btn_action.setEnabled(has_leaf)

    def _on_action_clicked(self) -> None:
        if self._action_mode == "revoke":
            rows = self.checked_leaves()
            if rows:
                self.revoke_requested.emit(rows)
        else:
            r = self._selected_leaf()
            if r is not None:
                self.modify_requested.emit(r)

    def _copy_spender(self) -> None:
        r = self._selected_leaf()
        if r is not None:
            QApplication.clipboard().setText(r.spender)
            self.copied.emit(r.spender)

    def _explorer_base(self) -> str | None:
        if self._host is None:
            return None
        base = getattr(self._host.current_chain(), "explorer", "") or ""
        return base.rstrip("/") or None

    def _open_in_explorer(self, address: str) -> None:
        base = self._explorer_base()
        if base and address:
            QDesktopServices.openUrl(QUrl(f"{base}/address/{address}"))

    def _open_selected_in_explorer(self) -> None:
        r = self._selected_leaf()
        if r is not None:
            self._open_in_explorer(r.spender)

    def _on_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        r = item.data(0, _ROW_ROLE) if item is not None else None
        if isinstance(r, ApprovalRow):
            self._open_in_explorer(r.spender)

    def _on_context_menu(self, pos) -> None:
        it = self.tree.itemAt(pos)
        r = it.data(0, _ROW_ROLE) if it is not None else None
        r = r if isinstance(r, ApprovalRow) else None
        token = it.data(0, _TOKEN_ROLE) if it is not None else None
        menu = QMenu(self)
        act_modify = menu.addAction(_icon(*_IC_MODIFY), "Modify Approval…")
        act_modify.setEnabled(r is not None)
        act_revoke = menu.addAction(_icon(*_IC_REVOKE), "Revoke Approval")
        act_revoke.setEnabled(r is not None)
        menu.addSeparator()
        act_copy_sp = menu.addAction(_icon(*_IC_COPY), "Copy spender address")
        act_copy_sp.setEnabled(r is not None)
        act_copy_tok = menu.addAction(_icon(*_IC_COPY), "Copy token address")
        act_copy_tok.setEnabled(r is not None or token is not None)
        act_explorer = menu.addAction(_icon(*_IC_EXPLORER), "Open spender in explorer")
        act_explorer.setEnabled(r is not None and self._explorer_base() is not None)
        menu.addSeparator()
        act_refresh = menu.addAction(_icon(*_IC_REFRESH), "Rescan history")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_modify and r is not None:
            self.modify_requested.emit(r)
        elif chosen is act_revoke and r is not None:
            self.revoke_requested.emit([r])
        elif chosen is act_explorer and r is not None:
            self._open_in_explorer(r.spender)
        elif chosen is act_copy_sp and r is not None:
            QApplication.clipboard().setText(r.spender)
            self.copied.emit(r.spender)
        elif chosen is act_copy_tok:
            addr = r.token if r is not None else token
            if addr:
                QApplication.clipboard().setText(addr)
                self.copied.emit(addr)
        elif chosen is act_refresh:
            self.refresh_requested.emit()


# --- plugin ---------------------------------------------------------------

class ApprovalsPlugin(Plugin):
    name = "Approvals"

    def __init__(self, store):
        super().__init__()
        self._store = store
        self._panel: ApprovalsPanel | None = None
        self._loaded_for: tuple[int, str] | None = None
        self._epoch = 0
        self._metadata = TokenMetadataCache()
        # Approval-event logs are the discovery source of record (catches
        # permit/internal-call approvals, and costs a few windowed requests on a
        # >10k-tx account instead of paging its whole history). The tx source
        # only patches the recent tail a logs indexer may lag behind.
        self._log_source = ApprovalLogSource(
            lambda: getattr(store, "etherscan_api_key", None))
        self._source = RoutedTransactionSource(
            EtherscanV2TransactionSource(
                lambda: getattr(store, "etherscan_api_key", None)),
            BlockscoutTransactionSource())
        from ..transactions.contract_identity import ContractIdentitySource
        # Keyless: spender name-tags come from the free Blockscout metadata
        # service (fetch_labels needs no Etherscan key).
        self._label_source = ContractIdentitySource(lambda: None)
        from ...pricing import (
            ChainedPriceSource, DefiLlamaPrices, OnChainVaultPrices,
        )
        # USD valuation of finite allowances — DefiLlama (keyless) first, then
        # on-chain for vault/LP shares it can't quote (mirrors the Tokens tab).
        self._price_source = ChainedPriceSource(DefiLlamaPrices(), OnChainVaultPrices())
        self._workers: set[QThread] = set()
        self._scan: ScanWorker | None = None
        self._queue: RevokeQueue | None = None
        self._reconcile_pending: set[tuple[str, str]] = set()
        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.setSingleShot(True)
        self._reconcile_timer.setInterval(600)     # debounce bursts of confirms
        self._reconcile_timer.timeout.connect(self._run_reconcile)
        from ...transactions_cache import TransactionCache
        self._disk_cache = TransactionCache()
        from .cache import ApprovalsCache
        self._cache = ApprovalsCache()               # persisted allowances + last block
        self._last_block = 0
        self._scan_pairs: set[tuple[str, str]] = set()   # pairs a live scan re-confirmed
        self._scan_zeroed: set[tuple[str, str]] = set()  # pairs a live scan read as zero

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = ApprovalsPanel(host=self.host)
            self._panel.refresh_requested.connect(lambda: self._kick(force=True))
            self._panel.stop_requested.connect(self._stop_scan)
            self._panel.copied.connect(self._on_copied)
            self._panel.modify_requested.connect(self._on_modify)
            self._panel.revoke_requested.connect(self._on_revoke)
        return self._panel

    def action_widgets(self) -> list[QWidget]:
        return self._panel.action_widgets() if self._panel is not None else []

    def focus_widget(self) -> QWidget | None:
        return self._panel.tree if self._panel is not None else None

    def attach(self, host) -> None:
        super().attach(host)
        # Refresh when ANY approve confirms (Send, a dapp, our own dialog) — not
        # just approvals-initiated modify/revoke — so a cap changed elsewhere
        # doesn't sit stale in the list.
        tx = host.plugin("transactions")
        if tx is not None and hasattr(tx, "tx_confirmed"):
            tx.tx_confirmed.connect(self._on_tx_confirmed)

    def _on_tx_confirmed(self, chain, tx_hash, receipt) -> None:
        if self.host is None or self._panel is None:
            return
        view = self._current_view()
        if view is None or view[0] != getattr(chain, "chain_id", None):
            return
        owner = self.host.selected_address
        logs = receipt.get("logs") if hasattr(receipt, "get") else None
        pairs = approval_pairs_from_logs(logs, owner) if owner else set()
        if not pairs:
            return
        # An already-shown cap just needs a targeted re-read; a brand-new one
        # needs a scan to discover it (build the row with metadata/labels/price).
        if any(self._panel._leaf_for(t, s) is None for (t, s) in pairs):
            self._kick(force=True)
        else:
            for pair in pairs:
                self._schedule_reconcile(pair)

    def on_account_changed(self, address: str | None) -> None:
        self._invalidate()
        if self._panel is not None:
            self._panel.clear()
            if self._panel.isVisible():
                self._kick()

    def on_chain_changed(self) -> None:
        self.on_account_changed(self.host.selected_address if self.host else None)

    def on_activated(self) -> None:
        # If the loaded-for guard skips a full re-scan, still re-read the shown
        # caps' allowances — so a change made while we were away (or on another
        # tab) is reflected on return, not left stale behind the guard.
        if not self._kick():
            self._refresh_displayed()

    def _refresh_displayed(self) -> None:
        if self._panel is None:
            return
        for r in self._panel.all_rows():
            self._schedule_reconcile((r.token.lower(), r.spender.lower()))

    def shutdown(self) -> None:
        self._invalidate()

    # --- internals --------------------------------------------------------
    def _current_view(self) -> tuple[int, str] | None:
        if self.host is None:
            return None
        addr = self.host.selected_address
        if not addr:
            return None
        return (self.host.current_chain().chain_id, addr.lower())

    def _invalidate(self) -> None:
        self._epoch += 1
        self._loaded_for = None
        self._stop_scan()
        self._abort_queue()
        self._reconcile_pending.clear()
        self._reconcile_timer.stop()

    def _stop_scan(self) -> None:
        # The host deleteLater()s a finished worker (ui.py start_worker), so a
        # naturally-completed scan leaves self._scan a stale wrapper whose C++
        # object is gone — requestInterruption() on it raised "already deleted"
        # and aborted the account switch. Guard with isValid, then forget it.
        from shiboken6 import isValid
        scan, self._scan = self._scan, None
        if scan is not None and isValid(scan):
            scan.requestInterruption()

    def _forget_scan(self, worker: QThread) -> None:
        if self._scan is worker:
            self._scan = None

    def _abort_queue(self) -> None:
        if self._queue is not None:
            self._queue.abort()

    # --- modify / revoke --------------------------------------------------
    def _on_modify(self, row: ApprovalRow) -> None:
        self._open_approve(row, f"Modify {row.symbol or 'token'} approval")

    def _on_revoke(self, rows) -> None:
        rows = [r for r in rows if isinstance(r, ApprovalRow)]
        if not rows:
            return
        if len(rows) == 1:
            self._open_approve(rows[0], f"Revoke {rows[0].symbol or 'token'}")
            return
        self._start_revoke_queue(rows)

    def _approve_request(self, row: ApprovalRow):
        """``(SigningRequest, chain)`` for ``approve(spender, 0)`` from the
        selected owner, or ``None`` when there's no host/owner."""
        if self.host is None:
            return None
        owner = self.host.selected_address
        if not owner:
            return None
        from eth_utils import to_checksum_address

        from ...signing import SigningRequest
        chain = self.host.current_chain()
        req = SigningRequest(
            chain_id=chain.chain_id,
            from_addr=to_checksum_address(owner),
            to_addr=to_checksum_address(row.token),
            value_wei=0,
            data=_encode_approve(to_checksum_address(row.spender), 0),
        )
        return req, chain

    def _open_approve(self, row: ApprovalRow, label: str) -> None:
        built = self._approve_request(row)
        if built is None or self.host is None:
            return
        req, chain = built
        pair = (row.token.lower(), row.spender.lower())
        self.host.request_transaction(
            req, chain, label=label,
            on_broadcast=lambda h, p=pair: self._on_action_broadcast(p),
            on_confirmed=lambda rc, p=pair: self._schedule_reconcile(p))

    def _start_revoke_queue(self, rows: list[ApprovalRow]) -> None:
        self._abort_queue()                            # one batch at a time
        queue = RevokeQueue(rows, self._open_revoke_dialog, parent=self)
        queue.row_broadcast.connect(
            lambda row, h: self._on_action_broadcast(
                (row.token.lower(), row.spender.lower())))
        queue.row_confirmed.connect(
            lambda row, rc: self._schedule_reconcile(
                (row.token.lower(), row.spender.lower())))
        queue.finished.connect(self._on_queue_finished)
        self._queue = queue
        queue.start()

    def _open_revoke_dialog(self, row, index, total,
                            on_broadcast, on_confirmed, on_cancel) -> None:
        built = self._approve_request(row)
        if built is None or self.host is None:
            on_cancel()
            return
        req, chain = built
        label = f"Revoke {row.symbol or 'token'} ({index + 1}/{total})"
        self.host.request_transaction(
            req, chain, label=label,
            on_broadcast=on_broadcast, on_confirmed=on_confirmed,
            on_cancel=on_cancel)

    def _on_queue_finished(self, completed_all: bool) -> None:
        self._queue = None
        if self.host is not None and not completed_all:
            self.host.status_message("Revoke batch cancelled")

    def _on_action_broadcast(self, pair: tuple[str, str]) -> None:
        if self._panel is not None:
            self._panel.mark_pending(*pair)

    def _schedule_reconcile(self, pair: tuple[str, str]) -> None:
        self._reconcile_pending.add(pair)
        self._reconcile_timer.start()

    def _run_reconcile(self) -> None:
        pairs = list(self._reconcile_pending)
        self._reconcile_pending.clear()
        if not pairs or self.host is None:
            return
        owner = self.host.selected_address
        if not owner:
            return
        chain = self.host.current_chain()
        worker = ReconcileWorker(chain, owner, pairs)
        worker.reconciled.connect(self._on_reconciled)
        self._start(worker)

    def _on_reconciled(self, chain_id, addr_l, values) -> None:
        # A targeted re-read applies as long as the account/chain still matches —
        # NOT gated on the scan epoch, which a _kick between scheduling and
        # completing would bump (dropping a just-confirmed modify's update).
        if self._panel is None or self._current_view() != (chain_id, addr_l):
            return
        for (token, spender), value in values.items():
            if value > 0:
                self._panel.update_allowance(token, spender, int(value))
            else:
                self._panel.remove_leaf(token, spender)
                self._scan_pairs.discard((token.lower(), spender.lower()))
        self._persist()                              # keep the cache in step with a revoke

    def _kick(self, *, force: bool = False) -> bool:
        """Start a (re-)scan. Returns True if a scan was started, False when the
        loaded-for guard short-circuited it (so a caller can re-read caps another
        way)."""
        view = self._current_view()
        if view is None or self._panel is None or self.host is None:
            return False
        if not force and self._loaded_for == view:
            return False
        addr = self.host.selected_address
        if not addr:
            return False
        self._loaded_for = view
        self._epoch += 1
        epoch = self._epoch
        chain = self.host.current_chain()
        self._scan_pairs = set()
        self._scan_zeroed = set()
        # Instant paint from the cache; only show the progress bar on a cold
        # scan. A warm scan refreshes the tail silently under the shown rows.
        self._panel.clear()
        cached = self._cache.load(chain.chain_id, addr)
        known_pairs: set[tuple[str, str]] = set()
        known_soft: dict[str, str] = {}
        if cached and cached[0]:
            rows, self._last_block = cached
            self._panel.append_rows(rows)
            known_pairs = {(r.token.lower(), r.spender.lower()) for r in rows}
            # Carry already-resolved soft names forward so the worker doesn't
            # re-fetch them (or lose them when a later scan's budget runs out).
            known_soft = {r.spender.lower(): r.spender_soft_label
                          for r in rows if r.spender_soft_label}
        else:
            self._last_block = 0
            self._panel.begin_scan()
        snapshot = self._disk_cache.load(chain.chain_id, addr) or []
        worker = ScanWorker(chain, addr, self._log_source, self._source,
                            snapshot, self._metadata,
                            from_block=self._last_block,
                            label_source=self._label_source,
                            price_source=self._price_source,
                            known_pairs=known_pairs, known_soft_labels=known_soft,
                            get_api_key=lambda: getattr(
                                self._store, "etherscan_api_key", None))
        worker.batch_fetched.connect(self._on_batch)
        worker.rows_ready.connect(
            lambda c, a, rows, e=epoch: self._on_rows(c, a, rows, e))
        worker.pairs_zeroed.connect(
            lambda c, a, pairs, e=epoch: self._on_zeroed(c, a, pairs, e))
        worker.progress.connect(
            lambda c, a, s, t, e=epoch: self._on_progress(c, a, s, t, e))
        worker.scan_done.connect(
            lambda c, a, ok, head, e=epoch: self._on_done(c, a, ok, head, e))
        # Drop our reference the moment it finishes, before the host's
        # deleteLater() runs — so _stop_scan never reaches a dead wrapper.
        worker.finished.connect(lambda w=worker: self._forget_scan(w))
        self._scan = worker
        self._start(worker)
        return True

    def _start(self, worker: QThread) -> None:
        if self.host is not None and hasattr(self.host, "start_worker"):
            self.host.start_worker(worker)
            return
        self._workers.add(worker)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        worker.start()

    def _fresh(self, chain_id: int, addr_l: str, epoch: int) -> bool:
        return epoch == self._epoch and self._current_view() == (chain_id, addr_l)

    def _on_batch(self, chain_id, addr_l, txs) -> None:
        # MAIN thread: the only writer to the tx cache (no lock on it).
        from ...transactions_cache import merge_txs
        existing = self._disk_cache.load(chain_id, addr_l) or []
        merged = merge_txs(list(txs), existing)
        if merged != existing:
            self._disk_cache.save(chain_id, addr_l, merged)

    def _on_rows(self, chain_id, addr_l, rows, epoch) -> None:
        if self._panel is not None and self._fresh(chain_id, addr_l, epoch):
            self._panel.append_rows(rows)
            self._scan_pairs |= {(r.token.lower(), r.spender.lower()) for r in rows}

    def _on_zeroed(self, chain_id, addr_l, pairs, epoch) -> None:
        # Pairs the scan read as DEFINITIVELY zero — the only ones a completed
        # scan may prune. A pair whose read merely failed never lands here, so a
        # transient glitch can't drop a real cap.
        if self._fresh(chain_id, addr_l, epoch):
            self._scan_zeroed |= {(t.lower(), s.lower()) for (t, s) in pairs}

    def _on_progress(self, chain_id, addr_l, seen, total, epoch) -> None:
        if self._panel is not None and self._fresh(chain_id, addr_l, epoch):
            self._panel.set_progress(int(seen), int(total or 0))

    def _on_done(self, chain_id, addr_l, complete, logs_head, epoch) -> None:
        if self._panel is None or not self._fresh(chain_id, addr_l, epoch):
            return
        if complete:
            # Drop only caps the fresh scan definitively read as zero (revoked
            # elsewhere) — never a cap it just couldn't re-read — then persist
            # the reconciled state + how far the logs indexer served (the
            # incremental resume point).
            self._panel.prune_zeroed(self._scan_zeroed)
            self._persist(logs_head)
        else:
            # Stopped/failed before finishing — don't treat the view as loaded,
            # so the next activation resumes. Still advance the cursor to how far
            # the logs got, so the resume doesn't re-window from 0.
            if logs_head and int(logs_head) > self._last_block:
                self._last_block = int(logs_head)
            self._loaded_for = None
        self._panel.finish_scan(bool(complete))

    def _persist(self, logs_head=None) -> None:
        view = self._current_view()
        if view is None or self._panel is None:
            return
        cid, addr = view
        # last_block is the Approval-log cursor (highest block the indexer
        # served this scan), NOT the tx cache head. A reconcile/revoke persist
        # passes no head → keep the existing cursor.
        if logs_head is not None and int(logs_head) > self._last_block:
            self._last_block = int(logs_head)
        self._cache.save(cid, addr, self._panel.all_rows(), self._last_block)

    def _on_copied(self, text: str) -> None:
        if self.host is not None:
            self.host.status_message(f"Copied {text}")
