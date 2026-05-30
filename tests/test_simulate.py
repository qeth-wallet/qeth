"""Hermetic tests for qeth.simulate — the log-extraction logic.

A fake EVM class is injected so these never fork a real chain. The live
pyrevm-against-mainnet path is exercised manually (it's slow + networked).
"""

from types import SimpleNamespace

from qeth.simulate import simulate_logs

CHAIN = SimpleNamespace(chain_id=1, rpc_url="https://rpc.example/eth")
FROM = "0x7a16ff8270133f063aab6c9977183d9e72835428"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class _FakeLog:
    def __init__(self, address, topics, data_bytes):
        self.address = address
        self.topics = topics
        # pyrevm shape: .data is a (topics, data_bytes) tuple.
        self.data = (topics, data_bytes)


class _FakeEVM:
    """Records construction + the message_call, returns one Transfer."""
    seen: dict = {}

    def __init__(self, fork_url=None):
        _FakeEVM.seen["fork_url"] = fork_url

    def message_call(self, **kwargs):
        _FakeEVM.seen["call"] = kwargs
        self.result = SimpleNamespace(logs=[
            _FakeLog(USDC, [TRANSFER, "0x" + "00" * 31 + "01"],
                     b"\x00" * 31 + b"\x05"),
        ])


def test_returns_decode_ready_log_dicts():
    _FakeEVM.seen = {}
    logs = simulate_logs(CHAIN, FROM, USDC, "0xa9059cbb", 0, evm_cls=_FakeEVM)
    assert _FakeEVM.seen["fork_url"] == CHAIN.rpc_url
    assert len(logs) == 1
    lg = logs[0]
    assert lg["address"] == USDC
    assert lg["topics"][0] == TRANSFER
    assert lg["data"] == "0x" + "00" * 31 + "05"


def test_calldata_and_addresses_are_normalised():
    _FakeEVM.seen = {}
    simulate_logs(CHAIN, FROM, USDC, "0xa9059cbb00ff", 0, evm_cls=_FakeEVM)
    call = _FakeEVM.seen["call"]
    assert call["calldata"] == bytes.fromhex("a9059cbb00ff")
    # web3/pyrevm want checksum addresses — the lowercased inputs are fixed.
    assert call["caller"].lower() == FROM
    assert call["caller"] != FROM           # i.e. it got checksummed
    assert "value" not in call               # zero value omitted


def test_value_is_passed_when_nonzero():
    _FakeEVM.seen = {}
    simulate_logs(CHAIN, FROM, USDC, "0x", 10**18, evm_cls=_FakeEVM)
    assert _FakeEVM.seen["call"]["value"] == 10**18


def test_contract_creation_returns_none():
    assert simulate_logs(CHAIN, FROM, None, "0x", 0, evm_cls=_FakeEVM) is None


def test_simulation_error_returns_none():
    class _Boom:
        def __init__(self, fork_url=None): pass
        def message_call(self, **kw): raise RuntimeError("revm exploded")
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0, evm_cls=_Boom) is None
