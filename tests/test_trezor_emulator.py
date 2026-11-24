"""qeth's Trezor signer against Trezor's firmware emulator — the Safe 3 (T3B1,
buttons) and Safe 5 (T3T1, touch) on release firmware 2.13.1.

Not part of the default run (marker ``trezor_emulator``).
``scripts/trezor-emu.sh test`` starts the emulators, loads Trezor's test seed
("all" × 12) and sets ``QETH_TREZOR_EMU_<MODEL>`` (the UDP ``host:port``; the
debug link is the next port). A model whose variable is unset is skipped.

Everything goes through the production path: ``TrezorSigner`` →
``run_trezor_job`` → trezorlib over the emulator's UDP transport
(``TREZOR_PATH``), the debug link reading the screens and pressing like a user.
Each signature must recover to the seed's account. Definitions come from the
local copy, built from real production-signed ones
(``tests/data/trezor_definitions_sample.tar.xz``) — and the conftest network
guard fails any test that would fetch one during a signature.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

pytest.importorskip("trezorlib")

from trezorlib.debuglink import DebugLink  # noqa: E402
from trezorlib.transport.udp import UdpTransport  # noqa: E402

from qeth import trezor as trezor_mod  # noqa: E402
from qeth import trezor_definitions  # noqa: E402
from qeth.chains import Chain  # noqa: E402
from qeth.signing import (  # noqa: E402
    MessageSigningRequest, SignerError, SigningRequest, TypedDataSigningRequest,
)
from qeth.trezor import TrezorSigner  # noqa: E402

pytestmark = pytest.mark.trezor_emulator

MODELS = ("safe3", "safe5")
PATH = "44'/60'/0'/0/0"
SEED = "all all all all all all all all all all all all"
Account.enable_unaudited_hdwallet_features()
ADDRESS = Account.from_mnemonic(SEED, account_path=f"m/{PATH}").address

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
CRV = "0xd533a949740bb3306d119cc777fa900ba034cd52"
LIDO = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"


def _word(n: int) -> str:
    return n.to_bytes(32, "big").hex()


def _addr(a: str) -> str:
    return "00" * 12 + a.lower().removeprefix("0x")


@dataclass
class Emulator:
    model: str
    host: str
    port: int
    screens: list[str] = field(default_factory=list)

    def drive(self, fn: Callable[[], Any], *, approve: bool = True,
              timeout: float = 120.0) -> Any:
        """Run ``fn`` (which blocks on the device) while confirming — or, with
        ``approve=False``, rejecting — every screen, recording each one."""
        out: dict[str, Any] = {}

        def run() -> None:
            try:
                out["result"] = fn()
            except BaseException as e:  # noqa: BLE001 — re-raised below
                out["error"] = e

        debug = DebugLink(UdpTransport(f"{self.host}:{self.port + 1}"))
        try:
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            last = None
            deadline = time.monotonic() + timeout
            while worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.3)
                layout = debug.read_layout()
                if layout.json_str != last:
                    last = layout.json_str
                    self.screens.append(
                        f"{layout.title()} | {layout.text_content()}".lower())
                if "Homescreen" in layout.json_str:
                    continue
                debug.press_yes() if approve else debug.press_no()
            worker.join(5)
        finally:
            debug.transport.close()
        assert not worker.is_alive(), f"device still waiting: {self.screens[-3:]}"
        if "error" in out:
            raise out["error"]
        return out["result"]

    def text(self) -> str:
        return " ".join(self.screens)


@pytest.fixture(params=MODELS)
def emu(request, monkeypatch, tmp_path):
    address = os.environ.get(f"QETH_TREZOR_EMU_{request.param.upper()}")
    if not address:
        pytest.skip(f"{request.param} emulator not running (scripts/trezor-emu.sh test)")
    host, _, port = address.partition(":")
    monkeypatch.setenv("TREZOR_PATH", f"udp:{address}")
    # An empty local copy unless a test asks for one (``definitions``).
    monkeypatch.setattr(trezor_definitions, "CACHE_DIR", tmp_path / "trezor")
    trezor_mod._CONNECTION.reset()
    yield Emulator(request.param, host, int(port))
    trezor_mod._CONNECTION.reset()


@pytest.fixture
def definitions(emu):
    from test_trezor_definitions import FakeHttp
    assert trezor_definitions.default_definitions().refresh(session=FakeHttp())


def _signer() -> TrezorSigner:
    return TrezorSigner({"address": ADDRESS.lower(), "path": PATH,
                         "source": "trezor"}, timeout=180)


def _tx(emu: Emulator, *, chain_id: int = 1, to: str = "0x" + "22" * 20,
        value: int = 0, data: str = "0x", eip1559: bool = True) -> list[str]:
    fees: dict[str, Any] = (
        {"max_fee_per_gas": 2 * 10**9, "max_priority_fee_per_gas": 10**8}
        if eip1559 else {"gas_price": 3 * 10**9})
    req = SigningRequest(chain_id=chain_id, from_addr=ADDRESS.lower(), to_addr=to,
                         value_wei=value, gas=200_000, nonce=7, data=data, **fees)
    chain = Chain(name="test", chain_id=chain_id, rpc_url="http://127.0.0.1:1",
                  eip1559=eip1559)
    raw = emu.drive(lambda: _signer().sign(req, chain))
    assert Account.recover_transaction(raw) == ADDRESS
    return emu.screens


TRANSFER_CRV = "0xa9059cbb" + _addr("22" * 20) + _word(5 * 10**18)
LIDO_SUBMIT = "0xa1903eab" + _addr("00" * 20)
AAVE_SUPPLY = "0x617ba037" + _addr(USDC) + _word(1000 * 10**6) + _addr(ADDRESS) + _word(0)


# --- reads and plain signatures --------------------------------------------------


def test_the_emulator_derives_the_seeds_account(emu):
    address = trezor_mod.run_trezor_job(lambda session: trezor_mod._ethereum().get_address(
        session, trezor_mod._address_n(PATH)))
    assert address == ADDRESS


def test_eth_transfer(emu):
    _tx(emu, value=10**17)
    assert "0.1 eth" in emu.text()


def test_legacy_transfer(emu):
    _tx(emu, chain_id=56, value=10**15, eip1559=False)


def test_personal_sign(emu):
    sig = emu.drive(lambda: _signer().sign_message(MessageSigningRequest(
        from_addr=ADDRESS.lower(), raw=b"qeth Trezor emulator check")))
    assert Account.recover_message(
        encode_defunct(b"qeth Trezor emulator check"), signature=sig) == ADDRESS


def test_typed_data(emu):
    import json
    from pathlib import Path
    typed = json.loads((Path(__file__).parent / "data" / "ledger_eip712_golden.json")
                       .read_text())["messages"]["permit"]["typed_data"]
    sig = emu.drive(lambda: _signer().sign_typed_data(TypedDataSigningRequest(
        from_addr=ADDRESS.lower(), typed_data=typed)))
    assert Account.recover_message(
        encode_typed_data(full_message=typed), signature=sig) == ADDRESS


def test_rejected_on_the_device(emu):
    with pytest.raises(SignerError, match="(?i)cancel"):
        emu.drive(lambda: _signer().sign_message(MessageSigningRequest(
            from_addr=ADDRESS.lower(), raw=b"reject me")), approve=False)


# --- definitions: answered from the local copy ------------------------------------


def test_a_built_in_token_needs_no_definition(emu):
    """USDC is in the firmware: shown with no local copy at all."""
    _tx(emu, to=USDC, data="0xa9059cbb" + _addr("22" * 20) + _word(1_234_560_000))
    assert "1,234.56 usdc" in emu.text()


def test_a_token_named_from_the_local_copy(emu, definitions):
    _tx(emu, to=CRV, data=TRANSFER_CRV)
    assert "5 crv" in emu.text()


def test_without_the_local_copy_the_token_is_unknown(emu):
    _tx(emu, to=CRV, data=TRANSFER_CRV)
    assert "5 crv" not in emu.text()
    assert "unknown" in emu.text() or "unkn" in emu.text()


@pytest.mark.parametrize(("to", "value", "data", "shown"), [
    (LIDO, 10**18, LIDO_SUBMIT, ("lido dao: steth", "stake eth")),
    (AAVE_POOL, 0, AAVE_SUPPLY, ("aave dao", "supply", "1,000 usdc")),
], ids=["lido-stake", "aave-supply"])
def test_contract_calls_clear_signed_from_the_local_copy(
        emu, definitions, to, value, data, shown):
    """ERC-7730 descriptions from Trezor's archive: the device shows what the
    call does instead of its data."""
    _tx(emu, to=to, value=value, data=data)
    for needle in shown:
        assert needle in emu.text(), (needle, emu.screens)


def test_without_the_local_copy_a_contract_call_shows_its_data(emu):
    _tx(emu, to=LIDO, value=10**18, data=LIDO_SUBMIT)
    assert "stake eth" not in emu.text()


def test_a_definition_the_device_refuses_falls_back_to_raw(emu, definitions, monkeypatch):
    """A definition the firmware refuses (here one damaged after the host's
    check) aborts its signature before anything shows; qeth signs again
    without definitions and the device shows the raw call."""
    real = trezor_definitions.DefinitionMirror.lookup

    def damaged(self, path):
        blob = real(self, path)
        return None if blob is None else blob[:-1] + bytes([blob[-1] ^ 1])
    monkeypatch.setattr(trezor_definitions.DefinitionMirror, "lookup", damaged)
    _tx(emu, to=LIDO, value=10**18, data=LIDO_SUBMIT)
    assert "stake eth" not in emu.text()
