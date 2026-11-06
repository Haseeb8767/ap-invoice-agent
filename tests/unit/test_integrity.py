"""Spec: stage 4, completeness and arithmetic.

The extractor transcribes what is printed and never fixes it. This stage is where wrong numbers
and missing fields are caught, explained and routed.
"""
from datetime import timedelta

import pytest

from app.rules import check_integrity
from support import TODAY, codes


def test_clean_invoice_has_no_integrity_issues(invoice):
    detail, issues = check_integrity(invoice())
    assert issues == []
    assert all(c["ok"] for c in detail["checks"])


# --- completeness ----------------------------------------------------------
@pytest.mark.parametrize("field, code", [
    ("invoice_number", "MISSING_INVOICE_NUMBER"),
    ("invoice_date", "MISSING_INVOICE_DATE"),
    ("total", "MISSING_INVOICE_TOTAL"),
    ("vendor_name", "MISSING_VENDOR_NAME"),
])
def test_missing_required_field_puts_invoice_on_hold(invoice, field, code):
    _, issues = check_integrity(invoice(**{field: None}))
    found = {i["code"]: i for i in issues}
    assert code in found
    assert found[code]["severity"] == "HOLD"


# --- arithmetic ------------------------------------------------------------
def test_total_off_by_exactly_the_tax_is_diagnosed_as_tax_added_twice(invoice):
    # 5,090 + 419.93 tax = 5,509.93; vendor printed 5,929.86 (tax added twice)
    _, issues = check_integrity(invoice(total=5929.86))
    mismatch = next(i for i in issues if i["code"] == "TOTAL_MISMATCH")
    assert mismatch["severity"] == "HOLD"
    assert "added twice" in mismatch["message"]
    assert mismatch["detail"]["difference"] == pytest.approx(419.93)


def test_tax_inclusive_claim_with_tax_on_top_is_called_out(invoice):
    _, issues = check_integrity(invoice(total=5929.86, tax_inclusive_prices=True))
    mismatch = next(i for i in issues if i["code"] == "TOTAL_MISMATCH")
    assert "already include tax" in mismatch["message"]


def test_rounding_within_five_cents_is_tolerated(invoice):
    _, issues = check_integrity(invoice(total=5509.97))
    assert "TOTAL_MISMATCH" not in codes(issues)


def test_line_quantity_times_price_must_equal_line_amount(invoice):
    lines = invoice()["line_items"]
    lines[0]["amount"] = 1800.00          # 40 x 42.50 is 1,700
    _, issues = check_integrity(invoice(line_items=lines, subtotal=5190.00, tax_amount=0, total=5190.00))
    assert "LINE_MATH" in codes(issues)


def test_lines_that_do_not_add_up_to_the_subtotal_put_invoice_on_hold(invoice):
    _, issues = check_integrity(invoice(subtotal=5900.00, total=6319.93))
    assert "SUBTOTAL_MISMATCH" in codes(issues)


def test_missing_subtotal_is_derived_from_lines_not_flagged(invoice):
    detail, issues = check_integrity(invoice(subtotal=None))
    assert issues == []
    assert detail["normalized"]["subtotal"] == pytest.approx(5090.00)


# --- dates and currency ----------------------------------------------------
def test_future_dated_invoice_needs_review(invoice):
    _, issues = check_integrity(invoice(invoice_date=(TODAY + timedelta(days=5)).isoformat()))
    assert "FUTURE_DATED" in codes(issues)


def test_invoice_older_than_policy_window_is_flagged_stale(invoice):
    _, issues = check_integrity(invoice(invoice_date=(TODAY - timedelta(days=200)).isoformat()))
    assert "STALE_INVOICE" in codes(issues)


def test_non_usd_invoice_needs_fx_review(invoice):
    _, issues = check_integrity(invoice(currency="EUR"))
    assert "FOREIGN_CURRENCY" in codes(issues)


# --- reader confidence -----------------------------------------------------
def test_low_confidence_read_is_sent_to_a_human(invoice):
    conf = invoice()["field_confidence"] | {"total": 0.42}
    _, issues = check_integrity(invoice(field_confidence=conf))
    low = next(i for i in issues if i["code"] == "LOW_CONFIDENCE")
    assert low["severity"] == "REVIEW"
    assert low["detail"]["fields"] == ["total"]


def test_low_confidence_on_an_absent_field_is_not_double_counted(invoice):
    # A missing PO reference has confidence 0 by design; PO matching handles it, not this check.
    conf = invoice()["field_confidence"] | {"po_reference": 0.0}
    _, issues = check_integrity(invoice(po_reference=None, field_confidence=conf))
    assert "LOW_CONFIDENCE" not in codes(issues)
