"""RevokeQueue — sequential auto-advance + abort semantics (commit 3).

The opener is faked so the test drives the broadcast/confirm/cancel callbacks
by hand — no dialog, no chain. qtbot only supplies the QApplication the
QObject signals need.
"""

from types import SimpleNamespace

from qeth.plugins.approvals.discovery import ApprovalRow
from qeth.plugins.approvals.revoke_queue import RevokeQueue


def _rows(n):
    return [ApprovalRow(token="0x" + f"{i + 1:02x}" * 20,
                        spender="0x" + f"{i + 100:02x}" * 20,
                        allowance=1, symbol=f"T{i}") for i in range(n)]


class _Opener:
    def __init__(self):
        self.calls: list = []

    def __call__(self, row, index, total, on_broadcast, on_confirmed, on_cancel):
        self.calls.append(SimpleNamespace(
            row=row, index=index, total=total, on_broadcast=on_broadcast,
            on_confirmed=on_confirmed, on_cancel=on_cancel))


def _queue(rows):
    op = _Opener()
    q = RevokeQueue(rows, op)
    done: list = []
    q.finished.connect(done.append)
    return q, op, done


def test_start_opens_first_only(qtbot):
    q, op, _ = _queue(_rows(3))
    q.start()
    assert len(op.calls) == 1
    assert op.calls[0].index == 0 and op.calls[0].total == 3


def test_advances_on_broadcast(qtbot):
    q, op, _ = _queue(_rows(3))
    bcast: list = []
    q.row_broadcast.connect(lambda row, h: bcast.append((row.symbol, h)))
    q.start()
    op.calls[0].on_broadcast("0xh0")
    assert len(op.calls) == 2 and op.calls[1].index == 1
    assert bcast == [("T0", "0xh0")]


def test_finished_true_after_all_broadcast(qtbot):
    q, op, done = _queue(_rows(2))
    q.start()
    op.calls[0].on_broadcast("0xh0")
    op.calls[1].on_broadcast("0xh1")
    assert done == [True]
    assert len(op.calls) == 2


def test_cancel_aborts_remaining(qtbot):
    q, op, done = _queue(_rows(3))
    q.start()
    op.calls[0].on_cancel()
    assert done == [False]
    assert len(op.calls) == 1          # nothing more opened


def test_abort_after_partial_progress(qtbot):
    q, op, done = _queue(_rows(3))
    q.start()
    op.calls[0].on_broadcast("0xh0")   # on row 1 now
    q.abort()
    assert done == [False]
    assert len(op.calls) == 2          # row 2 never opened


def test_row_confirmed_forwarded(qtbot):
    q, op, _ = _queue(_rows(1))
    conf: list = []
    q.row_confirmed.connect(lambda row, rc: conf.append((row.symbol, rc)))
    q.start()
    op.calls[0].on_confirmed({"status": 1})
    assert conf == [("T0", {"status": 1})]


def test_empty_queue_finishes_true(qtbot):
    q, op, done = _queue([])
    q.start()
    assert done == [True]
    assert op.calls == []


def test_finished_fires_exactly_once(qtbot):
    q, op, done = _queue(_rows(1))
    q.start()
    op.calls[0].on_broadcast("0xh")    # finished(True)
    q.abort()                          # no-op
    op.calls[0].on_cancel()            # no-op
    assert done == [True]
