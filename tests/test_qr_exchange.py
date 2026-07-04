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


def _dialog(qtbot, scanner):
    from qeth.qr_exchange_dialog import QRExchangeDialog
    dlg = QRExchangeDialog(
        "ur:eth-sign-request/aeadcylabntfgm", scanner=scanner)
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
    assert host.exchange_qr("ur:eth-sign-request/aeadcylabntfgm") == resp


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
