"""Keyboard-default behavior for the signature result dialog.

The result dialog has a Copy and a Close button. Qt's auto-default would
make the *first* button (Copy) the Enter target, so an accidental Enter
would silently copy instead of dismissing. We pin Close as the default
(GNOME HIG: a dialog's default action should be the safe, expected one —
here, dismissing it).
"""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from qeth.plugins.sign_message import SignatureResultDialog


def test_close_is_the_default_not_copy(qtbot):
    dlg = SignatureResultDialog("0x" + "ab" * 65)
    qtbot.addWidget(dlg)
    dlg.show()
    defaults = [b.text() for b in dlg.findChildren(QPushButton) if b.isDefault()]
    assert defaults == ["&Close"]


def test_escape_dismisses(qtbot):
    dlg = SignatureResultDialog("0x" + "ab" * 65)
    qtbot.addWidget(dlg)
    dlg.show()
    assert dlg.isVisible()
    QTest.keyClick(dlg, Qt.Key_Escape)
    assert not dlg.isVisible()
