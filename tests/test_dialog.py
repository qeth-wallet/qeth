"""The Dialog base's form handling under macOS's QFormLayout defaults.

QMacStyle centres a form and keeps its fields at their size hint, so a
dialog's line edits rendered as short boxes in the middle (a Mac report of
the chain-RPC dialog). The base replaces those defaults; a proxy style that
answers the style hints the way QMacStyle does reproduces it on Linux."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout, QLineEdit, QProxyStyle, QStyle, QVBoxLayout,
)

import qeth.chainlist as cl
from qeth.chain_rpc_dialog import ChainRpcDialog
from qeth.dialog import Dialog


class _MacFormStyle(QProxyStyle):
    """Fusion, but with QMacStyle's form-layout defaults."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_FormLayoutFieldGrowthPolicy:
            return QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint.value
        if hint == QStyle.StyleHint.SH_FormLayoutFormAlignment:
            return (Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop).value
        return super().styleHint(hint, option, widget, returnData)


@pytest.fixture
def mac_style():
    style = _MacFormStyle("Fusion")
    yield style


def _form_dialog(qtbot, style) -> tuple[Dialog, QFormLayout, QLineEdit]:
    dlg = Dialog()
    dlg.setStyle(style)
    outer = QVBoxLayout(dlg)
    form = QFormLayout()
    edit = QLineEdit()
    form.addRow("URL:", edit)
    outer.addLayout(form)
    qtbot.addWidget(dlg)
    return dlg, form, edit


def test_mac_style_reproduces_the_defaults(qtbot, mac_style):
    # Guard for the test itself: the proxy really yields the Mac defaults
    # (before the base touches the form).
    dlg, form, _ = _form_dialog(qtbot, mac_style)
    assert form.fieldGrowthPolicy() == QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
    assert form.formAlignment() & Qt.AlignmentFlag.AlignHCenter


def test_mac_form_defaults_are_pinned_on_show(qtbot, mac_style):
    dlg, form, edit = _form_dialog(qtbot, mac_style)
    dlg.resize(640, 200)
    dlg.show()
    qtbot.waitExposed(dlg)
    assert form.fieldGrowthPolicy() == QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
    assert form.formAlignment() == Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
    # The field takes the row's width instead of its ~150px size hint.
    assert edit.width() > dlg.width() * 0.6


def test_an_explicit_policy_is_left_alone(qtbot, mac_style):
    dlg, form, _ = _form_dialog(qtbot, mac_style)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    dlg.show()
    qtbot.waitExposed(dlg)
    assert form.fieldGrowthPolicy() == QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow


def test_chain_rpc_dialog_fields_span_the_dialog_on_mac(qtbot, monkeypatch, mac_style):
    """The reported dialog: both its forms' line edits fill the width."""
    monkeypatch.setattr(cl, "lookup", lambda cid: None)
    monkeypatch.setattr(cl, "probe_rpc", lambda *a, **k: (True, 10.0, None))
    chain = SimpleNamespace(name="Ethereum", chain_id=1, rpc_url="")
    dlg = ChainRpcDialog(chain)
    dlg.setStyle(mac_style)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    for edit in (dlg.url_edit, dlg.etherscan_edit):
        assert edit.width() > dlg.width() * 0.6
