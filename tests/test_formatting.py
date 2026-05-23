"""Tests for qeth.formatting — pure helpers, no Qt."""

from decimal import Decimal

import pytest

from qeth.formatting import format_balance, format_usd


# --- format_balance --------------------------------------------------------

class TestFormatBalance:
    @pytest.mark.parametrize("inp,expected", [
        ("0",                "0"),
        ("0.5",              "0.5"),
        ("1234.5",           "1234.5"),
        ("0.0837759",        "0.0837759"),
        # Six significant figures cap
        ("123456.789",       "123457"),
    ])
    def test_normal_range_passes_through(self, inp, expected):
        assert format_balance(Decimal(inp)) == expected

    @pytest.mark.parametrize("inp,expected", [
        ("9.12e+10",         "9.12 × 10¹⁰"),
        ("1.5e-9",           "1.5 × 10⁻⁹"),
        ("1.13e+59",         "1.13 × 10⁵⁹"),
        # Positive exponent has its '+' dropped, negative keeps '-'.
        ("4.257e+09",        "4.257 × 10⁹"),
        ("4.257e-9",         "4.257 × 10⁻⁹"),
    ])
    def test_scientific_to_superscript(self, inp, expected):
        assert format_balance(Decimal(inp)) == expected

    def test_two_digit_exponent_both_chars_superscripted(self):
        # 10¹³ uses two superscript digits — both must transform.
        s = format_balance(Decimal("12000000000000"))
        # The number formats with .6g to "1.20000e+13"; mantissa preserved.
        assert "× 10¹³" in s

    def test_no_e_means_no_substitution(self):
        # "1234.5" -> no "e" in output; ensure the "1234" digits don't
        # accidentally get superscripted.
        out = format_balance(Decimal("1234.5"))
        assert "×" not in out
        assert "⁰" not in out


# --- format_usd ------------------------------------------------------------

class TestFormatUsd:
    @pytest.mark.parametrize("inp", ["0", "-1", "-0.50"])
    def test_zero_or_negative_returns_empty(self, inp):
        assert format_usd(Decimal(inp)) == ""

    def test_sub_cent_label(self):
        assert format_usd(Decimal("0.001")) == "<$0.01"
        assert format_usd(Decimal("0.009999")) == "<$0.01"

    def test_threshold_at_one_cent(self):
        # Exactly one cent should display with two decimals, not the
        # sub-cent label.
        assert format_usd(Decimal("0.01")) == "$0.01"

    @pytest.mark.parametrize("inp,expected", [
        ("0.10",         "$0.10"),
        ("1",            "$1.00"),
        ("999.99",       "$999.99"),
        ("1000",         "$1,000.00"),
        ("1234567.89",   "$1,234,567.89"),
    ])
    def test_dollar_amounts(self, inp, expected):
        assert format_usd(Decimal(inp)) == expected

    def test_rounds_to_two_decimals(self):
        assert format_usd(Decimal("1.2345")) == "$1.23"
        assert format_usd(Decimal("1.2378")) == "$1.24"
