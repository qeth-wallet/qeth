"""Integration tests for the ws live watcher against a local anvil fork.

Anvil forks mainnet (real token contracts + whale balances) and serves a real
ws JSON-RPC on localhost, so the watcher exercises its *actual* ws /
eth_subscribe path — but the events are deterministic because we trigger them
ourselves (impersonate → transfer → mine). This is the robust counterpart to
the flaky, rate-limited live-RPC checks: we control exactly which blocks and
logs happen.

Marked ``network`` (forking needs an upstream RPC for state); skipped cleanly
when anvil isn't installed or the fork is unreachable. Override the fork RPC
with ``QETH_ANVIL_FORK_RPC``.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

from qeth.chains import Chain
from qeth.live_watcher import LiveWatcher, PendingTx

FORK_RPC = os.environ.get("QETH_ANVIL_FORK_RPC",
                          "https://ethereum-rpc.publicnode.com")
USDC  = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WHALE = "0x28C6c06298d514Db089934071355E5743bf21d60"   # Binance 14 (USDC + ETH)
ACCT  = "0x1111111111111111111111111111111111111111"   # the watched account
ANY   = "0x2222222222222222222222222222222222222222"


def _pad(addr: str) -> str:
    return addr[2:].lower().rjust(64, "0")


class _Anvil:
    def __init__(self, port: int):
        self.http = f"http://127.0.0.1:{port}"
        self.chain = Chain("AnvilFork", 1, self.http,
                           ws_url=(f"ws://127.0.0.1:{port}",))

    def rpc(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params or []}).encode()
        req = urllib.request.Request(
            self.http, data=body, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if resp.get("error"):
            raise RuntimeError(resp["error"])
        return resp["result"]

    def wait_ready(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                self.rpc("eth_blockNumber")
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def mine(self):
        self.rpc("evm_mine")

    def impersonate(self, addr):
        self.rpc("anvil_impersonateAccount", [addr])
        self.rpc("anvil_setBalance", [addr, hex(10 ** 18)])   # gas

    def send(self, frm, to, data="0x"):
        return self.rpc("eth_sendTransaction",
                        [{"from": frm, "to": to, "data": data}])

    def erc20_balance(self, token, holder):
        return int(self.rpc("eth_call",
            [{"to": token, "data": "0x70a08231" + _pad(holder)}, "latest"]), 16)

    def erc20_transfer(self, token, frm, to, amount):
        """transfer(to, amount) from an impersonated holder; returns tx hash."""
        return self.send(frm, token,
                         "0xa9059cbb" + _pad(to) + hex(amount)[2:].rjust(64, "0"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def anvil():
    """A forked-mainnet anvil with ws + manual mining (so the test controls
    exactly when blocks happen)."""
    if not shutil.which("anvil"):
        pytest.skip("anvil not installed")
    port = _free_port()
    proc = subprocess.Popen(
        ["anvil", "--fork-url", FORK_RPC, "--port", str(port),
         "--no-mining", "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    a = _Anvil(port)
    try:
        if not a.wait_ready(40):
            pytest.skip(f"anvil fork unreachable ({FORK_RPC})")
        yield a
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.network
def test_ws_captures_transfer_log_as_balance_dirty(anvil, qtbot):
    """A real ERC-20 Transfer to the watched account, mined on the fork, is
    captured by the logs subscription and surfaced as balance_dirty."""
    if anvil.erc20_balance(USDC, WHALE) < 10 ** 6:
        pytest.skip("whale lacks USDC at this fork block")
    dirty: list = []
    up: list = []
    w = LiveWatcher(lambda: [anvil.chain],
                    account_provider=lambda: (anvil.chain, ACCT))
    w.balance_dirty.connect(lambda c, a, t: dirty.append(t.lower()))
    w.link_state.connect(lambda c, on: up.append(on) if on else None)
    w.start()
    try:
        qtbot.waitUntil(lambda: bool(up), timeout=10_000)   # connected + subscribed
        anvil.impersonate(WHALE)
        anvil.send(WHALE, USDC,
                   "0xa9059cbb" + _pad(ACCT) + hex(10 ** 6)[2:].rjust(64, "0"))
        anvil.mine()
        qtbot.waitUntil(lambda: USDC.lower() in dirty, timeout=10_000)
    finally:
        w.stop()
    assert USDC.lower() in dirty


@pytest.mark.network
def test_ws_confirms_pending_tx_on_mine(anvil, qtbot):
    """A pending tx confirms via the newHeads-driven receipt probe the moment
    its block is mined on the fork."""
    anvil.impersonate(WHALE)
    txhash = anvil.send(WHALE, ANY, "0x")             # pending (no-mining)
    pending = [PendingTx(txhash, WHALE, 0, None)]
    confirmed: list = []
    up: list = []
    w = LiveWatcher(lambda: [anvil.chain], pending_provider=lambda cid: pending)
    w.confirmed.connect(lambda c, h, r: confirmed.append(h))
    w.link_state.connect(lambda c, on: up.append(on) if on else None)
    w.start()
    try:
        qtbot.waitUntil(lambda: bool(up), timeout=10_000)
        anvil.mine()                                  # tx mines -> newHead -> probe
        qtbot.waitUntil(lambda: txhash in confirmed, timeout=10_000)
    finally:
        w.stop()
    assert txhash in confirmed


# --- Tokens-panel integration: a confirmed tx must update the list ----------
#
# The flaky part of the "Transfer seen but list not updated" bug lived in the
# TokensPlugin, NOT the watcher: a fully-sent token (balanceOf -> 0) has to
# DISAPPEAR from the list. These tests run the REAL plugin against the fork —
# the targeted BalanceWorker reads balanceOf from anvil — so the assertion is
# on actual on-chain state, not a mocked balance.

USDC_DECIMALS = 6


def _visible_tokens(panel):
    from PySide6.QtCore import Qt
    out = []
    for r in range(panel.table.rowCount()):
        it = panel.table.item(r, 0)
        key = it.data(Qt.ItemDataRole.UserRole) if it else None
        if (key and key[1] != panel.NATIVE_CONTRACT
                and not panel.table.isRowHidden(r)):
            out.append(key[1].lower())
    return out


def _make_tokens_plugin(anvil, tmp_qeth):
    """A real TokensPlugin + panel wired to the fork, holding USDC in cache."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import WalletCache
    from qeth.icons import IconCache
    from qeth.store import Store

    store = Store.load()
    panel = TokenListPanel(IconCache(), store)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache()
    workers: list = []

    def start_worker(w):
        workers.append(w)                       # keep a ref (QThread dtor abort)
        w.finished.connect(lambda: workers.remove(w) if w in workers else None)
        w.start()

    tp.host = SimpleNamespace(
        selected_address=ACCT.lower(),
        current_chain=lambda: anvil.chain,
        start_worker=start_worker,
        tokens_plugin=None,
    )
    return tp, panel, workers


def _seed_usdc(tp, panel, anvil, held_raw):
    """Cache + render ACCT holding ``held_raw`` USDC, so it's on the list."""
    from qeth.wallet_cache import CachedToken, CachedWallet
    cached = CachedWallet(
        chain_id=1, address=ACCT.lower(),
        native_balance_wei=10 ** 18, native_price_updated=1,
        native_price_usd="2000",
        tokens=[CachedToken(
            contract=USDC.lower(), symbol="USDC", name="USD Coin",
            decimals=USDC_DECIMALS, balance_raw=held_raw,
            price_usd="1.0", price_updated=1)])
    tp._wallet_cache.save(cached)
    panel.show_cached(anvil.chain, cached)
    tp._displayed_view = (1, ACCT.lower())


def _ensure_usdc(anvil, holder, want):
    """Make ``holder`` hold exactly ``want`` USDC on the fork: top up from the
    whale, or burn the excess to a sink. ACCT (0x1111…) already holds USDC on
    real mainnet, so we can't assume it starts empty."""
    have = anvil.erc20_balance(USDC, holder)
    if have < want:
        anvil.impersonate(WHALE)
        anvil.erc20_transfer(USDC, WHALE, holder, want - have)
    elif have > want:
        anvil.impersonate(holder)
        anvil.erc20_transfer(USDC, holder, ANY, have - want)
    anvil.mine()
    assert anvil.erc20_balance(USDC, holder) == want


@pytest.mark.network
@pytest.mark.parametrize("active_tab", ["tokens", "transactions", "ens"])
def test_send_to_zero_removes_token_from_list(anvil, qtbot, tmp_qeth, active_tab):
    """ACCT holds USDC, then sends ALL of it away. However the confirmation is
    observed — whichever tab is active when the tx lands — the now-zero USDC
    must drop off the list (the exact bug: it lingered until a wallet switch).

    The Tokens panel keeps ``_displayed_view`` set to the wallet it last
    rendered regardless of which tab is active, so all three cases drive the
    same on-view rerender; for the non-Tokens tabs we also fire on_activated
    (the user switching back) to prove that path stays correct too."""
    start = 100 * 10 ** USDC_DECIMALS
    _ensure_usdc(anvil, ACCT, start)

    tp, panel, workers = _make_tokens_plugin(anvil, tmp_qeth)
    _seed_usdc(tp, panel, anvil, start)
    assert USDC.lower() in _visible_tokens(panel)       # on the list to start

    # ACCT spends its ENTIRE USDC balance → balanceOf becomes 0.
    anvil.impersonate(ACCT)
    anvil.erc20_transfer(USDC, ACCT, ANY, start)
    anvil.mine()
    assert anvil.erc20_balance(USDC, ACCT) == 0

    # The watcher would emit balance_dirty for USDC; drive the plugin entry the
    # relay calls. The real BalanceWorker then reads balanceOf == 0 from anvil.
    tp.on_balance_dirty(anvil.chain, ACCT, USDC)
    if active_tab != "tokens":
        # user was on another tab; switching to Tokens fires on_activated
        qtbot.waitUntil(
            lambda: not tp._wallet_cache.load(1, ACCT.lower()).tokens,
            timeout=15_000)
        tp.on_activated()

    qtbot.waitUntil(
        lambda: USDC.lower() not in _visible_tokens(panel), timeout=15_000)
    assert USDC.lower() not in _visible_tokens(panel)
    # the cache must not retain a zero-balance row either
    assert not tp._wallet_cache.load(1, ACCT.lower()).tokens


@pytest.mark.network
def test_on_view_when_confirmed_drops_token_via_live_path(anvil, qtbot, tmp_qeth):
    """The case the user flagged: you are ALREADY on the Tokens tab when the tx
    confirms. on_balance_dirty drives BOTH the eager targeted read AND the
    on-view live-refresh reconcile — let the real QTimers fire (no shortcuts)
    and the fully-sent token must drop off, not linger until the sweep."""
    start = 100 * 10 ** USDC_DECIMALS
    _ensure_usdc(anvil, ACCT, start)

    tp, panel, workers = _make_tokens_plugin(anvil, tmp_qeth)
    _seed_usdc(tp, panel, anvil, start)
    assert USDC.lower() in _visible_tokens(panel)
    # make the on-view live-refresh fire fast so the test isn't slow; the
    # targeted debounce stays as shipped.
    tp.LIVE_REFRESH_DEBOUNCE_MS = 200

    anvil.impersonate(ACCT)
    anvil.erc20_transfer(USDC, ACCT, ANY, start)
    anvil.mine()
    assert anvil.erc20_balance(USDC, ACCT) == 0

    # on-screen (_displayed_view already == this view) → the live path runs.
    tp.on_balance_dirty(anvil.chain, ACCT, USDC)
    qtbot.waitUntil(
        lambda: USDC.lower() not in _visible_tokens(panel), timeout=15_000)
    assert not tp._wallet_cache.load(1, ACCT.lower()).tokens


@pytest.mark.network
def test_on_view_stale_read_then_reconcile_drops_token(anvil, qtbot, tmp_qeth):
    """Deterministic repro of the on-view bug. We make the EAGER targeted read
    fire while the send is still pending (so it reads the stale, pre-send
    balance and does NOT drop the token — exactly what an http RPC a block
    behind the ws would do), THEN mine. Only the later on-view live-refresh
    reconcile sees zero. It must drop the token; without that reconcile the
    token would linger until the slow sweep (the reported symptom)."""
    start = 100 * 10 ** USDC_DECIMALS
    _ensure_usdc(anvil, ACCT, start)

    tp, panel, workers = _make_tokens_plugin(anvil, tmp_qeth)
    _seed_usdc(tp, panel, anvil, start)
    tp.TARGETED_BALANCE_DEBOUNCE_MS = 50      # eager read fires fast…
    tp.LIVE_REFRESH_DEBOUNCE_MS = 1200        # …reconcile fires after we mine

    anvil.impersonate(ACCT)
    anvil.erc20_transfer(USDC, ACCT, ANY, start)   # PENDING (anvil --no-mining)

    tp.on_balance_dirty(anvil.chain, ACCT, USDC)
    qtbot.wait(600)                                # let the eager read complete
    assert anvil.erc20_balance(USDC, ACCT) == start            # still pending
    assert USDC.lower() in _visible_tokens(panel)              # stale → not dropped

    anvil.mine()                                   # NOW the send lands → 0
    assert anvil.erc20_balance(USDC, ACCT) == 0
    # only the on-view live-refresh reconcile can drop it now
    qtbot.waitUntil(
        lambda: USDC.lower() not in _visible_tokens(panel), timeout=15_000)
    assert not tp._wallet_cache.load(1, ACCT.lower()).tokens


@pytest.mark.network
def test_switching_to_tab_reconciles_without_a_ws_event(anvil, qtbot, tmp_qeth):
    """The faithful repro of the reported bug: a token is sent in full, but NO
    balance_dirty is delivered (ws hiccup / a path we don't subscribe to). The
    user simply switches to the Tokens tab. on_activated must reconcile against
    the chain and drop the now-zero token — no live event, no wallet switch."""
    start = 100 * 10 ** USDC_DECIMALS
    _ensure_usdc(anvil, ACCT, start)

    tp, panel, workers = _make_tokens_plugin(anvil, tmp_qeth)
    _seed_usdc(tp, panel, anvil, start)
    assert USDC.lower() in _visible_tokens(panel)

    anvil.impersonate(ACCT)
    anvil.erc20_transfer(USDC, ACCT, ANY, start)        # send it ALL
    anvil.mine()
    assert anvil.erc20_balance(USDC, ACCT) == 0

    # NO on_balance_dirty — only the tab activation, like the real report.
    tp.on_activated()

    qtbot.waitUntil(
        lambda: USDC.lower() not in _visible_tokens(panel), timeout=15_000)
    assert not tp._wallet_cache.load(1, ACCT.lower()).tokens


@pytest.mark.network
def test_partial_send_keeps_token_with_new_balance(anvil, qtbot, tmp_qeth):
    """A partial send must NOT drop the token — it stays on the list with its
    reduced balance (the row updates in place, no flicker)."""
    start = 100 * 10 ** USDC_DECIMALS
    keep = 40 * 10 ** USDC_DECIMALS
    _ensure_usdc(anvil, ACCT, start)

    tp, panel, workers = _make_tokens_plugin(anvil, tmp_qeth)
    _seed_usdc(tp, panel, anvil, start)

    anvil.impersonate(ACCT)
    anvil.erc20_transfer(USDC, ACCT, ANY, start - keep)     # send 60, keep 40
    anvil.mine()
    assert anvil.erc20_balance(USDC, ACCT) == keep

    tp.on_balance_dirty(anvil.chain, ACCT, USDC)
    qtbot.waitUntil(
        lambda: (c := tp._wallet_cache.load(1, ACCT.lower())).tokens
        and c.tokens[0].balance_raw == keep,
        timeout=15_000)
    # still on the list, with the new balance
    assert USDC.lower() in _visible_tokens(panel)
    cached = tp._wallet_cache.load(1, ACCT.lower())
    assert cached.tokens[0].balance_raw == keep
