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
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Optional

from eth_utils import is_address, to_checksum_address


def _escape_html(text: str) -> str:
    return _html.escape(text, quote=False)

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence,
    QPalette, QTextDocument, QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QSizePolicy, QSpinBox, QStyle,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from ..abi import (
    KNOWN_EVENT_NAMES, BlockscoutAbiSource, EtherscanV2AbiSource,
    RoutedAbiSource, decode_call, decode_event,
)

# The three ABI sources are duck-typed (no common base class); this Union
# is their shared static type for the params/attrs that hold whichever one
# the caller wired up — or the RoutedAbiSource built internally below.
AnyAbiSource = BlockscoutAbiSource | EtherscanV2AbiSource | RoutedAbiSource
from ..abi_cache import AbiCache
from ..contract_identity import (
    ContractIdentityCache, ContractIdentitySource, describe_identity,
)
from ..chain import EthClient, wei_to_ether
from ..signing import SignerError, SigningRequest
from ..formatting import format_datetime as _format_datetime
from ..plugin import Plugin
from ..transactions import (
    BlockscoutTransactionSource, EtherscanV2TransactionSource,
    RoutedTransactionSource, Transaction, TransactionSource,
)
from ..transactions_cache import TransactionCache, merge_txs


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


def _install_copy_menu(label: "QLabel", value: str, url: "Optional[str]") -> None:
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
                 raw_signed: Optional[str], rebroadcast: bool, parent=None):
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

    def _on_failed(self, _chain, tx_hash: str, msg: str) -> None:
        self._in_flight_hashes.discard(tx_hash)
        log.warning("PendingProbeWorker for %s failed: %s", tx_hash, msg)


log = logging.getLogger("qeth.plugin.transactions")


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
                    token_context: Optional[dict] = None,
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
              token_context: Optional[dict] = None,
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
    fetched = Signal(int, str, int, object, bool)
    # (chain_id, addr_lower, page_idx, list[Transaction], has_more)
    failed = Signal(str)

    def __init__(self, source: TransactionSource, chain, address: str,
                 page: int = 1, page_size: int = 100,
                 sent_only: bool = True, parent=None):
        super().__init__(parent)
        self.source = source
        self.chain = chain
        self.address = address
        self.page = page
        self.page_size = page_size
        self.sent_only = sent_only

    def run(self) -> None:
        viewer = self.address.lower()
        try:
            raw = self.source.list_transactions(
                self.chain, self.address,
                page=self.page, limit=self.page_size,
            )
            # A partial page means Blockscout has nothing more — used
            # by the plugin to flag the (chain, addr) as exhausted.
            has_more = len(raw) >= self.page_size
            page = raw
            if self.sent_only:
                page = [t for t in raw if t.from_addr.lower() == viewer]
            self.fetched.emit(
                self.chain.chain_id, viewer, self.page, page, has_more,
            )
        except Exception as e:
            self.failed.emit(str(e))


class TransactionsPlugin(Plugin):
    name = "Transactions"

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

    def __init__(
        self,
        source: Optional[TransactionSource] = None,
        disk_cache: Optional[TransactionCache] = None,
        abi_source: Optional[AnyAbiSource] = None,
        abi_cache: Optional[AbiCache] = None,
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
        self._identity_source: Optional[ContractIdentitySource] = (
            ContractIdentitySource(lambda: store.etherscan_api_key)
            if store is not None else None
        )
        self._identity_cache = ContractIdentityCache()
        # In-memory cache, keyed by (chain_id, address_lower). Hydrated
        # lazily from the disk cache on first ``on_account_changed`` for
        # a (chain, addr) — that's what prevents the empty → populated
        # flicker on startup.
        self._cache: dict[tuple[int, str], list[Transaction]] = {}
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
        self._next_page: dict[tuple[int, str], int] = {}
        self._exhausted: set[tuple[int, str]] = set()
        self._displayed_count: dict[tuple[int, str], int] = {}
        # Which (chain, addr) the panel's table currently shows. Used
        # to skip the show_transactions rebuild when on_activated fires
        # for the same view we already painted — Qt then preserves the
        # scrollbar and the user's scrolled-in batches stay intact.
        self._rendered_for: Optional[tuple[int, str]] = None
        # The widget is built lazily so the plugin can be instantiated
        # outside a Qt event loop (useful in pure-Python imports).
        self._panel: Optional[TransactionListPanel] = None
        # Wired in attach(); polls for receipts of broadcast txs whose
        # hashes are sitting in cache with pending=True.
        self._pending_watcher: Optional[PendingTxWatcher] = None
        # Keys with a NonceCheckWorker in flight (coalesce polls).
        self._nonce_in_flight: set[tuple[int, str]] = set()
        self._nonce_timer: Optional[QTimer] = None

    # --- Plugin contract ----------------------------------------------------

    def attach(self, host) -> None:
        super().attach(host)
        self._pending_watcher = PendingTxWatcher(self, parent=self)
        self._pending_watcher.start()
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
        dialog.show()

    def action_widgets(self):
        return self._panel.action_widgets() if self._panel is not None else []

    # --- pending-tx integration ---------------------------------------------

    def add_pending(self, tx_hash: str, req: SigningRequest, chain,
                    raw_signed: Optional[str] = None) -> None:
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
                return

    def _on_tx_dropped(self, chain, tx_hash: str) -> None:
        """PendingProbeWorker → here. The tx's nonce was consumed by a
        different tx, so this hash will never confirm. Flip the cached
        entry to the terminal ``dropped`` state (no longer pending, not
        a revert), drop the stored raw bytes, persist, and repaint the
        one row if it's on screen."""
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

    def on_account_changed(self, address: Optional[str]) -> None:
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
                # Estimate where to resume Blockscout pagination based
                # on cache size. Assumes ~50 sent txs per Blockscout
                # page (i.e. sent_ratio = 1.0); if the actual ratio is
                # lower the auto-advance walk picks up the slack. With
                # a 1213-entry cache and page_size 50 we jump straight
                # to page ~25, avoiding the ~5s walk through pages 2..24
                # that all return entries we already have.
                if key not in self._next_page and disk:
                    self._next_page[key] = max(2, (len(disk) // 50) + 1)
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
            self._panel.show_transactions(cached[:cap])
            self._rendered_for = key
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

    def _fetch_page(self, key, address: str, page: int,
                    walk_on_overlap: bool = False) -> None:
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
            self._source, chain, address, page=page,
        )
        worker.fetched.connect(
            lambda c, a, p, t, m, w=walk_on_overlap:
                self._on_page_fetched(c, a, p, t, m, walk_on_overlap=w)
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
        next_page = self._next_page.get(key, 1)
        self._fetch_page(key, addr, page=next_page, walk_on_overlap=True)

    def _on_page_fetched(self, chain_id: int, address_lower: str,
                         page_idx: int, page: list, has_more: bool,
                         walk_on_overlap: bool = False) -> None:
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
        existing = self._cache.get(key) or []
        existing_hashes = {t.hash for t in existing}
        merged = merge_txs(page, existing)
        self._cache[key] = merged
        self._disk_cache.save(chain_id, address_lower, merged)

        self._next_page[key] = max(
            self._next_page.get(key, 1), page_idx + 1,
        )
        new_rows = [t for t in page if t.hash not in existing_hashes]

        if not has_more or _is_full_history(merged):
            self._exhausted.add(key)
        elif len(new_rows) < self.INITIAL_BATCH:
            # Two cases roll up here:
            # 1. Scroll-driven fetch whose page was mostly cached
            #    overlap — walk forward to find genuinely new
            #    older data.
            # 2. Initial fetch or scroll on a receive-heavy address
            #    (e.g. an exchange wallet, or Vitalik's): the raw
            #    page is mostly received txs that the sent-only
            #    filter strips, yielding far fewer than the page
            #    size. Walk to fill a batch.
            # Stops when either has_more goes False (Blockscout is
            # done) or _is_full_history confirms the cache covers
            # every nonce; either branch sets _exhausted on the
            # next fetch.
            self._fetch_page(
                key, address_lower, page=page_idx + 1,
                walk_on_overlap=True,
            )

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

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        # Status / Nonce / Time / Hash. The Status column has an empty
        # label — the ✓/✗ glyph speaks for itself, and dropping the word
        # "Status" lets the column be tight against the left edge.
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Nonce", "Time", "Hash"])
        # Status column shows a themed icon (glyph fallback) — keep it small.
        self.table.setIconSize(QSize(16, 16))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        # Padding + hover only — see TokenListPanel comment.
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
        # ElideMiddle on the view lets the Hash column adapt: the full
        # hash is stored in the cell, and Qt truncates at paint time
        # only as much as needed to fit the column width — so the
        # rendered text grows as the user widens the column.
        # Short-text cells (Status/Nonce/Time, all ResizeToContents)
        # always fit, so this setting only ever takes effect on Hash.
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        # Scroll-to-bottom drives the load-more UX.
        self.table.verticalScrollBar().valueChanged.connect(
            self._on_scroll_change
        )
        h = self.table.horizontalHeader()
        # Status / Nonce / Time auto-fit content (no user-drag — there's
        # nothing meaningful to widen them to). Hash stretches to fill
        # the remaining space; its rendered text is the short
        # 0x1234…abcd form, so the wider cell looks padded rather than
        # full-bleed. Stretch + ResizeToContents together also mean
        # there's no empty trailing space after Hash.
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # Status
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # Nonce
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # Time
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)           # Hash
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
        self.btn_details.setToolTip("Show selected transaction's details")
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
        self.btn_explorer.setToolTip("Open selected transaction in the block explorer")
        self.btn_explorer.setEnabled(False)

        self.btn_copy_hash = QPushButton()
        self.btn_copy_hash.setIcon(QIcon.fromTheme(
            "edit-copy",
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton),
        ))
        self.btn_copy_hash.setToolTip("Copy selected transaction's hash")
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

    def _selected_tx(self) -> Optional["Transaction"]:
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
        self._chain = None
        self._viewer = None

    def show_transactions(self, txs: list[Transaction]) -> None:
        if not txs:
            self.show_empty()
            return
        self.status_lbl.setVisible(False)
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

        hash_item = QTableWidgetItem(tx.hash)
        hash_item.setFont(QFont("monospace"))
        hash_item.setToolTip(tx.hash)
        hash_item.setData(Qt.ItemDataRole.UserRole, tx)

        self.table.setItem(row, 0, status)
        self.table.setItem(row, 1, nonce)
        self.table.setItem(row, 2, time_item)
        self.table.setItem(row, 3, hash_item)

    def _tx_at(self, row: int) -> Optional[Transaction]:
        item = self.table.item(row, 3)
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
        item = self.table.itemAt(pos)
        if item is None:
            return
        tx = self._tx_at(item.row())
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
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_details:
            self.tx_details_requested.emit(tx)
        elif chosen is act_open:
            self._open_in_explorer(tx)
        elif chosen is act_copy_hash:
            QApplication.clipboard().setText(tx.hash)


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

    def __init__(self, source: Optional[ContractIdentitySource],
                 cache: ContractIdentityCache, chain_id: int, address: str,
                 my_addresses, tx_cache: Optional[TransactionCache] = None,
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
            if idy is not None:
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
    """Run a not-yet-broadcast tx through local revm simulation off the
    main thread and emit the event logs it would produce — the same
    ``{address, topics, data}`` shape ``LogsFetchWorker`` emits, so the
    two feed ``_EventsView`` interchangeably. ``ready(None)`` when pyrevm
    is absent or the simulation fails (the pane shows a placeholder)."""

    ready = Signal(object)   # list of log dicts, or None

    def __init__(self, chain, from_addr, to_addr, data, value, parent=None):
        super().__init__(parent)
        self._args = (chain, from_addr, to_addr, data, value)

    def run(self) -> None:
        from ..simulate import simulate_logs
        self.ready.emit(simulate_logs(*self._args))


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

    def set_placeholder(self, text: str) -> None:
        self.events_view.setPlainText(text)
        self.show_all_events_btn.setEnabled(False)

    def set_logs(self, logs) -> None:
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
        _abi_source: Optional[AnyAbiSource]
        _abi_cache: AbiCache
        _start_worker: Any

        def _sim_params(self) -> Any: ...

    # How long the UI waits for a simulation before giving up. The fast
    # path (eth_simulateV1) returns in well under this; the local fork
    # can take far longer on a slow / rate-limited endpoint (e.g. Arbitrum
    # on DRPC, which has no simulateV1), and a single fork attempt can't
    # be interrupted — so we stop *waiting* and let the worker finish in
    # the background (it releases the GIL; the UI stays responsive) and
    # self-evict via the host's worker set.
    SIM_TIMEOUT_MS = 35_000

    def _init_event_preview(self, tabs, *, known_addresses) -> None:
        self._sim_tabs = tabs
        self._sim_key = None
        self._sim_worker: Optional[SimulateWorker] = None
        self._sim_done = True   # no sim in flight
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
        lay.setContentsMargins(0, 6, 0, 0)
        lay.addWidget(self._events, 1)
        tabs.addTab(self._events_page, "&Events")
        tabs.currentChanged.connect(self._on_sim_tab_changed)
        # Parented to the dialog so it can't fire after the dialog dies.
        self._sim_timer = QTimer(self)  # type: ignore[arg-type]  # mixin is always mixed into a QDialog (a QObject)
        self._sim_timer.setSingleShot(True)
        self._sim_timer.timeout.connect(self._on_sim_timeout)
        # The simulation worker is tracked by the host, not this dialog, so
        # it outlives a closed dialog (these dialogs are non-modal). Detach
        # its signal on close so a late `ready` can't call into a deleted
        # dialog.
        self.finished.connect(self._detach_sim)

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
            return
        if params == self._sim_key:
            return   # already simulated (or in flight) for this exact tx
        self._sim_key = params
        from ..simulate import simulation_available
        if not simulation_available(self.chain):
            self._events.set_placeholder(self._no_simulation_text())
            return
        self._events.set_placeholder("(simulating…)")
        self._sim_done = False
        self._detach_sim()                 # drop any prior in-flight worker
        worker = SimulateWorker(self.chain, *params)
        self._sim_worker = worker
        worker.ready.connect(
            lambda logs, k=params: self._on_sim_ready(k, logs)
        )
        self._start_worker(worker)
        self._sim_timer.start(self.SIM_TIMEOUT_MS)

    def _no_simulation_text(self) -> str:
        return ("(no simulation available — this RPC has no eth_simulateV1 "
                "and the optional 'pyrevm' package isn't installed)")

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
        if logs is None:
            # The worker may have just *learned* this endpoint can't
            # simulate (no eth_simulateV1, no pyrevm) — distinguish that
            # from a genuine revert so the note is accurate.
            from ..simulate import simulation_available
            if not simulation_available(self.chain):
                self._events.set_placeholder(self._no_simulation_text())
            else:
                self._events.set_placeholder(
                    "(simulation failed — the transaction would likely "
                    "revert on-chain)"
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


def _make_identity_row(*, to_addr: Optional[str], chain,
                       identity_source: Optional[ContractIdentitySource],
                       identity_cache: ContractIdentityCache,
                       my_addresses, start_worker,
                       tx_cache: Optional[TransactionCache] = None):
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

    def kick() -> None:
        # Skip entirely when there's nothing to go on (no source/key AND
        # not already cached) — leaves the row blank rather than spinning.
        if (identity_source is None
                and identity_cache.load(chain.chain_id, to_addr) is None):
            return
        worker = ContractIdentityWorker(
            identity_source, identity_cache, chain.chain_id, to_addr,
            my_addresses, tx_cache=tx_cache)
        worker.ready.connect(_apply)
        start_worker(worker)

    return label, kick


class TransactionDetailsDialog(QDialog):
    """Modal-ish dialog showing the full tx record.

    Calldata decoding runs asynchronously: the dialog opens with a
    "(decoding…)" placeholder, kicks an AbiFetchWorker, and fills in
    the function name + arguments when the worker returns. The
    explorer link button is always available regardless of ABI state.
    """

    def __init__(self, tx: Transaction, chain, *,
                 abi_source: Optional[AnyAbiSource],
                 abi_cache: AbiCache,
                 start_worker,
                 token_info=None,
                 icon_cache=None,
                 native_price_usd=None,
                 known_addresses=None,
                 identity_source: Optional[ContractIdentitySource] = None,
                 identity_cache: Optional[ContractIdentityCache] = None,
                 tx_cache: Optional[TransactionCache] = None,
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
        self._to_icon_label: Optional[QLabel] = None
        self._to_addr_lower: Optional[str] = None

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
        tabs = QTabWidget()
        details_page = QWidget()
        details_layout = QVBoxLayout(details_page)
        details_layout.setContentsMargins(0, 8, 0, 0)
        details_layout.setSpacing(8)
        events_page = QWidget()
        events_layout = QVBoxLayout(events_page)
        events_layout.setContentsMargins(0, 8, 0, 0)
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
            self._events.set_placeholder("(loading events…)")
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

    def _link_label(self, text: str, url: Optional[str], *,
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
                       *, ref_addr: Optional[str] = None) -> Optional[str]:
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

    def _build_to_row(self, to_addr: Optional[str], from_addr: str,
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
            self._to_icon_label.setScaledContents(True)
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
                    self._to_icon_label.setPixmap(pix)
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
            self._to_icon_label.setPixmap(pix)


# --- Sign-transaction dialog + gas-suggestion worker ----------------------


_WEI_PER_GWEI = 10 ** 9
# Spinbox upper bounds. 30 M gas is the current Ethereum block limit;
# 100k gwei is ~$300/gas at $3000 ETH — well beyond any realistic fee.
_GAS_LIMIT_MAX = 30_000_000
_GWEI_MAX = 100_000.0


def _wei_to_gwei(wei: int, places: int = 4) -> float:
    """Convert wei → gwei for spinbox display. Using float for the
    spinbox's native value; we always recompute back to wei via
    Decimal at submission time so display rounding doesn't corrupt
    the on-chain value."""
    from decimal import Decimal
    return float(Decimal(wei) / Decimal(_WEI_PER_GWEI))


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
        maxPriorityFeePerGas   = max(baseFee × 0.05, node tip)
        maxFeePerGas           = max(baseFee × 2, baseFee + tip)  (≥ dapp's)
      EIP-1559 chain, baseFee == 0 (BSC-style):
        maxPriorityFeePerGas   = max(gasPrice, node tip)
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
            # tip; floor it at the node's own suggested minimum.
            priority = max(priority, max_priority_fee_wei)
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
            priority = max(ref, max_priority_fee_wei)
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

    def __init__(self, chain, req: SigningRequest, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._req = req

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
            out["nonce"] = client.get_transaction_count(
                self._req.from_addr, "pending",
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


class SignTransactionDialog(_EventPreviewMixin, QDialog):
    """Confirmation dialog for an incoming ``eth_sendTransaction``
    from the Frame RPC. Reuses the decoded-call renderer used by the
    history details dialog, and exposes editable gas / fee fields
    pre-filled by ``GasSuggestionWorker``.

    The dialog is driven asynchronously by the host: clicking
    "Confirm and sign" emits ``sign_requested`` (rather than
    closing the dialog). The host runs signing on a worker; on
    success it calls ``accept()``; on failure it pops an error
    parented to the still-open dialog and calls
    ``set_signing_in_progress(False)`` so the user can retry."""

    # User clicked Confirm and sign. The dialog stays open;
    # caller does the signing on a worker.
    sign_requested = Signal()

    def __init__(self, req: SigningRequest, chain, *,
                 abi_source: Optional[AnyAbiSource],
                 abi_cache: AbiCache,
                 start_worker,
                 token_info=None,
                 icon_cache=None,
                 native_price_usd=None,
                 known_addresses=None,
                 identity_source: Optional[ContractIdentitySource] = None,
                 identity_cache: Optional[ContractIdentityCache] = None,
                 tx_cache: Optional[TransactionCache] = None,
                 parent=None):
        super().__init__(parent)
        self.req = req
        self.chain = chain
        self._abi_source = abi_source
        self._abi_cache = abi_cache
        self._identity_source = identity_source
        self._identity_cache = (
            identity_cache if identity_cache is not None
            else ContractIdentityCache())
        self._tx_cache = tx_cache
        self._start_worker = start_worker
        self._token_info = token_info
        self._known_addresses = {a.lower() for a in (known_addresses or ())}
        # Same shape as in TransactionDetailsDialog: when the
        # recipient is a known ERC-20, _build_to_row renders an
        # icon + symbol + linked-address. Async icon updates land
        # in _on_to_icon_ready, which checks these two attrs to
        # know which row to repaint.
        self._icon_cache = icon_cache
        self._to_icon_label: Optional[QLabel] = None
        self._to_addr_lower: Optional[str] = None
        # Decimal USD-per-native price (e.g. ETH price); when set,
        # the Expected-fee line shows a "(0.015 USD)" annotation.
        # None when no cached price is available — the line just
        # omits the USD parenthetical, no other behaviour changes.
        self._native_price_usd = native_price_usd
        # Filled in by _on_gas_suggested; the Confirm button stays
        # disabled until then so the user can't submit with
        # uninitialised fee fields.
        self._gas_ready = False
        # Captured from the suggestion so _update_expected_fee can
        # combine baseFee + the (user-editable) priority tip to
        # produce the "expected" rate.
        self._base_fee_wei = 0
        # The node's own gas estimate — what the chain will actually
        # charge for a successful tx. The spinner's value (gas LIMIT)
        # is a ceiling per the project's × 1.5 / dapp-floor policy;
        # using the limit in the expected-fee math overstates by 50 %
        # or more. We hold the estimate separately and use it (or the
        # spinner if the user manually lowered it below the estimate)
        # for the live "Expected fee" line.
        self._estimated_gas = 0

        self.setWindowTitle("Sign Transaction")
        self.resize(720, 640)
        self._link_color = self.palette().color(QPalette.ColorRole.WindowText).name()

        # Tabbed shell: the existing detail widgets live on a "Details"
        # page; the mixin appends an "Events" page that previews the tx's
        # logs via local simulation. Buttons sit below the tabs, on the
        # dialog's own (root) layout, so they're shared across tabs.
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(8)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        _details_page = QWidget()
        outer = QVBoxLayout(_details_page)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.setSpacing(8)
        self._tabs.addTab(_details_page, "&Details")

        # --- header block (Network / From / To / Value) -----------------
        header = QFormLayout()
        header.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        header.setHorizontalSpacing(16)
        header.setVerticalSpacing(6)
        outer.addLayout(header)

        mono = QFont("monospace")
        self._mono_font = mono

        def _lbl(text: str, *, monospace: bool = False) -> QLabel:
            q = QLabel(text)
            q.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if monospace:
                q.setFont(mono)
            q.setWordWrap(True)
            return q

        from_cs = to_checksum_address(req.from_addr)
        # "Requested by:" — the caller's Origin (typically the dapp
        # URL). Only shown when the RPC layer captured one; local-
        # send flows leave this None and the row is hidden so the
        # user doesn't see "Requested by: None".
        if req.origin:
            header.addRow(
                "Requested by:",
                self._link_label(req.origin, req.origin),
            )
        header.addRow("Network:", _lbl(f"{chain.name} ({chain.chain_id})"))
        header.addRow(
            "From:",
            self._link_label(from_cs,
                             self._explorer_url("address", from_cs),
                             monospace=True),
        )
        header.addRow(
            "To:", self._build_to_row(req.to_addr, req.from_addr, chain, mono),
        )
        _id_label, _id_kick = _make_identity_row(
            to_addr=req.to_addr, chain=chain,
            identity_source=self._identity_source,
            identity_cache=self._identity_cache,
            my_addresses=known_addresses or [],
            start_worker=self._start_worker, tx_cache=self._tx_cache)
        if _id_label is not None and _id_kick is not None:
            header.addRow("Contract:", _id_label)
            _id_kick()
        if req.value_wei > 0:
            ether = wei_to_ether(req.value_wei)
            header.addRow(
                "Value:",
                _lbl(f"{ether} {chain.symbol}  ({req.value_wei} wei)"),
            )
        else:
            header.addRow("Value:", _lbl("0"))

        # --- decoded calldata (read-only, syntax-highlighted) ---------
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

        # --- gas / fee editors ---------------------------------------
        # The auto gas policy is sensible, so the editable controls live
        # behind a collapsed "Gas settings" expander (progressive
        # disclosure). The Expected fee summary stays visible below it.
        outer.addSpacing(4)
        self._gas_section = _CollapsibleSection("Gas settings")
        gas_form = QFormLayout()
        gas_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        gas_form.setHorizontalSpacing(16)

        self.spin_gas = QSpinBox()
        self.spin_gas.setRange(21_000, _GAS_LIMIT_MAX)
        self.spin_gas.setSingleStep(1_000)
        self.spin_gas.setSuffix(" gas")
        self.spin_gas.setEnabled(False)
        gas_form.addRow("Gas limit:", self.spin_gas)

        # Exactly one fee mode is populated; the other three stay None.
        self.spin_max_fee: Optional[QDoubleSpinBox]
        self.spin_priority: Optional[QDoubleSpinBox]
        self.spin_gas_price: Optional[QDoubleSpinBox]
        if chain.eip1559:
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
            assert self.spin_gas_price is not None
            self.spin_max_fee = None
            self.spin_priority = None
            self.spin_gas_price = QDoubleSpinBox()
            self.spin_gas_price.setRange(0.0, _GWEI_MAX)
            self.spin_gas_price.setDecimals(4)
            self.spin_gas_price.setSingleStep(0.5)
            self.spin_gas_price.setSuffix(" gwei")
            self.spin_gas_price.setEnabled(False)
            gas_form.addRow("Gas price:", self.spin_gas_price)

        self.base_fee_lbl = _lbl("(fetching…)")
        gas_form.addRow("Network base fee:", self.base_fee_lbl)
        self._gas_section.set_content_layout(gas_form)
        outer.addWidget(self._gas_section)

        # Always-visible fee summary (the number the user actually
        # decides on; the editable knobs above are the detail).
        summary = QFormLayout()
        summary.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        summary.setHorizontalSpacing(16)
        self.max_total_lbl = _lbl("—")
        summary.addRow("Expected fee:", self.max_total_lbl)
        outer.addLayout(summary)

        # --- buttons -------------------------------------------------
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel,
        )
        self.confirm_btn = self.buttons.addButton(
            "&Confirm and Sign", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        # Checkmark icon — universal "approve". Distinguishes the
        # primary action visually from Cancel, which Qt themes
        # generally render with an ×.
        _ok_icon = QIcon.fromTheme(
            "emblem-ok",
            QIcon.fromTheme(
                "dialog-ok-apply",
                QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton),
            ),
        )
        if not _ok_icon.isNull() and _ok_icon.availableSizes():
            self.confirm_btn.setIcon(_ok_icon)
        self.confirm_btn.setEnabled(False)
        self.buttons.rejected.connect(self.reject)
        # Emit a request signal rather than accept()ing here. The
        # host runs signing on a worker while the dialog stays
        # visible; on failure the host pops a popup parented to
        # this dialog and re-enables the confirm button so the
        # user can fix the device and retry without losing the
        # dialog state. The host calls dialog.accept() only on
        # successful broadcast.
        self.confirm_btn.clicked.connect(self.sign_requested.emit)

        # --- events preview tab (lazy local simulation) --------------
        self._init_event_preview(self._tabs, known_addresses=known_addresses)

        root.addWidget(self.buttons)

        # --- decode calldata in the background ----------------------
        if (req.data and req.data not in ("0x", "0X") and req.to_addr
                and self._abi_source is not None):
            self.decoded_view.setPlainText("(decoding…)")
            worker = AbiFetchWorker(
                self._abi_source, self._abi_cache,
                chain.chain_id, req.to_addr,
            )
            worker.ready.connect(self._on_abi_ready)
            self._start_worker(worker)
        elif not req.to_addr:
            self.decoded_view.setPlainText("(contract creation — no method call)")
        else:
            self.decoded_view.setPlainText("(plain value transfer — no calldata)")

        # --- kick the gas suggestion --------------------------------
        gas_worker = GasSuggestionWorker(chain, req)
        gas_worker.suggested.connect(self._on_gas_suggested)
        gas_worker.failed.connect(self._on_gas_failed)
        self._start_worker(gas_worker)

        # Recompute the "Expected fee" line whenever the user
        # touches an input that affects it: gas limit and either
        # (1559) priority tip or (legacy) gas price. spin_max_fee is
        # excluded — it caps the upper bound but doesn't change the
        # expected effective rate (base + tip).
        for sp in (self.spin_gas, self.spin_priority, self.spin_gas_price):
            if sp is not None:
                sp.valueChanged.connect(self._update_max_total)

    # --- callbacks ---------------------------------------------------

    def _sim_params(self):
        """Tx params for the events preview — fixed, straight from the
        dapp's request (the user can't edit recipient/value here)."""
        return (self.req.from_addr, self.req.to_addr,
                self.req.data or "0x", int(self.req.value_wei or 0))

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

    def _on_gas_suggested(self, info: dict) -> None:
        self._base_fee_wei = int(info.get("base_fee") or 0)
        self._estimated_gas = int(info.get("estimated_gas") or 0)
        self.spin_gas.setValue(info["gas"])
        self.spin_gas.setEnabled(True)
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            self.spin_max_fee.setValue(_wei_to_gwei(info["max_fee_per_gas"]))
            self.spin_max_fee.setEnabled(True)
            self.spin_priority.setValue(
                _wei_to_gwei(info["max_priority_fee_per_gas"])
            )
            self.spin_priority.setEnabled(True)
        else:
            assert self.spin_gas_price is not None
            self.spin_gas_price.setValue(_wei_to_gwei(info["gas_price"]))
            self.spin_gas_price.setEnabled(True)
        self.base_fee_lbl.setText(
            f"{_wei_to_gwei(self._base_fee_wei):.4f} gwei"
        )
        self._suggested_nonce = info.get("nonce")
        self._gas_ready = True
        self.confirm_btn.setEnabled(True)
        self._update_max_total()

    def _on_gas_failed(self, msg: str) -> None:
        self.base_fee_lbl.setText(f"(failed: {msg})")
        # Confirm stays disabled — without fee info we can't submit.

    def set_signing_in_progress(self, busy: bool) -> None:
        """Lock / unlock the dialog while the host is running the
        sign-and-broadcast worker. Locked: Confirm + Cancel + all
        gas spinners disabled (so the user can't fire another sign
        or close mid-flight). Unlocked: re-enable everything that
        was enabled before, so the user can fix the device and
        retry on failure."""
        self.confirm_btn.setEnabled(not busy and self._gas_ready)
        # Cancel re-enabled even mid-busy is OK — host treats it as
        # "user gave up"; but disabling it removes a foot-gun race
        # against the worker resolving.
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

    def _update_max_total(self) -> None:
        """Expected gas fee at the current settings — what the user
        is actually likely to pay, not the worst-case ceiling.

        Gas-side: a successful tx is only charged for ``gas_used``,
        which the node's ``eth_estimateGas`` predicts directly. The
        spinner shows the gas LIMIT (estimate × 1.5 by policy), so
        using it here would overstate by 50 % or more. Use the
        estimate, clamped by the spinner in case the user manually
        lowered the limit below what the chain expects to consume
        (in which case the tx will likely run out at the spinner
        value and pay that much).

        Fee-side: for EIP-1559 that's ``baseFee + priorityTip``; in
        legacy mode it's the user-set gas price.

        Doesn't include the tx's ``value`` — that's shown separately
        in the Value row above. When the dialog has a native price
        cached (loaded from the wallet cache by the host), the line
        also shows the dollar value in parentheses."""
        if not self._gas_ready:
            return
        gas = min(self._estimated_gas or self.spin_gas.value(),
                  self.spin_gas.value())
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            effective_per_gas_wei = (
                self._base_fee_wei + _gwei_to_wei(self.spin_priority.value())
            )
        else:
            assert self.spin_gas_price is not None
            effective_per_gas_wei = _gwei_to_wei(self.spin_gas_price.value())
        fee_wei = gas * effective_per_gas_wei
        text = f"≈ {wei_to_ether(fee_wei)} {self.chain.symbol}"
        if self._native_price_usd is not None:
            usd = wei_to_ether(fee_wei) * self._native_price_usd
            text += f"  ({_format_usd(usd)})"
        self.max_total_lbl.setText(text)

    # --- finalised request -------------------------------------------

    # ---- To: row helpers (mirror TransactionDetailsDialog so the
    # ---- ERC-20 token row renders identically: icon + symbol +
    # ---- linked address to /token/<addr>?a=<from>). Duplicated
    # ---- rather than inherited because the two dialogs already
    # ---- share enough other state to make a base class awkward.

    def _link_label(self, text: str, url: Optional[str], *,
                    monospace: bool = False) -> QLabel:
        if not url:
            lbl = QLabel(text)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if monospace:
                lbl.setFont(self._mono_font)
            lbl.setWordWrap(True)
            return lbl
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
                       *, ref_addr: Optional[str] = None) -> Optional[str]:
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

    def _build_to_row(self, to_addr: Optional[str], from_addr: str,
                      chain, mono: QFont) -> QWidget:
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
            self._to_icon_label.setScaledContents(True)
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
                    self._to_icon_label.setPixmap(pix)
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
            self._to_icon_label.setPixmap(pix)

    def finalised_request(self) -> SigningRequest:
        """Returns the SigningRequest with all gas / fee / nonce
        fields filled from the dialog's current state — ready to
        hand to a Signer."""
        if not self._gas_ready:
            raise SignerError("gas suggestion did not complete")
        from dataclasses import replace
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
        return replace(self.req, **kwargs)


def _erc20_transfer_calldata(recipient: str, amount_raw: int) -> str:
    """Encode an ERC-20 ``transfer(address,uint256)`` call. Returned
    as ``"0x"``-prefixed hex so it slots straight into a
    SigningRequest.data field."""
    from eth_abi import encode
    selector = bytes.fromhex("a9059cbb")
    encoded = encode(["address", "uint256"], [recipient, amount_raw])
    return "0x" + (selector + encoded).hex()


class SendTokenDialog(_EventPreviewMixin, QDialog):
    """User-driven counterpart to ``SignTransactionDialog``. Same
    overall shape (gas controls, expected fee, signing flow) but
    the recipient + amount are *editable*: the user types them
    here rather than receiving them from a dapp. On Confirm the
    dialog builds the SigningRequest and emits ``sign_requested``;
    the host runs the same worker pipeline as for RPC-driven
    requests."""

    sign_requested = Signal()

    def __init__(self, asset: dict, chain, from_addr: str, *,
                 abi_source: Optional[AnyAbiSource],
                 abi_cache: AbiCache,
                 start_worker,
                 token_info=None,
                 icon_cache=None,
                 native_price_usd=None,
                 known_addresses=None,
                 identity_source: Optional[ContractIdentitySource] = None,
                 identity_cache: Optional[ContractIdentityCache] = None,
                 tx_cache: Optional[TransactionCache] = None,
                 parent=None):
        super().__init__(parent)
        self._asset = asset
        self.chain = chain
        self._from_addr = to_checksum_address(from_addr)
        self._abi_source = abi_source
        self._abi_cache = abi_cache
        self._identity_source = identity_source
        self._identity_cache = (
            identity_cache if identity_cache is not None
            else ContractIdentityCache())
        self._tx_cache = tx_cache
        self._start_worker = start_worker
        self._token_info = token_info
        self._icon_cache = icon_cache
        self._native_price_usd = native_price_usd
        # Addresses the user owns (lowercased), so we can flag when the
        # recipient is one of their own wallets. Empty set = no hints.
        self._known_addresses = {
            a.lower() for a in (known_addresses or ())
        }
        self._known_addresses_list = list(known_addresses or ())
        # Contract-identity row for the typed recipient (filled async as
        # the user enters a valid address). Created in the form below.
        self._identity_label: Optional[QLabel] = None
        self._identity_last_addr: Optional[str] = None
        self._recipient_hint = ""  # "", "own", or "token"
        self._gas_ready = False
        self._base_fee_wei = 0
        self._estimated_gas = 0
        self._suggested_nonce = None
        # Token-icon async update; only used for ERC-20s.
        self._to_icon_label: Optional[QLabel] = None
        self._to_addr_lower: Optional[str] = None

        self.setWindowTitle(f"Send {asset['symbol']}")
        self.resize(720, 640)
        self._link_color = self.palette().color(QPalette.ColorRole.WindowText).name()

        # Tabbed shell (see SignTransactionDialog): existing widgets on a
        # "Details" page, an "Events" page that previews the tx via local
        # simulation, buttons shared below on the root layout.
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(8)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)
        _details_page = QWidget()
        outer = QVBoxLayout(_details_page)
        outer.setContentsMargins(0, 6, 0, 0)
        outer.setSpacing(8)
        self._tabs.addTab(_details_page, "&Details")

        header = QFormLayout()
        header.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        header.setHorizontalSpacing(16)
        header.setVerticalSpacing(6)
        outer.addLayout(header)

        mono = QFont("monospace")
        self._mono_font = mono

        header.addRow("Network:",
                      self._value_label(f"{chain.name} ({chain.chain_id})"))
        if asset["is_native"]:
            header.addRow("Asset:", self._value_label(asset["symbol"]))
        else:
            # Token row uses the same icon + symbol + linked-contract
            # treatment as the rest of the app for consistency. The
            # contract is what the tx's To: field is set to (not the
            # recipient); the recipient is collected via the field
            # further down.
            header.addRow("Token:", self._build_token_header_row(asset, mono))

        from_url = self._explorer_url("address", self._from_addr)
        header.addRow(
            "From:",
            self._link_label(self._from_addr, from_url, monospace=True),
        )

        # Recipient — editable. Validated only on Confirm so the user
        # can paste partial text without seeing intermediate errors.
        self.recipient_edit = QLineEdit()
        self.recipient_edit.setPlaceholderText("0x… recipient address")
        self.recipient_edit.setFont(mono)
        header.addRow("&To:", self.recipient_edit)
        # Identity of the typed recipient (when it resolves to a contract):
        # name / verified / age / deployer — filled async, see
        # _update_recipient_identity.
        self._identity_label = QLabel("")
        # Keep the wrapped label's default size policy — see _make_identity_row.
        self._identity_label.setWordWrap(True)
        self._identity_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addRow("Contract:", self._identity_label)

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

        # Decoded preview of the call about to be signed. Live-updated
        # as the user types recipient + amount. Set to Expanding so it
        # absorbs vertical space (otherwise the form layouts above and
        # below stretch to fill the dialog and leave huge gaps around
        # the "Gas settings" label).
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

        # Gas section — mirrors SignTransactionDialog. Editable controls
        # live behind a collapsed "Gas settings" expander (progressive
        # disclosure); the fee summary stays visible.
        outer.addSpacing(4)
        self._gas_section = _CollapsibleSection("Gas settings")
        gas_form = QFormLayout()
        gas_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        gas_form.setHorizontalSpacing(16)

        self.spin_gas = QSpinBox()
        self.spin_gas.setRange(21_000, _GAS_LIMIT_MAX)
        self.spin_gas.setSingleStep(1_000)
        self.spin_gas.setSuffix(" gas")
        self.spin_gas.setEnabled(False)
        gas_form.addRow("Gas limit:", self.spin_gas)

        # Exactly one fee mode is populated; the other three stay None.
        self.spin_max_fee: Optional[QDoubleSpinBox]
        self.spin_priority: Optional[QDoubleSpinBox]
        self.spin_gas_price: Optional[QDoubleSpinBox]
        if chain.eip1559:
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
            assert self.spin_gas_price is not None
            self.spin_max_fee = None
            self.spin_priority = None
            self.spin_gas_price = QDoubleSpinBox()
            self.spin_gas_price.setRange(0.0, _GWEI_MAX)
            self.spin_gas_price.setDecimals(4)
            self.spin_gas_price.setSingleStep(0.5)
            self.spin_gas_price.setSuffix(" gwei")
            self.spin_gas_price.setEnabled(False)
            gas_form.addRow("Gas price:", self.spin_gas_price)

        self.base_fee_lbl = self._value_label("(enter recipient to estimate)")
        gas_form.addRow("Network base fee:", self.base_fee_lbl)
        self._gas_section.set_content_layout(gas_form)
        outer.addWidget(self._gas_section)

        # Always-visible fee summary.
        summary = QFormLayout()
        summary.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        summary.setHorizontalSpacing(16)
        self.max_total_lbl = self._value_label("—")
        summary.addRow("Expected fee:", self.max_total_lbl)
        # When sending native ETH, the value moves out of the wallet
        # too — so a "Total leaving wallet" line that combines fee +
        # value is genuinely useful. For ERC-20s it'd just duplicate
        # the Amount field, so we only show it for native sends.
        self.total_lbl: Optional[QLabel]
        if asset["is_native"]:
            self.total_lbl = self._value_label("—")
            summary.addRow("Total to send:", self.total_lbl)
        else:
            self.total_lbl = None
        outer.addLayout(summary)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.confirm_btn = self.buttons.addButton(
            "&Send", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        # Same mail-send icon as the toolbar Send button on the
        # tokens panel — same meaning across all the places the
        # user can launch a send.
        _send_icon = QIcon.fromTheme(
            "mail-send",
            QIcon.fromTheme(
                "document-send",
                QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp),
            ),
        )
        if not _send_icon.isNull() and _send_icon.availableSizes():
            self.confirm_btn.setIcon(_send_icon)
        self.confirm_btn.setEnabled(False)
        self.buttons.rejected.connect(self.reject)
        self.confirm_btn.clicked.connect(self.sign_requested.emit)

        # --- events preview tab (lazy local simulation) --------------
        self._init_event_preview(self._tabs, known_addresses=known_addresses)

        root.addWidget(self.buttons)

        # Wire live updates: recipient/amount changes re-evaluate the
        # Confirm button enable state and (for native) the Total line.
        # Gas spinners re-render the Expected fee + Total.
        self.recipient_edit.textChanged.connect(self._update_recipient_identity)
        self.recipient_edit.textChanged.connect(self._update_state)
        self.recipient_edit.textChanged.connect(self._update_recipient_hint)
        self.amount_edit.textChanged.connect(self._update_state)
        self.amount_edit.textChanged.connect(self._update_usd_value)
        for sp in (self.spin_gas, self.spin_priority, self.spin_gas_price):
            if sp is not None:
                sp.valueChanged.connect(self._update_max_total)

        # Estimate gas only against the recipient the user has
        # actually typed — never against a placeholder. ERC-20
        # transfer cost depends heavily on the recipient's storage
        # slot (cold vs warm, zero vs non-zero balance) and a
        # placeholder is just wrong in some direction. Until a
        # valid address lands the spinners stay un-populated and
        # the Send button stays disabled. Debounced so each
        # keystroke doesn't fire an RPC call.
        from PySide6.QtCore import QTimer
        self._reestimate_timer = QTimer(self)
        self._reestimate_timer.setSingleShot(True)
        self._reestimate_timer.setInterval(400)
        self._reestimate_timer.timeout.connect(self._reestimate_gas)
        self._last_estimated_recipient: Optional[str] = None
        self.recipient_edit.textChanged.connect(
            lambda _t: self._reestimate_timer.start()
        )

        # Render an empty-state preview right away so the user can
        # see the shape of the tx they're about to build before they
        # type anything.
        self._refresh_decoded_view()

    # --- header helpers -------------------------------------------

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
        self._to_icon_label.setScaledContents(True)
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
                self._to_icon_label.setPixmap(pix)
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
            self._to_icon_label.setPixmap(pix)

    # --- shared widget helpers (copied from SignTransactionDialog) -

    def _value_label(self, text: str, *, monospace: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if monospace:
            lbl.setFont(self._mono_font)
        lbl.setWordWrap(True)
        return lbl

    def _link_label(self, text: str, url: Optional[str], *,
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
                       *, ref_addr: Optional[str] = None) -> Optional[str]:
        if not self.chain.explorer or not addr:
            return None
        base = self.chain.explorer.rstrip("/")
        if kind == "address":
            return f"{base}/address/{addr}"
        if kind == "token":
            url = f"{base}/token/{addr}"
            if ref_addr:
                url += f"?a={ref_addr}"
            return url
        return None

    # --- input handling --------------------------------------------

    def _update_recipient_identity(self) -> None:
        """Resolve the typed recipient's contract identity once it's a
        valid, changed address. The worker hits the disk cache first, so
        re-typing a seen address is instant + offline; a stale in-flight
        result (recipient changed meanwhile) is dropped in the handler."""
        label = self._identity_label
        if label is None:
            return
        text = self.recipient_edit.text().strip()
        if not is_address(text):
            label.setText("")
            label.setStyleSheet("")
            self._identity_last_addr = None
            return
        addr = text.lower()
        if addr == self._identity_last_addr:
            return
        self._identity_last_addr = addr
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

    def _asset_price_usd(self) -> Optional[Decimal]:
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

    def _parsed_recipient(self) -> Optional[str]:
        text = self.recipient_edit.text().strip()
        if not (text.startswith("0x") and len(text) == 42):
            return None
        try:
            return to_checksum_address(text)
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
        if self._token_info is not None:
            try:
                return self._token_info(self.chain.chain_id, recipient) is not None
            except Exception:
                return False
        return False

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
            self.recipient_edit.setToolTip(
                "This is a token contract — sending here usually burns "
                "the funds"
            )
        elif hint == "own":
            self.recipient_edit.setStyleSheet(
                "QLineEdit { background-color: #d7f0db; color: #14532d; }"
            )
            self.recipient_edit.setToolTip("This is one of your own wallets")
        else:
            self.recipient_edit.setStyleSheet("")
            self.recipient_edit.setToolTip("")

    def _parsed_amount_raw_unchecked(self) -> Optional[int]:
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

    def _parsed_amount_raw(self) -> Optional[int]:
        """Strict parse — used to enable/disable the Send button.
        Returns None if the typed amount exceeds the wallet-cached
        balance. The cap protects against the obvious mistake but
        is not authoritative (cache can lag the chain by minutes)."""
        raw = self._parsed_amount_raw_unchecked()
        if raw is None or raw > self._asset["balance_raw"]:
            return None
        return raw

    def _update_state(self) -> None:
        ok = (
            self._gas_ready
            and self._parsed_recipient() is not None
            and self._parsed_amount_raw() is not None
        )
        self.confirm_btn.setEnabled(ok)
        self._refresh_decoded_view()
        self._update_max_total()

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

    def _on_gas_suggested(self, info: dict) -> None:
        self._base_fee_wei = int(info.get("base_fee") or 0)
        self._estimated_gas = int(info.get("estimated_gas") or 0)
        self.spin_gas.setValue(info["gas"])
        self.spin_gas.setEnabled(True)
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            self.spin_max_fee.setValue(_wei_to_gwei(info["max_fee_per_gas"]))
            self.spin_max_fee.setEnabled(True)
            self.spin_priority.setValue(
                _wei_to_gwei(info["max_priority_fee_per_gas"])
            )
            self.spin_priority.setEnabled(True)
        else:
            assert self.spin_gas_price is not None
            self.spin_gas_price.setValue(_wei_to_gwei(info["gas_price"]))
            self.spin_gas_price.setEnabled(True)
        self.base_fee_lbl.setText(
            f"{_wei_to_gwei(self._base_fee_wei):.4f} gwei"
        )
        self._suggested_nonce = info.get("nonce")
        self._gas_ready = True
        if self._asset["is_native"]:
            self.max_btn.setEnabled(True)
            self.max_btn.setToolTip("")
        self._update_state()

    def _on_gas_failed(self, msg: str) -> None:
        self.base_fee_lbl.setText(f"(failed: {msg})")

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
        gas_worker = GasSuggestionWorker(self.chain, probe)
        gas_worker.suggested.connect(self._on_gas_suggested)
        gas_worker.failed.connect(self._on_gas_failed)
        self._start_worker(gas_worker)

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
        # Total to send (native only) = fee + amount value.
        if self.total_lbl is not None:
            amount_raw = self._parsed_amount_raw() or 0
            total_wei = fee_wei + amount_raw
            text = f"{wei_to_ether(total_wei)} {self.chain.symbol}"
            if self._native_price_usd is not None:
                usd = wei_to_ether(total_wei) * self._native_price_usd
                text += f"  ({_format_usd(usd)})"
            self.total_lbl.setText(text)

    def set_signing_in_progress(self, busy: bool) -> None:
        """Symmetric with SignTransactionDialog so the host's
        _begin_sign flow can lock both dialog types identically."""
        ok_to_enable = (not busy and self._gas_ready
                        and self._parsed_recipient() is not None
                        and self._parsed_amount_raw() is not None)
        self.confirm_btn.setEnabled(ok_to_enable)
        for btn in self.buttons.buttons():
            if btn is not self.confirm_btn:
                btn.setEnabled(not busy)
        self.recipient_edit.setEnabled(not busy)
        self.amount_edit.setEnabled(not busy)
        self.max_btn.setEnabled(not busy)
        self.spin_gas.setEnabled(not busy and self._gas_ready)
        if self.chain.eip1559:
            assert self.spin_max_fee is not None and self.spin_priority is not None
            self.spin_max_fee.setEnabled(not busy and self._gas_ready)
            self.spin_priority.setEnabled(not busy and self._gas_ready)
        else:
            assert self.spin_gas_price is not None
            self.spin_gas_price.setEnabled(not busy and self._gas_ready)

    # --- request construction --------------------------------------

    def _sim_params(self):
        """Tx params for the events preview, derived from the live
        recipient/amount. Raises SignerError (caught by the mixin) until
        both are valid, so the preview shows a 'fill these in' note."""
        req = self._build_request()
        return (req.from_addr, req.to_addr, req.data or "0x",
                int(req.value_wei or 0))

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

    def finalised_request(self) -> SigningRequest:
        if not self._gas_ready:
            raise SignerError("Gas suggestion did not complete")
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
