"""Dialog for editing the RPC URL of an existing chain.

Opens from the small button next to the chain selector in the
toolbar. Two ways to set the URL:

- Manual paste / edit in the URL field.
- Click an entry from the chainlist.org-sourced list below the
  field — that endpoint's URL fills in immediately.

The picker doesn't trust chainlist's listing. Each HTTP endpoint
is probed live with ``eth_chainId`` so we can both (a) drop
anything that requires registration (it returns 401/403/HTML),
and (b) rank surviving endpoints by measured round-trip latency.
The user sees probing happen in real time and ends up with a
working, latency-sorted list — no hardcoded "needs-an-account"
denylist to maintain.

The dialog never silently changes the running chain. The user
explicitly clicks OK; on accept the store is updated via
``Store.set_chain_rpc_url``.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QVBoxLayout,
)


log = logging.getLogger("qeth.chain_rpc_dialog")


# Max concurrent probes. chainlist entries for popular chains
# top out at ~20 HTTP RPCs; 16 in-flight keeps the local TCP
# pool reasonable without serializing the wait.
_PROBE_CONCURRENCY = 16
_PROBE_TIMEOUT_S = 5.0


class _ChainlistLoader(QThread):
    """Run the registry lookup + live probes off the Qt main
    thread. Emits the chain entry once it's known, then a series
    of per-URL probe results, then ``probing_done`` when the
    thread pool drains."""

    loaded = Signal(object)              # ChainEntry | None
    probed = Signal(str, bool, object)   # url, ok, latency_ms (or None)
    probing_done = Signal()
    failed = Signal(str)

    def __init__(self, chain_id: int, parent=None):
        super().__init__(parent)
        self._chain_id = chain_id

    def run(self) -> None:
        try:
            from .chainlist import lookup, probe_rpc
            entry = lookup(self._chain_id)
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.loaded.emit(entry)
        if entry is None:
            self.probing_done.emit()
            return
        urls = [r.url for r in entry.rpcs
                if r.url.startswith(("http://", "https://"))]
        if not urls:
            self.probing_done.emit()
            return
        max_workers = min(_PROBE_CONCURRENCY, len(urls))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {
                ex.submit(
                    probe_rpc, u, self._chain_id,
                    timeout=_PROBE_TIMEOUT_S,
                ): u for u in urls
            }
            for fut in as_completed(futs):
                url = futs[fut]
                try:
                    ok, latency, _ = fut.result()
                except Exception:
                    ok, latency = False, None
                self.probed.emit(url, ok, latency)
        self.probing_done.emit()


class ChainRpcDialog(QDialog):
    """Edit the JSON-RPC URL for one chain.

    Host (MainWindow) reads ``self.rpc_url`` after
    ``exec() == Accepted`` and persists it via
    ``Store.set_chain_rpc_url``."""

    def __init__(self, chain, parent=None):
        super().__init__(parent)
        self._chain = chain
        # url -> (latency_ms or None, ok); used to sort the
        # picker once all probes have come back.
        self._results: dict[str, tuple[Optional[float], bool]] = {}
        self._total_to_probe = 0
        self._done_probing = 0
        self.setWindowTitle(f"RPC for {chain.name} ({chain.chain_id})")
        self.resize(640, 540)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        mono = QFont("monospace")
        self.url_edit = QLineEdit(chain.rpc_url)
        self.url_edit.setFont(mono)
        self.url_edit.setMinimumHeight(30)
        form.addRow("RPC URL:", self.url_edit)
        outer.addLayout(form)

        outer.addWidget(QLabel(
            "Or pick a public endpoint from chainlist.org "
            "(live-probed, fastest first):"
        ))
        self.picker = QListWidget()
        self.picker.setFont(mono)
        self.picker.setTextElideMode(Qt.ElideMiddle)
        self.picker.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.picker.setSelectionMode(QAbstractItemView.SingleSelection)
        self.picker.itemClicked.connect(self._on_pick)
        outer.addWidget(self.picker, 1)

        self.status_lbl = QLabel("Loading from chainlist.org…")
        self.status_lbl.setStyleSheet("color: gray;")
        outer.addWidget(self.status_lbl)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        # Track on self so Python doesn't GC the QThread mid-run;
        # deleteLater on finish cleans up if the dialog closes
        # while still probing.
        self._loader = _ChainlistLoader(chain.chain_id, parent=self)
        self._loader.loaded.connect(self._on_chainlist_loaded)
        self._loader.probed.connect(self._on_probed)
        self._loader.probing_done.connect(self._on_probing_done)
        self._loader.failed.connect(self._on_chainlist_failed)
        self._loader.finished.connect(self._loader.deleteLater)
        self._loader.start()

    def closeEvent(self, event):  # noqa: N802 — Qt method name
        # Wait for the loader to finish so its QThread destructor
        # doesn't abort() the whole process when the dialog is
        # destroyed mid-probe.
        if self._loader is not None and self._loader.isRunning():
            self._loader.wait(_PROBE_TIMEOUT_S * 1000 + 500)
        super().closeEvent(event)

    @property
    def rpc_url(self) -> str:
        return self.url_edit.text().strip()

    # --- loader callbacks -------------------------------------

    def _on_chainlist_loaded(self, entry) -> None:
        if entry is None:
            self.status_lbl.setText(
                f"No chainlist entry for chain "
                f"{self._chain.chain_id}. Enter the URL manually."
            )
            return
        http_rpcs = [
            r for r in entry.rpcs
            if r.url.startswith(("http://", "https://"))
        ]
        if not http_rpcs:
            self.status_lbl.setText(
                f"chainlist has no HTTP(S) RPC for "
                f"{entry.name} ({entry.chain_id})."
            )
            return
        self._total_to_probe = len(http_rpcs)
        # Pre-populate the picker so the user sees activity. Each
        # row gets a "probing…" placeholder; ``_on_probed``
        # replaces it once the probe lands. URL is stored in
        # UserRole so we can find rows by URL on update.
        for r in http_rpcs:
            item = QListWidgetItem(self._format_row(r.url, None, None))
            item.setData(Qt.UserRole, r.url)
            # Initially make probing rows non-selectable so the
            # user doesn't accidentally pick something we haven't
            # confirmed works. ``_on_probed`` flips selectable on
            # success.
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled)
            self.picker.addItem(item)
        self.status_lbl.setText(
            f"Probing {self._total_to_probe} endpoint(s) for "
            f"{entry.name}…"
        )

    def _on_probed(
        self, url: str, ok: bool, latency_ms,
    ) -> None:
        self._results[url] = (
            float(latency_ms) if latency_ms is not None else None, ok,
        )
        self._done_probing += 1
        # Find the row carrying this URL and update its text /
        # selectability. Iterating is fine — at most ~20 rows.
        for i in range(self.picker.count()):
            it = self.picker.item(i)
            if it.data(Qt.UserRole) == url:
                it.setText(self._format_row(
                    url, ok,
                    float(latency_ms) if latency_ms is not None else None,
                ))
                if ok:
                    it.setFlags(
                        it.flags()
                        | Qt.ItemIsSelectable
                        | Qt.ItemIsEnabled
                    )
                break
        if self._done_probing < self._total_to_probe:
            self.status_lbl.setText(
                f"Probing endpoints… "
                f"{self._done_probing}/{self._total_to_probe} done."
            )

    def _on_probing_done(self) -> None:
        # Drop failed rows, then sort surviving rows by latency
        # ascending. We rebuild item text via the same formatter
        # so the visible row stays consistent.
        survivors: list[tuple[float, str]] = []
        for url, (latency, ok) in self._results.items():
            if ok and latency is not None:
                survivors.append((latency, url))
        survivors.sort()
        self.picker.clear()
        for latency, url in survivors:
            item = QListWidgetItem(self._format_row(url, True, latency))
            item.setData(Qt.UserRole, url)
            self.picker.addItem(item)
        if not survivors:
            self.status_lbl.setText(
                "No reachable public endpoints for this chain. "
                "Enter a URL manually above."
            )
        else:
            self.status_lbl.setText(
                f"{len(survivors)} reachable endpoint(s), "
                f"sorted by latency. Click one to fill the URL "
                f"field above."
            )

    def _on_chainlist_failed(self, msg: str) -> None:
        self.status_lbl.setText(f"chainlist fetch failed: {msg}")

    # --- picker behaviour -------------------------------------

    def _on_pick(self, item) -> None:
        url = item.data(Qt.UserRole)
        if isinstance(url, str):
            self.url_edit.setText(url)

    # --- formatting -------------------------------------------

    @staticmethod
    def _format_row(
        url: str, ok: Optional[bool], latency_ms: Optional[float],
    ) -> str:
        """Single source of truth for picker-row text. ``ok=None``
        is the in-flight ``probing…`` state; ``ok=True`` shows
        latency; ``ok=False`` shows a failure marker."""
        if ok is None:
            chip = "  …    "
        elif ok and latency_ms is not None:
            chip = f"{latency_ms:5.0f} ms"
        else:
            chip = "  ✗    "
        return f"{chip}  {url}"
