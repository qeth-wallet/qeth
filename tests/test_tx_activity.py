"""Unit tests for tx_activity.transfer_legs_from_logs — turning a tx's
event logs (receipt or pre-broadcast simulation) into the ERC-20 contracts
the viewer sent / received, which the Activity column folds in so a swap's
coins show before Blockscout indexes the transfers."""

from qeth.tx_activity import TRANSFER_TOPIC0, transfer_legs_from_logs

VIEWER = "0x7a16ff8270133f063aab6c9977183d9e72835428"
OTHER = "0x" + "11" * 20
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WBTC = "0x" + "22" * 20


def _transfer(token: str, frm: str, to: str) -> dict:
    """An ERC-20 Transfer log: addresses are 32-byte left-padded topics."""
    return {
        "topics": [
            TRANSFER_TOPIC0,
            "0x" + "00" * 12 + frm[2:].lower(),
            "0x" + "00" * 12 + to[2:].lower(),
        ],
        "data": "0x" + "00" * 31 + "64",   # value (irrelevant to legs)
        "address": token,
    }


def test_received_token_is_an_in_leg():
    assert transfer_legs_from_logs([_transfer(USDC, OTHER, VIEWER)], VIEWER) \
        == ([], [USDC])


def test_sent_token_is_an_out_leg():
    assert transfer_legs_from_logs([_transfer(USDC, VIEWER, OTHER)], VIEWER) \
        == ([USDC], [])


def test_swap_shows_both_sides():
    logs = [_transfer(USDC, VIEWER, OTHER), _transfer(WBTC, OTHER, VIEWER)]
    assert transfer_legs_from_logs(logs, VIEWER) == ([USDC], [WBTC])


def test_duplicates_collapse_and_untouched_ignored():
    other2 = "0x" + "33" * 20
    logs = [
        _transfer(USDC, OTHER, VIEWER),
        _transfer(USDC, OTHER, VIEWER),     # same token again → deduped
        _transfer(WBTC, OTHER, other2),     # never touches the viewer
    ]
    assert transfer_legs_from_logs(logs, VIEWER) == ([], [USDC])


def test_viewer_match_is_case_insensitive():
    out, inn = transfer_legs_from_logs(
        [_transfer(USDC, OTHER, VIEWER)], VIEWER.upper())
    assert inn == [USDC]


def test_non_transfer_logs_skipped():
    log = {
        "topics": ["0x" + "ab" * 32, "0x" + "00" * 32, "0x" + "00" * 32],
        "data": "0x",
        "address": USDC,
    }
    assert transfer_legs_from_logs([log], VIEWER) == ([], [])


def test_empty_or_none_logs():
    assert transfer_legs_from_logs(None, VIEWER) == ([], [])
    assert transfer_legs_from_logs([], VIEWER) == ([], [])


# --- pass 0: the method verb paints before the (slow) coins fetch -----------

def _tx(contract, method_id="0xa9059cbb"):
    from qeth.transactions import Transaction
    return Transaction(
        chain_id=1, hash="0x" + "cd" * 32, block_number=100, timestamp=0,
        nonce=0, from_addr=VIEWER, to_addr=contract, value_wei=0,
        gas_used=0, gas_price_wei=0, method_id=method_id, input_data="0x",
        success=True, pending=True,
    )


_TRANSFER_ABI = [{
    "type": "function", "name": "transfer",
    "inputs": [{"name": "_to", "type": "address"},
               {"name": "_value", "type": "uint256"}],
    "outputs": [],
}]


def test_verb_emitted_before_coins_fetch(tmp_path, monkeypatch):
    """A cached-ABI method label needs no network, so it must paint BEFORE the
    tokentx/internal fetch — otherwise a just-created pending tx shows a blank
    method until that (possibly slow) round-trip returns. Regression for the
    'method appears only after navigating away and back' report."""
    import qeth.tx_activity as ta
    from qeth.abi_cache import AbiCache
    from qeth.chains import DEFAULT_CHAINS

    chain = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
    contract = "0x" + "ab" * 20
    cache = AbiCache(root=tmp_path)
    cache.save(1, contract, _TRANSFER_ABI)

    order: list[str] = []

    def slow_rows(*a, **k):
        order.append("coins_fetch")        # stands in for the slow network call
        return []
    monkeypatch.setattr(ta, "_account_rows", slow_rows)

    batches: list[dict] = []

    def on_batch(b):
        order.append("emit")
        batches.append(b)

    tx = _tx(contract)
    ta.fetch_activities(chain, VIEWER, [tx], abi_cache=cache, on_batch=on_batch)

    # the verb was emitted before the coins fetch ran...
    assert order[0] == "emit"
    assert order.index("emit") < order.index("coins_fetch")
    # ...carrying the decoded method name, not a blank or bare selector
    assert batches[0][tx.hash].verb == "transfer"


def test_pass0_emits_known_verb_but_not_cold_placeholder(tmp_path, monkeypatch):
    """Pass 0 emits only verbs resolvable without network: a cached-ABI tx is
    named immediately, but a cold-ABI tx must NOT get a bare-selector
    placeholder there (that risks persisting a non-final row) — it waits for
    the pass-2 fetch."""
    import qeth.tx_activity as ta
    from qeth.abi_cache import AbiCache
    from qeth.chains import DEFAULT_CHAINS
    from qeth.transactions import Transaction

    chain = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
    known_c = "0x" + "ab" * 20
    cold_c = "0x" + "ef" * 20
    cache = AbiCache(root=tmp_path)
    cache.save(1, known_c, _TRANSFER_ABI)           # only the known one cached
    monkeypatch.setattr(ta, "_account_rows", lambda *a, **k: [])
    monkeypatch.setattr(ta._Verbs, "resolve", lambda self, c: {})   # no network

    known = _tx(known_c)
    cold = Transaction(
        chain_id=1, hash="0x" + "ee" * 32, block_number=100, timestamp=0,
        nonce=1, from_addr=VIEWER, to_addr=cold_c, value_wei=0, gas_used=0,
        gas_price_wei=0, method_id="0xdeadbeef", input_data="0x", success=True)

    batches: list[dict] = []
    ta.fetch_activities(chain, VIEWER, [known, cold], abi_cache=cache,
                        on_batch=batches.append)

    # First (pass-0) batch: the cached verb, and NOT the cold tx.
    assert batches[0][known.hash].verb == "transfer"
    assert cold.hash not in batches[0]
