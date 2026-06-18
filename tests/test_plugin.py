"""Tests for qeth.plugin — Plugin ABC + Slot widget.

These pin the contract that the refactor will lean on: a single
plugin in a slot has no tab bar; adding a second makes it appear;
lifecycle broadcasts reach every plugin; switching tabs swaps both
the visible widget AND the bottom action row.
"""


from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from qeth.plugin import Plugin, Slot


class _StubHost:
    """Minimal Host-shaped stand-in for plugin tests."""
    selected_address: str | None = None

    def current_chain(self):
        return None

    def start_worker(self, worker) -> None:
        pass

    def status_message(self, text: str, timeout_ms: int = 3000) -> None:
        pass


class _RecorderPlugin(Plugin):
    """Plugin that records every lifecycle call it receives."""

    def __init__(self, name: str, n_actions: int = 0):
        super().__init__()
        self.name = name
        self._widget = QLabel(f"{name} widget")
        self._actions = [QPushButton(f"{name}-{i}") for i in range(n_actions)]
        self.events: list[tuple[str, object]] = []

    def widget(self) -> QWidget:
        return self._widget

    def action_widgets(self):
        return list(self._actions)

    def on_account_changed(self, address):
        self.events.append(("account", address))

    def on_chain_changed(self):
        self.events.append(("chain", None))

    def on_activated(self):
        self.events.append(("activated", None))


# --- single-plugin slot has no tab bar ------------------------------------

def test_single_plugin_slot_hides_tab_bar(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    p = _RecorderPlugin("Solo")
    slot.add_plugin(p, _StubHost())
    assert slot._tab_bar.isHidden()
    assert slot.active() is p


def test_two_plugins_show_tab_bar(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    a = _RecorderPlugin("Alpha")
    b = _RecorderPlugin("Beta")
    slot.add_plugin(a, _StubHost())
    slot.add_plugin(b, _StubHost())
    assert not slot._tab_bar.isHidden()
    labels = [slot._tab_bar.tabText(i) for i in range(slot._tab_bar.count())]
    assert labels == ["Alpha", "Beta"]


# --- attach / host wiring -------------------------------------------------

def test_attach_passes_host(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    host = _StubHost()
    p = _RecorderPlugin("X")
    slot.add_plugin(p, host)
    assert p.host is host


# --- broadcasts hit every plugin ------------------------------------------

def test_broadcast_account_changed_reaches_all_plugins(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    a = _RecorderPlugin("Alpha")
    b = _RecorderPlugin("Beta")
    slot.add_plugin(a, _StubHost())
    slot.add_plugin(b, _StubHost())

    slot.broadcast_account_changed("0xabc")
    assert ("account", "0xabc") in a.events
    assert ("account", "0xabc") in b.events


def test_broadcast_chain_changed_reaches_all_plugins(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    a = _RecorderPlugin("Alpha")
    b = _RecorderPlugin("Beta")
    slot.add_plugin(a, _StubHost())
    slot.add_plugin(b, _StubHost())

    slot.broadcast_chain_changed()
    assert ("chain", None) in a.events
    assert ("chain", None) in b.events


# --- tab switch swaps stack + action row, fires on_activated --------------

def test_tab_switch_updates_stack_and_actions(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    a = _RecorderPlugin("Alpha", n_actions=2)
    b = _RecorderPlugin("Beta", n_actions=3)
    slot.add_plugin(a, _StubHost())
    slot.add_plugin(b, _StubHost())

    # Initial state: plugin a active. Mounting a fired on_activated
    # once (tab bar -1 → 0); mounting b did not (current stayed at 0).
    assert slot._plugin_actions.count() == 2
    assert slot._stack.currentWidget() is a.widget()
    assert a.events.count(("activated", None)) == 1
    assert b.events.count(("activated", None)) == 0

    # Clear so we only see post-switch lifecycle calls.
    a.events.clear()
    b.events.clear()

    # Switch to b.
    slot._tab_bar.setCurrentIndex(1)
    assert slot.active() is b
    assert slot._stack.currentWidget() is b.widget()
    assert slot._plugin_actions.count() == 3
    assert ("activated", None) in b.events
    # a should NOT be re-activated by the switch.
    assert ("activated", None) not in a.events


def test_active_plugin_changed_signal_fires(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    a = _RecorderPlugin("Alpha")
    b = _RecorderPlugin("Beta")
    slot.add_plugin(a, _StubHost())
    slot.add_plugin(b, _StubHost())

    with qtbot.waitSignal(slot.active_plugin_changed, timeout=500) as blocker:
        slot._tab_bar.setCurrentIndex(1)
    assert blocker.args == [b]


# --- set_active works on single- and multi-plugin slots ------------------

def test_set_active_in_single_plugin_slot_runs_actions_rebuild(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    p = _RecorderPlugin("Solo", n_actions=2)
    slot.add_plugin(p, _StubHost())
    # Single-plugin slot: add_plugin already pointed the stack at p
    # and built its action row. set_active should be idempotent.
    slot.set_active(p)
    assert slot._plugin_actions.count() == 2


def test_set_active_switches_tab_in_multi_plugin_slot(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    a = _RecorderPlugin("Alpha")
    b = _RecorderPlugin("Beta")
    slot.add_plugin(a, _StubHost())
    slot.add_plugin(b, _StubHost())
    slot.set_active(b)
    assert slot.active() is b
    assert slot._tab_bar.currentIndex() == 1


# --- shared widgets persist across tab switches ---------------------------

def test_shared_widget_survives_tab_switch(qtbot):
    slot = Slot()
    qtbot.addWidget(slot)
    a = _RecorderPlugin("Alpha")
    b = _RecorderPlugin("Beta")
    slot.add_plugin(a, _StubHost())
    slot.add_plugin(b, _StubHost())

    shared = QLabel("chain combo placeholder")
    slot.add_shared_widget(shared)

    # The shared widget lives in the bottom row.
    bottom = slot._bottom
    contained = False
    for i in range(bottom.count()):
        if bottom.itemAt(i).widget() is shared:
            contained = True
            break
    assert contained

    # Switching tabs must NOT remove it.
    slot._tab_bar.setCurrentIndex(1)
    contained_after = False
    for i in range(bottom.count()):
        if bottom.itemAt(i).widget() is shared:
            contained_after = True
            break
    assert contained_after
