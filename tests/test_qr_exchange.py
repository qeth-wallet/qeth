"""QR exchange (step 3c): the zxing decoder round-trip, and the exchange
dialog's accept/cancel/ignore logic driven by a fake scanner (no camera, no
QtMultimedia — those are verified on hardware)."""

import io

import segno
from cbor2 import CBORTag, dumps
from PIL import Image
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog

from qeth.qr import ur
from qeth.qr_scan import decode_qr


def _signature_ur() -> str:
    body = {1: CBORTag(37, b"\x01" * 16), 2: b"\x11" * 65}
    return ur.encode("eth-signature", dumps(body, canonical=True))


def test_decode_qr_reads_what_segno_wrote():
    urs = _signature_ur()
    buf = io.BytesIO()
    segno.make(urs.upper(), error="l").save(buf, kind="png", scale=5)
    buf.seek(0)
    decoded = decode_qr(Image.open(buf))
    # QR alphanumeric preserves the uppercased UR; ur.decode lowercases it back.
    assert decoded == urs.upper()
    assert ur.decode(decoded)[0] == "eth-signature"


def test_decode_qr_returns_none_on_a_blank_image():
    assert decode_qr(Image.new("RGB", (64, 64), "white")) is None


def test_decode_qr_from_grayscale_qimage(qtbot):
    """The camera path decodes a Format_Grayscale8 QImage (the luminance zxing
    wants), not RGB — round-trip a QR through _qimage_to_gray."""
    from PySide6.QtGui import QImage
    from qeth.qr_scan import _qimage_to_gray
    urs = _signature_ur()
    buf = io.BytesIO()
    segno.make(urs.upper(), error="l").save(buf, kind="png", scale=6)
    qimg = QImage.fromData(buf.getvalue())
    assert not qimg.isNull()
    assert decode_qr(_qimage_to_gray(qimg)) == urs.upper()


# --- _pick_video_format: ~720p sweet spot with graceful fallback ------------

def _fmt(w, h, fps=30.0):
    from types import SimpleNamespace
    from PySide6.QtCore import QSize
    size = QSize(w, h)
    return SimpleNamespace(resolution=lambda: size, maxFrameRate=lambda: fps)


def _device(*formats):
    from types import SimpleNamespace
    return SimpleNamespace(videoFormats=lambda: list(formats))


def test_pick_format_prefers_720p():
    from qeth.qr_scan import _pick_video_format
    dev = _device(_fmt(640, 480), _fmt(1280, 720), _fmt(1920, 1080))
    assert _pick_video_format(dev).resolution().height() == 720


def test_pick_format_falls_back_to_best_when_no_720p():
    """A 640×480-only webcam keeps its best format rather than forcing 720p."""
    from qeth.qr_scan import _pick_video_format
    dev = _device(_fmt(320, 240), _fmt(640, 480))
    assert _pick_video_format(dev).resolution().height() == 480


def test_pick_format_uses_1080_when_720_absent():
    from qeth.qr_scan import _pick_video_format
    dev = _device(_fmt(640, 480), _fmt(1920, 1080))
    assert _pick_video_format(dev).resolution().height() == 1080


def test_pick_format_avoids_4k():
    from qeth.qr_scan import _pick_video_format
    dev = _device(_fmt(640, 480), _fmt(3840, 2160))
    assert _pick_video_format(dev).resolution().height() == 480   # 4K skipped


def test_pick_format_higher_fps_wins_at_same_resolution():
    from qeth.qr_scan import _pick_video_format
    dev = _device(_fmt(1280, 720, 15.0), _fmt(1280, 720, 30.0))
    assert _pick_video_format(dev).maxFrameRate() == 30.0


def test_pick_format_none_when_device_reports_nothing():
    from qeth.qr_scan import _pick_video_format
    assert _pick_video_format(_device()) is None


class _FakeScanner(QObject):
    decoded = Signal(str)
    frame = Signal(object)

    def __init__(self):
        super().__init__()
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


def _dialog(qtbot, scanner, next_frame=None):
    from qeth.qr_exchange_dialog import QRExchangeDialog
    nf = next_frame or (lambda: "ur:eth-sign-request/aeadcylabntfgm")
    dlg = QRExchangeDialog(nf, scanner=scanner)
    qtbot.addWidget(dlg)
    return dlg


def test_dialog_accepts_the_first_scanned_ur(qtbot):
    scanner = _FakeScanner()
    dlg = _dialog(qtbot, scanner)
    dlg.show()
    assert scanner.started == 1                    # camera started on show
    resp = _signature_ur()
    scanner.decoded.emit(resp)
    assert dlg.scanned_ur() == resp                 # captured synchronously
    # …but the close is DEFERRED (out of the frame callback) — pump the loop.
    qtbot.waitUntil(lambda: dlg.result() == QDialog.DialogCode.Accepted,
                    timeout=2000)
    assert scanner.stopped == 1                     # camera stopped on close


def test_dialog_animates_by_pulling_fresh_frames(qtbot):
    seq = iter(f"ur:eth-sign-request/{i}-3/aeadcylabntfgm" for i in range(1, 5))
    dlg = _dialog(qtbot, _FakeScanner(), next_frame=lambda: next(seq))
    assert dlg._shown == "ur:eth-sign-request/1-3/aeadcylabntfgm"   # first frame
    dlg._render_frame()
    assert dlg._shown == "ur:eth-sign-request/2-3/aeadcylabntfgm"   # fresh part
    assert dlg._anim is not None and dlg._anim.isActive()


def test_dialog_single_part_renders_once(qtbot):
    dlg = _dialog(qtbot, _FakeScanner(),
                  next_frame=lambda: "ur:eth-sign-request/const")
    dlg._render_frame()
    dlg._render_frame()                       # constant → no re-render
    assert dlg._shown == "ur:eth-sign-request/const"


def test_dialog_ignores_non_ur_barcodes(qtbot):
    scanner = _FakeScanner()
    dlg = _dialog(qtbot, scanner)
    dlg.show()
    scanner.decoded.emit("https://example.com/not-a-ur")
    assert dlg.scanned_ur() is None
    assert dlg.isVisible()                          # still waiting


def test_dialog_cancel_returns_none(qtbot):
    scanner = _FakeScanner()
    dlg = _dialog(qtbot, scanner)
    dlg.show()
    dlg.reject()
    assert dlg.scanned_ur() is None
    assert scanner.stopped == 1


def test_exchange_qr_opens_the_dialog_and_returns_the_scan(qtbot, monkeypatch):
    """DialogInteraction.exchange_qr wires request→dialog→scanned response.
    Patch the dialog to a stub so no real window/camera is needed."""
    import qeth.qr_exchange_dialog as ex
    from qeth.signer_interaction import DialogInteraction
    from PySide6.QtWidgets import QWidget

    resp = _signature_ur()

    class _StubDialog:
        def __init__(self, request_ur, *, parent=None):
            self.request_ur = request_ur

        def exec(self):
            return 1

        def scanned_ur(self):
            return resp
    monkeypatch.setattr(ex, "QRExchangeDialog", _StubDialog)

    parent = QWidget()
    qtbot.addWidget(parent)
    host = DialogInteraction(parent, title="Signing")
    assert host.exchange_qr(lambda: "ur:eth-sign-request/aeadcylabntfgm") == resp


def test_ur_to_pixmap_is_non_null(qtbot):
    from qeth.qr_exchange_dialog import ur_to_pixmap
    pm = ur_to_pixmap(_signature_ur())
    assert not pm.isNull() and pm.width() > 0


def test_scan_dialog_accepts_first_ur_and_runs_camera(qtbot):
    from PySide6.QtWidgets import QDialog

    from qeth.qr_exchange_dialog import QRScanDialog
    scanner = _FakeScanner()
    dlg = QRScanDialog(scanner=scanner)
    qtbot.addWidget(dlg)
    dlg.show()
    assert scanner.started == 1
    scanner.decoded.emit("ur:crypto-hdkey/aeadcylabntfgm")
    assert dlg.scanned_ur() == "ur:crypto-hdkey/aeadcylabntfgm"
    qtbot.waitUntil(lambda: dlg.result() == QDialog.DialogCode.Accepted,
                    timeout=2000)
    assert scanner.stopped == 1


def test_scan_dialog_cancel_returns_none(qtbot):
    from qeth.qr_exchange_dialog import QRScanDialog
    scanner = _FakeScanner()
    dlg = QRScanDialog(scanner=scanner)
    qtbot.addWidget(dlg)
    dlg.show()
    dlg.reject()
    assert dlg.scanned_ur() is None and scanner.stopped == 1
