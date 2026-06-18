"""Lint gate: the package must stay ruff-clean.

Mirrors ``tests/test_typing.py`` (the mypy gate). Runs ``ruff check`` over
the ruleset declared in ``[tool.ruff.lint]`` (pyflakes — real-bug checks:
unused imports/vars, redefinitions, undefined names) and fails the suite on
any finding, so the linter is enforced, not advisory. To run just this gate:

    uv run pytest tests/test_lint.py
    uv run ruff check qeth tests   # the same check, directly
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_package_is_ruff_clean():
    # ruff is a standalone binary (no `python -m ruff`), so resolve it on
    # PATH — the uv-synced .venv/bin/ruff under `uv run`, else a system one.
    ruff = shutil.which("ruff")
    assert ruff is not None, "ruff is not installed (it's in the dev group)"
    proc = subprocess.run(
        [ruff, "check", "qeth", "tests"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "ruff found lint errors:\n\n"
        f"{proc.stdout}{proc.stderr}"
    )
