"""Pure formatting helpers for displaying balances and USD values.

Lives outside ui.py so it can be unit-tested without spinning up Qt /
PySide6 (the whole module would otherwise fail to import in a test
environment without a display)."""

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
