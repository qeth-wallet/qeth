"""Regression tests for SendTokenDialog bug fixes.

Covers two bugs that surfaced from a "send after dapp swap" flow:

1. Calldata preview froze when the typed amount exceeded the
   *cached* token balance — the cache lags the chain by minutes
   right after a dapp tx, so the user (correctly) typed a value
   above what we thought they had and the preview rendered "?"
   for the amount, looking broken.

2. Gas estimate at dialog-open time used the sender's OWN
   address as the placeholder recipient, making the recipient
   storage slot warm + non-zero — drastically under-estimating
   real cold/zero-balance transfers (USDC ~28k estimated, ~65k
   actual; ×1.5 policy ⇒ 42k limit ⇒ out of gas on broadcast).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from qeth.chains import DEFAULT_CHAINS


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
FROM = "0x7a16ff8270133f063aab6c9977183d9e72835428"
USDC_CONTRACT = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def _make_dialog(qtbot, monkeypatch, *, balance_raw: int, is_native=False,
                  worker_factory=None, known_addresses=None, token_info=None):
    """Construct a SendTokenDialog without firing a real
    GasSuggestionWorker (which would hit the network). We replace
    the worker class with a stub so the rest of the dialog wiring
    still runs. ``worker_factory`` lets a test substitute a stub
    that captures the requests it sees. ``token_info`` stubs the
    curated-token lookup used to flag token-contract recipients."""
    import qeth.plugins.transactions as tx
    if worker_factory is None:
        worker_factory = lambda *a, **kw: MagicMock(
            suggested=MagicMock(connect=MagicMock()),
            failed=MagicMock(connect=MagicMock()),
            start=MagicMock(),
        )
    monkeypatch.setattr(tx, "GasSuggestionWorker", worker_factory)
    asset = {
        "is_native": is_native,
        "contract": None if is_native else USDC_CONTRACT,
        "symbol": "ETH" if is_native else "USDC",
        "decimals": 18 if is_native else 6,
        "balance_raw": balance_raw,
        "logo_uri": None,
    }
    dlg = tx.SendTokenDialog(
        asset, ETH, FROM,
        abi_source=MagicMock(),
        abi_cache=MagicMock(),
        start_worker=lambda w: None,
        known_addresses=known_addresses,
        token_info=token_info,
    )
    qtbot.addWidget(dlg)
    return dlg


class TestStaleBalanceCalldataPreview:
    """Bug 1: typing a value > cached balance must still update
    the calldata preview. The strict balance check belongs ONLY
    on the Send button, not on the preview rendering."""

    def test_unchecked_parser_accepts_amount_above_balance(
        self, qtbot, monkeypatch,
    ):
        # Cache says 5 USDC; user types 100 (perhaps because they
        # just swapped ETH → USDC and the cache hasn't caught up).
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=5_000_000)
        dlg.amount_edit.setText("100")
        # Strict variant rejects (Send button must stay disabled).
        assert dlg._parsed_amount_raw() is None
        # Unchecked variant returns 100 * 1e6 (the calldata preview
        # uses this so the user sees the actual call they're
        # building — not a "?" sentinel).
        assert dlg._parsed_amount_raw_unchecked() == 100_000_000

    def test_unchecked_parser_still_rejects_invalid_input(
        self, qtbot, monkeypatch,
    ):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        # Empty, junk, and non-positive values stay rejected — the
        # unchecked variant only loosens the balance cap.
        dlg.amount_edit.setText("")
        assert dlg._parsed_amount_raw_unchecked() is None
        dlg.amount_edit.setText("not-a-number")
        assert dlg._parsed_amount_raw_unchecked() is None
        dlg.amount_edit.setText("0")
        assert dlg._parsed_amount_raw_unchecked() is None
        dlg.amount_edit.setText("-5")
        assert dlg._parsed_amount_raw_unchecked() is None

    def test_decoded_preview_renders_typed_amount_above_balance(
        self, qtbot, monkeypatch,
    ):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=5_000_000)
        dlg.recipient_edit.setText(
            "0x000000000000000000000000000000000000dEaD"
        )
        dlg.amount_edit.setText("42")
        # _update_state triggers _refresh_decoded_view through the
        # textChanged signal. Force it directly so we don't depend
        # on Qt event timing in the test.
        dlg._refresh_decoded_view()
        # The rendered preview must contain the typed amount (in
        # smallest units: 42 * 10^6 for USDC), proving the calldata
        # IS updating even when the cached balance disagrees.
        rendered = dlg.decoded_view.toPlainText()
        assert "42000000" in rendered

    def test_send_button_still_blocked_when_exceeding_balance(
        self, qtbot, monkeypatch,
    ):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=5_000_000)
        # Simulate gas-ready so the only remaining check is balance.
        dlg._gas_ready = True
        dlg.recipient_edit.setText(
            "0x000000000000000000000000000000000000dEaD"
        )
        dlg.amount_edit.setText("100")  # > cached 5
        dlg._update_state()
        assert not dlg.confirm_btn.isEnabled()
        dlg.amount_edit.setText("3")  # < cached 5
        dlg._update_state()
        assert dlg.confirm_btn.isEnabled()


class TestGasEstimateNoPlaceholder:
    """Bug 2 (original symptom: out-of-gas USDC send): the gas
    estimator used to fire against a placeholder recipient at
    dialog-open time. Any placeholder is wrong in some direction
    (sender ⇒ warm + non-zero slot, underestimating; 0xdEaD ⇒
    cold + zero, overestimating in the warm case). The fix is to
    never use a placeholder — wait until the user types a real
    address, then estimate against that."""

    def test_no_worker_starts_until_recipient_entered(
        self, qtbot, monkeypatch,
    ):
        import qeth.plugins.transactions as tx
        constructed: list = []

        class _StubWorker:
            def __init__(self, chain, req):
                constructed.append(req)
                self.suggested = MagicMock(connect=MagicMock())
                self.failed = MagicMock(connect=MagicMock())
            def start(self):
                pass

        _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            worker_factory=_StubWorker,
        )
        # No placeholder estimate — the dialog opens with empty
        # gas fields and a "(enter recipient to estimate)" label.
        assert constructed == []


class TestGasReestimateOnRecipientChange:
    """Once the user types a real recipient we should re-estimate
    gas against that actual address — storage costs depend on
    whether the recipient slot is cold/warm and zero/non-zero, and
    a single placeholder-based estimate can be ~20 k under the
    real cost. The placeholder is only the dialog-open stopgap."""

    def test_re_estimate_fires_with_typed_recipient(
        self, qtbot, monkeypatch,
    ):
        import qeth.plugins.transactions as tx

        constructed: list = []

        class _StubWorker:
            def __init__(self, chain, req):
                constructed.append(req)
                self.suggested = MagicMock(connect=MagicMock())
                self.failed = MagicMock(connect=MagicMock())
            def start(self):
                pass

        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            worker_factory=_StubWorker,
        )
        # No placeholder estimate at dialog-open.
        assert constructed == []
        # Typing a real recipient + flushing the debounce timer
        # triggers the estimate against THAT address.
        target = "0xa9D1e08C7793af67e9d92fe308d5697FB81d3E43"
        dlg.recipient_edit.setText(target)
        dlg._reestimate_gas()  # bypass timer for determinism
        assert len(constructed) == 1
        req = constructed[0]
        assert target[2:].lower() in req.data.lower()

    def test_re_estimate_skips_duplicate_recipient(
        self, qtbot, monkeypatch,
    ):
        import qeth.plugins.transactions as tx
        constructed: list = []

        class _StubWorker:
            def __init__(self, chain, req):
                constructed.append(req)
                self.suggested = MagicMock(connect=MagicMock())
                self.failed = MagicMock(connect=MagicMock())
            def start(self):
                pass

        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            worker_factory=_StubWorker,
        )
        target = "0xa9D1e08C7793af67e9d92fe308d5697FB81d3E43"
        dlg.recipient_edit.setText(target)
        dlg._reestimate_gas()
        before = len(constructed)
        # Calling again with the same recipient — no new worker.
        dlg._reestimate_gas()
        assert len(constructed) == before

    def test_re_estimate_skips_invalid_recipient(
        self, qtbot, monkeypatch,
    ):
        import qeth.plugins.transactions as tx
        constructed: list = []

        class _StubWorker:
            def __init__(self, chain, req):
                constructed.append(req)
                self.suggested = MagicMock(connect=MagicMock())
                self.failed = MagicMock(connect=MagicMock())
            def start(self):
                pass

        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            worker_factory=_StubWorker,
        )
        before = len(constructed)
        # Partial address — don't fire a worker.
        dlg.recipient_edit.setText("0xa9d1")
        dlg._reestimate_gas()
        assert len(constructed) == before


# A second wallet the user owns (checksum-mixed to prove we match
# case-insensitively against the lowercased known set).
OWN_OTHER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
STRANGER = "0x1111111111111111111111111111111111111111"


class TestRecipientOwnWalletHint:
    """Typing a recipient that is one of the user's own wallets tints
    the field (light-green bg + dark-green text, set together so it's
    legible in any palette). Anything else leaves it on the default
    style."""

    def test_own_wallet_tints_the_field(self, qtbot, monkeypatch):
        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            known_addresses=[FROM, OWN_OTHER],
        )
        dlg.recipient_edit.setText(OWN_OTHER)
        assert dlg._recipient_hint == "own"
        ss = dlg.recipient_edit.styleSheet()
        assert "background-color" in ss and "color" in ss
        assert dlg.recipient_edit.toolTip()  # explains the tint

    def test_case_insensitive_match(self, qtbot, monkeypatch):
        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            known_addresses=[OWN_OTHER.lower()],
        )
        dlg.recipient_edit.setText(OWN_OTHER.upper().replace("0X", "0x"))
        assert dlg._recipient_hint == "own"

    def test_stranger_leaves_default_style(self, qtbot, monkeypatch):
        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            known_addresses=[FROM],
        )
        dlg.recipient_edit.setText(STRANGER)
        assert dlg._recipient_hint == ""
        assert dlg.recipient_edit.styleSheet() == ""

    def test_clears_when_address_edited_away(self, qtbot, monkeypatch):
        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            known_addresses=[OWN_OTHER],
        )
        dlg.recipient_edit.setText(OWN_OTHER)
        assert dlg._recipient_hint == "own"
        # Backspace one char — no longer a valid/known address.
        dlg.recipient_edit.setText(OWN_OTHER[:-1])
        assert dlg._recipient_hint == ""
        assert dlg.recipient_edit.styleSheet() == ""

    def test_no_known_addresses_never_tints(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        dlg.recipient_edit.setText(OWN_OTHER)
        assert dlg._recipient_hint == ""
        assert dlg.recipient_edit.styleSheet() == ""


class TestRecipientTokenContractHint:
    """Sending a token (or ETH) to a token contract almost always
    burns the funds, so the recipient field turns red. Detection is
    local: the asset's own contract, or any address on the curated
    token lists (so it catches tokens the user doesn't hold). Red
    outranks the green own-wallet hint."""

    def test_sending_to_the_assets_own_contract_is_flagged(
        self, qtbot, monkeypatch,
    ):
        # Sending USDC to the USDC contract — classic footgun.
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        dlg.recipient_edit.setText(USDC_CONTRACT)
        assert dlg._recipient_hint == "token"
        ss = dlg.recipient_edit.styleSheet()
        assert "background-color" in ss and "color" in ss
        assert "burn" in dlg.recipient_edit.toolTip().lower()

    def test_curated_token_not_held_is_flagged(self, qtbot, monkeypatch):
        some_token = "0x1111111111111111111111111111111111111111"
        # token_info returns truthy => it's a known token contract.
        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            token_info=lambda cid, addr: (
                object() if addr.lower() == some_token else None
            ),
        )
        dlg.recipient_edit.setText(some_token)
        assert dlg._recipient_hint == "token"

    def test_red_outranks_green_when_address_is_both(
        self, qtbot, monkeypatch,
    ):
        addr = "0x2222222222222222222222222222222222222222"
        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            known_addresses=[addr],
            token_info=lambda cid, a: object() if a.lower() == addr else None,
        )
        dlg.recipient_edit.setText(addr)
        assert dlg._recipient_hint == "token"  # danger wins

    def test_plain_eoa_recipient_is_not_flagged(self, qtbot, monkeypatch):
        dlg = _make_dialog(
            qtbot, monkeypatch, balance_raw=10_000_000,
            token_info=lambda cid, addr: None,  # not a known token
        )
        dlg.recipient_edit.setText(
            "0x000000000000000000000000000000000000dEaD"
        )
        assert dlg._recipient_hint == ""
        assert dlg.recipient_edit.styleSheet() == ""


class TestSendDialogKeyboardDefaults:
    """Enter should trigger Send (the affirmative) and Esc should
    cancel — the GNOME-HIG default-button / escape contract."""

    def test_send_is_the_default_button(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        dlg.show()
        # QDialogButtonBox promotes the AcceptRole button to default on
        # show; confirm it's Send (so Enter confirms, not Cancel).
        from PySide6.QtWidgets import QPushButton
        defaults = [b.text() for b in dlg.findChildren(QPushButton)
                    if b.isDefault()]
        assert defaults == ["&Send"]

    def test_escape_closes(self, qtbot, monkeypatch):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        dlg.show()
        assert dlg.isVisible()
        QTest.keyClick(dlg, Qt.Key_Escape)
        assert not dlg.isVisible()


class TestGasProgressiveDisclosure:
    """Gas controls live behind a collapsed expander; the Expected fee
    summary stays visible (GNOME-HIG progressive disclosure)."""

    def test_gas_controls_hidden_until_expanded(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        dlg.show()
        # Collapsed by default → spinner not visible, but the Expected
        # fee summary label IS.
        assert not dlg._gas_section.is_expanded()
        assert not dlg.spin_gas.isVisible()
        assert dlg.max_total_lbl.isVisible()
        # Expand → spinner shows.
        dlg._gas_section.set_expanded(True)
        qtbot.waitUntil(lambda: dlg.spin_gas.isVisible(), timeout=1000)
        assert dlg.spin_gas.isVisible()

    def test_summary_label_is_outside_the_collapsible(self, qtbot, monkeypatch):
        # The fee summary must not be a descendant of the collapsible
        # content, or it would vanish when collapsed.
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        section = dlg._gas_section
        assert not _is_descendant(dlg.max_total_lbl, section)
        assert _is_descendant(dlg.spin_gas, section)


def _is_descendant(widget, ancestor) -> bool:
    w = widget.parentWidget()
    while w is not None:
        if w is ancestor:
            return True
        w = w.parentWidget()
    return False


class TestAddressLinkCopyMenu:
    """Link labels (address/hash) get a working Copy item instead of
    Qt's default rich-text menu where "Copy" is for selected text and
    sits disabled."""

    def test_copy_noun_classifies_value(self):
        from qeth.plugins.transactions import _copy_noun
        assert _copy_noun("0x" + "ab" * 20) == "Address"   # 42 chars
        assert _copy_noun("0x" + "cd" * 32) == "Hash"      # 66 chars
        assert _copy_noun("https://app.uniswap.org") == "Link"

    def test_link_label_uses_a_custom_context_menu(self, qtbot, monkeypatch):
        from PySide6.QtCore import Qt
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        # The From row is built via _link_label with an explorer URL.
        lbl = dlg._link_label("0x" + "11" * 20, "https://etherscan.io/address/x")
        assert lbl.contextMenuPolicy() == Qt.CustomContextMenu


class TestLiveUsdValue:
    """Typing an amount shows its USD value live, for both ERC-20 sends
    (using the token's cached price) and native sends (using the host's
    native price). No price → no value (rather than a misleading $0)."""

    def test_token_amount_shows_usd(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        dlg._asset["price_usd"] = "0.9998"          # USDC ~ $1
        dlg.amount_edit.setText("100")
        assert "USD" in dlg._value_usd_lbl.text()
        assert dlg._value_usd_lbl.text().startswith("≈")
        # 100 * 0.9998 = 99.98
        assert "99.98" in dlg._value_usd_lbl.text()

    def test_native_amount_uses_host_native_price(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        dlg._native_price_usd = Decimal("2500")
        dlg.amount_edit.setText("2")
        assert "5000" in dlg._value_usd_lbl.text()    # 2 ETH * 2500

    def test_no_price_shows_no_value(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        # price_usd absent on the asset; native price is None.
        dlg.amount_edit.setText("100")
        assert dlg._value_usd_lbl.text() == ""

    def test_invalid_or_empty_amount_clears_value(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10_000_000)
        dlg._asset["price_usd"] = "1.0"
        dlg.amount_edit.setText("100")
        assert dlg._value_usd_lbl.text() != ""
        dlg.amount_edit.setText("")                   # cleared
        assert dlg._value_usd_lbl.text() == ""
        dlg.amount_edit.setText("abc")                # garbage
        assert dlg._value_usd_lbl.text() == ""


class TestRecipientIdentity:
    """The Contract: row resolves the typed recipient's identity once
    it's a valid address. (The worker is stubbed out via the no-op
    start_worker, so we assert the synchronous kick/clear behaviour:
    a valid recipient enters the "…" loading state and records the
    address; an invalid one clears the row.)"""

    def test_identity_row_present(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        assert dlg._identity_label is not None

    def test_valid_recipient_kicks_lookup(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        addr = "0x" + "12" * 20
        dlg.recipient_edit.setText(addr)
        assert dlg._identity_last_addr == addr.lower()
        assert dlg._identity_label.text() == "…"      # loading; worker stubbed

    def test_invalid_recipient_clears_row(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        dlg.recipient_edit.setText("0x" + "12" * 20)  # valid → kicks
        dlg.recipient_edit.setText("0xnope")          # invalid → clears
        assert dlg._identity_last_addr is None
        assert dlg._identity_label.text() == ""
