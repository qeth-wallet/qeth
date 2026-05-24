"""Pure formatting helpers for displaying balances and USD values.

Lives outside ui.py so it can be unit-tested without spinning up Qt /
PySide6 (the whole module would otherwise fail to import in a test
environment without a display)."""

import datetime
from decimal import Decimal


# Map "e[+-]?N" suffixes to typographic ×10ⁿ notation, since balances on
# scam-airdrop tokens routinely land in the 10¹⁵+ range and "9.12e+10"
# reads noticeably worse than "9.12 × 10¹⁰".
_SUPERSCRIPT = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")


def format_balance(value: Decimal) -> str:
    """Format a token balance with up to 6 significant figures, replacing
    scientific notation's ``eNN`` suffix with typographic ``× 10ⁿ``.

    Examples::

        format_balance(Decimal("0.5"))                 -> "0.5"
        format_balance(Decimal("1234.5"))              -> "1234.5"
        format_balance(Decimal("9.12e+10"))            -> "9.12 × 10¹⁰"
        format_balance(Decimal("1.5e-9"))              -> "1.5 × 10⁻⁹"
    """
    s = f"{value:.6g}"
    if "e" not in s and "E" not in s:
        return s
    mantissa, _, exp = s.lower().partition("e")
    exp = exp.lstrip("+")              # drop leading "+", keep "-"
    return f"{mantissa} × 10{exp.translate(_SUPERSCRIPT)}"


def format_usd(value: Decimal) -> str:
    """Format a USD value with two-decimal dollars/cents, falling back to
    ``"<$0.01"`` for sub-cent amounts and an empty string for zero."""
    if value <= 0:
        return ""
    if value < Decimal("0.01"):
        return "<$0.01"
    return f"${value:,.2f}"


def short_addr(addr: str | None) -> str:
    """Truncate an Ethereum address for compact display: 0x1234…abcd.

    Treats ``None`` as a contract creation placeholder so callers don't
    have to special-case the tx ``to`` field on deploys."""
    if not addr:
        return "(contract creation)"
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def format_datetime(ts: int) -> str:
    """Format a unix timestamp as the locale-preferred date + time.

    Uses ``strftime("%x %X")`` — Python's C-library hooks for "locale's
    appropriate date representation" and "locale's appropriate time
    representation". The actual format (DD/MM/YYYY vs MM/DD/YYYY vs
    YYYY-MM-DD, 12-hour vs 24-hour, etc.) follows whatever LC_TIME is
    set to. qeth's entry point calls ``locale.setlocale(LC_TIME, "")``
    so the user's environment-configured locale takes effect; tests
    that need deterministic output should set the locale themselves.

    Returns ``"—"`` for non-positive timestamps (Blockscout sometimes
    drops the field on very old chain reorgs)."""
    if ts <= 0:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%x %X")
