#!/usr/bin/env bash
# Run every static check in one go:
#   - ruff  (lint)   — gated in pytest via tests/test_lint.py
#   - mypy  (types)  — gated in pytest via tests/test_typing.py
#   - ty    (types)  — PREVIEW, non-blocking; informational only for now
#
# ruff + mypy are the enforced gates (they also run under `uv run pytest`);
# this script is the convenience all-in-one and the only place ty runs.
#
# ty doesn't follow the venv's include-system-site-packages into the system
# site-packages where PySide6 lives (this project uses `uv venv
# --system-site-packages`), so ty alone can't resolve PySide6. We inject that
# path here — derived from PySide6's actual location, so it's portable across
# machines/Python versions. Re-evaluate gating ty once it hits a stable
# release. See the pyvenv/system-site-packages note in CLAUDE.md.
set -u
cd "$(dirname "$0")/.."

fail=0

echo "== ruff (lint) =="
uv run ruff check qeth tests extensions || fail=1

echo "== mypy (types) =="
uv run mypy || fail=1

echo "== ty (types, preview — non-blocking) =="
if uv run --quiet ty --version >/dev/null 2>&1; then
    syspkgs="$(uv run python -c 'import PySide6, os; print(os.path.dirname(os.path.dirname(PySide6.__file__)))')"
    uv run ty check --extra-search-path "$syspkgs" qeth extensions/falkon || true
else
    echo "  (ty not installed — skipping; \`uv pip install ty\` to enable)"
fi

if [ "$fail" -ne 0 ]; then
    echo "" ; echo "FAILED: ruff or mypy reported errors." >&2
fi
exit $fail
