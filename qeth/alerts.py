"""Two-tier alert dialogs in the GNOME-HIG shape.

An alert states the problem in bold *primary* text and the detail in
secondary text beneath it, rather than packing everything onto one line
or leaning on the title bar to carry meaning. Per the HIG the window
title stays minimal — the message lives in the body — so these helpers
deliberately don't set a title.

Usage mirrors the old ``QMessageBox.warning(parent, title, text)`` calls
they replace: the former *title* becomes the bold primary line, the
former *text* becomes the secondary detail.

    warn(parent, "Cannot sign", f"No known signer for {addr}")
"""

from PySide6.QtWidgets import QMessageBox


def _build(parent, primary: str, secondary: str, icon) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setText(primary)
    if secondary:
        box.setInformativeText(secondary)
    box.setStandardButtons(QMessageBox.Ok)
    return box


def _alert(parent, primary: str, secondary: str, icon) -> int:
    return _build(parent, primary, secondary, icon).exec()


def _build_confirm(parent, primary: str, secondary: str, action: str,
                   destructive: bool):
    """Build a confirmation box with a verb button instead of Yes/No
    (HIG: the affirmative button names the action it performs), Cancel
    as the safe default + Escape target, and a two-tier message."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning if destructive else QMessageBox.Question)
    box.setText(primary)
    if secondary:
        box.setInformativeText(secondary)
    accept = box.addButton(action, QMessageBox.AcceptRole)
    cancel = box.addButton("&Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    return box, accept


def confirm(parent, primary: str, secondary: str = "", *,
            action: str, destructive: bool = False) -> bool:
    """Ask the user to confirm an action. ``action`` is the verb on the
    affirmative button (e.g. "&Remove"); the other button is Cancel and
    is the default, so an accidental Enter/Escape is safe. Returns True
    only if the user clicked the action button."""
    box, accept = _build_confirm(parent, primary, secondary, action,
                                 destructive)
    box.exec()
    return box.clickedButton() is accept


def warn(parent, primary: str, secondary: str = "") -> int:
    return _alert(parent, primary, secondary, QMessageBox.Warning)


def error(parent, primary: str, secondary: str = "") -> int:
    return _alert(parent, primary, secondary, QMessageBox.Critical)


def info(parent, primary: str, secondary: str = "") -> int:
    return _alert(parent, primary, secondary, QMessageBox.Information)
