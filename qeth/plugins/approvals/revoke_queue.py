"""Sequential, auto-advancing revoke driver for a batch of allowances.

One sign dialog is open at a time. The queue advances to the next row the
moment the current one BROADCASTS — not when it confirms: ``add_pending``
records the just-sent tx before ``on_broadcast`` fires, so the next dialog
resolves its nonce to N+1 through the shared ``pending_nonce_floor`` provider.
Waiting for confirmations would stall the batch for a block each; chaining on
broadcast keeps it a rapid run of pre-filled dialogs the user just signs.

Cancelling any dialog (``on_cancel``) aborts the remaining rows; the host can
also ``abort()`` on account/chain change or shutdown. ``finished(bool)`` fires
exactly once — True only if every row broadcast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .discovery import ApprovalRow

    Opener = Callable[
        [ApprovalRow, int, int,
         "Callable[[str], None]", "Callable[[object], None]", "Callable[[], None]"],
        None,
    ]


class RevokeQueue(QObject):
    row_broadcast = Signal(object, str)      # ApprovalRow, tx_hash
    row_confirmed = Signal(object, object)   # ApprovalRow, receipt
    finished = Signal(bool)                  # broadcast_all

    def __init__(self, rows: Sequence[ApprovalRow], opener: Opener,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._rows = list(rows)
        self._opener = opener
        self._i = 0
        self._ended = False

    def total(self) -> int:
        return len(self._rows)

    def start(self) -> None:
        if not self._rows:
            self._end(True)
            return
        self._open_current()

    def abort(self) -> None:
        self._end(False)

    # --- internals --------------------------------------------------------
    def _end(self, ok: bool) -> None:
        if self._ended:
            return
        self._ended = True
        self.finished.emit(ok)

    def _open_current(self) -> None:
        if self._ended:
            return
        row = self._rows[self._i]                    # stable per call — no default-arg capture
        self._opener(
            row, self._i, len(self._rows),
            lambda h: self._on_broadcast(row, h),
            lambda rc: self.row_confirmed.emit(row, rc),
            self._on_cancel)

    def _on_broadcast(self, row: ApprovalRow, tx_hash: str) -> None:
        if self._ended:
            return
        self.row_broadcast.emit(row, tx_hash)
        self._i += 1
        if self._i >= len(self._rows):
            self._end(True)
        else:
            self._open_current()

    def _on_cancel(self) -> None:
        self._end(False)
