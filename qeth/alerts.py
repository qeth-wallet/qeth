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


def warn(parent, primary: str, secondary: str = "") -> int:
    return _alert(parent, primary, secondary, QMessageBox.Warning)


def error(parent, primary: str, secondary: str = "") -> int:
    return _alert(parent, primary, secondary, QMessageBox.Critical)


def info(parent, primary: str, secondary: str = "") -> int:
    return _alert(parent, primary, secondary, QMessageBox.Information)
