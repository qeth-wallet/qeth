#!/usr/bin/env python3
"""Measure physical Shell transfers using qeth's production animation path.

Run at --fps 5, 8, 10, 12 and 15 with identical payload and viewing conditions.
Enter records 100% completion; X records failure; R starts another attempt.
These are unsigned diagnostic requests; do not sign or broadcast them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import rlp
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from qeth.qr import eth, multipart
from qeth.qr_animation import PreparedFrame, QRAnimation, TARGET_FPS
from qeth.qr_widget import QRWidget


def _rlp_int(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 7) // 8, "big")


def build_message(target_fragments: int, calldata: bytes | None) -> tuple[str, bytes]:
    if calldata is None:
        calldata = bytes(
            (i * 131 + 7) % 256
            for i in range(max(200, target_fragments * multipart.FRAGMENT_LEN - 300))
        )
    to = bytes.fromhex("c727cb1d104e7ad2a81d001a5f75e9558cc8d2d9")
    unsigned = b"\x02" + rlp.encode(
        [
            _rlp_int(1),
            _rlp_int(0),
            _rlp_int(10**9),
            _rlp_int(20 * 10**9),
            _rlp_int(1_000_000),
            to,
            _rlp_int(0),
            calldata,
            [],
        ]
    )
    ur_type, payload, _rid = eth.encode_eth_sign_request(
        sign_data=unsigned,
        data_type=eth.DataType.TYPED_TRANSACTION,
        chain_id=1,
        path="m/44'/60'/0'/0",  # Ledger Legacy
        source_fingerprint=0,
        request_id=bytes.fromhex("f48cac714aaa4b23b8a877087e94ed91"),
        address=bytes.fromhex("7a16ff8270133f063aab6c9977183d9e72835428"),
    )
    return ur_type, payload


class FountainWindow(QWidget):
    def __init__(self, ur_type: str, message: bytes, args: argparse.Namespace) -> None:
        super().__init__()
        self._ur_type, self._message, self._args = ur_type, message, args
        self._started: float | None = None
        self._finished = False
        self._attempt = 0
        self._frames = 0
        self._source_sizes: dict[int, int] = {}
        self._grid_policy = args.grid_policy
        self._animation: QRAnimation | None = None
        self.setWindowTitle(f"qeth QR trial — {args.fps:g} fps")
        layout = QVBoxLayout(self)
        self._qr = QRWidget(preferred_side=args.qr_size)
        self._qr.setFixedSize(args.qr_size, args.qr_size)
        layout.addWidget(self._qr, alignment=Qt.AlignmentFlag.AlignCenter)
        self._status = QLabel()
        layout.addWidget(self._status)
        layout.addWidget(
            QLabel("Enter: 100% complete · X: failed · R: new attempt · Q: quit")
        )
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._record("timeout"))
        self._restart()

    def _restart(self) -> None:
        if self._animation is not None:
            if not self._finished:
                self._record("restarted")
            self._animation.stop()
            self._animation.deleteLater()
        self._started = None
        self._finished = False
        self._frames = 0
        self._source_sizes = {}
        self._attempt += 1
        self._grid_policy = (
            ("fixed", "per-frame", "per-frame", "fixed")[(self._attempt - 1) % 4]
            if self._args.compare_framing
            else self._args.grid_policy
        )
        self.setWindowTitle(
            f"qeth QR trial — {self._args.fps:g} fps — {self._grid_policy}"
        )
        self._status.setText("Preparing QR…")
        self._animation = QRAnimation(
            self._qr,
            multipart.frame_source(
                self._ur_type,
                self._message,
                fragment_len=self._args.fragment_len,
                rng=random.Random(self._args.seed),
            ),
            fps=self._args.fps,
            fixed_version=self._grid_policy == "fixed",
        )
        self._animation.displayed.connect(self._displayed)
        self._animation.failed.connect(self._failed)

    def _displayed(self, frame: PreparedFrame) -> None:
        if self._started is None:
            self._started = time.monotonic()
            self._timeout.start(round(self._args.timeout * 1000))
        self._frames += 1
        side = frame.image.width()
        self._source_sizes[side] = self._source_sizes.get(side, 0) + 1
        self._status.setText(
            f"Attempt {self._attempt} · {self._grid_policy} · {self._args.fps:g} fps · "
            f"{time.monotonic() - self._started:.1f} s · {self._frames} frames"
        )

    def _failed(self, message: str) -> None:
        self._record("preparation_error")
        self._status.setText(f"QR preparation failed: {message}")

    def _record(self, outcome: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._timeout.stop()
        if self._animation is not None:
            self._animation.stop()
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attempt": self._attempt,
            "outcome": outcome,
            "seconds": time.monotonic() - self._started
            if self._started is not None
            else None,
            "frames": self._frames,
            "fps": self._args.fps,
            "grid_policy": self._grid_policy,
            "compare_framing": self._args.compare_framing,
            "source_modules_including_border": self._source_sizes,
            "payload_sha256": hashlib.sha256(self._message).hexdigest(),
            "payload_bytes": len(self._message),
            "seed": self._args.seed,
            "fragment_len": self._args.fragment_len,
            "qr_logical_px": self._args.qr_size,
            "dpr": self._qr.devicePixelRatioF(),
            "brightness": self._args.brightness,
            "distance_cm": self._args.distance_cm,
        }
        line = json.dumps(result)
        with Path(self._args.results).open("a") as output:
            output.write(line + "\n")
        print(line, flush=True)
        self._status.setText(
            f"{outcome}: {result['seconds']} s — R for another attempt"
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Q, Qt.Key.Key_Escape):
            self.close()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._started is not None:
            self._record("complete")
        elif key == Qt.Key.Key_X:
            self._record("failed")
        elif key == Qt.Key.Key_R:
            self._restart()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._record("closed")
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fps", type=float, default=TARGET_FPS)
    parser.add_argument("--fragments", type=int, default=120)
    parser.add_argument("--fragment-len", type=int, default=None)
    payload = parser.add_mutually_exclusive_group()
    payload.add_argument("--calldata", help="unsigned transaction calldata as hex")
    payload.add_argument(
        "--calldata-bytes",
        type=int,
        help="generate this many deterministic calldata bytes (seed 892)",
    )
    framing = parser.add_mutually_exclusive_group()
    framing.add_argument(
        "--grid-policy", choices=("fixed", "per-frame"), default="fixed"
    )
    framing.add_argument(
        "--compare-framing",
        action="store_true",
        help="cycle fixed, per-frame, per-frame, fixed on successive attempts",
    )
    parser.add_argument("--qr-size", type=int, default=320)
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--results", default="/tmp/qeth-qr-trials.jsonl")
    parser.add_argument(
        "--brightness", default="unrecorded", help="record monitor brightness"
    )
    parser.add_argument(
        "--distance-cm", type=float, help="record camera-to-screen distance"
    )
    args = parser.parse_args()
    from qeth.qr_animation import FrameDeadline

    FrameDeadline(args.fps)
    if args.fragments < 1 or args.qr_size < 192 or args.timeout <= 0:
        parser.error(
            "fragments and timeout must be positive; qr-size must be at least 192"
        )
    calldata = None
    if args.calldata_bytes is not None:
        if args.calldata_bytes < 0:
            parser.error("calldata-bytes must be nonnegative")
        calldata = random.Random(892).randbytes(args.calldata_bytes)
    if args.calldata:
        calldata = bytes.fromhex(
            Path(args.calldata).read_text().strip().removeprefix("0x")
        )
    ur_type, message = build_message(args.fragments, calldata)
    if (
        args.fragment_len is not None
        and args.fragment_len < multipart._fragment_len_for(len(message))
    ):
        parser.error("fragment-len must respect the production fragment size/cap")
    print(
        f"payload: {len(message)} bytes, SHA256 {hashlib.sha256(message).hexdigest()}"
    )
    app = QApplication(sys.argv)
    win = FountainWindow(ur_type, message, args)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
