__version__ = "0.10.0"

# Single source of truth for the HTTP User-Agent we send on every
# outbound request. DRPC's Cloudflare rejects the default
# ``Python-urllib/x.y`` UA with HTTP 403 / "error code: 1010", so
# any code making HTTP calls out of qeth must set this header.
# Derived from ``__version__`` so a release bump propagates
# automatically — no hardcoded ``"qeth/0.1"`` strings to chase.
USER_AGENT = f"qeth/{__version__}"
