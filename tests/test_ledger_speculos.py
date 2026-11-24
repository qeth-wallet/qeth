"""qeth's Ledger signer against the real Ethereum app on Ledger's Speculos
emulator — Nano S (app 1.16.1, its last), Nano X and Nano S Plus (current app).

Not part of the default run (marker ``speculos``). ``scripts/speculos.sh test``
starts the emulators and sets, per model, ``QETH_SPECULOS_<MODEL>`` (the APDU
``host:port``) and ``QETH_SPECULOS_<MODEL>_API`` (the REST API that reads the
screen and presses the buttons). A model whose variables are unset is skipped.

Everything goes through the production path: ``LedgerSigner`` →
``run_ledger_hid_job`` → ``ledger_app`` over Speculos' APDU socket
(``QETH_LEDGER_SPECULOS``), with the test driving the device's buttons the way
a user would. Each signature must recover to the account Speculos' test seed
derives at the path.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

from qeth import ledger as ledger_mod
from qeth.chains import Chain
from qeth.ledger_app import SPECULOS_ENV, EthApp, connect, list_devices
from qeth.signing import (
    MessageSigningRequest, SignerError, SigningRequest, TypedDataSigningRequest,
)

pytestmark = pytest.mark.speculos

SEED = ("glory promote mansion idle axis finger extra february uncover one "
        "trip resource lawn turtle enact monster seven myth punch hobby "
        "comfort wild raise skin")
MODELS = ("nanos", "nanox", "nanosp")
PATH = "44'/60'/0'/0/0"
TO = "0x" + "11" * 20
# ERC-20 transfer(0x2222…, 100) — a contract call the app can only blind-sign
# without token metadata.
ERC20_TRANSFER = ("0xa9059cbb" + "00" * 12 + "22" * 20 + "00" * 31 + "64")

# Confirm screens only — not "approve": an ERC-20 approval's REVIEW screens
# read "Type: Approve" / "Approve 0 USDC".
_APPROVE = ("accept", "sign message", "sign transaction",
            "sign typed message", "and sign", "press both")
_REJECT = ("reject", "cancel")

Account.enable_unaudited_hdwallet_features()


def _seed_address(path: str) -> str:
    return Account.from_mnemonic(SEED, account_path=f"m/{path}").address


@dataclass
class Emulator:
    model: str
    apdu: str
    api: str

    def texts(self) -> list[str]:
        with urllib.request.urlopen(
                f"{self.api}/events?currentscreenonly=true", timeout=5) as r:
            return [e["text"] for e in json.load(r)["events"]]

    def screen(self) -> str:
        return " ".join(t.strip() for t in self.texts()).lower()

    def press(self, button: str) -> None:
        req = urllib.request.Request(
            f"{self.api}/button/{button}",
            data=json.dumps({"action": "press-and-release"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        time.sleep(0.3)

    def home(self) -> None:
        """Back to the first item of the app's home menu — dismissing the
        "Blind signing must be enabled in Settings" notice a refusal leaves."""
        for _ in range(4):
            screen = self.screen()
            if "must be" not in screen and "error" not in screen:
                break
            # A paged notice (the Nano S's "Error (1/2)") closes from its last
            # page only.
            self.press("right" if "(1/" in screen else "both")
        # Walk left to the "ready" screen: the Nano X menu stops at its first
        # item, the Nano S one wraps around — both get there.
        for _ in range(8):
            if "ready" in self.screen():
                return
            self.press("left")
        raise AssertionError(f"no home screen on the {self.model}: {self.texts()}")

    def _to_item(self, labels: tuple[str, ...], stop: tuple[str, ...]) -> None:
        for _ in range(12):
            texts = [t.strip() for t in self.texts()]
            if any(label in texts for label in labels):
                return
            if any(s in texts for s in stop):
                break
            self.press("right")
        raise AssertionError(f"no {labels} on the {self.model}: {self.texts()}")

    def set_blind_signing(self, enabled: bool) -> None:
        """Flip the app's Blind signing setting through its menus."""
        self.home()
        self._to_item(("Settings", "App settings"), stop=("Quit", "Quit app"))
        self.press("both")
        self._to_item(("Blind signing",), stop=("Back",))
        want = "Enabled" if enabled else "Disabled"
        if want not in [t.strip() for t in self.texts()]:
            self.press("both")
        assert want in [t.strip() for t in self.texts()], self.texts()
        self._to_item(("Back",), stop=())
        self.press("both")
        self.home()

    def drive(self, fn: Callable[[], Any], *, approve: bool = True,
              timeout: float = 90.0) -> Any:
        """Run ``fn`` (which blocks on the device) while reading the screens
        like a user: step right through the review, then press both buttons on
        the approve screen — or, with ``approve=False``, on the reject one."""
        out: dict[str, Any] = {}

        def run() -> None:
            try:
                out["result"] = fn()
            except BaseException as e:  # noqa: BLE001 — re-raised below
                out["error"] = e

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.8)
        deadline = time.monotonic() + timeout
        while worker.is_alive() and time.monotonic() < deadline:
            screen = self.screen()
            is_reject = any(w in screen for w in _REJECT)
            if approve and not is_reject and any(w in screen for w in _APPROVE):
                self.press("both")
            elif not approve and is_reject:
                self.press("both")
            else:
                self.press("right")
        worker.join(5)
        assert not worker.is_alive(), f"device still waiting: {self.texts()}"
        if "error" in out:
            raise out["error"]
        return out["result"]


@pytest.fixture(params=MODELS)
def emu(request, monkeypatch) -> Emulator:
    model = request.param
    apdu = os.environ.get(f"QETH_SPECULOS_{model.upper()}")
    api = os.environ.get(f"QETH_SPECULOS_{model.upper()}_API")
    if not apdu or not api:
        pytest.skip(f"{model} emulator not running (scripts/speculos.sh test)")
    monkeypatch.setenv(SPECULOS_ENV, apdu)
    emulator = Emulator(model, apdu, api)
    emulator.home()
    return emulator


def _signer(path: str = PATH) -> ledger_mod.LedgerSigner:
    store = SimpleNamespace(accounts=[
        {"address": _seed_address(path), "source": "ledger", "path": path}])
    return ledger_mod.LedgerSigner(store, hid_timeout=120)


def _chain(chain_id: int, eip1559: bool) -> Chain:
    return Chain(name="test", chain_id=chain_id, rpc_url="http://127.0.0.1:1",
                 eip1559=eip1559)


def _tx(**kw) -> SigningRequest:
    base: dict[str, Any] = dict(
        chain_id=1, from_addr=_seed_address(PATH), to_addr=TO,
        value_wei=10**17, gas=21000, nonce=7, data="0x")
    base.update(kw)
    return SigningRequest(**base)


# --- reads ---------------------------------------------------------------------


def test_app_version(emu):
    [device] = list_devices()
    with connect(device) as transport:
        config = EthApp(transport).get_configuration()
    if emu.model == "nanos":
        assert config.version == (1, 16, 1)
    else:
        assert config.version >= (1, 24, 0)


@pytest.mark.parametrize("path", [
    "44'/60'/0'/0/0", "44'/60'/5'/0/0", "44'/60'/0'/3", "44'/60'/0'/0/9"])
def test_addresses_match_the_seed(emu, path):
    assert ledger_mod._derive_ledger_paths([path]) == [(path, _seed_address(path))]


def test_probe_sees_the_app(emu):
    assert ledger_mod.is_ledger_available() == (True, None)


# --- signing -------------------------------------------------------------------


def test_eip1559_transfer(emu):
    raw = emu.drive(lambda: _signer().sign(
        _tx(max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**8),
        _chain(1, True)))
    assert Account.recover_transaction(raw) == _seed_address(PATH)


@pytest.mark.parametrize("chain_id", [56, 117])
def test_legacy_transfer(emu, chain_id):
    """BSC, and a chain id whose EIP-155 ``v`` doesn't fit the device's byte."""
    raw = emu.drive(lambda: _signer().sign(
        _tx(chain_id=chain_id, gas_price=10**9), _chain(chain_id, False)))
    assert Account.recover_transaction(raw) == _seed_address(PATH)


def test_personal_sign(emu):
    message = b"Sign in to qeth"
    sig = emu.drive(lambda: _signer().sign_message(MessageSigningRequest(
        from_addr=_seed_address(PATH), raw=message)))
    assert Account.recover_message(
        encode_defunct(message), signature=sig) == _seed_address(PATH)


def test_reject_on_device(emu):
    with pytest.raises(SignerError, match="rejected"):
        emu.drive(lambda: _signer().sign_message(MessageSigningRequest(
            from_addr=_seed_address(PATH), raw=b"no thanks")), approve=False)


def test_contract_call_with_blind_signing_off_explains(emu):
    emu.set_blind_signing(False)
    with pytest.raises(SignerError, match="Blind signing is off"):
        emu.drive(lambda: _signer().sign(
            _tx(value_wei=0, gas=60000, data=ERC20_TRANSFER,
                max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**8),
            _chain(1, True)))
    emu.home()


def test_contract_call_with_blind_signing_on(emu):
    emu.set_blind_signing(True)
    try:
        raw = emu.drive(lambda: _signer().sign(
            _tx(value_wei=0, gas=60000, data=ERC20_TRANSFER,
                max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**8),
            _chain(1, True)))
    finally:
        emu.set_blind_signing(False)
    assert Account.recover_transaction(raw) == _seed_address(PATH)


TYPED = {
    "types": {
        "EIP712Domain": [{"name": "name", "type": "string"},
                         {"name": "chainId", "type": "uint256"}],
        "Mail": [{"name": "contents", "type": "string"}],
    },
    "primaryType": "Mail",
    "domain": {"name": "qeth", "chainId": 1},
    "message": {"contents": "hello"},
}


def test_typed_data_by_hashes(emu):
    emu.set_blind_signing(True)
    try:
        sig = emu.drive(lambda: _signer().sign_typed_data(
            TypedDataSigningRequest(from_addr=_seed_address(PATH),
                                    typed_data=TYPED)))
    finally:
        emu.set_blind_signing(False)
    assert Account.recover_message(
        encode_typed_data(full_message=TYPED), signature=sig) == _seed_address(PATH)


# --- M4: token info from the local mirror ------------------------------------------

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
# Ledger's real, prod-signed USDC descriptor (CAL, 2026-11-24).
USDC_ENTRY = (
    "55534443a0b86991c6218b36c1d19d4a2e9eb0ce3606eb480000000600000001:"
    "3045022100b2e358726e4e6a6752cf344017c0e9d45b9a904120758d45f61b2804f9ad5299"
    "022015161ef28d8c4481bd9432c13562def9cce688bcfec896ef244c9a213f106cdd")


@pytest.fixture
def usdc_mirror(tmp_path, monkeypatch):
    from qeth import ledger_metadata
    monkeypatch.setattr(ledger_metadata, "CACHE_DIR", tmp_path / "ledger")
    path = tmp_path / "ledger" / "tokens" / "1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"tokens": {USDC: USDC_ENTRY}}))


@pytest.mark.parametrize("selector,amount", [
    ("a9059cbb", 1234560000),      # transfer 1234.56 USDC
    ("095ea7b3", 0),               # approve 0 — the Approvals tab's revoke
])
def test_known_token_call_is_clear_signed_without_blind_signing(
        emu, usdc_mirror, selector, amount):
    """With the token's descriptor in the local mirror, an ERC-20 transfer /
    approval signs with blind signing OFF — the device shows the token amount
    instead of refusing."""
    emu.set_blind_signing(False)
    data = "0x" + selector + "00" * 12 + "22" * 20 + amount.to_bytes(32, "big").hex()
    seen: list[str] = []
    original = emu.press

    def press_and_record(button):
        seen.append(emu.screen())
        original(button)
    emu.press = press_and_record     # type: ignore[method-assign]
    raw = emu.drive(lambda: _signer().sign(
        _tx(to_addr=USDC, value_wei=0, gas=60000, data=data,
            max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**8),
        _chain(1, True)))
    assert Account.recover_transaction(raw) == _seed_address(PATH)
    assert any("usdc" in screen for screen in seen), seen
