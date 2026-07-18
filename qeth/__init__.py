from typing import Any

__version__ = "0.20.0"

# PySide6 accepts C++ type names as strings in ``Signal(...)`` declarations;
# ``"qulonglong"`` marshals a chain id through unsigned 64-bit (dapp-added
# chain ids exceed qint32 — Palm = 11297108109). The PySide6 stubs type
# Signal's args as ``type``, so the string form trips mypy; typing this
# constant ``Any`` keeps call sites clean. See CLAUDE.md "PySide6 signals".
QULONGLONG: Any = "qulonglong"

# Single source of truth for the HTTP User-Agent we send on every
# outbound request. DRPC's Cloudflare rejects the default
# ``Python-urllib/x.y`` UA with HTTP 403 / "error code: 1010", so
# any code making HTTP calls out of qeth must set this header.
# Derived from ``__version__`` so a release bump propagates
# automatically — no hardcoded ``"qeth/0.1"`` strings to chase.
USER_AGENT = f"qeth/{__version__}"
