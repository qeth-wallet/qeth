"""Tests for EnsPanel + EnsPlugin in isolation.

Drive the panel's tree-building and the plugin's lifecycle against a stub
host, with no network. Locks in: the name→subdomain→records tree shape,
expiry-status colouring, lazy records expansion, custom-name pinning, and
that the plugin renders from cache without a fetch.
"""


from PySide6.QtCore import Qt

from qeth.chains import DEFAULT_CHAINS
from qeth.ens_app import (
    EnsName, EnsRecords, OwnershipCheck, build_tree,
)
from qeth.plugins.ens import (
    _EXPIRY_STYLE, _NAME_ROLE, _STATUS_ROLE, _UNSAFE_ROLE, _VALUE_ROLE,
    EnsPanel, EnsPlugin,
)


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"
NOW = 1_700_000_000


class _StubHost:
    def __init__(self, chain=ETH, address: str | None = None):
        self._chain = chain
        self.selected_address = address
        self.started_workers: list = []
        self.tx_requests: list = []

    def current_chain(self):
        return self._chain

    def chain_by_id(self, chain_id: int):
        return self._chain if self._chain.chain_id == chain_id else None

    def start_worker(self, worker):
        self.started_workers.append(worker)

    def request_transaction(self, req, chain, label, on_broadcast=None,
                            on_confirmed=None):
        self.tx_requests.append((req, chain, label, on_confirmed))


class _StubStore:
    def __init__(self):
        self.custom_ens_names: set[str] = set()
        # Accounts the wallet can sign for; _can_sign() reads source.
        self.accounts: list[dict] = []

    def add_custom_ens_name(self, name: str) -> None:
        self.custom_ens_names.add(name.strip().lower())


# --- EnsPanel --------------------------------------------------------------

class TestEnsPanel:
    def test_populate_nests_subdomains_under_parent(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        names = [
            EnsName("vitalik.eth", resolved_address="0x" + "d8" * 20),
            EnsName("blog.vitalik.eth", resolved_address="0x" + "ab" * 20),
        ]
        panel.populate(build_tree(names), NOW)
        assert panel.tree.topLevelItemCount() == 1
        root = panel.tree.topLevelItem(0)
        assert root.text(0) == "vitalik.eth"
        # first child is the owned subdomain; a records placeholder follows
        sub = root.child(0)
        assert sub.text(0) == "blog.vitalik.eth"
        assert sub.data(0, _NAME_ROLE).name == "blog.vitalik.eth"

    def test_leaf_gets_lazy_records_placeholder(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([EnsName("alice.eth")]), NOW)
        root = panel.tree.topLevelItem(0)
        # a childless name is still expandable to pull records
        assert root.childCount() == 1
        placeholder = root.child(0)
        assert placeholder.data(0, _NAME_ROLE) is None

    def test_expiring_name_is_coloured(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        soon = NOW + 5 * 24 * 3600        # within the 30-day warn window
        panel.populate(build_tree([EnsName("soon.eth", expiry_ts=soon)]), NOW)
        root = panel.tree.topLevelItem(0)
        text, colour = _EXPIRY_STYLE["expiring"]
        assert root.text(1) == text
        assert root.foreground(1).color() == colour

    def test_add_records_replaces_placeholder_with_rows(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([EnsName("alice.eth")]), NOW)
        rec = EnsRecords(
            addresses={"60": "0x" + "ab" * 20},
            texts={"url": "https://alice.example"},
            contenthash="ipfs://bafyfoo",
        )
        panel.add_records("alice.eth", rec)
        root = panel.tree.topLevelItem(0)
        # placeholder gone, three record rows present
        labels = [root.child(i).text(0) for i in range(root.childCount())]
        assert "…loading records" not in labels
        assert "address" in labels
        assert "content" in labels
        assert "url" in labels
        # record rows carry their copyable value
        url_row = next(root.child(i) for i in range(root.childCount())
                       if root.child(i).text(0) == "url")
        assert url_row.data(0, _VALUE_ROLE) == "https://alice.example"

    def test_add_records_unknown_name_is_noop(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([EnsName("alice.eth")]), NOW)
        # must not raise
        panel.add_records("nobody.eth", EnsRecords())

    def test_expand_emits_records_requested_once(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([EnsName("alice.eth")]), NOW)
        seen: list[str] = []
        panel.records_requested.connect(seen.append)
        root = panel.tree.topLevelItem(0)
        root.setExpanded(True)
        root.setExpanded(False)
        root.setExpanded(True)
        assert seen == ["alice.eth"]   # guarded against re-emit

    def test_sortable_by_name_and_expiry(self, qtbot):
        from qeth.plugins.ens import _NAME_COL, _EXPIRES_COL
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([
            EnsName("zzz.eth", expiry_ts=NOW + 10 * 86400),    # expires soonest
            EnsName("aaa.eth", expiry_ts=NOW + 400 * 86400),   # expires latest
            EnsName("mmm.eth"),                                # no expiry
        ]), NOW)

        def order():
            return [panel.tree.topLevelItem(i).text(0)
                    for i in range(panel.tree.topLevelItemCount())]

        panel.tree.sortByColumn(_NAME_COL, Qt.SortOrder.AscendingOrder)
        assert order() == ["aaa.eth", "mmm.eth", "zzz.eth"]
        panel.tree.sortByColumn(_NAME_COL, Qt.SortOrder.DescendingOrder)
        assert order() == ["zzz.eth", "mmm.eth", "aaa.eth"]
        # Expires sorts by real timestamp (not the 'expiring soon' text); names
        # with no expiry sort last.
        panel.tree.sortByColumn(_EXPIRES_COL, Qt.SortOrder.AscendingOrder)
        assert order() == ["zzz.eth", "aaa.eth", "mmm.eth"]

    def test_subdomains_sort_after_domains(self, qtbot):
        from qeth.plugins.ens import _NAME_COL
        panel = EnsPanel()
        qtbot.addWidget(panel)
        # top-level mix: real 2LDs + orphan subdomains (parent not owned)
        panel.populate(build_tree([
            EnsName("zeta.eth"), EnsName("alpha.eth"),
            EnsName("dao.curvefi.eth"), EnsName("aaa.somedao.eth"),
        ]), NOW)
        panel.tree.sortByColumn(_NAME_COL, Qt.SortOrder.AscendingOrder)
        names = [panel.tree.topLevelItem(i).text(0)
                 for i in range(panel.tree.topLevelItemCount())]
        # domains first, then subdomains, alphabetical within each
        assert names == ["alpha.eth", "zeta.eth",
                         "aaa.somedao.eth", "dao.curvefi.eth"]

    def test_records_sort_by_tier_then_alpha(self, qtbot):
        from qeth.plugins.ens import _NAME_COL
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([
            EnsName("vitalik.eth"),
            EnsName("zsub.vitalik.eth"),       # subdomain (late alphabetically)
            EnsName("asub.vitalik.eth"),       # subdomain (early)
        ]), NOW)
        panel.add_records("vitalik.eth", EnsRecords(
            addresses={"60": "0xabc"},          # "address" — other record
            texts={"url": "u", "com.github": "g"},
            contenthash="ipfs://x"))            # "content" — its own tier
        root = panel.tree.topLevelItem(0)

        def labels():
            return [root.child(i).text(0) for i in range(root.childCount())]

        # tiers: subdomains, then content, then other records (alpha within each)
        panel.tree.sortByColumn(_NAME_COL, Qt.SortOrder.AscendingOrder)
        assert labels() == ["asub.vitalik.eth", "zsub.vitalik.eth",
                            "content", "address", "com.github", "url"]
        # descending reverses WITHIN each tier; the tier order is preserved
        panel.tree.sortByColumn(_NAME_COL, Qt.SortOrder.DescendingOrder)
        assert labels() == ["zsub.vitalik.eth", "asub.vitalik.eth",
                            "content", "url", "com.github", "address"]

    def test_verified_records_get_check_prefix(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([EnsName("alice.eth")]), NOW)
        rec = EnsRecords(texts={"url": "https://alice.example"})
        panel.add_records("alice.eth", rec, verified=True)
        root = panel.tree.topLevelItem(0)
        url_row = next(root.child(i) for i in range(root.childCount())
                       if root.child(i).text(0) == "url")
        # verified status lives in the icon column, value stays raw
        assert url_row.data(0, _STATUS_ROLE) == "ok"
        assert url_row.text(2) == "https://alice.example"
        assert url_row.data(0, _VALUE_ROLE) == "https://alice.example"

    def test_mark_verified_badges_ownership_and_resolution(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(
            build_tree([EnsName("alice.eth", resolved_address=me)]), NOW)
        states = {"alice.eth": OwnershipCheck(controller=me, resolved_address=me)}
        panel.mark_verified(states, me)
        root = panel.tree.topLevelItem(0)
        assert root.data(0, _STATUS_ROLE) == "ok"   # line verified (icon column)
        assert root.text(0) == "alice.eth"          # no glyph in the name
        assert root.text(2) == me                   # no glyph on the value

    def test_mark_verified_removes_disowned_name(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me, other = "0x" + "11" * 20, "0x" + "22" * 20
        panel.populate(
            build_tree([EnsName("alice.eth"), EnsName("mine.eth")]), NOW)
        # chain says someone else owns alice.eth → it's an indexer lie → dropped
        removed = panel.mark_verified({
            "alice.eth": OwnershipCheck(controller=other, owner_known=True),
            "mine.eth": OwnershipCheck(controller=me, owner_known=True),
        }, me)
        assert removed == ["alice.eth"]
        labels = [panel.tree.topLevelItem(i).text(0)
                  for i in range(panel.tree.topLevelItemCount())]
        assert not any(l.startswith("alice.eth") for l in labels)
        assert any(l.startswith("mine.eth") for l in labels)

    def test_mark_verified_drops_nonexistent_name(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(build_tree([EnsName("ghost.eth")]), NOW)
        # read landed (owner_known) but the node has no owner → doesn't exist
        removed = panel.mark_verified(
            {"ghost.eth": OwnershipCheck(controller=None, owner_known=True)}, me)
        assert removed == ["ghost.eth"]
        assert panel.tree.topLevelItemCount() == 0

    def test_failed_read_does_not_drop(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(build_tree([EnsName("maybe.eth")]), NOW)
        # owner_known False (transient/failed read) → keep, never drop
        removed = panel.mark_verified(
            {"maybe.eth": OwnershipCheck(controller=None, owner_known=False)}, me)
        assert removed == []
        assert panel.tree.topLevelItemCount() == 1

    def test_subdomain_owned_uses_control_tooltip(self, qtbot):
        from qeth.plugins.ens import _CONTROL_TIP
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(build_tree([
            EnsName("vitalik.eth"), EnsName("dao.vitalik.eth")]), NOW)
        panel.mark_verified({
            "vitalik.eth": OwnershipCheck(controller=me, owner_known=True),
            "dao.vitalik.eth": OwnershipCheck(controller=me, owner_known=True),
        }, me)
        col = panel._STATUS_COL
        sub = panel._items_by_name["dao.vitalik.eth"]
        assert _CONTROL_TIP in sub.toolTip(col)
        # the 2LD uses the ownership (not subdomain-control) tooltip
        top = panel._items_by_name["vitalik.eth"]
        assert _CONTROL_TIP not in top.toolTip(col)

    def test_mark_verified_keeps_pinned_unowned_name(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me, other = "0x" + "11" * 20, "0x" + "22" * 20
        # a custom-pinned (watched) name you don't own must NOT be removed
        panel.populate(
            build_tree([EnsName("watch.eth", source="custom")]), NOW)
        removed = panel.mark_verified(
            {"watch.eth": OwnershipCheck(controller=other)}, me)
        assert removed == []
        assert panel.tree.topLevelItemCount() == 1

    def test_confusable_name_flagged_and_never_gets_check(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        zwj = "‍"
        scam = "v" + zwj + "i" + zwj + "t" + zwj + "alik.eth"
        me = "0x" + "11" * 20
        panel.populate(build_tree([EnsName(scam, resolved_address=me)]), NOW)
        root = panel.tree.topLevelItem(0)
        # warning shown immediately (before any verification), in the icon column
        assert root.data(0, _UNSAFE_ROLE) is True
        assert root.data(0, _STATUS_ROLE) == "warn"
        # even if the chain says it's "owned" (poisoned controller), stays "warn"
        panel.mark_verified(
            {scam.lower(): OwnershipCheck(controller=me, resolved_address=me)}, me)
        assert panel.tree.topLevelItem(0).data(0, _STATUS_ROLE) == "warn"

    def test_mark_verified_replaces_stale_indexer_address(self, qtbot):
        # The verify pass reads at the chain head, so its address IS the truth:
        # a difference from the indexer's hint is just lag — replace it silently
        # (no "mismatch" alarm) and badge the line verified.
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        stale, real = "0x" + "33" * 20, "0x" + "44" * 20
        item_name = EnsName("alice.eth", resolved_address=stale)
        panel.populate(build_tree([item_name]), NOW)
        panel.mark_verified(
            {"alice.eth": OwnershipCheck(controller=me, resolved_address=real)},
            me)
        root = panel.tree.topLevelItem(0)
        assert root.data(0, _STATUS_ROLE) == "ok"     # verified, NOT an alarm
        assert root.text(2) == real                   # proven head value shown
        assert root.data(0, _NAME_ROLE).resolved_address == real


# --- EnsPlugin -------------------------------------------------------------

class TestEnsPlugin:
    def test_widget_is_panel(self, qtbot):
        plugin = EnsPlugin(_StubStore())
        w = plugin.widget()
        qtbot.addWidget(w)
        assert isinstance(w, EnsPanel)

    def test_render_from_cache_without_fetch(self, qtbot, tmp_qeth):
        store = _StubStore()
        plugin = EnsPlugin(store)
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        # prime the disk cache
        plugin._cache.save(1, ADDR, [EnsName("alice.eth")])

        plugin.on_account_changed(ADDR)
        # cache rendered synchronously
        assert plugin.widget().tree.topLevelItemCount() == 1
        # a refresh fetch is also kicked off (one worker)
        assert len(host.started_workers) == 1

    def test_account_none_clears(self, qtbot):
        plugin = EnsPlugin(_StubStore())
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin._render([EnsName("alice.eth")])
        assert plugin.widget().tree.topLevelItemCount() == 1
        plugin.on_account_changed(None)
        assert plugin.widget().tree.topLevelItemCount() == 0

    def test_add_custom_pins_and_refreshes(self, qtbot, monkeypatch):
        store = _StubStore()
        plugin = EnsPlugin(store)
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        monkeypatch.setattr(
            "qeth.plugins.ens.QInputDialog.getText",
            staticmethod(lambda *a, **k: ("Foo.ETH", True)),
        )
        plugin._on_add_custom()
        assert "foo.eth" in store.custom_ens_names
        # refresh fired with the pinned name carried into the worker
        assert len(host.started_workers) == 1
        assert "foo.eth" in host.started_workers[0]._custom

    def test_disowned_name_stays_filtered_on_rerender(self, qtbot):
        plugin = EnsPlugin(_StubStore())
        plugin.attach(_StubHost(address=ADDR))
        qtbot.addWidget(plugin.widget())
        me, other = ADDR, "0x" + "22" * 20
        names = [EnsName("alice.eth"), EnsName("mine.eth", resolved_address=me)]
        plugin._render(names)
        # verify proves alice.eth belongs to someone else → dropped + remembered
        plugin._on_verified(me, {
            "alice.eth": OwnershipCheck(controller=other, owner_known=True),
            "mine.eth": OwnershipCheck(controller=me, owner_known=True),
        }, True)
        assert "alice.eth" in plugin._denied
        # a refresh that re-lists alice.eth must not bring it back
        plugin._render(names)
        labels = [plugin.widget().tree.topLevelItem(i).text(0)
                  for i in range(plugin.widget().tree.topLevelItemCount())]
        assert not any(l.startswith("alice.eth") for l in labels)

    def test_records_request_starts_worker(self, qtbot):
        plugin = EnsPlugin(_StubStore())
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin._on_records_requested("alice.eth")
        assert len(host.started_workers) == 1
        assert host.started_workers[0]._name == "alice.eth"

    def test_records_cache_paints_instantly(self, qtbot, tmp_qeth):
        plugin = EnsPlugin(_StubStore())
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin.widget().populate(build_tree([EnsName("alice.eth")]), NOW)
        # prime the disk records cache
        rec = EnsRecords(texts={"url": "https://alice.example"})
        plugin._cache.save_records(1, "alice.eth", rec, verified=True)

        plugin._on_records_requested("alice.eth")
        # records rendered synchronously from cache (before any worker result)
        root = plugin.widget().tree.topLevelItem(0)
        labels = [root.child(i).text(0) for i in range(root.childCount())]
        assert "url" in labels
        # ...and a refresh worker still kicked off
        assert len(host.started_workers) == 1

    def test_head_read_shows_then_verified_upgrades(self, qtbot, tmp_qeth):
        # Both reads are at the chain head: the fast unverified read paints the
        # value, the Helios read upgrades it to verified ✓ — and a late
        # unverified re-emit must not downgrade it back.
        plugin = EnsPlugin(_StubStore())
        plugin.attach(_StubHost(address=ADDR))
        qtbot.addWidget(plugin.widget())
        plugin.widget().populate(build_tree([EnsName("alice.eth")]), NOW)
        rec = EnsRecords(texts={"url": "v1"})
        plugin._on_records_ready("alice.eth", rec, False, True)
        assert plugin._rec_cache["alice.eth"] == (rec, False)
        plugin._on_records_ready("alice.eth", rec, True, True)
        assert plugin._rec_cache["alice.eth"] == (rec, True)
        # a stale late unverified emit for the same value can't downgrade the ✓
        plugin._on_records_ready("alice.eth", rec, False, True)
        assert plugin._rec_cache["alice.eth"] == (rec, True)

    def test_records_glitch_does_not_wipe(self, qtbot, tmp_qeth):
        plugin = EnsPlugin(_StubStore())
        plugin.attach(_StubHost(address=ADDR))
        qtbot.addWidget(plugin.widget())
        plugin.widget().populate(build_tree([EnsName("alice.eth")]), NOW)
        good = EnsRecords(texts={"url": "https://alice.example"})
        plugin._on_records_ready("alice.eth", good, False, True)
        # a glitchy read (ok=False, empty) must NOT overwrite the shown records
        plugin._on_records_ready("alice.eth", EnsRecords(), True, False)
        assert plugin._rec_cache["alice.eth"] == (good, False)
        root = plugin.widget().tree.topLevelItem(0)
        labels = [root.child(i).text(0) for i in range(root.childCount())]
        assert "url" in labels and "no records" not in labels


# --- EnsPlugin: write actions ----------------------------------------------

class TestEnsWriteActions:
    RESOLVER = "0x231b0Ee14048e9dCcD1d247744d114a4EB5E8E63"
    OTHER = "0x" + "cd" * 20

    def _plugin(self, qtbot, *, signable=True, owned=("vitalik.eth",)):
        from qeth.plugins.ens import ENS_CHAIN_ID  # noqa: F401
        store = _StubStore()
        if signable:
            store.accounts = [{"address": ADDR, "source": "hot"}]
        else:
            store.accounts = [{"address": ADDR, "source": "watch_only"}]
        plugin = EnsPlugin(store)
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin._render([EnsName(n) for n in owned])
        for n in owned:
            plugin._resolver_cache[n.lower()] = self.RESOLVER
            plugin._owned.add(n.lower())
        return plugin, host, store

    def test_owned_signable_name_is_writable(self, qtbot):
        plugin, host, store = self._plugin(qtbot)
        plugin._on_verified(ADDR, {
            "vitalik.eth": OwnershipCheck(
                controller=ADDR, owner_known=True, resolver=self.RESOLVER),
        }, True)
        assert "vitalik.eth" in plugin.widget()._writable

    def test_watch_only_is_not_writable(self, qtbot):
        plugin, host, store = self._plugin(qtbot, signable=False)
        plugin._on_verified(ADDR, {
            "vitalik.eth": OwnershipCheck(
                controller=ADDR, owner_known=True, resolver=self.RESOLVER),
        }, True)
        assert plugin.widget()._writable == set()

    def test_set_text_builds_request_to_resolver(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QDialog
        from qeth.plugins.ens import _RecordDialog
        plugin, host, store = self._plugin(qtbot)
        monkeypatch.setattr(_RecordDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(_RecordDialog, "result_values",
                            lambda self: ("Text record", "url", "https://x"))
        plugin._write_text("vitalik.eth")
        assert len(host.tx_requests) == 1
        req, chain, label, cb = host.tx_requests[0]
        assert req.chain_id == 1
        assert req.from_addr.lower() == ADDR.lower()
        assert req.to_addr.lower() == self.RESOLVER.lower()
        assert req.data[2:10] == "10f13a8c"        # setText
        assert callable(cb)

    def test_set_addr_validates_and_builds(self, qtbot, monkeypatch):
        plugin, host, store = self._plugin(qtbot)
        monkeypatch.setattr(
            "qeth.plugins.ens.QInputDialog.getText",
            staticmethod(lambda *a, **k: (self.OTHER, True)))
        plugin._write_addr("vitalik.eth")
        req, *_ = host.tx_requests[0]
        assert req.to_addr.lower() == self.RESOLVER.lower()
        assert req.data[2:10] == "d5fa2b00"         # setAddr(node,address)

    def test_set_addr_rejects_garbage(self, qtbot, monkeypatch):
        warned: list = []
        plugin, host, store = self._plugin(qtbot)
        monkeypatch.setattr(
            "qeth.plugins.ens.QInputDialog.getText",
            staticmethod(lambda *a, **k: ("not-an-address", True)))
        monkeypatch.setattr(plugin, "_warn", lambda t: warned.append(t))
        plugin._write_addr("vitalik.eth")
        assert host.tx_requests == []               # nothing submitted
        assert warned                               # user told why

    def test_no_resolver_offers_to_set_one_first(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        from qeth.ens_app import ENS_REGISTRY
        plugin, host, store = self._plugin(qtbot)
        plugin._resolver_cache.clear()              # name has no resolver
        monkeypatch.setattr(
            "qeth.plugins.ens.QInputDialog.getText",
            staticmethod(lambda *a, **k: (self.OTHER, True)))
        monkeypatch.setattr(
            "PySide6.QtWidgets.QMessageBox.question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
        plugin._write_addr("vitalik.eth")
        # only the resolver-setting tx is submitted (the addr write is deferred)
        assert len(host.tx_requests) == 1
        req, *_ = host.tx_requests[0]
        assert req.to_addr.lower() == ENS_REGISTRY.lower()
        assert req.data[2:10] == "1896f70a"         # setResolver

    def test_add_subdomain_unwrapped_targets_registry(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QDialog
        from qeth.ens_app import ENS_REGISTRY
        from qeth.plugins.ens import _SubnodeDialog
        owner = self.OTHER
        plugin, host, store = self._plugin(qtbot)
        monkeypatch.setattr(_SubnodeDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(_SubnodeDialog, "values",
                            lambda self: ("blog", owner))
        plugin._add_subdomain("vitalik.eth")
        req, _c, _l, cb = host.tx_requests[0]
        assert req.to_addr.lower() == ENS_REGISTRY.lower()
        assert req.data[2:10] == "5ef2c7f0"         # registry.setSubnodeRecord

    def test_add_subdomain_wrapped_targets_namewrapper(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QDialog
        from qeth.ens_app import ENS_NAME_WRAPPER
        from qeth.plugins.ens import _SubnodeDialog
        owner = self.OTHER
        plugin, host, store = self._plugin(qtbot)
        plugin._wrapped.add("vitalik.eth")
        monkeypatch.setattr(_SubnodeDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(_SubnodeDialog, "values",
                            lambda self: ("blog", owner))
        plugin._add_subdomain("vitalik.eth")
        req, *_ = host.tx_requests[0]
        assert req.to_addr.lower() == ENS_NAME_WRAPPER.lower()
        assert req.data[2:10] == "24c1af44"         # NameWrapper.setSubnodeRecord

    def test_subdomain_rejects_dotted_label(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QDialog
        from qeth.plugins.ens import _SubnodeDialog
        owner = self.OTHER
        plugin, host, store = self._plugin(qtbot)
        monkeypatch.setattr(plugin, "_warn", lambda t: None)
        monkeypatch.setattr(_SubnodeDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(_SubnodeDialog, "values",
                            lambda self: ("a.b", owner))
        plugin._add_subdomain("vitalik.eth")
        assert host.tx_requests == []

    def test_edit_record_routes_by_label(self, qtbot):
        plugin, host, store = self._plugin(qtbot)
        calls: list = []
        plugin._write_addr = lambda name, prefill="": calls.append(("addr", prefill))
        plugin._write_content = lambda name, prefill="": calls.append(("content", prefill))
        plugin._write_text = lambda name, key="", value="": calls.append(("text", key, value))
        plugin._write_record = lambda name, **kw: calls.append(("record", kw))
        plugin._on_edit_record("vitalik.eth", "address", "0xabc")
        plugin._on_edit_record("vitalik.eth", "content", "ipfs://x")
        plugin._on_edit_record("vitalik.eth", "url", "https://x")
        plugin._on_edit_record("vitalik.eth", "address (OP)", "0xdef")
        assert calls[0] == ("addr", "0xabc")
        assert calls[1] == ("content", "ipfs://x")
        assert calls[2] == ("text", "url", "https://x")
        assert calls[3][0] == "record" and calls[3][1]["coin"] == "OP"

    def test_confirmation_force_refreshes_records(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QDialog
        from qeth.plugins.ens import _RecordDialog
        plugin, host, store = self._plugin(qtbot)
        forced: list = []
        monkeypatch.setattr(plugin, "_on_records_requested",
                            lambda name, force=False: forced.append((name, force)))
        monkeypatch.setattr(_RecordDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(_RecordDialog, "result_values",
                            lambda self: ("Text record", "url", "v"))
        plugin._write_text("vitalik.eth")
        # nothing refreshes on broadcast — only when the tx actually confirms
        assert forced == []
        _req, _chain, _label, on_confirmed = host.tx_requests[0]
        on_confirmed({"status": "0x1"})
        assert forced == [("vitalik.eth", True)]

    def test_force_refresh_drops_stale_cache_and_disk(self, qtbot, tmp_qeth):
        plugin, host, store = self._plugin(qtbot)
        old = EnsRecords(texts={"url": "old"})
        # a previously-verified value sits in memory + on disk
        plugin._rec_cache["vitalik.eth"] = (old, True)
        plugin._cache.save_records(1, "vitalik.eth", old, verified=True)
        plugin._on_records_requested("vitalik.eth", force=True)
        # forcing wipes the stale state so the fresh head read becomes the truth
        assert "vitalik.eth" not in plugin._rec_cache
        assert plugin._cache.load_records(1, "vitalik.eth") is None
        # the new head read then shows immediately, marked unverified (no proof)
        new = EnsRecords(texts={"url": "new"})
        plugin._on_records_ready("vitalik.eth", new, False, True)
        assert plugin._rec_cache["vitalik.eth"] == (new, False)

    def test_subdomain_confirmation_rediscovers(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QDialog
        from qeth.plugins.ens import _SubnodeDialog
        owner = self.OTHER
        plugin, host, store = self._plugin(qtbot)
        refreshed: list = []
        monkeypatch.setattr(plugin, "_on_refresh", lambda: refreshed.append(True))
        monkeypatch.setattr(_SubnodeDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(_SubnodeDialog, "values",
                            lambda self: ("blog", owner))
        plugin._add_subdomain("vitalik.eth")
        _req, _chain, _label, on_confirmed = host.tx_requests[0]
        on_confirmed({"status": "0x1"})
        assert refreshed == [True]


# --- EnsPlugin: name-row resolution follows the head address read ----------

class TestResolvedAddressFollowsRecords:
    OLD = "0x" + "99" * 20
    NEW = "0x" + "22" * 20

    def _plugin(self, qtbot):
        plugin = EnsPlugin(_StubStore())
        plugin.attach(_StubHost(address=ADDR))
        qtbot.addWidget(plugin.widget())
        plugin.widget().populate(
            build_tree([EnsName("curvelend.eth", resolved_address=self.OLD)]), NOW)
        return plugin

    def _row_addr(self, plugin):
        return plugin.widget().tree.topLevelItem(0).text(2)

    def test_records_read_updates_name_row_address(self, qtbot, tmp_qeth):
        plugin = self._plugin(qtbot)
        assert self._row_addr(plugin) == self.OLD
        # a head read carrying the new addr updates the prominent name-row column
        rec = EnsRecords(addresses={"60": self.NEW})
        plugin._on_records_ready("curvelend.eth", rec, False, True)
        assert self._row_addr(plugin) == self.NEW

    def test_normal_read_does_not_clear_address(self, qtbot, tmp_qeth):
        # An ordinary expand whose head read has no addr (e.g. CCIP/offchain)
        # must NOT blank a resolution the name row already shows.
        plugin = self._plugin(qtbot)
        plugin._on_records_ready("curvelend.eth", EnsRecords(), False, True)
        assert self._row_addr(plugin) == self.OLD

    def test_forced_read_clears_address_on_zero(self, qtbot, tmp_qeth):
        # A just-confirmed write (force) IS authoritative — an empty head read
        # means the addr was set to 0x0, so the name row clears.
        plugin = self._plugin(qtbot)
        plugin._on_records_requested("curvelend.eth", force=True)
        plugin._on_records_ready("curvelend.eth", EnsRecords(), False, True)
        assert self._row_addr(plugin) == ""

    def test_lagging_verified_read_does_not_revert_to_old(self, qtbot, tmp_qeth):
        # The bug: after a setAddr confirms, the fast RPC read shows the NEW
        # address, but Helios's verified head still trails the execution RPC and
        # proves the OLD value — which must NOT overwrite the fresh one.
        plugin = self._plugin(qtbot)
        new_rec = EnsRecords(addresses={"60": self.NEW})
        old_rec = EnsRecords(addresses={"60": self.OLD})
        # fast RPC head read: the new value
        plugin._on_records_ready("curvelend.eth", new_rec, False, True)
        assert self._row_addr(plugin) == self.NEW
        assert plugin._rec_cache["curvelend.eth"] == (new_rec, False)
        # lagging Helios proof of the OLD value must be ignored
        plugin._on_records_ready("curvelend.eth", old_rec, True, True)
        assert self._row_addr(plugin) == self.NEW
        assert plugin._rec_cache["curvelend.eth"] == (new_rec, False)
        # once Helios catches up and proves the NEW value, it earns the ✓
        plugin._on_records_ready("curvelend.eth", new_rec, True, True)
        assert self._row_addr(plugin) == self.NEW
        assert plugin._rec_cache["curvelend.eth"] == (new_rec, True)
