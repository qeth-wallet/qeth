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

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QDialogButtonBox, QFormLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QVBoxLayout,
)

from .dialog import Dialog


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
    # url, ok, latency_ms (or None), simv1 (True/False/None)
    probed = Signal(str, bool, object, object)
    probing_done = Signal()
    failed = Signal(str)

    def __init__(self, chain_id: int, parent=None):
        super().__init__(parent)
        self._chain_id = chain_id

    def run(self) -> None:
        try:
            from .chainlist import lookup, probe_rpc, probe_simulate_v1
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

        def _probe_one(u):
            # Reachability + latency first; only ask reachable endpoints
            # about eth_simulateV1 (saves a round-trip on dead URLs).
            ok, latency, _ = probe_rpc(u, self._chain_id,
                                       timeout=_PROBE_TIMEOUT_S)
            simv1 = (probe_simulate_v1(u, timeout=_PROBE_TIMEOUT_S)
                     if ok else None)
            return ok, latency, simv1

        max_workers = min(_PROBE_CONCURRENCY, len(urls))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_probe_one, u): u for u in urls}
            for fut in as_completed(futs):
                url = futs[fut]
                try:
                    ok, latency, simv1 = fut.result()
                except Exception:
                    ok, latency, simv1 = False, None, None
                self.probed.emit(url, ok, latency, simv1)
        self.probing_done.emit()


class ChainRpcDialog(Dialog):
    """Edit the JSON-RPC URL for one chain, plus the (global)
    Etherscan v2 API key.

    Host (MainWindow) reads ``self.rpc_url`` and
    ``self.etherscan_api_key`` after ``exec() == Accepted`` and
    persists them via ``Store.set_chain_rpc_url`` /
    ``Store.set_etherscan_api_key`` respectively. The Etherscan
    field is global (one key covers every chain Etherscan v2
    supports), but the dialog is reachable from any chain so it
    can be set / changed from wherever the user happens to be."""

    def __init__(self, chain, parent=None, etherscan_api_key: str = ""):
        super().__init__(parent)
        self._chain = chain
        self._initial_etherscan_key = etherscan_api_key or ""
        # url -> (latency_ms or None, ok, simv1); used to sort the
        # picker once all probes have come back. simv1 is True/False/None
        # (supports eth_simulateV1 / definitively not / unknown).
        self._results: dict[str, tuple[float | None, bool, object]] = {}
        self._total_to_probe = 0
        self._done_probing = 0
        self.setWindowTitle(f"RPC for {chain.name} ({chain.chain_id})")
        self.resize(640, 540)

        outer = QVBoxLayout(self)
        # Outer margins come from the Dialog base (font-derived, uniform).
        outer.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        mono = QFont("monospace")
        self.url_edit = QLineEdit(chain.rpc_url)
        self.url_edit.setFont(mono)
        self.url_edit.setMinimumHeight(30)
        form.addRow("RPC &URL:", self.url_edit)
        outer.addLayout(form)

        outer.addWidget(QLabel(
            "Or pick a public endpoint from chainlist.org "
            "(live-probed, fastest first):"
        ))
        self.picker = QListWidget()
        self.picker.setFont(mono)
        self.picker.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.picker.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.picker.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.picker.itemClicked.connect(self._on_pick)
        outer.addWidget(self.picker, 1)

        self.status_lbl = QLabel("Loading from chainlist.org…")
        self.status_lbl.setStyleSheet("color: gray;")
        # Wrap rather than widen the dialog: a long status line must not
        # dictate the window width.
        self.status_lbl.setWordWrap(True)
        outer.addWidget(self.status_lbl)

        # Etherscan v2 key: optional, global. Lives below the
        # picker so users see it without scrolling but the chain-
        # specific bits stay on top. The label spells out that
        # one key covers every supported chain so people don't
        # try to enter a different key per dialog open.
        key_form = QFormLayout()
        key_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        key_form.setHorizontalSpacing(12)
        key_form.setVerticalSpacing(8)
        self.etherscan_edit = QLineEdit(self._initial_etherscan_key)
        self.etherscan_edit.setFont(mono)
        self.etherscan_edit.setMinimumHeight(30)
        self.etherscan_edit.setPlaceholderText(
            "Leave blank to use Blockscout only"
        )
        key_form.addRow(
            "&Etherscan v2 key (all chains):",
            self.etherscan_edit,
        )
        outer.addLayout(key_form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        # Parent the loader to the QApplication, not to the
        # dialog, so it can outlive the dialog when the user
        # cancels mid-probe. The blocking ``urlopen`` calls in the
        # probe pool can't be cooperatively interrupted, so the
        # only safe options are (a) wait for them on close, or
        # (b) detach. (b) is friendlier — the dialog closes
        # instantly and the loader runs to completion in the
        # background, then cleans itself up via ``deleteLater``.
        from PySide6.QtCore import QCoreApplication
        self._loader = _ChainlistLoader(
            chain.chain_id, parent=QCoreApplication.instance(),
        )
        self._loader.loaded.connect(self._on_chainlist_loaded)
        self._loader.probed.connect(self._on_probed)
        self._loader.probing_done.connect(self._on_probing_done)
        self._loader.failed.connect(self._on_chainlist_failed)
        self._loader.finished.connect(self._loader.deleteLater)
        self._loader.start()

    def done(self, result):  # noqa: N802 — Qt method name
        # Both Accept and Reject (and any other ``done(...)`` path
        # like the WM close button) flow through here. Disconnect
        # before the dialog dies so signals from the still-running
        # loader don't fire into a half-destroyed receiver and
        # SEGFAULT. The loader is parented to the QApplication
        # (see __init__), so cutting it loose is safe — it'll
        # finish on its own and deleteLater itself.
        if self._loader is not None:
            try:
                self._loader.loaded.disconnect()
                self._loader.probed.disconnect()
                self._loader.probing_done.disconnect()
                self._loader.failed.disconnect()
            except (RuntimeError, TypeError):
                # Already disconnected, or the QThread destructor
                # ran ahead of us — nothing useful to do.
                pass
        super().done(result)

    @property
    def rpc_url(self) -> str:
        return self.url_edit.text().strip()

    @property
    def etherscan_api_key(self) -> str:
        return self.etherscan_edit.text().strip()

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
            item = QListWidgetItem(
                self._format_row(r.url, ok=None, latency_ms=None))
            item.setData(Qt.ItemDataRole.UserRole, r.url)
            # Initially make probing rows non-selectable so the
            # user doesn't accidentally pick something we haven't
            # confirmed works. ``_on_probed`` flips selectable on
            # success.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
            self.picker.addItem(item)
        self.status_lbl.setText(
            f"Probing {self._total_to_probe} endpoint(s) for "
            f"{entry.name}…"
        )

    def _on_probed(
        self, url: str, ok: bool, latency_ms, simv1=None,
    ) -> None:
        self._results[url] = (
            float(latency_ms) if latency_ms is not None else None, ok, simv1,
        )
        self._done_probing += 1
        # Find the row carrying this URL and update its text /
        # selectability. Iterating is fine — at most ~20 rows.
        for i in range(self.picker.count()):
            it = self.picker.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == url:
                it.setText(self._format_row(
                    url, ok=ok,
                    latency_ms=float(latency_ms) if latency_ms is not None else None,
                    simv1=simv1,
                ))
                if ok:
                    it.setFlags(
                        it.flags()
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsEnabled
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
        # Rank simulateV1-capable endpoints first (they make tx-event
        # previews a single fast call), then by latency within each group.
        survivors: list[tuple[int, float, str, object]] = []
        for url, (latency, ok, simv1) in self._results.items():
            if ok and latency is not None:
                survivors.append((0 if simv1 is True else 1, latency, url,
                                  simv1))
        survivors.sort(key=lambda t: (t[0], t[1]))
        self.picker.clear()
        n_sim = 0
        for _rank, latency, url, simv1 in survivors:
            item = QListWidgetItem(
                self._format_row(url, ok=True, latency_ms=latency, simv1=simv1))
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.picker.addItem(item)
            if simv1 is True:
                n_sim += 1
        if not survivors:
            self.status_lbl.setText(
                "No reachable public endpoints for this chain. "
                "Enter a URL manually above."
            )
        else:
            line2 = (f"\n⚡ marks the {n_sim} supporting tx simulation"
                     if n_sim else "")
            self.status_lbl.setText(
                f"{len(survivors)} reachable endpoint(s), from best to worst "
                f"by simulation ability and latency"
                + line2
            )

    def _on_chainlist_failed(self, msg: str) -> None:
        self.status_lbl.setText(f"chainlist fetch failed: {msg}")

    # --- picker behaviour -------------------------------------

    def _on_pick(self, item) -> None:
        url = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(url, str):
            self.url_edit.setText(url)

    # --- formatting -------------------------------------------

    @staticmethod
    def _format_row(
        url: str, ok: bool | None, latency_ms: float | None,
        simv1: object = None,
    ) -> str:
        """Single source of truth for picker-row text. ``ok=None``
        is the in-flight ``probing…`` state; ``ok=True`` shows
        latency; ``ok=False`` shows a failure marker. ``simv1`` (True/
        False/None) adds a fixed-width eth_simulateV1 capability tag."""
        if ok is None:
            chip = "  …    "
        elif ok and latency_ms is not None:
            chip = f"{latency_ms:5.0f} ms"
        else:
            chip = "  ✗    "
        # Fixed-width so URLs stay column-aligned: ⚡ supported, blank
        # otherwise (a definitive 'no' or 'unknown' both read as no badge —
        # the absence of ⚡ is the signal).
        sim = "⚡sim" if simv1 is True else "    "
        return f"{chip}  {sim}  {url}"
