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


@pytest.fixture
def dialog(qtbot):
    from qeth.plugins.transactions.tron_send import TronSendDialog
    started = []

    def make(asset=TRX_ASSET):
        d = TronSendDialog(asset, CHAIN, OWNER, start_worker=started.append)
        qtbot.addWidget(d)
        d.started = started
        return d
    return make


def _fill(d, to=TO, amount="1.5"):
    d.to_edit.setText(tron_from_hex(to) if to.startswith("0x") else to)
    d.amount_edit.setText(amount)
    d._on_amount_typed(amount)


def test_invalid_recipient_is_flagged(dialog):
    d = dialog()
    _fill(d, to="TNotAnAddress")
    assert "isn't a valid Tron address" in d.status_lbl.text()
    assert d.contract() is None and not d.send_btn.isEnabled()


def test_trx_and_trc20_contracts(dialog):
    d = dialog()
    _fill(d)
    assert d.contract() == TransferContract(OWNER, d.recipient(), 1_500_000)
    t = dialog(USDT_ASSET)
    _fill(t, amount="2")
    c = t.contract()
    assert isinstance(c, TriggerSmartContract) and c.contract == USDT
    assert c.data == trc20_transfer_data(t.recipient(), 2_000_000)


def test_too_many_decimals_is_not_an_amount(dialog):
    d = dialog()
    _fill(d, amount="0.0000001")
    assert d.amount_raw() is None


def test_estimate_enables_send_and_explains_the_burn(dialog):
    d = dialog()
    _fill(d)
    d._kick_estimate()
    assert d.started, "an estimate worker was started"
    fee = TronFee(bandwidth=268, bandwidth_burn=100_000, activation=1_000_000)
    d._on_estimated(d._seq, fee, 10_000_000)
    assert "burns ≈ 1.1 TRX" in d.fee_lbl.text()
    assert "activate the recipient" in d.fee_lbl.text()
    assert d.send_btn.isEnabled()
    got = []
    d.send_requested.connect(lambda c, lim: got.append((c, lim)))
    d.send_btn.click()
    assert got == [(d.contract(), 0)]


def test_a_stale_estimate_is_ignored(dialog):
    d = dialog()
    _fill(d)
    d._kick_estimate()
    stale = d._seq
    d._kick_estimate()
    d._on_estimated(stale, TronFee(bandwidth=1, bandwidth_burn=0), 10**9)
    assert d._fee is None


def test_not_enough_trx_for_amount_plus_burn(dialog):
    d = dialog()
    _fill(d, amount="9.5")
    d._kick_estimate()
    d._on_estimated(d._seq, TronFee(bandwidth=268, bandwidth_burn=600_000), 10_000_000)
    assert "Not enough TRX" in d.status_lbl.text()
    assert not d.send_btn.isEnabled()


def test_trc20_fee_needs_trx_even_with_tokens(dialog):
    d = dialog(USDT_ASSET)
    _fill(d, amount="1")
    d._kick_estimate()
    fee = TronFee(bandwidth=345, bandwidth_burn=0, energy=64285,
                  energy_burn=6_428_500, fee_limit=9_642_751)
    d._on_estimated(d._seq, fee, 1_000_000)        # 1 TRX < 6.43 burn
    assert "Not enough TRX" in d.status_lbl.text()
    assert "Fee limit: 9.642751 TRX" in d.fee_lbl.text()


def test_max_trx_leaves_room_for_the_burn(dialog):
    d = dialog()
    _fill(d)
    d._on_max()
    d._kick_estimate()
    d._on_estimated(d._seq, TronFee(bandwidth=268, bandwidth_burn=268_000), 10_000_000)
    assert d.amount_raw() == 10_000_000 - 268_000
    assert d.send_btn.isEnabled()


# --- MainWindow ---------------------------------------------------------------

class TestMainWindow:
    def test_send_on_tron_opens_the_tron_dialog(self, mainwindow, monkeypatch):
        from qeth.plugins.transactions import tron_send
        opened = []
        monkeypatch.setattr(tron_send.TronSendDialog, "show",
                            lambda self: opened.append(self))
        mainwindow.open_send_dialog(TRX_ASSET, CHAIN, OWNER)
        assert len(opened) == 1 and isinstance(opened[0], tron_send.TronSendDialog)

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
