"""Tron dapps through qeth's injected TronWeb: the TIP-191 / TIP-712 digests
(checked against TronWeb 6.5.1's own output), the RPC server's ``tron_*``
methods and node proxy, the review dialogs and the UI routing. The in-page
half runs end to end in tests/test_webext_tron_browser.py (opt-in)."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from eth_keys import keys

from qeth.address import tron_from_hex
from qeth.chains import DEFAULT_CHAINS, EVM, TRON, Chain
from qeth.rpc import RpcError, RpcServer
from qeth.signing import (
    SignerError, SignMessageWorker, TronMessageSigningRequest, TronSigningRequest,
    TronSignWorker, TronTypedDataSigningRequest,
)
from qeth.tron.messages import (
    TypedDataEncoder, TypedDataError, check_domain_chain, hash_domain,
    message_digest, typed_data_digest,
)
from qeth.tron.tx import (
    TransferContract, TriggerSmartContract, TronTx, recover_signer, signature_v27,
    signed_transaction,
)

# eth-account's documentation key — the TronWeb vectors below were made with it.
PRIV = bytes.fromhex("4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318")
ACCOUNT = "0x" + keys.PrivateKey(PRIV).public_key.to_canonical_address().hex()
ACCOUNT_B58 = "TE2H9hWjzYdwzDFRJfx9BFhr4MmjH1CHaz"
TRON_CHAIN = next(c for c in DEFAULT_CHAINS if c.family == TRON)
TO = "0x" + "33" * 20


def _sig(digest: bytes) -> str:
    return "0x" + signature_v27(keys.PrivateKey(PRIV).sign_msg_hash(digest).to_bytes()).hex()


# --- digests vs TronWeb ----------------------------------------------------------

@pytest.mark.parametrize("raw, version, tronweb", [
    ("hello qeth ✓".encode(), 2,
     "0x7e6825ae426de904d1d1ad61a43de533a18f86d66c9a880066d3349b1e4230256021a9bd2d5"
     "85fb2b9971f884bd51bd2bc9aa34819e0c1ab7913edfe56d705a91b"),
    (bytes([1, 2, 3, 255]), 2,
     "0x7039f5c09a00ebafdc9f207ef87221b9d81f6d566745b29e89140eb408de83450cd19588"
     "65a791c54533e7b96a1464cb15e2669c57707542cb04bc43f74c0c1b1b"),
    (bytes.fromhex("ab" * 32), 1,
     "0xfc3c00780ab2fde72edfe47cb4dc07391c387aaa09a55d4a465f9149e1f9ef800c382e01"
     "28890ba62aba6e881bb4577c4b199dbad9ab76a31a45ab57d7cb1a161c"),
    (bytes.fromhex("deadbeef"), 1,         # v1's header says 32 whatever the length
     "0xc898b1ea8de492def3d25e32cdced046bb95e480f29ecbc850a8edc59a44a72027c5001e"
     "58184d83fb2d76da0a74ac10ad68348615250caa79fe547cf93278381c"),
])
def test_message_signatures_match_tronweb(raw, version, tronweb):
    """TronWeb's signMessageV2 / legacy trx.sign(hex) with the same key."""
    assert _sig(message_digest(raw, version)) == tronweb


# TronWeb's own TIP-712 example, widened: nested structs, trcToken (scalar,
# array, nested array), every address form, bytes, int8, bool.
DOMAIN = {"name": "TRON Mail", "version": "1", "chainId": "0x2b6653dc",
          "verifyingContract": "TUe6BwpA7sVTDKaJQoia7FWZpC9sK8WM2t"}
TYPES = {
    "FromPerson": [{"name": "name", "type": "string"}, {"name": "wallet", "type": "address"},
                   {"name": "trcTokenId", "type": "trcToken"}],
    "ToPerson": [{"name": "name", "type": "string"}, {"name": "wallet", "type": "address"},
                 {"name": "trcTokenArr", "type": "trcToken[]"}],
    "Mail": [{"name": "from", "type": "FromPerson"}, {"name": "to", "type": "ToPerson"},
             {"name": "contents", "type": "string"}, {"name": "tAddr", "type": "address[]"},
             {"name": "trcTokenId", "type": "trcToken"},
             {"name": "trcTokenArr", "type": "trcToken[][]"},
             {"name": "blob", "type": "bytes"}, {"name": "b4", "type": "bytes4"},
             {"name": "neg", "type": "int8"}, {"name": "ok", "type": "bool"}],
}
VALUE = {
    "from": {"name": "Cow", "wallet": "TUg28KYvCXWW81EqMUeZvCZmZw2BChk1HQ",
             "trcTokenId": "1002000"},
    "to": {"name": "Bob", "wallet": "0xd1e7a6bc354106cb410e65ff8b181c600ff14292",
           "trcTokenArr": ["1002000", "1002000"]},
    "contents": "Hello, Bob!",
    "tAddr": ["0xd1e7a6bc354106cb410e65ff8b181c600ff14292",
              "41d1e7a6bc354106cb410e65ff8b181c600ff14292"],
    "trcTokenId": "1002000",
    "trcTokenArr": [["1002000", "1002000"], ["1002000", "1002000"]],
    "blob": "0x0102", "b4": "0xdeadbeef", "neg": -5, "ok": True,
}


def test_typed_data_matches_tronweb():
    enc = TypedDataEncoder(TYPES)
    assert enc.primary_type == "Mail"
    assert enc.encode_type("Mail") == (
        "Mail(FromPerson from,ToPerson to,string contents,address[] tAddr,"
        "trcToken trcTokenId,trcToken[][] trcTokenArr,bytes blob,bytes4 b4,int8 neg,"
        "bool ok)FromPerson(string name,address wallet,trcToken trcTokenId)"
        "ToPerson(string name,address wallet,trcToken[] trcTokenArr)")
    assert hash_domain(DOMAIN).hex() == \
        "d88c5d5bdba3a9334e78e7250bcb2a7739d24776adeef9a461dac55f74cdd26a"
    digest = typed_data_digest(DOMAIN, TYPES, VALUE)
    assert digest.hex() == "a4ea279eb7fb79dfe9b2e363a87fab254487a076adc18b9889c55e13e2a42af6"
    assert _sig(digest) == (
        "0xb87bd9dc0b19a5fc2fc42fc3fec04fc961eca8e9ecc8a936ca2a90f02f7ad4f71d0f12cf"
        "c40ec84302ecdd0f5fee1167ac7c4e25e5e7b30a9bb85a4fa52702791b")
    # A MetaMask-style types object (with EIP712Domain) hashes the same.
    with_domain = {"EIP712Domain": [{"name": "name", "type": "string"}], **TYPES}
    assert typed_data_digest(DOMAIN, with_domain, VALUE) == digest


def test_the_domain_must_pin_the_tron_chain():
    """Without it the signature is a valid EIP-712 one for the same key's EVM
    account (a Permit on mainnet, say)."""
    check_domain_chain(DOMAIN, 728126428)
    with pytest.raises(TypedDataError, match="chain 1,"):
        check_domain_chain({**DOMAIN, "chainId": 1}, 728126428)
    with pytest.raises(TypedDataError, match="no chainId"):
        check_domain_chain({"name": "x"}, 728126428)


@pytest.mark.parametrize("types, value, match", [
    ({"A": [{"name": "x", "type": "uint8"}], "B": [{"name": "y", "type": "uint8"}]},
     {"x": 1}, "ambiguous primary"),
    ({"A": [{"name": "b", "type": "B"}], "B": [{"name": "a", "type": "A"}],
      "P": [{"name": "a", "type": "A"}]}, {}, "circular"),
    ({"A": [{"name": "x", "type": "uint8"}]}, {"x": 256}, "out of bounds"),
    ({"A": [{"name": "x", "type": "address"}]}, {"x": "TNotAnAddress"}, "not a Tron address"),
    ({"A": [{"name": "x", "type": "Missing"}]}, {"x": 1}, "unknown type"),
    ({"A": [{"name": "x", "type": "bytes4"}]}, {"x": "0x01"}, "invalid length"),
    ({"A": [{"name": "x", "type": "uint8"}]}, {}, "missing value"),
])
def test_malformed_typed_data_is_refused(types, value, match):
    with pytest.raises(TypedDataError, match=match):
        typed_data_digest({"chainId": 1}, types, value)


# --- signing workers ------------------------------------------------------------------

class _KeySigner:
    """Signs like a hot wallet (optionally with the WRONG key)."""

    def __init__(self, priv=PRIV):
        self.priv = keys.PrivateKey(priv)

    def sign_tron(self, req):
        return signature_v27(self.priv.sign_msg_hash(req.tx.txid()).to_bytes())

    def sign_tron_message(self, req):
        return signature_v27(self.priv.sign_msg_hash(req.digest()).to_bytes())


def _tx(owner=ACCOUNT, contract=None, **kw) -> TronTx:
    return TronTx(contract or TransferContract(owner, TO, 1_500_000),
                  bytes.fromhex("6594"), bytes.fromhex("f5d4c2b3f4151356"),
                  expiration=kw.pop("expiration", 1790272989000 + 60_000),
                  timestamp=1790272989000, **kw)


def _collect(worker):
    out: dict = {}
    worker.signed.connect(lambda s: out.update(signed=s))
    worker.failed.connect(lambda m: out.update(failed=m))
    worker.run()
    return out


def test_message_worker_signs_tron_requests_and_checks_the_key():
    req = TronMessageSigningRequest(ACCOUNT, b"hi", 2)
    out = _collect(SignMessageWorker(_KeySigner(), req))
    assert out == {"signed": _sig(req.digest())}
    td = TronTypedDataSigningRequest(ACCOUNT, DOMAIN, TYPES, VALUE)
    assert _collect(SignMessageWorker(_KeySigner(), td)) == {"signed": _sig(td.digest())}
    wrong = _collect(SignMessageWorker(_KeySigner(b"\x07" * 32), req))
    assert "doesn't match" in wrong["failed"]


def test_tron_sign_worker_returns_tronwebs_signature_form():
    req = TronSigningRequest(TRON_CHAIN.chain_id, _tx())
    out = _collect(TronSignWorker(_KeySigner(), req))
    assert out["signed"] == _sig(req.tx.txid())[2:]          # plain hex, v = 1b/1c
    assert "doesn't match" in _collect(
        TronSignWorker(_KeySigner(b"\x07" * 32), req))["failed"]


def test_hot_wallet_signs_tron_messages(tmp_qeth):
    from qeth import hot_wallet
    from qeth.hot_wallet import HotWalletSigner
    store = SimpleNamespace(accounts=[{"address": ACCOUNT, "source": "hot"}])
    signer = HotWalletSigner(store, unlocked=(ACCOUNT, PRIV))
    hot_wallet.KEYSTORE_DIR.mkdir(parents=True, exist_ok=True)
    hot_wallet.keystore_path(ACCOUNT).write_text("{}")
    req = TronMessageSigningRequest(ACCOUNT, "hello qeth ✓".encode(), 2)
    assert "0x" + signer.sign_tron_message(req).hex() == _sig(req.digest())


def test_other_signers_refuse_tron_messages():
    from qeth.ledger import LedgerSigner
    with pytest.raises(SignerError, match="only transactions"):
        LedgerSigner(SimpleNamespace(accounts=[])).sign_tron_message(
            TronMessageSigningRequest(ACCOUNT, b"x"))


# --- the RPC server ---------------------------------------------------------------------

class _Bridge:
    def __init__(self, answer=None, error=None):
        self.requests: list = []
        self.seen: list = []
        self._answer, self._error = answer, error
        self.tron_broadcast_seen = SimpleNamespace(
            emit=lambda req, raw: self.seen.append((req, raw)))

    async def submit_async(self, req):
        self.requests.append(req)
        if self._error:
            raise SignerError(self._error)
        if self._answer is not None:
            return self._answer
        digest = req.tx.txid() if isinstance(req, TronSigningRequest) else req.digest()
        sig = _sig(digest)
        return sig[2:] if isinstance(req, TronSigningRequest) else sig


def _server(*, tron=ACCOUNT, chains=None, bridge=None, api_url=None):
    tron_chain = dataclasses.replace(TRON_CHAIN, api_url=api_url or TRON_CHAIN.api_url)
    eth = Chain("Ethereum", 1, "http://127.0.0.1:9/", "ETH", "")
    store = SimpleNamespace(
        chains=chains if chains is not None else [eth, tron_chain],
        default_account="0x" + "11" * 20, current_chain=lambda: eth,
        dapp_chain=lambda: eth,
        default_for=lambda fam: ({EVM: "0x" + "11" * 20, TRON: tron}.get(fam), None))
    return RpcServer(store, signer_bridge=bridge)


def _call(server, method, params=(), origin="https://dapp.example"):
    async def go():
        server._client = ClientSession()
        try:
            return await server._dispatch(method, list(params), origin=origin)
        finally:
            await server._client.close()
    return asyncio.run(go())


def test_accounts_and_network():
    s = _server()
    assert _call(s, "tron_accounts") == [ACCOUNT_B58]
    assert _call(s, "tron_requestAccounts") == [ACCOUNT_B58]
    assert _call(_server(tron=None), "tron_accounts") == []
    assert _call(s, "tron_network") == {
        "chainId": "0x2b6653dc", "name": "Tron", "fullHost": "https://api.trongrid.io"}
    with pytest.raises(RpcError) as e:
        _call(_server(chains=[]), "tron_accounts")
    assert e.value.code == 4901


def test_an_unknown_tron_method_never_reaches_the_evm_node():
    s = _server()
    s._proxy = MagicMock(side_effect=AssertionError("proxied"))
    with pytest.raises(RpcError) as e:
        _call(s, "tron_getSomething")
    assert e.value.code == -32601


def _wire(tx: TronTx, **extra) -> dict:
    raw = tx.raw_data()
    return {"txID": hashlib.sha256(raw).hexdigest(), "raw_data_hex": raw.hex(),
            "raw_data": {"contract": []}, "visible": False, **extra}


class TestSignTransaction:
    def test_signs_the_dapps_exact_bytes(self):
        bridge = _Bridge()
        s = _server(bridge=bridge)
        tx = _tx(contract=TriggerSmartContract(
            ACCOUNT, "0x" + "a6" * 20, bytes.fromhex("a9059cbb") + bytes(64)), fee_limit=10**8)
        sig = _call(s, "tron_signTransaction", [_wire(tx)])
        [req] = bridge.requests
        assert isinstance(req, TronSigningRequest)
        assert req.tx == tx and req.origin == "https://dapp.example"
        assert recover_signer(tx.txid(), bytes.fromhex(sig)).lower() == ACCOUNT
        # Remembered, to recognise the dapp's broadcast.
        assert tx.txid().hex() in s._tron_signed

    @pytest.mark.parametrize("wire, code, match", [
        (lambda tx: _wire(tx, signature=["00"]), -32602, "already signed"),
        (lambda tx: {**_wire(tx), "txID": "00" * 32}, -32602, "txID"),
        (lambda tx: {"raw_data_hex": "zz"}, -32602, "raw_data_hex"),
        # ref_block_num (field 3) decodes but wouldn't re-encode: not shown → refused.
        (lambda tx: _wire_raw(bytes.fromhex("1801") + tx.raw_data()), -32602,
         "doesn't show"),
        (lambda tx: _wire(_tx(owner="0x" + "44" * 20)), 4100, "isn't from"),
    ])
    def test_refusals(self, wire, code, match):
        bridge = _Bridge()
        with pytest.raises(RpcError, match=match) as e:
            _call(_server(bridge=bridge), "tron_signTransaction", [wire(_tx())])
        assert e.value.code == code
        assert bridge.requests == []                  # no dialog for any of them

    def test_cancel_is_a_user_rejection(self):
        with pytest.raises(RpcError) as e:
            _call(_server(bridge=_Bridge(error="User cancelled")),
                  "tron_signTransaction", [_wire(_tx())])
        assert e.value.code == 4001

    def test_needs_a_signer(self):
        with pytest.raises(RpcError) as e:
            _call(_server(), "tron_signTransaction", [_wire(_tx())])
        assert e.value.code == -32601


def _wire_raw(raw: bytes) -> dict:
    return {"txID": hashlib.sha256(raw).hexdigest(), "raw_data_hex": raw.hex()}


def test_sign_message_forms():
    bridge = _Bridge()
    s = _server(bridge=bridge)
    sig = _call(s, "tron_signMessage", ["0x" + b"hi".hex(), 2, ACCOUNT_B58])
    req = bridge.requests[-1]
    assert isinstance(req, TronMessageSigningRequest) and (req.raw, req.version) == (b"hi", 2)
    assert sig == _sig(req.digest())
    _call(s, "tron_signMessage", ["abcd", 1])
    assert (bridge.requests[-1].raw, bridge.requests[-1].version) == (b"\xab\xcd", 1)
    with pytest.raises(RpcError) as e:
        _call(s, "tron_signMessage", ["0x00", 3])
    assert e.value.code == -32602
    with pytest.raises(RpcError) as e:
        _call(s, "tron_signMessage", ["0x00", 2, tron_from_hex("0x" + "44" * 20)])
    assert e.value.code == 4100


def test_sign_typed_data_checks_before_any_dialog():
    bridge = _Bridge()
    s = _server(bridge=bridge)
    assert _call(s, "tron_signTypedData", [DOMAIN, TYPES, VALUE]) == \
        _sig(typed_data_digest(DOMAIN, TYPES, VALUE))
    for bad in ([{**DOMAIN, "chainId": 1}, TYPES, VALUE],
                [DOMAIN, {"A": [{"name": "x", "type": "Nope"}]}, {"x": 1}]):
        with pytest.raises(RpcError) as e:
            _call(s, "tron_signTypedData", bad)
        assert e.value.code == -32602
    assert len(bridge.requests) == 1


class TestNodeProxy:
    def _node(self, *, fail_first=False):
        seen: list = []

        async def handler(request: web.Request):
            body = await request.json() if request.can_read_body else {}
            seen.append((request.path, dict(request.query), body))
            if request.path == "/wallet/broadcasttransaction":
                return web.json_response({"result": True, "txid": body.get("txID")})
            if request.path == "/wallet/broadcasthex":
                return web.json_response({"result": True})
            return web.json_response({"path": request.path})

        async def down(request):
            return web.Response(status=503)
        good = web.Application()
        good.router.add_route("*", "/{tail:.*}", handler)
        bad = web.Application()
        bad.router.add_route("*", "/{tail:.*}", down)
        return seen, TestServer(good), TestServer(bad) if fail_first else None

    def _run(self, fn, *, fail_first=False, bridge=None):
        seen, good, bad = self._node(fail_first=fail_first)

        async def go():
            await good.start_server()
            if bad is not None:
                await bad.start_server()
            base = str(good.make_url("")).rstrip("/")
            chain = dataclasses.replace(
                TRON_CHAIN, api_url=str(bad.make_url("")).rstrip("/") if bad else base,
                api_fallbacks=(base,) if bad else ())
            eth = Chain("Ethereum", 1, "http://127.0.0.1:9/", "ETH", "")
            store = SimpleNamespace(
                chains=[eth, chain], dapp_chain=lambda: eth,
                default_for=lambda fam: (ACCOUNT if fam == TRON else None, None))
            server = RpcServer(store, signer_bridge=bridge)
            server._client = ClientSession()
            try:
                return await fn(server)
            finally:
                await server._client.close()
                await good.close()
                if bad is not None:
                    await bad.close()
        return asyncio.run(go()), seen

    def test_get_and_post_reach_the_node(self):
        async def fn(s):
            a = await s._dispatch("tron_node", ["walletsolidity/getaccount",
                                                {"address": "41ab", "visible": True}, "get"])
            b = await s._dispatch("tron_node", ["/wallet/getnowblock", {}, "post"])
            return a, b
        (a, b), seen = self._run(fn)
        assert a == {"path": "/walletsolidity/getaccount"}
        assert b == {"path": "/wallet/getnowblock"}
        assert seen[0][1] == {"address": "41ab", "visible": "true"}

    def test_fails_over_to_the_next_node(self):
        async def fn(s):
            return await s._dispatch("tron_node", ["wallet/getnowblock", {}, "post"])
        out, seen = self._run(fn, fail_first=True)
        assert out == {"path": "/wallet/getnowblock"} and len(seen) == 1

    @pytest.mark.parametrize("path", [
        "wallet/../admin", "admin/shutdown", "jsonrpc", "wallet/get.node",
        "v1/accounts/../x", "wallet/getnowblock?x=1", "v1/x?a=<script>",
        "https://evil.example/wallet/getnowblock",
    ])
    def test_only_tron_api_paths(self, path):
        async def fn(s):
            with pytest.raises(RpcError) as e:
                await s._dispatch("tron_node", [path, {}, "get"])
            return e.value.code
        code, seen = self._run(fn)
        assert code == -32601 and seen == []

    def test_a_signed_transactions_broadcast_is_noticed(self):
        bridge = _Bridge()
        tx = _tx()

        async def fn(s):
            sig = await s._dispatch("tron_signTransaction", [_wire(tx)], origin="https://d.example")
            signed = signed_transaction(tx.raw_data(), [bytes.fromhex(sig)])
            await s._dispatch("tron_node", ["wallet/broadcasthex",
                                            {"transaction": signed.hex()}, "post"])
            return signed
        signed, _ = self._run(fn, bridge=bridge)
        [(req, raw)] = bridge.seen
        assert req.tx == tx and raw == "0x" + signed.hex()

    def test_the_json_broadcast_form_too_and_only_once(self):
        bridge = _Bridge()
        tx = _tx()

        async def fn(s):
            sig = await s._dispatch("tron_signTransaction", [_wire(tx)])
            body = {**_wire(tx), "signature": [sig]}
            for _ in range(2):          # a dapp re-broadcasting: one pending row
                await s._dispatch("tron_node", ["wallet/broadcasttransaction", body, "post"])
        self._run(fn, bridge=bridge)
        assert len(bridge.seen) == 1


def test_accounts_changed_is_a_wallet_subscription():
    from qeth import rpc
    assert "tronAccountsChanged" in rpc._WALLET_SUBSCRIPTIONS
    s = _server()
    pushed = []
    s._schedule_event = lambda ev, data, **kw: pushed.append((ev, data))
    s.broadcast_tron_accounts_changed([ACCOUNT_B58])
    assert pushed == [("tronAccountsChanged", [ACCOUNT_B58])]


# --- the review dialogs -------------------------------------------------------------------

def _dapp_dialog(qtbot, tx: TronTx, origin="https://dapp.example"):
    import qeth.plugins.transactions as txp
    started: list = []
    d = txp.TronSignTransactionDialog(
        TronSigningRequest(TRON_CHAIN.chain_id, tx, origin), TRON_CHAIN,
        abi_source=None, abi_cache=MagicMock(), start_worker=started.append,
        known_addresses=[(ACCOUNT, "me")])
    qtbot.addWidget(d)
    d.started = started
    return d


def test_dapp_dialog_is_the_sign_dialog_read_only(qtbot):
    from qeth.plugins.transactions import SignTransactionDialog
    from qeth.plugins.transactions.tron_send import TronFeeWorker
    approve = (bytes.fromhex("095ea7b3") + bytes(11) + b"\x41" + bytes.fromhex("55" * 20)
               + (2**256 - 1).to_bytes(32, "big"))
    tx = _tx(contract=TriggerSmartContract(ACCOUNT, "0x" + "a6" * 20, approve),
             fee_limit=5_000_000, memo=b"order #7")
    d = _dapp_dialog(qtbot, tx)
    assert isinstance(d, SignTransactionDialog)
    assert d._allow_edit is None                      # an approve, but not editable
    # The decode sees the 41-prefixed address word normalised.
    assert d.req.data == "0x095ea7b3" + "00" * 12 + "55" * 20 + "ff" * 32
    # The fee estimate is for the dapp's exact contract, memo included.
    [fee_worker] = [w for w in d.started if isinstance(w, TronFeeWorker)]
    assert fee_worker._contract == tx.contract and fee_worker._memo == b"order #7"
    assert d._expiry_lbl.text().startswith("⚠ expired")   # 2026 timestamps are past


def test_dapp_dialog_uses_the_dapps_fee_limit_and_warns(qtbot):
    from qeth.tron.fees import TronFee
    tx = _tx(contract=TriggerSmartContract(ACCOUNT, "0x" + "a6" * 20, b"\x01\x02\x03\x04"),
             fee_limit=5_000_000)
    d = _dapp_dialog(qtbot, tx)
    d._on_tron_estimated(TronFee(bandwidth=300, bandwidth_burn=0, energy=64285,
                                 energy_burn=6_428_500, fee_limit=9_642_751), 10**9)
    text = d._fee_limit_lbl.text()
    assert text.startswith("5 TRX — set by the dapp")
    assert "exceeds it" in text


def test_dapp_dialog_counts_down_the_expiration(qtbot):
    import time
    d = _dapp_dialog(qtbot, _tx(expiration=int(time.time() * 1000) + 45_000))
    assert d._expiry_lbl.text() in ("in 44 s", "in 45 s")


def test_message_dialog_names_tron(qtbot):
    from qeth.plugins.sign_message import SignMessageDialog
    d = SignMessageDialog(TronMessageSigningRequest(ACCOUNT, b"hello", 2))
    qtbot.addWidget(d)
    assert d.windowTitle() == "Sign Tron Message"
    from PySide6.QtWidgets import QLabel
    labels = [lbl.text() for lbl in d.findChildren(QLabel)]
    assert ACCOUNT_B58 in labels and "Tron" in labels
    assert "signMessageV2 (TIP-191)" in labels
    td = SignMessageDialog(TronTypedDataSigningRequest(ACCOUNT, DOMAIN, TYPES, VALUE))
    qtbot.addWidget(td)
    assert td.windowTitle() == "Sign Typed Data (TIP-712)"


# --- the UI routing -------------------------------------------------------------------------

def test_dapp_requests_open_the_tron_dialogs(mainwindow, monkeypatch):
    import qeth.plugins.transactions as txp
    from qeth.plugins import sign_message
    opened, started = [], []
    # The dialog's fee / simulation workers would reach real Tron nodes.
    monkeypatch.setattr(mainwindow, "start_worker", started.append)
    monkeypatch.setattr(txp.TronSignTransactionDialog, "show", lambda self: opened.append(self))
    monkeypatch.setattr(sign_message.SignMessageDialog, "show", lambda self: opened.append(self))
    mainwindow._launch_signing_dialog(
        TronSigningRequest(TRON_CHAIN.chain_id, _tx(), "https://d.example"), MagicMock())
    mainwindow._launch_signing_dialog(TronMessageSigningRequest(ACCOUNT, b"x"), MagicMock())
    assert isinstance(opened[0], txp.TronSignTransactionDialog)
    assert isinstance(opened[1], sign_message.SignMessageDialog)


def test_a_tron_connect_is_pushed_to_the_tron_provider(mainwindow, monkeypatch):
    pushed = []
    monkeypatch.setattr(mainwindow.rpc, "broadcast_tron_accounts_changed", pushed.append)
    evm_pushed = []
    monkeypatch.setattr(mainwindow.rpc, "broadcast_accounts_changed", evm_pushed.append)
    mainwindow.store.accounts.append({"address": ACCOUNT, "source": "hot", "label": ""})
    mainwindow.store.set_default_account(ACCOUNT, family=TRON)
    mainwindow._push_accounts_changed()
    mainwindow._push_accounts_changed()               # no change → no second push
    assert pushed == [[ACCOUNT_B58]]


def test_a_seen_dapp_broadcast_becomes_a_pending_row(mainwindow, monkeypatch):
    added = []
    monkeypatch.setattr(mainwindow.transactions_plugin, "add_tron_pending",
                        lambda *a: added.append(a))
    req = TronSigningRequest(TRON_CHAIN.chain_id, _tx())
    mainwindow._on_tron_dapp_broadcast(req, "0xabcd")
    [(tx_hash, r, chain, raw)] = added
    assert tx_hash == "0x" + req.tx.txid().hex() and raw == "0xabcd"
    assert chain.chain_id == TRON_CHAIN.chain_id
