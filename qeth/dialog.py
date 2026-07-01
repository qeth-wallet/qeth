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

from PySide6.QtGui import QCloseEvent, QFont, QFontMetrics, QShowEvent
from PySide6.QtWidgets import (
    QBoxLayout, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QVBoxLayout, QWidget,
)


def standard_dialog_margins(widget: QWidget) -> tuple[int, int, int, int]:
    """House dialog margin ``(left, top, right, bottom)`` for ``widget``'s
    font: half a line-height on every side."""
    m = widget.fontMetrics().height() // 2
    return (m, m, m, m)


# Paragraph spacing — the GNOME-2-HIG rule, font-derived so it tracks the user's
# Qt font instead of being pinned to a pixel count. Treat each logical group of
# controls (a value, a description, a widget, the button row) as a "paragraph":
# tight spacing *within* a paragraph, a wider gap *between* paragraphs. The
# between-gap is ~2x the within-gap — enough to read as a break without the
# cavernous look a full 3x leaves on a large font.
def item_spacing(widget: QWidget) -> int:
    """Vertical gap WITHIN a paragraph (related rows — the base "line" spacing)."""
    return max(2, widget.fontMetrics().height() // 3)


def group_spacing(widget: QWidget) -> int:
    """Vertical gap BETWEEN paragraphs (and above the button row) — 2x the
    within-paragraph line spacing, so a logical break reads clearly."""
    return 2 * item_spacing(widget)


def label_spacing(widget: QWidget) -> int:
    """Horizontal gap between a label and its control."""
    return max(4, widget.fontMetrics().height() * 2 // 3)


def address_field_min_width(widget: QWidget) -> int:
    """Minimum width for an input field that holds a 0x + 40-hex address,
    so address-entry dialogs show the whole value without the user having
    to scroll the field. Measured in the monospace font the address is
    rendered in, plus a little padding."""
    fm = QFontMetrics(QFont("monospace"))
    return fm.horizontalAdvance("0x" + "F" * 40) + widget.fontMetrics().height() * 3


class Dialog(QDialog):
    """A ``QDialog`` that applies the house-standard edge margins and paragraph
    spacing to its layout on first show. Subclass this instead of ``QDialog``.

    The spacing follows the GNOME-2-HIG "paragraph" rule (see ``group_spacing``):
    the dialog's top-level box layout gets the BETWEEN-paragraph gap, so a dialog
    built as ``VBox[ form, …, buttonBox ]`` reads as separated paragraphs — its
    action row sits a clear gap below the content — with no per-dialog spacing
    calls. The WITHIN-paragraph rhythm (a form's own row spacing, or a grouped
    sub-layout at ``item_spacing``) is left to the dialog, so a form that wants
    a rommier rhythm keeps it. A subclass that needs something bespoke can set
    ``_auto_spacing = False`` and lay out by hand.

    Applied from ``setVisible`` — *before* Qt sizes the window — so the window's
    size hint already reflects the spacing. (Doing it in ``showEvent``, after
    the geometry was committed, overflowed the window and clipped content.)"""

    _auto_spacing = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._spacing_applied = False
        self._height_checked = False
        # Set True (via set_signing_in_progress) while a sign-and-broadcast
        # worker is running for this dialog, to block dismissal — see reject().
        self._signing = False

    def reject(self) -> None:
        # Ignore Esc / window-close / Cancel while signing is in progress. The
        # signing continues on the device regardless and the tx will broadcast,
        # so emitting `rejected` (which tells a dapp "User cancelled") while the
        # tx lands on-chain would be a lie. The dialog re-enables itself on
        # failure (set_signing_in_progress(False)), restoring normal dismissal.
        if self._signing:
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt name
        if self._signing:
            event.ignore()
            return
        super().closeEvent(event)

    def setVisible(self, visible: bool) -> None:  # noqa: N802 — Qt name
        if visible and not self._spacing_applied:
            self._apply_standard_spacing()
            self._spacing_applied = True
        super().setVisible(visible)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — Qt name
        super().showEvent(event)
        # Grow to fit a word-wrapped label that needs more height at the
        # window's actual width than its (wide, ~1-line) size hint reported —
        # otherwise the dialog opens too short and clips / compresses content.
        # Qt's auto-size uses the hint width, where such a label barely wraps;
        # at the real (narrower) width it wraps to several lines. Only ever
        # grows, and only the first time, so it never fights a user resize.
        if self._height_checked:
            return
        self._height_checked = True
        layout = self.layout()
        if layout is None or not layout.hasHeightForWidth():
            return
        need = layout.heightForWidth(self.width())
        if need > self.height():
            self.resize(self.width(), need)

    def _apply_standard_spacing(self) -> None:
        layout = self.layout()
        if layout is None:
            return
        layout.setContentsMargins(*standard_dialog_margins(self))
        if not self._auto_spacing:
            return
        # The two coupled house gaps: paragraphs (a form, a section, the button
        # row) sit ``group_spacing`` apart; a form's rows sit the tighter
        # ``item_spacing`` apart — so the break between paragraphs always reads
        # as ~1.5x the gap between the lines within one. label↔control gets the
        # standard horizontal gap.
        if isinstance(layout, QBoxLayout):
            layout.setSpacing(group_spacing(self))
        for form in self.findChildren(QFormLayout):
            form.setVerticalSpacing(item_spacing(self))
            form.setHorizontalSpacing(label_spacing(self))


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
    # The prompt and its field are one paragraph (kept tight); the button row is
    # a second paragraph, which the Dialog base spaces a wider gap below.
    para = QVBoxLayout()
    para.setSpacing(item_spacing(dlg))
    prompt = QLabel(label)
    prompt.setWordWrap(True)
    para.addWidget(prompt)
    edit = QLineEdit(text)
    if placeholder:
        edit.setPlaceholderText(placeholder)
    if password:
        edit.setEchoMode(QLineEdit.EchoMode.Password)
    if wide:
        edit.setMinimumWidth(address_field_min_width(dlg))
    para.addWidget(edit)
    v.addLayout(para)
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
