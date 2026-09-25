"""Tron's half of the transaction composer.

Send on Tron is the SAME dialog as on an EVM chain — ``SendTokenDialog``:
recipient + amount + Max + USD value, the recipient's identity row, the
decoded call, the Events preview (the node's own simulation), the revert
banner, the signing lock. Only how a transaction PAYS differs, and that is
what ``_TronFeesMixin`` swaps in over ``_TxComposerDialog``'s EVM fee hooks:

- no gas / fee spinners and no nonce: a "Network resources" section shows
  the bandwidth, energy and account activation the transaction will use
  and what it BURNS in TRX for whatever staked + free resources don't
  cover (``qeth.tron.fees``), plus a contract call's ``fee_limit``;
- the dialog still describes the transaction as a ``SigningRequest``
  (to / value / data) — ``finalised_tron`` turns that into the Tron
  contract + fee_limit the host signs (the TaPoS reference is added at
  signing time by ``TronSignAndBroadcastWorker``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QVBoxLayout

from ...chain import native_amount
from ...signing import SignerError, SigningRequest
from ...tron.client import TronClient, TronError
from ...tron.fees import TronFee, estimate_fee
from ...tron.tx import Contract, TransferContract, TriggerSmartContract



def tron_contract(owner: str, req: SigningRequest) -> Contract:
    """The Tron contract a composer's request describes: no calldata → a
    TRX ``TransferContract``; calldata → a ``TriggerSmartContract`` (a
    TRC-20 transfer, …) carrying the request's value as ``call_value``."""
    if not req.to_addr:
        raise SignerError("a Tron transaction needs a recipient")
    data = bytes.fromhex(req.data[2:]) if req.data and len(req.data) > 2 else b""
    if not data:
        return TransferContract(owner, req.to_addr, int(req.value_wei))
    return TriggerSmartContract(owner, req.to_addr, data, int(req.value_wei or 0))


class TronFeeWorker(QThread):
    """Estimate ``contract``'s fee and read the sender's TRX balance (the fee
    is burned in TRX whatever is sent). ``estimated(TronFee, balance_sun)``
    or ``failed(reason)``; the dialog drops a superseded worker's answer."""

    estimated = Signal(object, object)
    failed = Signal(str)

    def __init__(self, chain, contract: Contract, parent=None, *,
                 memo: bytes = b""):
        super().__init__(parent)
        self._chain = chain
        self._contract = contract
        self._memo = memo

    def run(self) -> None:
        try:
            client = TronClient(self._chain)
            fee = estimate_fee(client, self._contract, memo=self._memo)
            balance = client.get_balance(self._contract.owner)
        except TronError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:
            self.failed.emit(f"couldn't estimate the fee: {e}")
            return
        self.estimated.emit(fee, balance)


def _trx(sun: int) -> str:
    """A TRX amount, exact (sun has 6 decimals — nothing rounds away)."""
    return f"{(Decimal(sun) / 10**6).normalize():f}"


class _TronFeesMixin:
    """The Tron fee hooks over ``_TxComposerDialog`` (mixed in FIRST)."""

    # Re-estimate this often while the dialog is open: a transaction just
    # before it (a dapp's approve, then its swap) moves the balance and the
    # energy after the first estimate.
    TRON_FEE_REFRESH_MS = 12_000

    if TYPE_CHECKING:
        chain: Any
        _from_addr: Any
        _gas_ready: bool
        _gas_worker: QThread | None
        _native_price_usd: Any
        _start_worker: Any
        _signing: bool
        max_total_lbl: QLabel

        def layout(self) -> Any: ...
        def revert_banner(self) -> QLabel: ...
        def _value_label(self, text: str, *, monospace: bool = False) -> QLabel: ...
        def _is_stale_gas(self) -> bool: ...
        def _on_gas_ready(self) -> None: ...
        def _update_state(self) -> None: ...
        def _update_extra_totals(self, fee_wei: int) -> None: ...
        def _build_request(self) -> SigningRequest: ...

    def _build_gas_section(self, outer, *, base_fee_text: str) -> None:
        # Package-level helpers are imported here, not at the top: the
        # transactions package imports THIS module while it initialises.
        from . import _CollapsibleSection
        self._tron_fee: TronFee | None = None
        self._trx_balance: int | None = None
        self._gas_section = _CollapsibleSection("Network resources")
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        self._bandwidth_lbl = self._value_label("—")
        form.addRow("Bandwidth:", self._bandwidth_lbl)
        self._energy_lbl = self._value_label("—")
        form.addRow("Energy:", self._energy_lbl)
        self._activation_lbl = self._value_label("—")
        form.addRow("Account activation:", self._activation_lbl)
        self._fee_limit_lbl = self._value_label("—")
        form.addRow("Fee limit:", self._fee_limit_lbl)
        # The estimate's status line — the EVM base-fee row's slot.
        self.base_fee_lbl = self._value_label(base_fee_text)
        form.addRow("Estimate:", self.base_fee_lbl)
        self._resources_form = form
        for lbl in (self._activation_lbl, self._fee_limit_lbl):
            form.setRowVisible(lbl, False)
        self._gas_section.set_content_layout(form)
        outer.addWidget(self._gas_section)
        self._funds_banner: QLabel | None = None
        self._fee_refresh = QTimer(self)        # type: ignore[arg-type]
        self._fee_refresh.setInterval(self.TRON_FEE_REFRESH_MS)
        self._fee_refresh.timeout.connect(self._refresh_tron_fee)

    def _kick_gas(self, probe: SigningRequest) -> None:
        try:
            contract = tron_contract(self._from_addr, probe)
        except SignerError:
            return
        self._kick_tron_fee(contract)

    def _kick_tron_fee(self, contract: Contract, memo: bytes = b"") -> None:
        worker = TronFeeWorker(self.chain, contract, memo=memo)
        self._gas_worker = worker
        # Bound-method connections (receiver-tracked), and _is_stale_gas drops
        # a superseded worker's answer — same discipline as the EVM gas probe.
        worker.estimated.connect(self._on_tron_estimated)
        worker.failed.connect(self._on_tron_failed)
        self.base_fee_lbl.setText("estimating…")
        self._start_worker(worker)

    def _on_tron_estimated(self, fee: TronFee, trx_balance: int) -> None:
        if self._is_stale_gas():
            return                    # a newer probe (recipient edit) supersedes
        self._tron_fee = fee
        self._trx_balance = trx_balance
        form = self._resources_form

        def burn(sun: int) -> str:
            return f" — burns {_trx(sun)} TRX" if sun else " — covered"
        self._bandwidth_lbl.setText(
            f"{fee.bandwidth:,} bytes{burn(fee.bandwidth_burn)}")
        self._energy_lbl.setText(
            f"{fee.energy:,}{burn(fee.energy_burn)}" if fee.energy else "none")
        form.setRowVisible(self._activation_lbl, bool(fee.activation))
        self._activation_lbl.setText(
            f"{_trx(fee.activation)} TRX — the recipient's account is new")
        limit_text = self._fee_limit_text(fee)
        form.setRowVisible(self._fee_limit_lbl, bool(limit_text))
        self._fee_limit_lbl.setText(limit_text)
        self.base_fee_lbl.setText("ready")
        self._gas_ready = True
        if not self._fee_refresh.isActive():
            self._fee_refresh.start()
        self._on_gas_ready()
        self._update_state()

    def _fee_limit_text(self, fee: TronFee) -> str:
        """The Fee limit row ("" hides it): the cap qeth will put on the
        call. A dapp's transaction brings its own (``TronSignTransactionDialog``)."""
        if not fee.fee_limit:
            return ""
        return f"{_trx(fee.fee_limit)} TRX — the most the call may burn"

    def _on_tron_failed(self, msg: str) -> None:
        if self._is_stale_gas():
            return
        self._tron_fee = None
        self._gas_ready = False
        if self._funds_banner is not None:      # it described a stale estimate
            self._funds_banner.setVisible(False)
        self.base_fee_lbl.setText(f"(failed: {msg})")
        self.max_total_lbl.setText(f"— {msg}")
        self._update_state()

    def _update_max_total(self) -> None:
        fee = self._tron_fee
        if not self._gas_ready or fee is None:
            return
        from . import _format_usd
        burn = fee.total_burn
        if burn:
            text = f"≈ {_trx(burn)} TRX burned"
            if self._native_price_usd is not None:
                usd = native_amount(burn, self.chain) * self._native_price_usd
                text += f"  ({_format_usd(usd)})"
        else:
            text = "none — staked / free resources cover it"
        # Activation is the one charge with no EVM counterpart, and 1 TRX can
        # dwarf the rest — so say it on this always-visible line, not only in
        # the collapsed resources section.
        if fee.activation:
            text += (f"\nincludes {_trx(fee.activation)} TRX to activate the "
                     "recipient's new account (it has never received TRX)")
        self.max_total_lbl.setText(text)
        # The fee is paid in TRX whatever is sent.
        self._set_funds_warning(burn + self._native_outflow(), fee)
        self._update_extra_totals(burn)

    def _set_funds_warning(self, need: int, fee: TronFee) -> None:
        """A banner above Confirm when the account can't cover ``need`` sun —
        loud, not a suffix on the fee line, because on Tron a contract call
        that can't pay its energy isn't refused: it's mined, runs out of
        energy, FAILS, and still burns the TRX the account had."""
        short = self._trx_balance is not None and need > self._trx_balance
        banner = self._funds_banner
        if not short:
            if banner is not None:
                banner.setVisible(False)
            return
        if banner is None:
            from . import _IDENTITY_TINT
            banner = self._funds_banner = QLabel()
            banner.setWordWrap(True)
            bg, fg = _IDENTITY_TINT["warn"]
            banner.setStyleSheet(
                f"background:{bg}; color:{fg}; padding:6px 10px; border-radius:4px;")
            root = self.layout()
            if isinstance(root, QVBoxLayout):
                root.insertWidget(root.indexOf(self.revert_banner()), banner)
        held = f"{_trx(self._trx_balance or 0)} TRX"
        if fee.energy:
            text = (f"⚠ Not enough TRX: this needs about {_trx(need)} TRX and the "
                    f"account holds {held}. The call would still be mined, run "
                    "out of energy and fail — burning the TRX the account has.")
        else:
            text = (f"⚠ Not enough TRX: this needs {_trx(need)} TRX and the "
                    f"account holds {held} — the network will refuse it.")
        banner.setText(text)
        banner.setVisible(True)

    def _refresh_tron_fee(self) -> None:
        """The periodic re-estimate (fresh balance, energy, activation)."""
        if getattr(self, "_signing", False):
            return
        try:
            probe = self._build_request()
        except SignerError:
            return
        self._kick_gas(probe)

    def _native_outflow(self) -> int:
        """TRX leaving with the transaction itself (a TRX send's amount)."""
        try:
            return int(self._build_request().value_wei or 0)
        except SignerError:
            return 0

    def _set_fee_controls_enabled(self, enabled: bool) -> None:
        """Nothing to edit: a Tron fee is what the resources cost."""

    def _native_fee_reserve(self) -> int:
        """A TRX "Max" leaves exactly what the transfer burns — Tron fees are
        deterministic, there's no fee market to bump into."""
        return self._tron_fee.total_burn if self._tron_fee is not None else 0

    def finalised_request(self) -> SigningRequest:
        raise SignerError("a Tron transaction is finalised by finalised_tron")

    def finalised_tron(self) -> tuple[Contract, int]:
        """The contract to sign and its fee_limit (0 for a TRX transfer)."""
        if not self._gas_ready or self._tron_fee is None:
            raise SignerError("fee estimate did not complete")
        contract = tron_contract(self._from_addr, self._build_request())
        fee_limit = (self._tron_fee.fee_limit
                     if isinstance(contract, TriggerSmartContract) else 0)
        return contract, fee_limit
