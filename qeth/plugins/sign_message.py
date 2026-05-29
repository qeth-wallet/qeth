"""``SignMessageDialog`` and ``ComposeMessageDialog``.

Two dialogs that share the same backend:

- ``SignMessageDialog`` is the dapp-driven REVIEW dialog. A
  ``personal_sign`` or ``eth_signTypedData_v4`` arrived over JSON-
  RPC; the user sees what's being signed and either confirms or
  cancels. Mirrors the shape of ``SignTransactionDialog`` so the
  signing-flow code in MainWindow can drive both.

- ``ComposeMessageDialog`` is the LOCAL "sign anything you paste"
  dialog. Launched from the wallet-details panel's "Sign message…"
  button. The user pastes text (or a typed-data JSON object); the
  dialog builds a ``MessageSigningRequest`` / ``TypedDataSigningRequest``
  and emits it for the host to sign through the normal flow. After
  the signature comes back, ``ResultDialog`` shows the 0x-hex
  signature with a copy-to-clipboard button.

Decoding rules for ``personal_sign``:
  - bytes that decode to printable UTF-8 → render as text.
  - otherwise → render as 0x-hex.

Decoding for ``eth_signTypedData_v4``:
  - render ``domain`` and ``message`` as a pretty-printed nested
    JSON tree. ``types`` and ``primaryType`` are shown collapsed
    but accessible (the structure is what matters to a security-
    conscious user; the verbose type definitions are noise).
"""

from __future__ import annotations

import json
import re
from typing import Optional

from PySide6.QtCore import QRegularExpression, Qt, Signal
from PySide6.QtGui import (
    QClipboard, QColor, QFont, QGuiApplication, QPalette,
    QSyntaxHighlighter, QTextCharFormat,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QSizePolicy, QStyle, QTextEdit, QVBoxLayout, QWidget,
)

from ..signing import MessageSigningRequest, TypedDataSigningRequest
from ..alerts import warn


class _JsonHighlighter(QSyntaxHighlighter):
    """Minimal JSON syntax highlighter for the typed-data + section
    headers view. Distinguishes keys, string values, numbers,
    keywords (true/false/null), and our ``=== Section ===`` /
    ``--- Subsection ---`` markers so the structure reads clearly
    at a glance instead of looking like opaque text.

    Colors come from the active palette: text-colour with varying
    alpha + a green / orange / muted derived from the window
    background's luminance, so the highlighter works on both light
    and dark themes."""

    def __init__(self, document, palette):
        super().__init__(document)
        text_color = palette.color(QPalette.Text)
        window = palette.color(QPalette.Window)
        lum = (
            window.red() * 0.299
            + window.green() * 0.587
            + window.blue() * 0.114
        )
        is_dark = lum < 128

        # Key (string before colon): the default text colour bumped
        # toward the accent — most themes ship this as a blueish
        # link colour.
        key_fmt = QTextCharFormat()
        key_fmt.setForeground(palette.color(QPalette.Link))
        key_fmt.setFontWeight(QFont.Bold)

        # String VALUES: a muted green that works on both themes.
        string_fmt = QTextCharFormat()
        string_fmt.setForeground(QColor("#7cb342" if is_dark else "#2e7d32"))

        # Numbers and booleans / null: warm accent for both themes.
        number_fmt = QTextCharFormat()
        number_fmt.setForeground(QColor("#f48fb1" if is_dark else "#c2185b"))

        keyword_fmt = QTextCharFormat()
        keyword_fmt.setForeground(QColor("#ce93d8" if is_dark else "#7b1fa2"))
        keyword_fmt.setFontWeight(QFont.Bold)

        # Section headers (=== ... === and --- ... ---) get a
        # palette text colour + bold so the eye finds them first.
        header_fmt = QTextCharFormat()
        header_fmt.setForeground(text_color)
        header_fmt.setFontWeight(QFont.Bold)

        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = [
            # Key BEFORE value-colour rules so it wins.
            (QRegularExpression(r'"[^"\\]*(?:\\.[^"\\]*)*"\s*(?=:)'),
             key_fmt),
            # Then string values (any remaining quoted string).
            (QRegularExpression(r'"[^"\\]*(?:\\.[^"\\]*)*"'),
             string_fmt),
            # Numbers (including negative + scientific).
            (QRegularExpression(r'\b-?\d+(\.\d+)?([eE][+-]?\d+)?\b'),
             number_fmt),
            # JSON keywords.
            (QRegularExpression(r'\b(true|false|null)\b'),
             keyword_fmt),
            # Our own section headers ("=== Domain ===" etc.).
            (QRegularExpression(r'^={3,}.*={3,}$',
                                QRegularExpression.MultilineOption),
             header_fmt),
            (QRegularExpression(r'^-{3,}.*-{3,}$',
                                QRegularExpression.MultilineOption),
             header_fmt),
        ]

    def highlightBlock(self, text: str) -> None:
        for rx, fmt in self._rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


def _is_printable_utf8(b: bytes) -> Optional[str]:
    """Returns the decoded string if ``b`` is valid UTF-8 AND every
    character is either whitespace or printable. Many EIP-4361
    "Sign-in with Ethereum" challenges are plain ASCII; lots of
    DeFi domain bindings ship raw bytes — distinguish the two so
    we don't render gibberish."""
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if all(c.isprintable() or c in "\n\r\t" for c in s):
        return s
    return None


def _format_typed_data(typed: dict) -> str:
    """Pretty-print the EIP-712 ``domain`` and ``message`` blocks.
    ``types`` and ``primaryType`` are appended at the bottom for
    completeness but the user's eye lands on what they're actually
    authorising first."""
    out: list[str] = []
    out.append("=== Domain ===")
    out.append(json.dumps(typed.get("domain", {}), indent=2))
    out.append("")
    out.append(f"=== {typed.get('primaryType', 'message')} (primary) ===")
    out.append(json.dumps(typed.get("message", {}), indent=2))
    types = typed.get("types") or {}
    if types:
        out.append("")
        out.append("--- Types ---")
        out.append(json.dumps(types, indent=2))
    return "\n".join(out)


class SignMessageDialog(QDialog):
    """Review dialog for a dapp-initiated ``personal_sign`` /
    ``eth_signTypedData_v4`` request. The user sees the decoded
    content and either confirms or rejects. Mirrors
    ``SignTransactionDialog``'s ``sign_requested`` signal so the
    same host wiring can route the actual signing work."""

    sign_requested = Signal()

    def __init__(self, req, parent=None):
        super().__init__(parent)
        self._req = req
        if isinstance(req, MessageSigningRequest):
            self.setWindowTitle("Sign message")
        else:
            self.setWindowTitle("Sign typed data (EIP-712)")
        self.resize(680, 540)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(8)

        header = QFormLayout()
        header.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setHorizontalSpacing(16)
        header.setVerticalSpacing(6)
        outer.addLayout(header)

        mono = QFont("monospace")
        from_lbl = QLabel(req.from_addr)
        from_lbl.setFont(mono)
        from_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addRow("Signer:", from_lbl)

        if getattr(req, "origin", None):
            origin_lbl = QLabel(req.origin)
            origin_lbl.setWordWrap(True)
            origin_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            header.addRow("Origin:", origin_lbl)

        if isinstance(req, MessageSigningRequest):
            kind = "personal_sign (EIP-191)"
        else:
            kind = "eth_signTypedData_v4 (EIP-712)"
        header.addRow("Kind:", QLabel(kind))

        outer.addWidget(self._build_message_view(req), 1)

        # Buttons.
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.confirm_btn = self.buttons.addButton(
            "Sign", QDialogButtonBox.AcceptRole,
        )
        self.confirm_btn.setEnabled(True)
        self.buttons.rejected.connect(self.reject)
        self.confirm_btn.clicked.connect(self.sign_requested.emit)
        outer.addWidget(self.buttons)

    def _build_message_view(self, req) -> QWidget:
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("monospace"))
        view.setLineWrapMode(QTextEdit.WidgetWidth)
        view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        if isinstance(req, MessageSigningRequest):
            text = _is_printable_utf8(req.raw)
            if text is not None:
                view.setPlainText(text)
            else:
                view.setPlainText("0x" + req.raw.hex())
        else:
            view.setPlainText(_format_typed_data(req.typed_data))
            # EIP-712 IS structured — colour the keys, values,
            # numbers, and section headers so the eye can parse
            # what's being signed at a glance. The plain-text
            # message above doesn't need it.
            self._highlighter = _JsonHighlighter(
                view.document(), self.palette(),
            )
        return view

    def set_signing_in_progress(self, busy: bool) -> None:
        """Match ``SignTransactionDialog``'s lifecycle hook so the
        same host code can lock/unlock both."""
        self.confirm_btn.setEnabled(not busy)
        for btn in self.buttons.buttons():
            if btn is not self.confirm_btn:
                btn.setEnabled(not busy)


class ComposeMessageDialog(QDialog):
    """Local "sign anything you paste" dialog launched from the
    wallet details panel. The user pastes a UTF-8 string or a
    typed-data JSON object; we sniff the shape and build the
    right request type. On Sign, ``request_built`` fires with
    the request so the host can run the normal signing flow."""

    request_built = Signal(object)   # MessageSigningRequest | TypedDataSigningRequest

    def __init__(self, from_addr: str, parent=None):
        super().__init__(parent)
        self._from_addr = from_addr
        self.setWindowTitle(f"Sign message with {from_addr[:10]}…")
        self.resize(640, 480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        intro = QLabel(
            "Paste a plain text message (will be signed via "
            "<b>personal_sign</b>) <br>or an EIP-712 typed-data JSON "
            "object (signed via <b>eth_signTypedData_v4</b>). The "
            "wallet sniffs the shape automatically."
        )
        intro.setTextFormat(Qt.RichText)
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self.body_edit = QPlainTextEdit()
        self.body_edit.setFont(QFont("monospace"))
        self.body_edit.setPlaceholderText(
            "Hello world!\n\n— or —\n\n"
            '{\n  "domain": {...},\n  "types": {...},\n'
            '  "primaryType": "...",\n  "message": {...}\n}'
        )
        self.body_edit.textChanged.connect(self._update_state)
        outer.addWidget(self.body_edit, 1)

        self.kind_lbl = QLabel("")
        self.kind_lbl.setStyleSheet("color: gray;")
        outer.addWidget(self.kind_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.sign_btn = buttons.addButton("&Sign", QDialogButtonBox.AcceptRole)
        self.sign_btn.setEnabled(False)
        self.sign_btn.clicked.connect(self._emit_request)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _update_state(self) -> None:
        text = self.body_edit.toPlainText().strip()
        if not text:
            self.kind_lbl.setText("")
            self.sign_btn.setEnabled(False)
            return
        if self._looks_like_typed_data(text):
            self.kind_lbl.setText("→ EIP-712 typed data")
        else:
            self.kind_lbl.setText("→ personal_sign (plain text)")
        self.sign_btn.setEnabled(True)

    def _looks_like_typed_data(self, text: str) -> bool:
        if not text.startswith("{"):
            return False
        try:
            parsed = json.loads(text)
        except Exception:
            return False
        return (
            isinstance(parsed, dict)
            and "domain" in parsed
            and "message" in parsed
        )

    def _emit_request(self) -> None:
        text = self.body_edit.toPlainText()
        stripped = text.strip()
        if self._looks_like_typed_data(stripped):
            try:
                typed = json.loads(stripped)
            except Exception as e:
                warn(
                    self, "Bad typed data", f"JSON parse failed: {e}",
                )
                return
            req = TypedDataSigningRequest(
                from_addr=self._from_addr, typed_data=typed,
            )
        else:
            # Sign the text as-is (UTF-8 bytes, no trailing
            # newlines — most dapps don't add them, and adding
            # them silently would change the resulting signature
            # in surprising ways).
            req = MessageSigningRequest(
                from_addr=self._from_addr,
                raw=text.encode("utf-8"),
            )
        self.request_built.emit(req)
        self.accept()


class SignatureResultDialog(QDialog):
    """Post-sign confirmation showing the 65-byte signature with
    a copy-to-clipboard button. Tiny but visible so the user can
    grab the value when signing locally (no dapp to receive it
    automatically)."""

    def __init__(self, signature_hex: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signature")
        self.resize(560, 200)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        outer.addWidget(QLabel("Signature (65-byte r || s || v):"))
        self.sig_view = QTextEdit()
        self.sig_view.setReadOnly(True)
        self.sig_view.setFont(QFont("monospace"))
        self.sig_view.setPlainText(signature_hex)
        outer.addWidget(self.sig_view, 1)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Cop&y")
        copy_btn.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(signature_hex)
        )
        btn_row.addWidget(copy_btn)
        btn_row.addStretch(1)
        close_btn = QPushButton("&Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)
