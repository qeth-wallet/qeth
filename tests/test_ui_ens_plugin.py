"""Tests for EnsPanel + EnsPlugin in isolation.

Drive the panel's tree-building and the plugin's lifecycle against a stub
host, with no network. Locks in: the name→subdomain→records tree shape,
expiry-status colouring, lazy records expansion, custom-name pinning, and
that the plugin renders from cache without a fetch.
"""


from unittest.mock import MagicMock

import pytest
from eth_abi import encode as abi_encode
from PySide6.QtCore import Qt

from qeth.chains import DEFAULT_CHAINS
from qeth.ens_app import (
    EnsName, EnsRecords, OwnershipCheck, build_tree,
)
from qeth.plugins.ens import (
    _EXPIRY_STYLE, _NAME_ROLE, _OWNERSHIP_ROLE, _STATUS_ROLE, _UNSAFE_ROLE,
    _VALUE_ROLE, EnsPanel, EnsPlugin,
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
        self.status_messages: list = []
        # ENS writes now open a rich composer lazily; capture the op + the
        # post-confirm callback instead of a pre-built request.
        self.ens_ops: list = []

    def status_message(self, text, timeout_ms=3000):
        self.status_messages.append(text)

    def current_chain(self):
        return self._chain

    def chain_by_id(self, chain_id: int):
        return self._chain if self._chain.chain_id == chain_id else None

    def start_worker(self, worker):
        self.started_workers.append(worker)

    def request_transaction(self, req, chain, label, on_broadcast=None,
                            on_confirmed=None):
        self.tx_requests.append((req, chain, label, on_confirmed))

    def open_ens_composer(self, name, op, chain, from_addr, *,
                          on_confirmed=None):
        self.ens_ops.append((name, op, chain, from_addr, on_confirmed))


class _StubStore:
    def __init__(self):
        self.custom_ens_names: set[str] = set()
        # Accounts the wallet can sign for; _can_sign() reads source.
        self.accounts: list[dict] = []

    def add_custom_ens_name(self, name: str) -> None:
        self.custom_ens_names.add(name.strip().lower())


# --- EnsPanel --------------------------------------------------------------

class TestEnsNamesWorker:
    def test_merges_registrant_only_names(self, monkeypatch):
        # BENS gives the controller-owned names; the registrant sweep adds the
        # ones it misses (crv.eth), deduped, with its skip-set excluding the
        # .eth labelhashes BENS already returned.
        import qeth.ens_app as ea
        from qeth.plugins.ens import EnsNamesWorker

        monkeypatch.setattr(
            "qeth.plugins.ens.lookup_owned_names",
            lambda cid, addr: [EnsName("curvelend.eth"), EnsName("qeth.eth")])
        captured = {}

        def fake_registrant(cid, addr, *, skip_labelhashes):
            captured["skip"] = skip_labelhashes
            # returns crv.eth (new) + curvelend.eth (dup, must be dropped)
            return [EnsName("crv.eth", source="owned"),
                    EnsName("curvelend.eth")]
        monkeypatch.setattr(ea, "lookup_registrant_names", fake_registrant)

        worker = EnsNamesWorker("0xabc", [])
        got = {}
        worker.ready.connect(lambda a, ns: got.update(addr=a, names=ns))
        worker.run()
        names = sorted(n.name for n in got["names"])
        assert names == ["crv.eth", "curvelend.eth", "qeth.eth"]   # deduped
        # the skip-set held the labelhashes of the BENS .eth 2LDs
        assert int.from_bytes(ea._labelhash("curvelend"), "big") in captured["skip"]
        assert int.from_bytes(ea._labelhash("qeth"), "big") in captured["skip"]


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

    def _role_rows(self, item):
        """The manager/owner rows under a name → {label: shown-address}."""
        return {item.child(i).text(0): item.child(i).text(2)
                for i in range(item.childCount())
                if item.child(i).data(0, _OWNERSHIP_ROLE)}

    def test_mark_verified_renders_manager_and_owner_rows(self, qtbot):
        from eth_utils import to_checksum_address
        panel = EnsPanel()
        qtbot.addWidget(panel)
        # crv.eth: distinct manager (controller) and owner (registrant).
        mgr = "0x39415255619783A2E71fcF7d8f708A951d92e1b6"
        own = "0x7a16fF8270133F063aAb6C9977183D9e72835428"
        panel.populate(build_tree([EnsName("crv.eth", source="custom")]), NOW)
        panel.mark_verified({"crv.eth": OwnershipCheck(
            controller=mgr, registrant=own, owner_known=True)}, own)
        root = panel.tree.topLevelItem(0)
        rows = self._role_rows(root)
        assert rows["manager"] == to_checksum_address(mgr)
        assert rows["owner"] == to_checksum_address(own)
        # both carry their address as the copyable value
        mrow = next(root.child(i) for i in range(root.childCount())
                    if root.child(i).text(0) == "manager")
        assert mrow.data(0, _VALUE_ROLE) == to_checksum_address(mgr)

    def test_ownership_rows_survive_records_reload(self, qtbot):
        # Records load lazily after verification; the manager/owner rows must
        # not be wiped when the record rows arrive.
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(build_tree([EnsName("alice.eth")]), NOW)
        panel.mark_verified({"alice.eth": OwnershipCheck(
            controller=me, registrant=me, owner_known=True)}, me)
        root = panel.tree.topLevelItem(0)
        assert set(self._role_rows(root)) == {"manager", "owner"}
        panel.add_records("alice.eth", EnsRecords(texts={"url": "x"}))
        assert set(self._role_rows(root)) == {"manager", "owner"}   # still there
        labels = [root.child(i).text(0) for i in range(root.childCount())]
        assert "url" in labels                                      # records too

    def test_subdomain_shows_manager_only(self, qtbot):
        # A subdomain has a controller but no registrant → just a manager row.
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(build_tree([
            EnsName("vitalik.eth"), EnsName("dao.vitalik.eth")]), NOW)
        panel.mark_verified({
            "vitalik.eth": OwnershipCheck(controller=me, registrant=me,
                                          owner_known=True),
            "dao.vitalik.eth": OwnershipCheck(controller=me, owner_known=True),
        }, me)
        sub = panel._items_by_name["dao.vitalik.eth"]
        assert set(self._role_rows(sub)) == {"manager"}

    def _bar_panel(self, qtbot, *, writable=(), transferable=(), reclaimable=()):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([EnsName("crv.eth", source="custom")]), NOW)
        panel.set_writable(set(writable))
        panel.set_transferable(set(transferable))
        panel.set_reclaimable(set(reclaimable))
        return panel, panel.tree.topLevelItem(0)

    def test_action_bar_name_mode_full_rights(self, qtbot):
        panel, root = self._bar_panel(
            qtbot, writable={"crv.eth"}, transferable={"crv.eth"},
            reclaimable={"crv.eth"})
        panel.tree.setCurrentItem(root)
        assert not any(b.isHidden() for b in panel._name_btns)
        assert all(b.isHidden() for b in panel._rec_btns)
        for b in (panel._b_transfer, panel._b_renew, panel._b_manager,
                  panel._b_addr, panel._b_content, panel._b_copyname):
            assert b.isEnabled()

    def test_action_bar_owner_only_disables_record_buttons(self, qtbot):
        # registrant but not manager → can transfer / extend / set-manager /
        # copy, but the record-write buttons are disabled.
        panel, root = self._bar_panel(
            qtbot, transferable={"crv.eth"}, reclaimable={"crv.eth"})
        panel.tree.setCurrentItem(root)
        assert panel._b_transfer.isEnabled()
        assert panel._b_renew.isEnabled()
        assert panel._b_manager.isEnabled()
        assert panel._b_copyname.isEnabled()
        assert not panel._b_addr.isEnabled()
        assert not panel._b_content.isEnabled()

    def test_action_bar_switches_to_copy_edit_on_record(self, qtbot):
        panel, root = self._bar_panel(qtbot, writable={"crv.eth"})
        panel.add_records("crv.eth", EnsRecords(texts={"url": "https://x"}))
        url = next(root.child(i) for i in range(root.childCount())
                   if root.child(i).text(0) == "url")
        panel.tree.setCurrentItem(url)
        assert all(b.isHidden() for b in panel._name_btns)
        assert not any(b.isHidden() for b in panel._rec_btns)
        assert panel._b_reccopy.isEnabled()
        assert panel._b_recedit.isEnabled()           # parent is writable

    def test_action_bar_edit_disabled_on_role_row(self, qtbot):
        # The manager/owner rows are not editable records → Copy yes, Edit no.
        from qeth.ens_app import OwnershipCheck
        panel, root = self._bar_panel(qtbot, writable={"crv.eth"})
        me = "0x" + "11" * 20
        panel.mark_verified({"crv.eth": OwnershipCheck(
            controller=me, registrant=me, owner_known=True)}, me)
        mgr = next(root.child(i) for i in range(root.childCount())
                   if root.child(i).text(0) == "manager")
        panel.tree.setCurrentItem(mgr)
        assert panel._b_reccopy.isEnabled()
        assert not panel._b_recedit.isEnabled()

    def test_ctrl_c_copies_name_record_and_role(self, qtbot):
        from PySide6.QtWidgets import QApplication
        from qeth.ens_app import OwnershipCheck
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(build_tree([EnsName("crv.eth", source="custom")]), NOW)
        mgr = "0x39415255619783A2E71fcF7d8f708A951d92e1b6"
        panel.mark_verified({"crv.eth": OwnershipCheck(
            controller=mgr, registrant=me, owner_known=True)}, me)
        panel.add_records("crv.eth", EnsRecords(texts={"url": "https://x"}))
        root = panel.tree.topLevelItem(0)
        copied = []
        panel.copied.connect(copied.append)

        # name row → the name
        panel.tree.setCurrentItem(root)
        panel._copy_current()
        assert QApplication.clipboard().text() == "crv.eth"
        # a record row → its value
        url = next(root.child(i) for i in range(root.childCount())
                   if root.child(i).text(0) == "url")
        panel.tree.setCurrentItem(url)
        panel._copy_current()
        assert QApplication.clipboard().text() == "https://x"
        # the manager role row → the address it shows
        mrow = next(root.child(i) for i in range(root.childCount())
                    if root.child(i).text(0) == "manager")
        panel.tree.setCurrentItem(mrow)
        panel._copy_current()
        from eth_utils import to_checksum_address
        assert QApplication.clipboard().text() == to_checksum_address(mgr)
        assert copied[-1] == to_checksum_address(mgr)   # announced each time

    def test_ctrl_c_action_is_registered_on_the_tree(self, qtbot):
        # A tree-scoped Copy shortcut is wired to the copy handler (the actual
        # keystroke path; verified separately not to leak focus state).
        from PySide6.QtGui import QKeySequence
        panel = EnsPanel()
        qtbot.addWidget(panel)
        copy_seq = QKeySequence(QKeySequence.StandardKey.Copy)
        acts = [a for a in panel.tree.actions() if a.shortcut() == copy_seq]
        assert acts
        assert (acts[0].shortcutContext()
                == Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def test_copy_announces_to_status_line(self, qtbot):
        plugin = EnsPlugin(_StubStore())
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin.widget()._copy("crv.eth")
        assert host.status_messages == ["Copied crv.eth to clipboard"]

    def test_no_records_placeholder_is_gone(self, qtbot):
        # A name with zero resolver records still isn't empty (owner/manager),
        # so we never render a "no records" note.
        from qeth.ens_app import OwnershipCheck
        panel = EnsPanel()
        qtbot.addWidget(panel)
        me = "0x" + "11" * 20
        panel.populate(build_tree([EnsName("crv.eth", source="custom")]), NOW)
        panel.mark_verified({"crv.eth": OwnershipCheck(
            controller=me, registrant=me, owner_known=True)}, me)
        panel.add_records("crv.eth", EnsRecords())        # no records at all
        root = panel.tree.topLevelItem(0)
        labels = [root.child(i).text(0) for i in range(root.childCount())]
        assert "no records" not in labels
        assert set(labels) == {"manager", "owner"}

    def test_named_buttons_have_mnemonics(self, qtbot):
        # Transfer / Extend carry &-mnemonics like Send / Add Account; the
        # icon-only utilities have no label (so no mnemonic).
        panel = EnsPanel()
        qtbot.addWidget(panel)
        assert panel._b_transfer.text() == "&Transfer"
        assert panel._b_renew.text() == "&Extend"
        assert not panel._b_transfer.shortcut().isEmpty()
        assert panel._b_manager.text() == ""

    def test_add_button_emits_add_custom(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        seen = []
        panel.add_custom_requested.connect(lambda: seen.append(True))
        panel._b_add.click()
        assert seen == [True]

    def test_plugin_action_widgets_are_the_panel_buttons(self, qtbot):
        # The ENS buttons ride the slot's shared bottom row (like Tokens), with
        # the add button always present.
        plugin = EnsPlugin(_StubStore())
        plugin.attach(_StubHost(address=ADDR))
        qtbot.addWidget(plugin.widget())
        widgets = plugin.action_widgets()
        assert widgets and plugin.widget()._b_add in widgets
        assert plugin.widget()._b_transfer in widgets

    def test_action_bar_buttons_emit_and_copy(self, qtbot):
        from PySide6.QtWidgets import QApplication
        panel, root = self._bar_panel(qtbot, transferable={"crv.eth"})
        panel.tree.setCurrentItem(root)
        seen = []
        panel.write_requested.connect(lambda nm, k: seen.append((nm, k)))
        panel._b_transfer.click()
        assert seen == [("crv.eth", "transfer")]
        panel._b_copyname.click()
        assert QApplication.clipboard().text() == "crv.eth"

    def test_every_menu_action_has_an_icon(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.populate(build_tree([EnsName("vitalik.eth",
                                            resolved_address="0x" + "11" * 20)]),
                       NOW)
        panel.set_writable({"vitalik.eth"})
        panel.set_transferable({"vitalik.eth"})
        panel.set_reclaimable({"vitalik.eth"})
        menu = panel._build_menu(panel._items_by_name["vitalik.eth"])
        actions = [a for a in menu.actions() if not a.isSeparator()]
        assert actions and all(not a.icon().isNull() for a in actions)
        # the Set manager entry reuses the manager row icon
        sm = next(a for a in actions if a.text() == "Set manager")
        assert sm.icon().cacheKey() == panel._manager_icon.cacheKey()

    def _menu_kinds(self, panel, name):
        n = EnsName(name)
        return [kind for group in panel._write_menu_groups(n) for _label, kind in group]

    def test_owner_only_name_offers_transfer_and_set_manager(self, qtbot):
        # A name held as registrant but managed elsewhere (crv.eth) must still
        # offer Transfer + Set manager (+ renew), and NOT the record actions.
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.set_transferable({"crv.eth"})
        panel.set_reclaimable({"crv.eth"})
        kinds = self._menu_kinds(panel, "crv.eth")
        assert kinds == ["renew", "transfer", "manager"]   # no record actions

    def test_manager_only_name_offers_records_not_transfer(self, qtbot):
        # The controller (manager) sets records + renews, but a manager who
        # isn't the owner can't transfer or reclaim.
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.set_writable({"dao.eth"})
        kinds = self._menu_kinds(panel, "dao.eth")
        assert "transfer" not in kinds and "manager" not in kinds
        assert kinds == ["renew", "addr", "content", "text", "record", "subdomain"]

    def test_owner_and_manager_name_offers_everything(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.set_writable({"me.eth"})
        panel.set_transferable({"me.eth"})
        panel.set_reclaimable({"me.eth"})
        kinds = self._menu_kinds(panel, "me.eth")
        assert kinds == ["renew", "transfer", "manager",
                         "addr", "content", "text", "record", "subdomain"]

    def test_unrelated_name_offers_no_write_actions(self, qtbot):
        panel = EnsPanel()
        qtbot.addWidget(panel)
        assert panel._write_menu_groups(EnsName("someoneelse.eth")) == []

    def test_subdomain_manager_offers_records_no_registration(self, qtbot):
        # A subdomain has no registration of its own → no renew/transfer, but
        # its manager can still set records + add deeper subdomains.
        panel = EnsPanel()
        qtbot.addWidget(panel)
        panel.set_writable({"blog.me.eth"})
        kinds = self._menu_kinds(panel, "blog.me.eth")
        assert "renew" not in kinds and "transfer" not in kinds
        assert kinds == ["addr", "content", "text", "record", "subdomain"]

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
            "qeth.plugins.ens.prompt_text",
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

    def test_record_workers_do_not_share_an_ethclient(self, qtbot):
        """Each record worker must make its own EthClient on its own thread —
        never a shared one passed in by the plugin (issue #6: EthClient owns a
        non-thread-safe requests.Session + failover state)."""
        plugin = EnsPlugin(_StubStore())
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin._on_records_requested("alice.eth")
        plugin._on_records_requested("bob.eth")
        assert len(host.started_workers) == 2
        # No worker carries a plugin-supplied client → read_records builds one
        # inside run(), on the worker's own thread.
        assert all(w._client is None for w in host.started_workers)
        assert not hasattr(plugin, "_read_client")

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

    def _composer(self, op, name="vitalik.eth", *, start_worker=None):
        """Construct the real ``_EnsWriteComposer`` for an op with mocked
        shared kwargs — so a test can drive its field group + assert what
        ``_build_request`` produces (the request is now built lazily, on
        Send-click, rather than up front)."""
        from qeth.plugins.ens import _EnsWriteComposer
        return _EnsWriteComposer(
            op, name, ETH, ADDR,
            abi_source=MagicMock(), abi_cache=MagicMock(),
            start_worker=start_worker or (lambda w: None),
            identity_source=None, identity_cache=None, tx_cache=None)

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

    def test_set_text_builds_request_to_resolver(self, qtbot):
        plugin, host, store = self._plugin(qtbot)
        plugin._write_text("vitalik.eth")
        assert len(host.ens_ops) == 1
        name, op, chain, from_addr, cb = host.ens_ops[0]
        dlg = self._composer(op, name)
        qtbot.addWidget(dlg)
        dlg._fields.key.setCurrentText("url")
        dlg._fields.value.setText("https://x")
        req = dlg._build_request()
        assert req.chain_id == 1
        assert req.from_addr.lower() == ADDR.lower()
        assert req.to_addr.lower() == self.RESOLVER.lower()
        assert req.data[2:10] == "10f13a8c"        # setText
        assert callable(cb)

    def test_set_addr_validates_and_builds(self, qtbot):
        plugin, host, store = self._plugin(qtbot)
        plugin._write_addr("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.value.setText(self.OTHER)
        req = dlg._build_request()
        assert req.to_addr.lower() == self.RESOLVER.lower()
        assert req.data[2:10] == "d5fa2b00"         # setAddr(node,address)

    def test_set_addr_rejects_garbage(self, qtbot):
        from qeth.signing import SignerError
        plugin, host, store = self._plugin(qtbot)
        plugin._write_addr("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.value.setText("not-an-address")
        # Confirm stays disabled and the request won't build — the composer
        # surfaces the error rather than a popup.
        assert dlg._inputs_valid() is False
        with pytest.raises(SignerError):
            dlg._build_request()

    def test_no_resolver_offers_to_set_one_first(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        from qeth.ens_app import ENS_REGISTRY
        plugin, host, store = self._plugin(qtbot)
        plugin._resolver_cache.clear()              # name has no resolver
        monkeypatch.setattr(
            "PySide6.QtWidgets.QMessageBox.question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
        plugin._write_addr("vitalik.eth")
        # only the resolver-setting composer is opened (the addr write deferred)
        assert len(host.ens_ops) == 1
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        assert dlg._fields is None                  # no input rows
        req = dlg._build_request()
        assert req.to_addr.lower() == ENS_REGISTRY.lower()
        assert req.data[2:10] == "1896f70a"         # setResolver

    def test_add_subdomain_unwrapped_targets_registry(self, qtbot):
        from qeth.ens_app import ENS_REGISTRY
        plugin, host, store = self._plugin(qtbot)
        plugin._add_subdomain("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.label.setText("blog")
        dlg._fields.owner.setText(self.OTHER)
        req = dlg._build_request()
        assert req.to_addr.lower() == ENS_REGISTRY.lower()
        assert req.data[2:10] == "5ef2c7f0"         # registry.setSubnodeRecord

    def test_add_subdomain_wrapped_targets_namewrapper(self, qtbot):
        from qeth.ens_app import ENS_NAME_WRAPPER
        plugin, host, store = self._plugin(qtbot)
        plugin._wrapped.add("vitalik.eth")
        plugin._add_subdomain("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.label.setText("blog")
        dlg._fields.owner.setText(self.OTHER)
        req = dlg._build_request()
        assert req.to_addr.lower() == ENS_NAME_WRAPPER.lower()
        assert req.data[2:10] == "24c1af44"         # NameWrapper.setSubnodeRecord

    def test_subdomain_rejects_dotted_label(self, qtbot):
        plugin, host, store = self._plugin(qtbot)
        plugin._add_subdomain("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.label.setText("a.b")
        dlg._fields.owner.setText(self.OTHER)
        assert dlg._inputs_valid() is False         # dotted label rejected

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
        plugin, host, store = self._plugin(qtbot)
        forced: list = []
        monkeypatch.setattr(plugin, "_on_records_requested",
                            lambda name, force=False: forced.append((name, force)))
        plugin._write_text("vitalik.eth")
        # nothing refreshes on open — only when the tx actually confirms
        assert forced == []
        _name, _op, _chain, _from, on_confirmed = host.ens_ops[0]
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
        plugin, host, store = self._plugin(qtbot)
        refreshed: list = []
        monkeypatch.setattr(plugin, "_on_refresh", lambda: refreshed.append(True))
        plugin._add_subdomain("vitalik.eth")
        _name, _op, _chain, _from, on_confirmed = host.ens_ops[0]
        on_confirmed({"status": "0x1"})
        assert refreshed == [True]

    # --- renewal ----------------------------------------------------------

    OWNED_EXP = 1_900_000_000        # a far-future expiry (year ~2030)

    def test_renew_quotes_one_year_with_usd(self, qtbot):
        from qeth.plugins.ens import EnsRenewPriceWorker
        from qeth import ens_write
        plugin, host, store = self._plugin(qtbot)
        plugin._names_by_l["vitalik.eth"].expiry_ts = self.OWNED_EXP
        plugin._renew("vitalik.eth")
        assert len(host.ens_ops) == 1
        _name, op, *_rest = host.ens_ops[0]
        # The quote worker is started by the composer's field-group factory.
        started: list = []
        dlg = self._composer(op, start_worker=started.append)
        qtbot.addWidget(dlg)
        quotes = [w for w in started if isinstance(w, EnsRenewPriceWorker)]
        assert quotes
        assert quotes[0]._label == "vitalik"               # bare label, no .eth
        assert quotes[0]._duration == ens_write.SECONDS_PER_YEAR
        assert quotes[0]._with_usd is True

    def test_renew_rejects_subdomain(self, qtbot):
        plugin, host, store = self._plugin(qtbot, owned=("blog.vitalik.eth",))
        plugin._renew("blog.vitalik.eth")                   # not a .eth 2LD
        assert host.ens_ops == []

    def test_renew_quote_reaches_field_group(self, qtbot):
        from decimal import Decimal
        from qeth.plugins.ens import _RenewFields
        # The quote lands on the renew field group via set_quote → it prices the
        # selected term and the live cost label shows ETH + USD.
        fields = _RenewFields("vitalik.eth", self.OWNED_EXP)
        qtbot.addWidget(fields)
        assert fields.selected_value_wei() is None          # unpriced initially
        fields.set_quote(1_000_000_000, Decimal("2000"))
        assert fields.selected_value_wei() is not None
        assert "ETH" in fields._cost_lbl.text()

    def test_renew_builds_payable_tx_to_controller(self, qtbot):
        from qeth.ens_app import ENS_ETH_CONTROLLER
        plugin, host, store = self._plugin(qtbot)
        plugin._names_by_l["vitalik.eth"].expiry_ts = self.OWNED_EXP
        plugin._renew("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.set_quote(1_000_000_000, None)          # 1e9 wei / year
        req = dlg._build_request()
        assert req.to_addr.lower() == ENS_ETH_CONTROLLER.lower()
        assert req.data[2:10] == "acf1a841"                 # renew(string,uint256)
        # The duration is the chosen term in raw seconds.
        duration = dlg._fields.duration_seconds()
        assert req.data[10:] == abi_encode(
            ["string", "uint256"], ["vitalik", duration]).hex()
        # value = price + 10% buffer, and it reaches the request (→ sim + gas).
        price = dlg._fields.selected_value_wei()
        assert req.value_wei == price + price // 10
        # _sim_params derives from _build_request, so the payable value is in the
        # simulation params too (an underpaid renew would falsely revert).
        assert dlg._sim_params()[3] == req.value_wei

    def test_renew_without_quote_is_not_signable(self, qtbot):
        from qeth.signing import SignerError
        plugin, host, store = self._plugin(qtbot)
        plugin._names_by_l["vitalik.eth"].expiry_ts = self.OWNED_EXP
        plugin._renew("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)                             # no quote landed
        qtbot.addWidget(dlg)
        assert dlg._inputs_valid() is False                 # Confirm disabled
        with pytest.raises(SignerError):
            dlg._build_request()

    def test_renew_confirmation_rediscovers(self, qtbot):
        plugin, host, store = self._plugin(qtbot)
        plugin._names_by_l["vitalik.eth"].expiry_ts = self.OWNED_EXP
        refreshed: list = []
        plugin._on_refresh = lambda: refreshed.append(True)
        plugin._renew("vitalik.eth")
        _name, _op, _chain, _from, on_confirmed = host.ens_ops[0]
        on_confirmed({"status": "0x1"})
        assert refreshed == [True]

    def test_renew_dialog_scales_cost_by_date(self, qtbot):
        from qeth.plugins.ens import _RenewDialog
        from decimal import Decimal
        # expiry at a known instant; default date is +1 year.
        dlg = _RenewDialog("vitalik.eth", self.OWNED_EXP)
        qtbot.addWidget(dlg)
        dlg.set_quote(1_000_000_000, Decimal("3000"))      # 1e9 wei / year, $3000/ETH
        one_year = dlg.selected_value_wei()
        # Default term is the picked day +1 calendar year (~365 days, minus the
        # base instant's time-of-day) — close to a year's price, not exact.
        assert 0.97 * 1e9 < one_year < 1.0 * 1e9
        # Push the date out by ~another year → cost ~doubles (linear in seconds).
        dlg.date.setDate(dlg.date.date().addYears(1))
        assert dlg.selected_value_wei() > one_year * 9 // 5  # clearly grew
        assert "ETH" in dlg._cost_lbl.text() and "$" in dlg._cost_lbl.text()

    # --- transfer ---------------------------------------------------------

    def test_registrant_ownership_gates_transferable(self, qtbot):
        # Only registrant (NFT-owner) names become transferable; controller-only
        # ownership is writable but NOT transferable.
        plugin, host, store = self._plugin(qtbot)
        plugin._on_verified(ADDR, {
            "vitalik.eth": OwnershipCheck(
                controller=ADDR, registrant=ADDR, owner_known=True,
                resolver=self.RESOLVER),
            "manager.eth": OwnershipCheck(
                controller=ADDR, registrant=self.OTHER, owner_known=True),
        }, True)
        panel = plugin.widget()
        assert "vitalik.eth" in panel._transferable
        assert "manager.eth" not in panel._transferable      # controller-only
        assert "manager.eth" in panel._writable              # but still writable

    def test_transfer_unwrapped_targets_registrar(self, qtbot):
        from qeth.ens_app import ENS_ETH_REGISTRAR
        plugin, host, store = self._plugin(qtbot)
        plugin._transfer("vitalik.eth")
        name, op, _chain, _from, _cb = host.ens_ops[0]
        dlg = self._composer(op, name)
        qtbot.addWidget(dlg)
        dlg._fields.recipient.setText(self.OTHER)
        req = dlg._build_request()
        assert req.to_addr.lower() == ENS_ETH_REGISTRAR.lower()
        assert req.data[2:10] == "42842e0e"                  # ERC-721 safeTransferFrom
        assert req.from_addr.lower() == ADDR.lower()
        assert "Transfer" in op.confirm_label

    def test_transfer_wrapped_targets_namewrapper(self, qtbot):
        from qeth.ens_app import ENS_NAME_WRAPPER
        plugin, host, store = self._plugin(qtbot)
        plugin._wrapped.add("vitalik.eth")
        plugin._transfer("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.recipient.setText(self.OTHER)
        req = dlg._build_request()
        assert req.to_addr.lower() == ENS_NAME_WRAPPER.lower()
        assert req.data[2:10] == "f242432a"                  # ERC-1155 safeTransferFrom

    def test_transfer_rejects_garbage_recipient(self, qtbot):
        from qeth.signing import SignerError
        plugin, host, store = self._plugin(qtbot)
        plugin._transfer("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        dlg._fields.recipient.setText("not-an-address")
        assert dlg._inputs_valid() is False
        with pytest.raises(SignerError):
            dlg._build_request()

    def test_transfer_confirmation_rediscovers(self, qtbot):
        plugin, host, store = self._plugin(qtbot)
        refreshed: list = []
        plugin._on_refresh = lambda: refreshed.append(True)
        plugin._transfer("vitalik.eth")
        _name, _op, _chain, _from, on_confirmed = host.ens_ops[0]
        on_confirmed({"status": "0x1"})
        assert refreshed == [True]

    def test_transfer_note_warns_manager_stays(self, qtbot):
        # The unwrapped transfer carries the caveat that the manager role stays
        # behind; the wrapped one says both roles move.
        plugin, host, store = self._plugin(qtbot)
        plugin._transfer("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        assert op.note and "manager role stays" in op.note
        plugin._wrapped.add("vitalik.eth")
        plugin._transfer("vitalik.eth")
        _name, wop, *_rest = host.ens_ops[1]
        assert wop.note and "move together" in wop.note

    def test_transfer_confirm_shares_the_arrow_icon(self, qtbot):
        # The transfer dialog's confirm button uses the same right-arrow as the
        # Transfer action button (and the tokens Send button).
        from PySide6.QtCore import QBuffer, QByteArray, QSize
        plugin, host, store = self._plugin(qtbot)
        plugin._transfer("vitalik.eth")
        _name, op, *_rest = host.ens_ops[0]
        assert op.confirm_icon_names == ("go-next",)
        dlg = self._composer(op)
        qtbot.addWidget(dlg)

        def png(ic):
            px = ic.pixmap(QSize(16, 16))
            ba = QByteArray(); buf = QBuffer(ba)
            buf.open(QBuffer.OpenModeFlag.WriteOnly)
            px.toImage().save(buf, "PNG")
            return bytes(ba)

        assert png(dlg.confirm_btn.icon()) == png(plugin.widget()._b_transfer.icon())

    # --- set manager (reclaim) --------------------------------------------

    def test_registrant_only_manages_via_role_split(self, qtbot):
        # A registrant who is NOT the controller can transfer + reclaim the
        # manager, but can't set records (that needs the manager role).
        plugin, host, store = self._plugin(qtbot)
        plugin._on_verified(ADDR, {
            "vitalik.eth": OwnershipCheck(
                controller=self.OTHER, registrant=ADDR, owner_known=True),
        }, True)
        panel = plugin.widget()
        assert "vitalik.eth" not in panel._writable      # not the manager
        assert "vitalik.eth" in panel._transferable      # is the owner
        assert "vitalik.eth" in panel._reclaimable       # can reclaim manager

    def test_wrapped_registrant_cannot_reclaim(self, qtbot):
        # Wrapped names manage the controller through the NameWrapper, not
        # reclaim → never offered "Set manager".
        plugin, host, store = self._plugin(qtbot)
        plugin._wrapped.add("vitalik.eth")
        plugin._on_verified(ADDR, {
            "vitalik.eth": OwnershipCheck(
                controller=ADDR, registrant=ADDR, wrapped=True,
                owner_known=True),
        }, True)
        assert "vitalik.eth" not in plugin.widget()._reclaimable

    def test_set_manager_builds_reclaim_to_registrar(self, qtbot):
        from qeth.ens_app import ENS_ETH_REGISTRAR, _labelhash
        plugin, host, store = self._plugin(qtbot)
        plugin._registrant.add("vitalik.eth")
        plugin._set_manager("vitalik.eth")
        name, op, *_rest = host.ens_ops[0]
        dlg = self._composer(op)
        qtbot.addWidget(dlg)
        # Defaults the manager field to the signer (reclaim to self).
        assert dlg._fields.value().lower() == ADDR.lower()
        dlg._fields.recipient.setText(self.OTHER)
        req = dlg._build_request()
        assert req.to_addr.lower() == ENS_ETH_REGISTRAR.lower()
        assert req.data[2:10] == "28ed4f6c"              # reclaim(uint256,address)
        token_id = int.from_bytes(_labelhash("vitalik"), "big")
        assert req.data[10:] == abi_encode(
            ["uint256", "address"], [token_id, self.OTHER]).hex()
        assert "Set manager" in op.confirm_label

    def test_set_manager_rejects_subdomain_and_wrapped(self, qtbot):
        plugin, host, store = self._plugin(qtbot, owned=("blog.vitalik.eth",))
        plugin._set_manager("blog.vitalik.eth")          # not a .eth 2LD
        assert host.ens_ops == []
        plugin._wrapped.add("vitalik.eth")
        plugin._set_manager("vitalik.eth")               # wrapped → no reclaim
        assert host.ens_ops == []


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
