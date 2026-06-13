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
                  worker_factory=None, known_addresses=None, token_info=None,
                  address_book=None):
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
        address_book=address_book,
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
            def __init__(self, chain, req, nonce_floor=None):
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
            def __init__(self, chain, req, nonce_floor=None):
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
            def __init__(self, chain, req, nonce_floor=None):
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
            def __init__(self, chain, req, nonce_floor=None):
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


VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


class TestEnsRecipient:
    """Sending to an ENS name: forward-resolve, show the actual 0x
    address (highlighted) for verification, and only treat the recipient
    as valid once it resolves."""

    def test_looks_like_ens(self):
        from qeth.plugins.transactions import SendTokenDialog as S
        assert S._looks_like_ens("vitalik.eth")
        assert S._looks_like_ens("swiss-stake.eth")
        assert not S._looks_like_ens("0x" + "12" * 20)
        assert not S._looks_like_ens("")
        assert not S._looks_like_ens("plainword")        # no dot → not a name

    def test_name_gates_send_until_resolved(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        dlg.recipient_edit.setText("vitalik.eth")
        # Before resolution: not a valid recipient; the row shows "resolving".
        assert dlg._parsed_recipient() is None
        assert dlg._ens_form.isRowVisible(dlg._ens_label)
        assert "resolving" in dlg._ens_label.text()
        # Resolution lands → the resolved address becomes the recipient.
        dlg._on_ens_resolved("vitalik.eth", VITALIK)
        assert dlg._parsed_recipient() == VITALIK
        assert VITALIK in dlg._ens_label.text() and "↳" in dlg._ens_label.text()

    def test_not_found_is_warned_and_invalid(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        dlg.recipient_edit.setText("nope.eth")
        dlg._on_ens_resolved("nope.eth", "")
        assert dlg._parsed_recipient() is None
        assert "not found" in dlg._ens_label.text()

    def test_stale_resolution_is_dropped(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        dlg.recipient_edit.setText("vitalik.eth")
        dlg.recipient_edit.setText("swiss-stake.eth")    # changed mid-flight
        dlg._on_ens_resolved("vitalik.eth", VITALIK)     # old result arrives
        assert dlg._parsed_recipient() is None           # ignored

    def test_plain_address_clears_ens_row(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        dlg.recipient_edit.setText("vitalik.eth")
        dlg._on_ens_resolved("vitalik.eth", VITALIK)
        dlg.recipient_edit.setText("0x" + "22" * 20)     # switch to an address
        assert dlg._ens_input == ""
        assert not dlg._ens_form.isRowVisible(dlg._ens_label)
        assert dlg._parsed_recipient() == "0x" + "22" * 20

    def test_retyping_same_name_keeps_resolution(self, qtbot, monkeypatch):
        # Re-firing _update_ens for the already-resolved name is a no-op
        # (dedup), not a reset back to "resolving" — the address survives.
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        dlg.recipient_edit.setText("vitalik.eth")
        dlg._on_ens_resolved("vitalik.eth", VITALIK)
        dlg._update_ens()                                # same text again
        assert dlg._ens_resolved == VITALIK
        assert dlg._parsed_recipient() == VITALIK


def test_resolve_ens_address(monkeypatch):
    from types import SimpleNamespace
    from qeth import ens

    class _Ens:
        def address(self, name):
            return (VITALIK.lower() if name == "vitalik.eth" else None)

    class _W3:
        def __init__(self, _provider):
            self.ens = _Ens()
            # _make_w3 sets provider.global_ccip_read_enabled (strict toggle).
            self.provider = SimpleNamespace(global_ccip_read_enabled=True)

    monkeypatch.setattr("web3.Web3", _W3)
    assert ens.resolve_ens_address("http://x", "vitalik.eth") == VITALIK
    assert ens.resolve_ens_address("http://x", "nope.eth") is None


def test_resolve_ens_address_degrades_on_rpc_error(monkeypatch):
    """The resolver promises never to raise — an RPC failure → None."""
    from types import SimpleNamespace
    from qeth import ens

    class _W3:
        def __init__(self, _provider):
            self.ens = self
            self.provider = SimpleNamespace(global_ccip_read_enabled=True)

        def address(self, name):
            raise RuntimeError("rpc down")

    monkeypatch.setattr("web3.Web3", _W3)
    assert ens.resolve_ens_address("http://x", "vitalik.eth") is None


def test_resolve_ens_strict_disables_ccip(monkeypatch):
    """ccip=False is threaded onto the provider so the verified path can't
    follow an offchain (gateway) resolution."""
    from types import SimpleNamespace
    from qeth import ens
    seen = {}

    class _W3:
        def __init__(self, _provider):
            self.ens = SimpleNamespace(address=lambda name: VITALIK.lower())
            self.provider = SimpleNamespace(global_ccip_read_enabled=None)

    monkeypatch.setattr("web3.Web3", _W3)
    # capture what the provider flag ends up as
    import web3
    orig = web3.Web3

    def _cap(p):
        w = orig(p)
        seen["w"] = w
        return w
    monkeypatch.setattr("web3.Web3", _cap)
    ens.resolve_ens_address("http://x", "vitalik.eth", ccip=False)
    assert seen["w"].provider.global_ccip_read_enabled is False


def test_ens_resolve_worker_emits_name_and_address(qtbot, monkeypatch):
    from types import SimpleNamespace
    from qeth import ens
    # Stub the verified resolution (no Helios / network); worker just relays it.
    monkeypatch.setattr(
        ens, "verified_resolve_address",
        lambda chain, name, wait_s: (VITALIK, False))
    worker = ens.EnsResolveWorker(
        SimpleNamespace(rpc_url="http://x", chain_id=1), "vitalik.eth",
        wait_s=0.0)
    with qtbot.waitSignal(worker.resolved, timeout=2000) as blocker:
        worker.start()
    worker.wait()
    assert blocker.args == ["vitalik.eth", VITALIK, False]


LEDGER1 = "0x7a16fF8270133F063aAb6C9977183D9e72835428"
HOT2 = "0xB325c1AC788f02fF7997cF53C6FF40Dd762897B3"


class TestAddressBook:
    """Recipient autocomplete + own-wallet label, scoped to the user's
    own wallets only (no arbitrary saved contacts)."""

    def _book_dialog(self, qtbot, monkeypatch):
        return _make_dialog(
            qtbot, monkeypatch, balance_raw=10**18, is_native=True,
            known_addresses=[LEDGER1, HOT2],
            address_book=[(LEDGER1, "My Ledger 1"), (HOT2, "")],  # one unlabeled
        )

    def test_completer_lists_the_wallets(self, qtbot, monkeypatch):
        dlg = self._book_dialog(qtbot, monkeypatch)
        model = dlg._book_completer.model()
        entries = [model.item(i).text() for i in range(model.rowCount())]
        assert any("My Ledger 1" in e and LEDGER1 in e for e in entries)
        assert HOT2 in entries                     # unlabeled → bare address

    def test_no_book_means_no_completer(self, qtbot, monkeypatch):
        dlg = _make_dialog(qtbot, monkeypatch, balance_raw=10**18, is_native=True)
        assert dlg._book_completer is None

    def test_picking_inserts_bare_address_not_label(self, qtbot, monkeypatch):
        dlg = self._book_dialog(qtbot, monkeypatch)
        c = dlg._book_completer
        model = c.model()
        # pathFromIndex is what QLineEdit inserts when a row is chosen — it
        # must be the bare address, never the "label — 0x…" display string.
        inserted = c.pathFromIndex(model.index(0, 0))   # My Ledger 1 row
        assert inserted == LEDGER1
        assert "Ledger" not in inserted

    def test_own_wallet_shows_its_label(self, qtbot, monkeypatch):
        dlg = self._book_dialog(qtbot, monkeypatch)
        dlg.recipient_edit.setText(LEDGER1)
        dlg._update_recipient_identity()
        assert dlg._identity_label.text() == "Your wallet — My Ledger 1"
        # An unlabeled own wallet still flags as yours.
        dlg.recipient_edit.setText(HOT2)
        dlg._update_recipient_identity()
        assert dlg._identity_label.text() == "Your wallet"

    def test_popup_button_lists_all_wallets(self, qtbot, monkeypatch):
        dlg = self._book_dialog(qtbot, monkeypatch)
        dlg._show_book_popup()                     # the ▾ button handler
        assert dlg._book_completer.completionPrefix() == ""
        assert dlg._book_completer.completionCount() == 2   # both wallets
