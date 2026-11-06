"""Spec: invoice numbers and company names are compared after normalisation.

Why it matters: vendors re-type the same invoice number in different formats, and an exact-match
duplicate check misses `APX/2026/388` vs `APX-2026-0388`, which leads to a double payment.
"""
import pytest

from app.rules import normalize_company, normalize_invoice_number


@pytest.mark.parametrize("printed, on_file", [
    ("APX/2026/388", "APX-2026-0388"),     # different separators + leading zero
    ("apx 2026 0388", "APX-2026-0388"),    # case and spaces
    ("INV-10421", "10421"),                # "INV" prefix is noise
    ("Invoice No. 10421", "INV 10421"),    # label words are noise
    ("BR_77931", "BR-77931"),
])
def test_same_invoice_number_written_differently_normalises_equal(printed, on_file):
    assert normalize_invoice_number(printed) == normalize_invoice_number(on_file)


@pytest.mark.parametrize("a, b", [
    ("BR-77810", "BR-77931"),
    ("APX-2026-0388", "APX-2025-0388"),    # same sequence, different year
    ("SL-2291", "SL-2219"),                # transposed digits are a different invoice
])
def test_genuinely_different_invoice_numbers_stay_different(a, b):
    assert normalize_invoice_number(a) != normalize_invoice_number(b)


@pytest.mark.parametrize("missing", [None, "", "   ", "INV-"])
def test_missing_invoice_number_normalises_to_none(missing):
    assert normalize_invoice_number(missing) is None


@pytest.mark.parametrize("raw, expected", [
    ("Northwind Office Supplies Inc.", "northwind office supplies"),
    ("Quantum Print & Design LLC", "quantum print and design"),
    ("SUMMIT LOGISTICS CO.", "summit logistics"),
    (None, ""),
])
def test_company_names_drop_legal_suffixes_and_punctuation(raw, expected):
    assert normalize_company(raw) == expected
