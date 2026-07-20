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
