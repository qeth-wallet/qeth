"""Shutdown must release signing workers before the GUI thread joins them."""


def test_quit_releases_signing_workers_waiting_for_gui_callbacks():
    """Use a subprocess so a regression cannot deadlock the test runner."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''
        from threading import Event
        from types import SimpleNamespace
        from PySide6.QtCore import QThread, Qt
        from PySide6.QtWidgets import QApplication, QWidget
        from qeth.signer_interaction import DialogInteraction
        from qeth.signing import SignerError
        from qeth.ui import MainWindow

        app = QApplication([])
        class Window(QWidget):
            _join_workers = MainWindow._join_workers
            _SHUTDOWN_JOIN_S = 0.1

        window = Window()
        window._shutting_down = False
        window.plugins = {}
        window._token_catalog_service = SimpleNamespace(shutdown=lambda: None)
        interaction = DialogInteraction(window, "Signing")
        queued = Event()
        interaction._call.connect(
            lambda *_: queued.set(), Qt.ConnectionType.DirectConnection)
        prompts = []
        interaction._prompt = lambda *_: prompts.append("unexpected prompt")
        errors = []

        class Worker(QThread):
            def run(self):
                try:
                    interaction.request_secret("Passphrase:")
                except SignerError as error:
                    errors.append(str(error))
                finally:
                    interaction.close()

        worker = Worker()
        window._active_workers = {worker}
        worker.start()
        assert queued.wait(3), "worker did not queue its prompt"
        # Do not process events: quit must resolve queued GUI work itself.
        window._join_workers()
        assert worker.isFinished()
        assert errors == ["Application is shutting down"]
        app.processEvents()  # A late queued callback must not open a prompt.
        assert prompts == []
        print("shutdown completed")
    ''')
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "shutdown completed" in completed.stdout
