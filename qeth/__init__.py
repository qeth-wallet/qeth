import sys
from typing import Any

if sys.version_info >= (3, 15):
    # Python 3.15 binds concurrent.futures' executors as lazy imports (PEP
    # 810), and shiboken6 (PySide6 6.12) replaces builtins.__import__; once it
    # has, an attribute access or a function-local from-import hands back the
    # unresolved lazy object. asyncio's default executor is
    # `concurrent.futures.ThreadPoolExecutor(...)`, so run_in_executor — and
    # with it getaddrinfo, i.e. every aiohttp DNS lookup — fails with
    # "'lazy_import' object is not callable". Resolve it here, before anything
    # imports Qt: every entry point imports this package first.
    import concurrent.futures
    concurrent.futures.ThreadPoolExecutor  # touching the name resolves it

__version__ = "0.25.0"

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
