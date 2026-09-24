"""The Tron send path: the dialog (validation, fee text, Max, funds check),
MainWindow routing + signer capability, and the pending row / probe."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from qeth.address import tron_from_hex
from qeth.chains import DEFAULT_CHAINS, TRON
from qeth.tron.fees import TronFee, trc20_transfer_data
from qeth.tron.tx import TransferContract, TriggerSmartContract, TronTx, signed_transaction

CHAIN = next(c for c in DEFAULT_CHAINS if c.family == TRON)
OWNER = "0x" + "11" * 20
TO = "0x" + "22" * 20
USDT = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"
TRX_ASSET = {"is_native": True, "contract": None, "symbol": "TRX", "decimals": 6,
             "balance_raw": 10_000_000}
USDT_ASSET = {"is_native": False, "contract": USDT, "symbol": "USDT", "decimals": 6,
              "balance_raw": 50_000_000}


def _tron_dialog(qtbot, asset=TRX_ASSET, *, address_book=None):
    """The shared Send dialog on Tron, with workers captured, not run."""
    import qeth.plugins.transactions as tx
    from unittest.mock import MagicMock
    started: list = []
    d = tx.TronSendTokenDialog(
        asset, CHAIN, OWNER, abi_source=MagicMock(), abi_cache=MagicMock(),
        start_worker=started.append, address_book=address_book,
        known_addresses=[(OWNER, "me")])
    qtbot.addWidget(d)
    d.started = started
    return d


def _type(d, to=TO, amount="1.5"):
    d.recipient_edit.setText(tron_from_hex(to) if to.startswith("0x") else to)
    d.amount_edit.setText(amount)


def _estimate(d, fee, trx_balance=10**9):
    """Land a fee estimate as the (latest) TronFeeWorker would."""
    d._reestimate_gas()
    from qeth.plugins.transactions.tron_send import TronFeeWorker
    assert isinstance(d._gas_worker, TronFeeWorker)
    d._on_tron_estimated(fee, trx_balance)


def test_it_is_the_shared_send_dialog(qtbot):
    from qeth.plugins.transactions import SendTokenDialog
    d = _tron_dialog(qtbot)
    assert isinstance(d, SendTokenDialog)
    assert d.recipient_edit.placeholderText() == "T… address"
    assert d._gas_section is not None            # "Network resources"
    assert not hasattr(d, "spin_gas")            # no gas / nonce on Tron


def test_recipient_parses_tron_form_only(qtbot):
    d = _tron_dialog(qtbot)
    d.recipient_edit.setText(tron_from_hex(TO))
    assert d._parsed_recipient().lower() == TO
    for bad in (TO, "TNotAnAddress", "vitalik.eth"):   # hex / junk / ENS
        d.recipient_edit.setText(bad)
        assert d._parsed_recipient() is None


def test_trx_send_estimates_and_finalises(qtbot):
    d = _tron_dialog(qtbot)
    _type(d)
    assert not d.confirm_btn.isEnabled()          # no estimate yet
    fee = TronFee(bandwidth=268, bandwidth_burn=100_000, activation=1_000_000)
    _estimate(d, fee)
    assert d.confirm_btn.isEnabled()
    assert "1.1 TRX burned" in d.max_total_lbl.text()
    assert "account is new" in d._activation_lbl.text()
    contract, fee_limit = d.finalised_tron()
    assert contract == TransferContract(d._from_addr, d._parsed_recipient(), 1_500_000)
    assert fee_limit == 0


def test_trc20_send_is_a_contract_call_with_a_fee_limit(qtbot):
    d = _tron_dialog(qtbot, USDT_ASSET)
    _type(d, amount="2")
    fee = TronFee(bandwidth=345, bandwidth_burn=0, energy=64285,
                  energy_burn=6_428_500, fee_limit=9_642_751)
    _estimate(d, fee)
    contract, fee_limit = d.finalised_tron()
    assert isinstance(contract, TriggerSmartContract)
    assert contract.contract.lower() == USDT
    assert contract.data == trc20_transfer_data(d._parsed_recipient(), 2_000_000)
    assert fee_limit == 9_642_751
    assert "9.642751 TRX" in d._fee_limit_lbl.text()
    # The decoded call shows the recipient as a T… address.
    assert tron_from_hex(TO) in d.decoded_view.toPlainText()


def test_not_enough_trx_is_flagged(qtbot):
    d = _tron_dialog(qtbot, USDT_ASSET)
    _type(d, amount="1")
    fee = TronFee(bandwidth=345, bandwidth_burn=0, energy=64285,
                  energy_burn=6_428_500, fee_limit=9_642_751)
    _estimate(d, fee, trx_balance=1_000_000)       # 1 TRX < 6.43 burn
    assert "⚠ needs 6.4285 TRX" in d.max_total_lbl.text()


def test_max_trx_leaves_the_burn(qtbot):
    d = _tron_dialog(qtbot)
    _type(d)
    _estimate(d, TronFee(bandwidth=268, bandwidth_burn=268_000))
    d._on_max_clicked()
    assert d._parsed_amount_raw() == 10_000_000 - 268_000


def test_a_failed_estimate_blocks_send(qtbot):
    d = _tron_dialog(qtbot)
    _type(d)
    d._reestimate_gas()
    d._on_tron_failed("This address has never received TRX")
    assert not d.confirm_btn.isEnabled()
    assert "never received TRX" in d.max_total_lbl.text()
    with pytest.raises(Exception, match="fee estimate"):
        d.finalised_tron()


def test_address_book_offers_tron_forms(qtbot):
    d = _tron_dialog(qtbot, address_book=[(OWNER, "me"), (TO, "cold")])
    model = d._book_completer.model()
    shown = [model.index(i, 0).data() for i in range(model.rowCount())]
    assert f"cold — {tron_from_hex(TO)}" in shown


def test_tron_contract_mapping():
    from qeth.plugins.transactions.tron_send import tron_contract
    from qeth.signing import SigningRequest
    trx = SigningRequest(chain_id=1, from_addr=OWNER, to_addr=TO, value_wei=7)
    assert tron_contract(OWNER, trx) == TransferContract(OWNER, TO, 7)
    call = SigningRequest(chain_id=1, from_addr=OWNER, to_addr=USDT,
                          value_wei=3, data="0xa9059cbb")
    assert tron_contract(OWNER, call) == TriggerSmartContract(
        OWNER, USDT, bytes.fromhex("a9059cbb"), 3)


# --- MainWindow ---------------------------------------------------------------

class TestMainWindow:
    def test_send_on_tron_opens_the_shared_dialog(self, mainwindow, monkeypatch):
        import qeth.plugins.transactions as tx
        opened = []
        monkeypatch.setattr(tx.TronSendTokenDialog, "show",
                            lambda self: opened.append(self))
        mainwindow.open_send_dialog(TRX_ASSET, CHAIN, OWNER)
        assert len(opened) == 1 and isinstance(opened[0], tx.TronSendTokenDialog)

    def test_a_signer_that_cant_sign_tron_is_refused(self, mainwindow, monkeypatch):
        import qeth.ui as ui
        warned = []
        monkeypatch.setattr(ui, "warn", lambda *a, **k: warned.append(a))
        mainwindow.store.accounts.append(
            {"address": OWNER, "source": "ledger", "path": "44'/195'/0'/0/0",
             "label": "", "family": TRON})
        signer, _ = mainwindow._pick_signer_for(
            mainwindow, OWNER, SimpleNamespace(), family=TRON)
        assert signer is None
        assert "can't sign Tron" in warned[0][2]

    def test_tron_signing_picks_the_tron_capable_record(self, mainwindow):
        """The same address as an EVM Ledger record AND a hot wallet: a Tron
        send must resolve to the hot wallet."""
        mainwindow.store.accounts += [
            {"address": OWNER, "source": "ledger", "path": "44'/60'/0'/0/0", "label": ""},
            {"address": OWNER, "source": "hot", "label": ""},
        ]
        rec = mainwindow.store.account_for_signing(OWNER, family=TRON)
        assert rec["source"] == "hot"


# --- pending --------------------------------------------------------------------

def _signed(expiration_ms: int) -> str:
    tx = TronTx(TransferContract(OWNER, TO, 5), b"\x00\x01", bytes(8),
                expiration=expiration_ms, timestamp=expiration_ms - 60_000)
    return "0x" + signed_transaction(tx.raw_data(), [bytes(65)]).hex()


def _probe(monkeypatch, info, raw, *, fail=None):
    import qeth.plugins.transactions as txp
    pushed = []

    class Client:
        def __init__(self, chain):
            pass

        def transaction_info(self, tx_id):
            if fail:
                raise fail
            return info

        def broadcast(self, signed):
            pushed.append(signed)
            return "ok"
    monkeypatch.setattr(txp, "TronClient", Client)
    w = txp.PendingProbeWorker(CHAIN, "0x" + "ab" * 32, OWNER, -1, raw, True)
    out = {}
    w.confirmed.connect(lambda c, h, r: out.update(confirmed=r))
    w.dropped.connect(lambda c, h: out.update(dropped=True))
    w.still_pending.connect(lambda c, h: out.update(pending=True))
    w.failed.connect(lambda c, h, m: out.update(failed=m))
    w.run()
    return out, pushed


def test_probe_confirms_with_a_receipt_shaped_answer(monkeypatch):
    info = {"blockNumber": 100, "fee": 345000, "receipt": {
        "result": "SUCCESS", "energy_usage_total": 64285},
        "log": [{"address": USDT[2:], "topics": ["ddf2", "00"], "data": "05"}]}
    out, _ = _probe(monkeypatch, info, _signed(int(time.time() * 1000) + 60_000))
    r = out["confirmed"]
    assert (r["blockNumber"], r["status"], r["fee"]) == ("0x64", "0x1", 345000)
    assert r["logs"][0] == {"address": USDT, "topics": ["0xddf2", "0x00"], "data": "0x05"}


def test_probe_marks_a_reverted_call(monkeypatch):
    info = {"blockNumber": 1, "result": "FAILED", "receipt": {"result": "REVERT"}}
    out, _ = _probe(monkeypatch, info, _signed(int(time.time() * 1000)))
    assert out["confirmed"]["status"] == "0x0"


def test_probe_rebroadcasts_until_expiry_then_drops(monkeypatch):
    now = int(time.time() * 1000)
    out, pushed = _probe(monkeypatch, {}, _signed(now + 600_000))
    assert out == {"pending": True} and len(pushed) == 1
    out, pushed = _probe(monkeypatch, {}, _signed(now - 120_000))
    assert out == {"dropped": True} and pushed == []


def test_confirmed_receipt_carries_the_fee():
    from qeth.plugins.transactions import _confirmed_from_receipt
    from qeth.transactions import Transaction
    old = Transaction(CHAIN.chain_id, "0x1", 0, 1, -1, OWNER, TO, 5, 0, 0, "",
                      "0x", True, pending=True, raw_signed="0x00")
    new = _confirmed_from_receipt(old, {"blockNumber": "0x10", "status": "0x1",
                                        "gasUsed": "0x0", "fee": 268000})
    assert (new.block_number, new.fee, new.pending, new.raw_signed) == (16, 268000, False, None)
