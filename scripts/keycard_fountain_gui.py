#!/usr/bin/env python3
"""On-screen animated-QR fountain, for reproducing the Keycard "stuck at 0%" bug
on the real device -- and trying the two candidate fixes live.

It shows a large signing request (default ~120 fragments, the size that triggers
it) as an animated QR, using qeth's ACTUAL encoder and cadence, so a Keycard
Shell pointed at it sees what qeth would send.

Two independent knobs, both changeable live:

  M  stream ORDER
     CURRENT   -- qeth today: pure fragments (seqNum 1..seqLen) once, then
                  rateless mixes forever. The one that stalls / starts slowly.
     RESILIENT -- fix #1: pure fragments re-injected every cycle, so the device
                  can always recover. Byte-identical, spec-valid parts; only the
                  transmit order differs (a Keystone reads it unchanged).

  [ ] fragment SIZE  (fix #2: bigger QRs when there's a lot of data)
     Fewer, denser QRs = fewer frames = the whole transfer finishes faster, at
     the cost of a higher QR version the camera has to resolve. qeth today uses
     150 bytes/fragment (QR version ~10). The status line shows the resulting
     fragment count, QR version, and time for one full pass so you can see the
     trade-off and find the sweet spot on your device.

Run:
  uv run python scripts/keycard_fountain_gui.py                 # ~120-fragment synthetic tx
  uv run python scripts/keycard_fountain_gui.py --fragment-len 300
  uv run python scripts/keycard_fountain_gui.py --calldata tx.hex   # your real calldata

Keys:  M mode · [ ] fragment size · Space pause · R restart · F fullscreen
       +/- speed · Up/Down QR px · Q quit
"""
from __future__ import annotations

import argparse
import io
import itertools
import sys
from collections.abc import Callable, Iterator

import rlp
import segno
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from qeth.qr import eth, multipart, ur
from qeth.qr.multipart import _part, _plan, _split_part

FRAME_MS = 200          # qeth's real animated-QR cadence (QRExchangeDialog.FRAME_MS)
QR_ERROR = "l"          # qeth uses error-correction level L for the request QR
KEYCARD_MAX_PARTS = 128  # keycard-shell UR_MAX_PART_COUNT: it rejects seqLen > 128
# The device reassembles into a 128 KB heap needing ~145*fragment_len bytes, so
# very large fragments would overflow it. Stay well under.
MAX_FRAGMENT_LEN = 700


# --------------------------------------------------------------------------- #
# Payload: a real eth-sign-request wrapping an EIP-1559 tx with big calldata,
# modelled on the reported wallet (0x7a.., Ledger-Legacy path).
# --------------------------------------------------------------------------- #
def _rlp_int(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 7) // 8, "big")


def build_message(target_fragments: int, calldata: bytes | None) -> tuple[str, bytes]:
    if calldata is None:
        calldata = bytes((i * 131 + 7) % 256
                         for i in range(max(200, target_fragments * multipart.FRAGMENT_LEN - 300)))
    to = bytes.fromhex("c727cb1d104e7ad2a81d001a5f75e9558cc8d2d9")
    unsigned = b"\x02" + rlp.encode([
        _rlp_int(1), _rlp_int(0), _rlp_int(10**9), _rlp_int(20 * 10**9),
        _rlp_int(1_000_000), to, _rlp_int(0), calldata, [],
    ])
    ur_type, payload, _rid = eth.encode_eth_sign_request(
        sign_data=unsigned, data_type=eth.DataType.TYPED_TRANSACTION, chain_id=1,
        path="m/44'/60'/0'/0",                 # Ledger Legacy
        source_fingerprint=0,
        address=bytes.fromhex("7a16ff8270133f063aab6c9977183d9e72835428"),
    )
    return ur_type, payload


# --------------------------------------------------------------------------- #
# The two stream ORDERINGS. Both take a fragment_len (fix #2) and emit spec-valid,
# byte-identical parts; only the frame order differs, so a Keystone reads either.
# --------------------------------------------------------------------------- #
def current_stream(ur_type: str, message: bytes, fragment_len: int) -> Callable[[], str]:
    """qeth's OLD behaviour (kept here for the on-device comparison): pure
    fragments once, then rateless mixes forever. qeth's `frame_source` now uses
    the resilient ordering below."""
    if len(message) <= multipart.SINGLE_PART_MAX:
        part = ur.encode(ur_type, message)
        return lambda: part
    seq_len, frags, checksum = _plan(message, fragment_len)
    counter = itertools.count(1)
    return lambda: _part(ur_type, next(counter), seq_len, len(message), checksum, frags)


def resilient_stream(ur_type: str, message: bytes, fragment_len: int) -> Callable[[], str]:
    """Fix #1 (now qeth's default in multipart.frame_source): cycle [all pure
    fragments] + [a batch of fresh rateless mixes]."""
    if len(message) <= multipart.SINGLE_PART_MAX:
        part = ur.encode(ur_type, message)
        return lambda: part
    seq_len, frags, checksum = _plan(message, fragment_len)

    def gen() -> Iterator[str]:
        rateless = seq_len
        while True:
            for n in range(1, seq_len + 1):
                yield _part(ur_type, n, seq_len, len(message), checksum, frags)
            for _ in range(seq_len):
                rateless += 1
                yield _part(ur_type, rateless, seq_len, len(message), checksum, frags)

    it = gen()
    return lambda: next(it)


STREAMS: dict[str, Callable[[str, bytes, int], Callable[[], str]]] = {
    "CURRENT (vulnerable)": current_stream,
    "RESILIENT (fixed)": resilient_stream,
}


def qr_version(ur_string: str) -> int:
    return segno.make(ur_string.upper(), error=QR_ERROR).version


def render_qr(ur_string: str, size: int) -> QPixmap:
    """UR string -> QR QPixmap, rendered like qeth (uppercased for the compact
    alphanumeric mode, error level L, nearest-neighbour scaling)."""
    buf = io.BytesIO()
    segno.make(ur_string.upper(), error=QR_ERROR).save(buf, kind="png", scale=10)
    pixmap = QPixmap()
    pixmap.loadFromData(buf.getvalue())
    return pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.FastTransformation)


class FountainWindow(QWidget):
    def __init__(self, ur_type: str, message: bytes, fragment_len: int, qr_size: int) -> None:
        super().__init__()
        self._ur_type = ur_type
        self._message = message
        self._fragment_len = fragment_len
        self._qr_size = qr_size
        self._interval = FRAME_MS
        self._modes = list(STREAMS)
        self._mode_idx = 0
        self._frame = 0
        self._seq_len = 1
        self._qr_ver = 1
        self._current_ur = ""
        self._next_frame: Callable[[], str] = lambda: ""

        self.setWindowTitle("qeth fountain repro")
        self.setStyleSheet("background:#ffffff;")
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        self._qr = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._status = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("color:#111; font-size:15px;")
        self._plan = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._plan.setStyleSheet("color:#333; font-size:13px;")
        self._hint = QLabel(
            "M mode · [ ] fragment size · Space pause · R restart · F fullscreen · "
            "+/- speed · Up/Down px · Q quit", alignment=Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(self._qr, 1)
        root.addWidget(self._status)
        root.addWidget(self._plan)
        root.addWidget(self._hint)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._restart()
        self._timer.start(self._interval)

    # --- stream control ----------------------------------------------------
    def _restart(self) -> None:
        maker = STREAMS[self._modes[self._mode_idx]]
        self._next_frame = maker(self._ur_type, self._message, self._fragment_len)
        self._seq_len = _plan(self._message, self._fragment_len)[0]
        # QR version of a representative (rateless) part
        _s, frags, chk = _plan(self._message, self._fragment_len)
        sample = _part(self._ur_type, self._seq_len + 5, self._seq_len,
                       len(self._message), chk, frags)
        self._qr_ver = qr_version(sample)
        self._frame = 0
        self._tick()
        self._render_plan()

    def _tick(self) -> None:
        self._current_ur = self._next_frame()
        self._frame += 1
        self._render()

    def _render(self) -> None:
        if not self._current_ur:
            return
        self._qr.setPixmap(render_qr(self._current_ur, self._qr_size))
        _t, seq_num, seq_len, _cbor = _split_part(self._current_ur)
        pos = f"seqNum {seq_num} / seqLen {seq_len}" if seq_num is not None else "single part"
        paused = "   [PAUSED]" if not self._timer.isActive() else ""
        self._status.setText(f"{self._modes[self._mode_idx]}     frame {self._frame}"
                             f"     {pos}{paused}")

    def _render_plan(self) -> None:
        pass_s = self._seq_len * self._interval / 1000.0
        warn = ""
        if self._seq_len > KEYCARD_MAX_PARTS:
            warn = f"   ⚠ seqLen>{KEYCARD_MAX_PARTS}: Keycard rejects — enlarge fragment"
        elif self._qr_ver >= 18:
            warn = f"   ⚠ QR v{self._qr_ver} is dense — may be hard to scan"
        default = "  (qeth default)" if self._fragment_len == multipart.FRAGMENT_LEN else ""
        self._plan.setText(
            f"fragment {self._fragment_len} B{default}  →  {self._seq_len} fragments"
            f"  ·  QR version {self._qr_ver}  ·  {self._interval} ms/frame"
            f"  ·  one full pass ≈ {pass_s:.0f} s  ·  QR {self._qr_size}px{warn}")

    def _set_fragment_len(self, value: int) -> None:
        self._fragment_len = max(60, min(MAX_FRAGMENT_LEN, value))
        self._restart()

    # --- input -------------------------------------------------------------
    def keyPressEvent(self, event: object) -> None:  # noqa: N802 — Qt override
        key = event.key()  # type: ignore[attr-defined]
        K = Qt.Key
        if key in (K.Key_Q, K.Key_Escape):
            self.close()
        elif key == K.Key_M:
            self._mode_idx = (self._mode_idx + 1) % len(self._modes)
            self._restart()                       # fresh transfer; rescan on the device
        elif key == K.Key_BracketRight:
            self._set_fragment_len(self._fragment_len + 30)   # bigger QRs, fewer frames
        elif key == K.Key_BracketLeft:
            self._set_fragment_len(self._fragment_len - 30)
        elif key == K.Key_Space:
            self._timer.stop() if self._timer.isActive() else self._timer.start(self._interval)
            self._render()
        elif key == K.Key_R:
            self._restart()
        elif key == K.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif key in (K.Key_Plus, K.Key_Equal):
            self._interval = max(50, self._interval - 25)
            self._timer.setInterval(self._interval); self._render(); self._render_plan()
        elif key == K.Key_Minus:
            self._interval = min(1000, self._interval + 25)
            self._timer.setInterval(self._interval); self._render(); self._render_plan()
        elif key == K.Key_Up:
            self._qr_size = min(1400, self._qr_size + 40); self._render(); self._render_plan()
        elif key == K.Key_Down:
            self._qr_size = max(160, self._qr_size - 40); self._render(); self._render_plan()

    def resizeEvent(self, event: object) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)  # type: ignore[misc]
        fit = max(160, min(self.width() - 40, self.height() - 120))
        if abs(fit - self._qr_size) > 8:
            self._qr_size = fit
            self._render(); self._render_plan()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fragments", type=int, default=120,
                    help="target fragment count for the synthetic payload (default 120)")
    ap.add_argument("--fragment-len", type=int, default=multipart.FRAGMENT_LEN,
                    help=f"bytes per fragment (default {multipart.FRAGMENT_LEN} = qeth today; "
                         "bigger = fewer, denser QRs)")
    ap.add_argument("--calldata", metavar="FILE",
                    help="file with the real tx calldata as hex (0x-optional) to sign")
    ap.add_argument("--qr-size", type=int, default=320,
                    help="on-screen QR size in px (default 320, matching qeth's dialog)")
    args = ap.parse_args()

    calldata = None
    if args.calldata:
        text = open(args.calldata).read().strip()
        calldata = bytes.fromhex(text[2:] if text.lower().startswith("0x") else text)

    ur_type, message = build_message(args.fragments, calldata)
    seq_len = _plan(message, args.fragment_len)[0]
    print(f"payload {len(message)} bytes -> seqLen={seq_len} fragments at "
          f"fragment_len={args.fragment_len} ({FRAME_MS} ms/frame)")

    app = QApplication(sys.argv)
    win = FountainWindow(ur_type, message, args.fragment_len, args.qr_size)
    win.resize(max(args.qr_size + 40, 520), args.qr_size + 140)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
