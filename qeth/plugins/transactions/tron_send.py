"""``TronSendDialog`` — send TRX or a TRC-20 token on Tron.

The EVM composer's machinery (nonce, gas price, simulation) doesn't apply to
Tron, so this is its own small dialog: recipient (a ``T…`` address), amount,
and the fee as Tron charges it — the TRX a transaction will BURN for the
bandwidth / energy its sender's staked + free resources don't cover (plus
activating a brand-new recipient), estimated live by ``TronFeeWorker``
(``qeth.tron.fees``). A TRC-20 transfer also shows its ``fee_limit``, the most
it may burn.

Confirm emits ``send_requested(contract, fee_limit)``; the host signs and
broadcasts through ``TronSignAndBroadcastWorker``, which assembles the
transaction (fresh TaPoS) right before signing.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QStringListModel, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCompleter, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from ...address import codec_for
from ...dialog import Dialog, address_field_min_width
from ...formatting import format_balance
from ...tron.client import TronClient, TronError
from ...tron.fees import TronFee, estimate_fee, trc20_transfer_data
from ...tron.tx import Contract, TransferContract, TriggerSmartContract

# Debounce between an edit and the fee estimate it triggers.
_ESTIMATE_DELAY_MS = 400


def _amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def _trx(sun: int, decimals: int = 6) -> str:
    """A fee in TRX, exact (sun has 6 decimals, so nothing is rounded away —
    a fee cap should read as what it is)."""
    return f"{_amount(sun, decimals).normalize():f}"


class TronFeeWorker(QThread):
    """Estimate ``contract``'s fee and read the sender's TRX balance (the fee
    is always burned in TRX, whichever asset is sent). ``estimated`` carries
    ``(seq, TronFee, trx_balance_sun)``; ``failed`` ``(seq, reason)`` — the
    seq lets the dialog drop answers to an edit it has since superseded."""

    estimated = Signal(int, object, object)
    failed = Signal(int, str)

    def __init__(self, chain, contract: Contract, seq: int, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._contract = contract
        self._seq = seq

    def run(self) -> None:
        try:
            client = TronClient(self._chain)
            fee = estimate_fee(client, self._contract)
            balance = client.get_balance(self._contract.owner)
        except TronError as e:
            self.failed.emit(self._seq, str(e))
            return
        except Exception as e:
            self.failed.emit(self._seq, f"Couldn't estimate the fee: {e}")
            return
        self.estimated.emit(self._seq, fee, balance)


class TronSendDialog(Dialog):
    send_requested = Signal(object, object)    # (Contract, fee_limit sun)

    def __init__(self, asset: dict, chain, from_addr: str, *, start_worker,
                 address_book: list[tuple[str, str]] | None = None,
                 label: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chain = chain
        self._asset = asset
        self._from = from_addr
        self._start_worker = start_worker
        self._codec = codec_for(chain)
        self._is_native = bool(asset.get("is_native"))
        self._decimals = int(asset.get("decimals") or 0)
        self._balance_raw = int(asset.get("balance_raw") or 0)
        self._symbol = str(asset.get("symbol") or "?")
        self._fee: TronFee | None = None
        self._trx_balance: int | None = None
        self._seq = 0
        self._max_mode = False
        self._signing = False
        self.setWindowTitle(f"Send {self._symbol} on {chain.name}")

        v = QVBoxLayout(self)
        form = QFormLayout()
        mono = QFont("monospace")
        from_lbl = QLabel(self._codec.display(from_addr)
                          + (f"  ({label})" if label else ""))
        from_lbl.setFont(mono)
        from_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("From:", from_lbl)
        form.addRow("Asset:", QLabel(
            f"{self._symbol} — balance "
            f"{format_balance(_amount(self._balance_raw, self._decimals))}"))

        self.to_edit = QLineEdit()
        self.to_edit.setFont(mono)
        self.to_edit.setPlaceholderText(f"{self._codec.placeholder} address")
        self.to_edit.setMinimumWidth(address_field_min_width(self))
        book = [(self._codec.display(a), lab) for a, lab in (address_book or [])
                if a.lower() != from_addr.lower()]
        self._book_labels = {shown: lab for shown, lab in book}
        if book:
            comp = QCompleter(QStringListModel([shown for shown, _ in book], self), self)
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseSensitive)
            self.to_edit.setCompleter(comp)
        form.addRow("&To:", self.to_edit)

        amount_row = QHBoxLayout()
        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText("0.0")
        self.max_btn = QPushButton("&Max")
        self.max_btn.setAutoDefault(False)
        amount_row.addWidget(self.amount_edit, 1)
        amount_row.addWidget(self.max_btn)
        # A row built from a layout gets no buddy, so its label's & mnemonic
        # would show literally — make the label and buddy it by hand.
        amount_lbl = QLabel("&Amount:")
        amount_lbl.setBuddy(self.amount_edit)
        form.addRow(amount_lbl, amount_row)
        v.addLayout(form)

        # The fee paragraph: what this burns, and (contract calls) the cap.
        self.fee_lbl = QLabel("")
        self.fee_lbl.setWordWrap(True)
        self.fee_lbl.setVisible(False)
        v.addWidget(self.fee_lbl)
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setVisible(False)
        v.addWidget(self.status_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.send_btn = buttons.addButton("&Send", QDialogButtonBox.ButtonRole.AcceptRole)
        self.send_btn.setEnabled(False)
        buttons.rejected.connect(self.reject)
        self.send_btn.clicked.connect(self._on_send)
        v.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_ESTIMATE_DELAY_MS)
        self._timer.timeout.connect(self._kick_estimate)
        self.to_edit.textChanged.connect(self._on_edited)
        self.amount_edit.textEdited.connect(self._on_amount_typed)
        self.max_btn.clicked.connect(self._on_max)

    # --- input ------------------------------------------------------------

    def recipient(self) -> str | None:
        return self._codec.parse(self.to_edit.text())

    def amount_raw(self) -> int | None:
        """The typed amount in the asset's smallest unit, or None if it
        isn't a positive number with at most the asset's decimals."""
        text = self.amount_edit.text().strip().replace(",", "")
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
        raw = value * (Decimal(10) ** self._decimals)
        if value <= 0 or raw != raw.to_integral_value():
            return None
        return int(raw)

    def contract(self) -> Contract | None:
        """What Send would sign, from the current fields — or None."""
        to, amount = self.recipient(), self.amount_raw()
        if to is None or amount is None:
            return None
        if self._is_native:
            return TransferContract(self._from, to, amount)
        token = str(self._asset["contract"])
        return TriggerSmartContract(self._from, token, trc20_transfer_data(to, amount))

    def _on_amount_typed(self, _text: str) -> None:
        self._max_mode = False
        self._on_edited()

    def _on_max(self) -> None:
        """All of the asset. For TRX, less what this transfer burns — once
        the estimate lands (``_on_estimated`` re-applies it)."""
        self._max_mode = True
        self._apply_max()
        self._on_edited()

    def _apply_max(self) -> None:
        raw = self._balance_raw
        if self._is_native and self._fee is not None:
            raw = max(0, raw - self._fee.total_burn)
        self.amount_edit.setText(str(_amount(raw, self._decimals).normalize()
                                     if raw else "0"))

    def _on_edited(self, *_args) -> None:
        self._fee = None
        self.send_btn.setEnabled(False)
        self._set_status("")
        text = self.to_edit.text().strip()
        if text and self.recipient() is None:
            self._set_status(f"That isn't a valid {self.chain.name} address.")
        if self.contract() is None:
            self._set_fee("")
            self._timer.stop()
            return
        self._set_fee("Estimating the network fee…")
        self._timer.start()

    # --- the fee estimate ---------------------------------------------------

    def _kick_estimate(self) -> None:
        contract = self.contract()
        if contract is None:
            return
        self._seq += 1
        worker = TronFeeWorker(self.chain, contract, self._seq)
        worker.estimated.connect(self._on_estimated)
        worker.failed.connect(self._on_estimate_failed)
        self._start_worker(worker)

    def _on_estimated(self, seq: int, fee: TronFee, trx_balance: int) -> None:
        if seq != self._seq:
            return
        self._fee = fee
        self._trx_balance = trx_balance
        if self._max_mode and self._is_native:
            self._apply_max()      # textChanged isn't textEdited: no re-kick
        # What gets burned, item by item (what staked / free resources don't
        # cover): "burns ≈ 1.1 TRX — 1 TRX to activate …, 0.1 TRX bandwidth".
        items = []
        if fee.activation:
            items.append(f"{_trx(fee.activation)} TRX to activate the "
                         "recipient's new account")
        if fee.bandwidth_burn:
            items.append(f"{_trx(fee.bandwidth_burn)} TRX for bandwidth")
        if fee.energy_burn:
            items.append(f"{_trx(fee.energy_burn)} TRX for {fee.energy:,} energy")
        if fee.memo_fee:
            items.append(f"{_trx(fee.memo_fee)} TRX for the memo")
        if fee.total_burn:
            text = (f"Network fee: burns ≈ {_trx(fee.total_burn)} TRX — "
                    + ", ".join(items) + ".")
        else:
            text = ("Network fee: none — your free / staked bandwidth"
                    + (" and energy" if fee.energy else "") + " covers it.")
        if fee.fee_limit:
            text += f"\nFee limit: {_trx(fee.fee_limit)} TRX (the most it may burn)."
        self._set_fee(text)
        self._validate_funds()

    def _on_estimate_failed(self, seq: int, reason: str) -> None:
        if seq != self._seq:
            return
        self._set_fee("")
        self._set_status(reason)

    def _validate_funds(self) -> None:
        fee, amount = self._fee, self.amount_raw()
        if fee is None or amount is None or self._trx_balance is None:
            return
        trx_needed = fee.total_burn + (amount if self._is_native else 0)
        if not self._is_native and amount > self._balance_raw:
            self._set_status(f"That's more {self._symbol} than this account holds.")
        elif trx_needed > self._trx_balance:
            self._set_status(
                f"Not enough TRX: this needs {_trx(trx_needed)} TRX, the "
                f"account holds {_trx(self._trx_balance)}.")
        else:
            self._set_status("")
            self.send_btn.setEnabled(not self._signing)

    # --- send ------------------------------------------------------------

    def _on_send(self) -> None:
        contract = self.contract()
        if contract is None or self._fee is None:
            return
        self.send_requested.emit(contract, self._fee.fee_limit)

    def set_signing_in_progress(self, busy: bool) -> None:
        self._signing = busy
        for w in (self.to_edit, self.amount_edit, self.max_btn):
            w.setEnabled(not busy)
        self.send_btn.setEnabled(not busy and self._fee is not None
                                 and self.contract() is not None)

    # --- labels --------------------------------------------------------------

    def _set_fee(self, text: str) -> None:
        self.fee_lbl.setText(text)
        self.fee_lbl.setVisible(bool(text))

    def _set_status(self, text: str) -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setVisible(bool(text))
