"""The two-tier alert helper (GNOME-HIG shape).

An alert puts the plain-language problem in the bold *primary* text and
the technical detail in the *secondary* (informative) text, instead of
cramming both onto one line. These tests pin that structure on the box
builder so the helpers don't silently regress to single-line alerts.
"""

from PySide6.QtWidgets import QMessageBox

from qeth.alerts import _build, _build_confirm


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


def test_confirm_uses_a_verb_button_with_cancel_as_safe_default(qtbot):
    box, accept = _build_confirm(
        None, "Remove this account?", "0xabc…\n\nThis deletes the keystore.",
        "&Remove", destructive=True,
    )
    qtbot.addWidget(box)
    # Verb button, not Yes/No.
    assert accept.text() == "&Remove"
    assert box.buttonRole(accept) == QMessageBox.AcceptRole
    # Cancel is the default + escape target, so Enter/Esc is non-destructive.
    default = box.defaultButton()
    assert default is not None
    assert box.buttonRole(default) == QMessageBox.RejectRole
    assert default is box.escapeButton()
    # Destructive → warning icon; two-tier text intact.
    assert box.icon() == QMessageBox.Warning
    assert box.text() == "Remove this account?"
    assert "keystore" in box.informativeText()


def test_confirm_non_destructive_uses_question_icon(qtbot):
    box, _ = _build_confirm(None, "Proceed?", "", "&OK", destructive=False)
    qtbot.addWidget(box)
    assert box.icon() == QMessageBox.Question
