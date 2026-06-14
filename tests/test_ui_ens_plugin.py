"""Tests for EnsPanel + EnsPlugin in isolation.

Drive the panel's tree-building and the plugin's lifecycle against a stub
host, with no network. Locks in: the name→subdomain→records tree shape,
expiry-status colouring, lazy records expansion, custom-name pinning, and
that the plugin renders from cache without a fetch.
"""

from typing import Optional

from PySide6.QtCore import Qt

from qeth.chains import DEFAULT_CHAINS
from qeth.ens_app import (
    EnsName, EnsNode, EnsRecords, OwnershipCheck, build_tree,
)
from qeth.plugins.ens import (
    _EXPIRY_STYLE, _NAME_ROLE, _STATUS_ROLE, _UNSAFE_ROLE, _VALUE_ROLE,
    EnsPanel, EnsPlugin,
)


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"
NOW = 1_700_000_000


class _StubHost:
    def __init__(self, chain=ETH, address: Optional[str] = None):
        self._chain = chain
        self.selected_address = address
        self.started_workers: list = []

    def current_chain(self):
        return self._chain

    def chain_by_id(self, chain_id: int):
        return self._chain if self._chain.chain_id == chain_id else None

    def start_worker(self, worker):
        self.started_workers.append(worker)


class _StubStore:
    def __init__(self):
        self.custom_ens_names: set[str] = set()

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
        from PySide6.QtCore import Qt
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

    def test_mark_verified_corrects_resolution_mismatch(self, qtbot):
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
        assert root.data(0, _STATUS_ROLE) == "warn"   # mismatch flagged
        assert root.text(2) == real                   # corrected value, no glyph
        # the proof-verified address replaces the indexer's, so copy yields it
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

    def test_records_ready_keeps_verified_over_late_unverified(self, qtbot, tmp_qeth):
        plugin = EnsPlugin(_StubStore())
        plugin.attach(_StubHost(address=ADDR))
        qtbot.addWidget(plugin.widget())
        plugin.widget().populate(build_tree([EnsName("alice.eth")]), NOW)
        verified_rec = EnsRecords(texts={"url": "verified"})
        plugin._on_records_ready("alice.eth", verified_rec, True)
        # a later unverified emit must not clobber the verified result
        plugin._on_records_ready("alice.eth", EnsRecords(texts={"url": "stale"}), False)
        assert plugin._rec_cache["alice.eth"] == (verified_rec, True)
