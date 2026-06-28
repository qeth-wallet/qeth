"""Standard application dialog base + small input helpers.

Every modal/non-modal dialog subclasses :class:`Dialog` instead of
``QDialog`` so its content keeps consistent breathing room from the
window edge, rather than each dialog hand-picking — or forgetting — a
``setContentsMargins`` value (which is what made some dialogs, e.g. the
account QR popup, crowd their text right up against the frame).

The margin is GNOME-HIG-flavoured and derived from the dialog's font
metrics, so it tracks the user's Qt font size instead of being pinned
to a pixel count — half a line-height on every side.

It's applied to the dialog's top-level layout the first time the dialog
is shown (by then the subclass ``__init__`` has installed its layout),
so subclasses need no extra call — they just build their layout as
usual and inherit the margin. A subclass that genuinely wants a
different outer margin can still override it after first show.

:func:`prompt_text` is a ``QInputDialog.getText`` replacement built on
``Dialog`` (so one-off text prompts — edit label, set ENS address,
add token — inherit the same margins instead of Qt's stock spacing),
with a ``wide`` flag for fields that hold a full address.
"""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontMetrics, QShowEvent
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout, QWidget,
)


def standard_dialog_margins(widget: QWidget) -> tuple[int, int, int, int]:
    """House dialog margin ``(left, top, right, bottom)`` for ``widget``'s
    font: half a line-height on every side."""
    m = widget.fontMetrics().height() // 2
    return (m, m, m, m)


def address_field_min_width(widget: QWidget) -> int:
    """Minimum width for an input field that holds a 0x + 40-hex address,
    so address-entry dialogs show the whole value without the user having
    to scroll the field. Measured in the monospace font the address is
    rendered in, plus a little padding."""
    fm = QFontMetrics(QFont("monospace"))
    return fm.horizontalAdvance("0x" + "F" * 40) + widget.fontMetrics().height() * 3


class Dialog(QDialog):
    """A ``QDialog`` that gives its top-level layout the house-standard
    edge margins. Subclass this instead of ``QDialog`` directly."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._margins_applied = False

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — Qt name
        if not self._margins_applied:
            layout = self.layout()
            if layout is not None:
                layout.setContentsMargins(*standard_dialog_margins(self))
                self._margins_applied = True
        super().showEvent(event)


def prompt_text(
    parent: QWidget | None,
    title: str,
    label: str,
    text: str = "",
    *,
    password: bool = False,
    wide: bool = False,
    placeholder: str = "",
) -> tuple[str, bool]:
    """Single-line text prompt, returning ``(value, accepted)`` like
    ``QInputDialog.getText`` — but built on :class:`Dialog`, so it inherits
    the standard margins. ``password`` masks the input; ``wide`` widens the
    field to fit a full address without horizontal scroll."""
    dlg = Dialog(parent)
    dlg.setWindowTitle(title)
    v = QVBoxLayout(dlg)
    prompt = QLabel(label)
    prompt.setWordWrap(True)
    v.addWidget(prompt)
    edit = QLineEdit(text)
    if placeholder:
        edit.setPlaceholderText(placeholder)
    if password:
        edit.setEchoMode(QLineEdit.EchoMode.Password)
    if wide:
        edit.setMinimumWidth(address_field_min_width(dlg))
    v.addWidget(edit)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    v.addWidget(buttons)
    edit.setFocus()
    edit.selectAll()
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    return edit.text(), accepted
