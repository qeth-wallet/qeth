"""Plugin registry: manifest filtering + the disabled-plugins store setting."""

from types import SimpleNamespace

from qeth.plugins.registry import (
    BUILTIN_MANIFESTS, PluginManifest, enabled_manifests,
)


def _store(disabled):
    return SimpleNamespace(disabled_plugins=set(disabled))


def _never(store):        # a factory the filtering tests must never call
    raise AssertionError("factory called during filtering")


def _ids(disabled, manifests=BUILTIN_MANIFESTS):
    return [m.id for m in enabled_manifests(_store(disabled), manifests)]


def test_default_mounts_all_in_order():
    assert _ids(set()) == ["wallets", "tokens", "transactions", "ens", "approvals"]


def test_optional_disabled_is_dropped():
    ids = _ids({"tokens"})
    assert "tokens" not in ids
    assert ids == ["wallets", "transactions", "ens", "approvals"]   # both need transactions, on


def test_required_cannot_be_disabled():
    # A stale disable of a required plugin is ignored (it still mounts).
    assert "transactions" in _ids({"transactions"})
    assert "wallets" in _ids({"wallets"})


def test_unknown_disabled_id_is_ignored():
    assert len(_ids({"does-not-exist"})) == 5


def test_requires_cascade_drops_dependents():
    a = PluginManifest(id="a", title="A", factory=_never, slot="right", order=1)
    b = PluginManifest(id="b", title="B", factory=_never, slot="right",
                       order=2, requires=("a",))
    both = (a, b)
    assert _ids(set(), both) == ["a", "b"]
    assert _ids({"a"}, both) == []            # a off → b (requires a) off too


def test_filtering_never_constructs_a_plugin():
    # enabled_manifests must not call factories (lazy — construction is the
    # caller's job, and a disabled plugin must never be imported).
    a = PluginManifest(id="a", title="A", factory=_never, slot="right", order=1)
    enabled_manifests(_store(set()), (a,))    # _never would raise if called


def test_disabled_plugins_store_roundtrip(tmp_qeth):
    from qeth.store import Store
    s = Store.load()
    assert s.disabled_plugins == set()
    s.set_plugin_enabled("tokens", False)
    assert s.disabled_plugins == {"tokens"}
    assert Store.load().disabled_plugins == {"tokens"}     # persisted
    s.set_plugin_enabled("tokens", True)
    assert Store.load().disabled_plugins == set()          # re-enabled
