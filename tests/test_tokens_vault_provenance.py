"""VaultProvenanceWorker — held-vault discovery via asset() pre-filter +
own-tx provenance. Drives run() synchronously with fakes (no network)."""

from types import SimpleNamespace

import qeth.transactions as txmod
from qeth.plugins.tokens import VaultProvenanceWorker

CHAIN = SimpleNamespace(chain_id=1, name="Ethereum")
OWNER = "0x" + "7a" * 20
VAULT = "0x" + "a1" * 20        # self-acquired vault (should be discovered)
AIRDROP = "0x" + "c3" * 20      # vault-shaped but airdropped (should NOT)
SPAM = "0x" + "d4" * 20         # asset() reverts (filtered before provenance)
ASSET = "0x" + "b2" * 20
STRANGER = "0x" + "ee" * 20


class _Bal:
    def __init__(self, contract, raw=1):
        self.contract = contract
        self.balance_raw = raw


class _FakeSource:
    def __init__(self, balances):
        self._b = balances

    def supports(self, chain):
        return True

    def list_balances(self, chain, address):
        return self._b


class _FakeLists:
    def __init__(self, known=()):
        self._k = {a.lower() for a in known}

    def is_known(self, cid, c):
        return c.lower() in self._k


class _FakeStore:
    def __init__(self, discovered=(), hidden=(), forced=()):
        self._d = {a.lower() for a in discovered}
        self._h = {a.lower() for a in hidden}
        self._f = {a.lower() for a in forced}

    def is_discovered_token(self, cid, c):
        return c.lower() in self._d

    def is_hidden(self, cid, c):
        return c.lower() in self._h

    def is_force_shown(self, cid, c):
        return c.lower() in self._f


class _MCP:
    def __init__(self, success, value):
        self.success = success
        self.value = value


class _FakeMulticall:
    def __init__(self, assets):
        self._assets = assets

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, token, calldata, decoder=None):
        v = self._assets.get(token.lower())
        return _MCP(v is not None, v if v is not None else 0)


class _FakeClient:
    def __init__(self, assets, tx_from):
        self._assets = assets
        self._tx_from = tx_from

    def multicall(self, batch_size=200):
        return _FakeMulticall(self._assets)

    def rpc(self, method, params):
        if method == "eth_getTransactionByHash":
            return {"from": self._tx_from.get(params[0])}
        return None


def _run(worker):
    got = []
    worker.discovered.connect(lambda cid, s: got.append(set(s)))
    worker.run()
    return got


def _worker(monkeypatch, balances, *, assets, tx_from, logs, lists=None,
            store=None, my=(OWNER,)):
    monkeypatch.setattr(txmod, "fetch_incoming_transfer_logs",
                        lambda chain, token, owner, **k: logs.get(token.lower(), []))
    client = _FakeClient(assets, tx_from)
    return VaultProvenanceWorker(
        CHAIN, OWNER, _FakeSource(balances), lists or _FakeLists(),
        store or _FakeStore(), list(my), lambda: None,
        client_factory=lambda c: client)


def test_discovers_only_the_self_acquired_vault(monkeypatch, qtbot):
    w = _worker(
        monkeypatch,
        [_Bal(VAULT), _Bal(AIRDROP), _Bal(SPAM)],
        assets={VAULT.lower(): int(ASSET, 16), AIRDROP.lower(): int(ASSET, 16)},
        tx_from={"0xdep": OWNER, "0xair": STRANGER},
        logs={VAULT.lower(): [{"transactionHash": "0xdep"}],       # I sent it
              AIRDROP.lower(): [{"transactionHash": "0xair"}]},     # stranger sent it
    )
    assert _run(w) == [{VAULT.lower()}]        # airdrop rejected, spam pre-filtered


def test_no_emit_when_nothing_self_acquired(monkeypatch, qtbot):
    w = _worker(
        monkeypatch,
        [_Bal(AIRDROP)],
        assets={AIRDROP.lower(): int(ASSET, 16)},
        tx_from={"0xair": STRANGER},
        logs={AIRDROP.lower(): [{"transactionHash": "0xair"}]},
    )
    assert _run(w) == []


def test_acquired_from_another_of_my_accounts_counts(monkeypatch, qtbot):
    mine2 = "0x" + "b9" * 20
    w = _worker(
        monkeypatch,
        [_Bal(VAULT)],
        assets={VAULT.lower(): int(ASSET, 16)},
        tx_from={"0xdep": mine2},              # sent from my other account
        logs={VAULT.lower(): [{"transactionHash": "0xdep"}]},
        my=(OWNER, mine2),
    )
    assert _run(w) == [{VAULT.lower()}]


def test_known_and_hidden_candidates_are_skipped(monkeypatch, qtbot):
    # VAULT is already curated-known, AIRDROP is user-hidden → neither is even
    # asset()-probed or provenance-checked (both would otherwise be candidates).
    w = _worker(
        monkeypatch,
        [_Bal(VAULT), _Bal(AIRDROP)],
        assets={VAULT.lower(): int(ASSET, 16), AIRDROP.lower(): int(ASSET, 16)},
        tx_from={"0xdep": OWNER, "0xair": OWNER},
        logs={VAULT.lower(): [{"transactionHash": "0xdep"}],
              AIRDROP.lower(): [{"transactionHash": "0xair"}]},
        lists=_FakeLists(known=[VAULT]),
        store=_FakeStore(hidden=[AIRDROP]),
    )
    assert _run(w) == []                       # nothing to discover


def test_non_vault_holdings_never_reach_provenance(monkeypatch, qtbot):
    # SPAM's asset() reverts → filtered out before any transfer-log lookup.
    calls = []
    monkeypatch.setattr(
        txmod, "fetch_incoming_transfer_logs",
        lambda chain, token, owner, **k: calls.append(token) or [])
    client = _FakeClient({}, {})               # no asset resolves
    w = VaultProvenanceWorker(
        CHAIN, OWNER, _FakeSource([_Bal(SPAM)]), _FakeLists(), _FakeStore(),
        [OWNER], lambda: None, client_factory=lambda c: client)
    assert _run(w) == []
    assert calls == []                         # never queried transfer logs
