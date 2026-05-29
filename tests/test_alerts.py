"""The two-tier alert helper (GNOME-HIG shape).

An alert puts the plain-language problem in the bold *primary* text and
the technical detail in the *secondary* (informative) text, instead of
cramming both onto one line. These tests pin that structure on the box
builder so the helpers don't silently regress to single-line alerts.
"""

from PySide6.QtWidgets import QMessageBox

from qeth.alerts import _build


def test_primary_and_secondary_land_in_the_right_slots(qtbot):
    box = _build(
        None, "Cannot sign", "No known signer for 0xabc…",
        QMessageBox.Warning,
    )
    qtbot.addWidget(box)
    assert box.text() == "Cannot sign"             # bold primary line
    assert box.informativeText() == "No known signer for 0xabc…"
    assert box.icon() == QMessageBox.Warning


def test_empty_secondary_leaves_informative_text_blank(qtbot):
    box = _build(None, "Done", "", QMessageBox.Information)
    qtbot.addWidget(box)
    assert box.text() == "Done"
    assert box.informativeText() == ""
    assert box.icon() == QMessageBox.Information


def test_icon_tracks_severity(qtbot):
    for severity in (QMessageBox.Warning, QMessageBox.Critical,
                     QMessageBox.Information):
        box = _build(None, "x", "y", severity)
        qtbot.addWidget(box)
        assert box.icon() == severity
