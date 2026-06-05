"""Type-check gate: the Qt-free core must stay mypy-clean.

qeth's type hints are otherwise unenforced (we ship no runtime type
checking), so this test makes them real — it runs mypy over the
enforced scope declared in ``[tool.mypy]`` (``files = [...]``) and fails
the suite on any new type error. That gives the annotations a
generate→typecheck→fix verifier loop and catches drift where an
annotation silently stops matching the code.

Scope is the pure-Python core only; the PySide6 UI layer is excluded in
the mypy config because incomplete Qt stubs produce unfixable false
positives (see the comment in pyproject.toml). To run just this gate:

    uv run pytest tests/test_typing.py
    uv run mypy            # the same check, directly
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_core_is_mypy_clean():
    # No path args: mypy reads the enforced `files` list + settings from
    # pyproject.toml, so this test and `uv run mypy` check exactly the
    # same scope (one source of truth).
    proc = subprocess.run(
        [sys.executable, "-m", "mypy"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "mypy found type errors in the enforced core scope:\n\n"
        f"{proc.stdout}{proc.stderr}"
    )
