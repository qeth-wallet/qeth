"""Pure-logic tests for approvals discovery (no Qt, no network)."""

from qeth.plugins.approvals.discovery import approve_pairs_in, spender_of
from qeth.transactions import Transaction

A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
TOKEN = "0x" + "cc" * 20
SPENDER = "0x" + "ee" * 20


def _approve_data(spender, amount=0, sel="0x095ea7b3"):
    return sel + spender[2:].lower().rjust(64, "0") + format(amount, "064x")


def _tx(frm, to, data, nonce=0):
    return Transaction(
        chain_id=1, hash="0x" + format(nonce, "064x"), block_number=100 + nonce,
        timestamp=100 + nonce, nonce=nonce, from_addr=frm, to_addr=to,
        value_wei=0, gas_used=0, gas_price_wei=0,
        method_id=data[:10], input_data=data, success=True)


def test_spender_of_decodes_word1():
    assert spender_of(_approve_data(SPENDER)).lower() == SPENDER.lower()


def test_spender_of_malformed_returns_none():
    assert spender_of(None) is None
    assert spender_of("0x095ea7b3") is None
    assert spender_of("0x095ea7b3" + "zz" * 32) is None


def test_own_approve_is_a_pair():
    pairs = approve_pairs_in([_tx(A, TOKEN, _approve_data(SPENDER))], A)
    assert pairs == {(TOKEN.lower(), SPENDER.lower())}


def test_received_approve_ignored():
    # sent by someone else (from_addr != A) → not A's approval
    assert approve_pairs_in([_tx(B, TOKEN, _approve_data(SPENDER))], A) == set()


def test_increase_allowance_decoded():
    pairs = approve_pairs_in(
        [_tx(A, TOKEN, _approve_data(SPENDER, sel="0x39509351"))], A)
    assert pairs == {(TOKEN.lower(), SPENDER.lower())}


def test_non_approve_call_ignored():
    # a plain transfer (0xa9059cbb) is not an approval
    assert approve_pairs_in([_tx(A, TOKEN, "0xa9059cbb" + "00" * 64)], A) == set()


def test_dedupes_repeated_pair():
    txs = [_tx(A, TOKEN, _approve_data(SPENDER), nonce=1),
           _tx(A, TOKEN, _approve_data(SPENDER), nonce=2)]
    assert approve_pairs_in(txs, A) == {(TOKEN.lower(), SPENDER.lower())}


# --- approval_pairs_from_logs (refresh on confirmed approve) ---------------

from qeth.plugins.approvals.discovery import (  # noqa: E402
    _APPROVAL_TOPIC0, approval_pairs_from_logs,
)


def _approval_log(token, owner, spender, topic0=_APPROVAL_TOPIC0):
    return {"address": token,
            "topics": [topic0, "0x" + "00" * 12 + owner[2:],
                       "0x" + "00" * 12 + spender[2:]]}


def test_approval_pairs_from_own_approval():
    logs = [_approval_log(TOKEN, A, SPENDER)]
    assert approval_pairs_from_logs(logs, A) == {(TOKEN.lower(), SPENDER.lower())}


def test_approval_pairs_ignores_other_owner():
    logs = [_approval_log(TOKEN, B, SPENDER)]           # someone else's approval
    assert approval_pairs_from_logs(logs, A) == set()


def test_approval_pairs_ignores_non_approval_topic():
    logs = [_approval_log(TOKEN, A, SPENDER, topic0="0x" + "de" * 32)]
    assert approval_pairs_from_logs(logs, A) == set()


def test_approval_pairs_empty_logs():
    assert approval_pairs_from_logs([], A) == set()
    assert approval_pairs_from_logs(None, A) == set()


# --- approval_pairs_from_log_rows (explorer module=logs rows) ---------------

from qeth.plugins.approvals.discovery import (  # noqa: E402
    approval_pairs_from_log_rows,
)


def _log_row(token, owner, spender, block, *, extra_topic=False,
             topic0=_APPROVAL_TOPIC0):
    topics = [topic0, "0x" + "00" * 12 + owner[2:], "0x" + "00" * 12 + spender[2:]]
    if extra_topic:                          # ERC-721: tokenId as a 4th topic
        topics.append("0x" + "00" * 31 + "01")
    return {"address": token, "topics": topics, "blockNumber": hex(block)}


def test_log_rows_extract_pairs_and_max_block():
    C = "0x" + "dd" * 20
    rows = [_log_row(TOKEN, A, SPENDER, 100),
            _log_row(C, A, B, 250)]
    pairs, max_block = approval_pairs_from_log_rows(rows, A)
    assert pairs == {(TOKEN.lower(), SPENDER.lower()), (C.lower(), B.lower())}
    assert max_block == 250


def test_log_rows_skip_erc721_four_topic_approval():
    # A 4-topic Approval is an ERC-721 NFT approval, not an ERC-20 allowance.
    rows = [_log_row(TOKEN, A, SPENDER, 100, extra_topic=True)]
    pairs, max_block = approval_pairs_from_log_rows(rows, A)
    assert pairs == set()
    assert max_block == 100          # still advances the cursor past this block


def test_log_rows_skip_other_owner_and_wrong_topic():
    rows = [_log_row(TOKEN, B, SPENDER, 100),                       # other owner
            _log_row(TOKEN, A, SPENDER, 110, topic0="0x" + "de" * 32)]  # not Approval
    pairs, max_block = approval_pairs_from_log_rows(rows, A)
    assert pairs == set()
    # Neither row is a kept pair, but both were returned by the explorer for
    # this window → the cursor advances past the highest so a resume can't
    # re-fetch them.
    assert max_block == 110


def test_log_rows_blockscout_none_padded_topics():
    # Blockscout pads the topics array to 4 slots with a trailing None for a
    # 3-topic Approval — the parser must treat that as ERC-20, not ERC-721.
    row = {"address": TOKEN,
           "topics": [_APPROVAL_TOPIC0, "0x" + "00" * 12 + A[2:],
                      "0x" + "00" * 12 + SPENDER[2:], None],
           "blockNumber": hex(300)}
    pairs, max_block = approval_pairs_from_log_rows([row], A)
    assert pairs == {(TOKEN.lower(), SPENDER.lower())}
    assert max_block == 300


def test_log_rows_empty():
    assert approval_pairs_from_log_rows([], A) == (set(), 0)
    assert approval_pairs_from_log_rows(None, A) == (set(), 0)


# --- ApprovalLogSource (fake transport, no network) ------------------------

import json  # noqa: E402

from qeth.chains import DEFAULT_CHAINS  # noqa: E402
from qeth.transactions import (  # noqa: E402
    _APPROVAL_TOPIC0 as SRC_TOPIC0,
    ApprovalLogSource,
)

ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)


def test_source_topic0_matches_discovery_constant():
    # The source and the parser must agree on the Approval signature hash.
    assert SRC_TOPIC0 == _APPROVAL_TOPIC0


def test_source_builds_owner_topic_and_returns_rows():
    captured = {}

    def fake_transport(url, timeout):
        captured["url"] = url
        return json.dumps({"status": "1", "message": "OK", "result": [
            _log_row(TOKEN, A, SPENDER, 100)]}).encode()

    # keyless → Blockscout instance path
    src = ApprovalLogSource(lambda: None, transport=fake_transport)
    rows = src.fetch(ETH, A, from_block=42)
    assert len(rows) == 1
    # owner is filtered as the padded topic1, and the from_block is passed.
    assert ("topic1=0x" + "0" * 24 + A[2:].lower()) in captured["url"]
    assert "fromBlock=42" in captured["url"]
    assert "action=getLogs" in captured["url"]


def test_source_empty_result_is_end_not_error():
    def fake_transport(url, timeout):
        return json.dumps({"status": "0", "message": "No logs found",
                           "result": []}).encode()

    src = ApprovalLogSource(lambda: None, transport=fake_transport)
    assert src.fetch(ETH, A) == []


def test_source_real_error_raises():
    import pytest
    from qeth.transactions import TransactionSourceError

    def fake_transport(url, timeout):
        return json.dumps({"status": "0", "message": "NOTOK",
                           "result": "Invalid API Key"}).encode()

    src = ApprovalLogSource(lambda: "key", transport=fake_transport)
    with pytest.raises(TransactionSourceError):
        src.fetch(ETH, A)


def test_source_uses_etherscan_when_key_and_supported():
    captured = {}

    def fake_transport(url, timeout):
        captured["url"] = url
        # A non-empty Etherscan response is used directly (no fallback).
        return json.dumps({"status": "1", "result": [
            _log_row(TOKEN, A, SPENDER, 100)]}).encode()

    src = ApprovalLogSource(lambda: "APIKEY", transport=fake_transport)
    rows = src.fetch(ETH, A)
    assert len(rows) == 1
    assert "apikey=APIKEY" in captured["url"]     # served by Etherscan, keyed
    assert "chainid=1" in captured["url"]


def test_source_fails_over_from_etherscan_to_blockscout():
    # A keyed chain: Etherscan errors → fall back to the keyless Blockscout
    # instance (the proven logs path) rather than getting stuck.
    calls = []

    def fake_transport(url, timeout):
        calls.append(url)
        if "etherscan" in url:                    # Etherscan errors
            return json.dumps({"status": "0", "message": "NOTOK",
                               "result": "Query Timeout"}).encode()
        return json.dumps({"status": "1", "result": [   # Blockscout serves it
            _log_row(TOKEN, A, SPENDER, 100)]}).encode()

    src = ApprovalLogSource(lambda: "APIKEY", transport=fake_transport)
    rows = src.fetch(ETH, A)
    assert len(rows) == 1                          # got Blockscout's row
    assert any("etherscan" in u for u in calls)    # tried Etherscan first
    assert any("blockscout" in u for u in calls)   # then fell back


def test_source_cold_scan_empty_primary_tries_fallback():
    # Etherscan returns a VALID empty (no error) on a cold scan — a known
    # silent-failure mode for topic-only queries. A cold scan must still try
    # Blockscout rather than trust the empty.
    calls = []

    def fake_transport(url, timeout):
        calls.append(url)
        if "etherscan" in url:
            return json.dumps({"status": "0", "message": "No records found",
                               "result": []}).encode()      # valid-empty
        return json.dumps({"status": "1", "result": [
            _log_row(TOKEN, A, SPENDER, 100)]}).encode()

    src = ApprovalLogSource(lambda: "APIKEY", transport=fake_transport)
    assert len(src.fetch(ETH, A, from_block=0)) == 1         # fell back on empty
    assert any("blockscout" in u for u in calls)


def test_source_incremental_empty_is_respected_not_failed_over():
    # An incremental window (from_block > 0) that empties is genuinely "no new
    # logs" — do NOT waste a fallback call on it.
    calls = []

    def fake_transport(url, timeout):
        calls.append(url)
        return json.dumps({"status": "0", "message": "No records found",
                           "result": []}).encode()

    src = ApprovalLogSource(lambda: "APIKEY", transport=fake_transport)
    assert src.fetch(ETH, A, from_block=1_000_000) == []
    assert not any("blockscout" in u for u in calls)         # no fallback


# --- fetch_allowances: found / read-zero / failed (the prune-safety split) ---

from qeth.plugins.approvals.discovery import fetch_allowances  # noqa: E402


class _MCPending:
    def __init__(self, success, value):
        self.success = success
        self.value = value


class _FakeMulticall:
    def __init__(self, outcomes):
        self._outcomes = outcomes          # {spender_lower: (success, value)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, token, calldata, decoder=None):
        spender = "0x" + calldata[-20:].hex()
        success, value = self._outcomes.get(spender.lower(), (True, 0))
        return _MCPending(success, value)


class _FakeMCClient:
    def __init__(self, outcomes):
        self._outcomes = outcomes

    def multicall(self, batch_size=100):
        return _FakeMulticall(self._outcomes)


def test_fetch_allowances_splits_found_readzero_and_failed():
    s_pos = "0x" + "11" * 20
    s_zero = "0x" + "22" * 20
    s_fail = "0x" + "33" * 20
    client = _FakeMCClient({
        s_pos.lower(): (True, 500),        # positive → found + read
        s_zero.lower(): (True, 0),         # read as zero → read only (prunable)
        s_fail.lower(): (False, None),     # call reverted → NEITHER (never prune)
    })
    pairs = [(TOKEN.lower(), s_pos.lower()),
             (TOKEN.lower(), s_zero.lower()),
             (TOKEN.lower(), s_fail.lower())]
    found, read = fetch_allowances(client, A, pairs)
    assert found == {(TOKEN.lower(), s_pos.lower()): 500}       # only >0 displayed
    assert (TOKEN.lower(), s_pos.lower()) in read
    assert (TOKEN.lower(), s_zero.lower()) in read              # definitively zero
    assert (TOKEN.lower(), s_fail.lower()) not in read         # failed → not prunable
