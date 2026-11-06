"""Spec: stage 8, line prices and cumulative PO budget.

PO-26-1002 (Blue Ridge) is a $48,000 blanket order for 4,000 bearings at $12. BR-77810 for
$30,000 is already paid (seed), so $18,000 remains, plus a 2% overrun tolerance ($960).
"""
import pytest

from app import db
from app.rules import normalize_invoice_number, validate_against_po
from support import codes


def _bearings(invoice, units, unit_price=12.00):
    amount = round(units * unit_price, 2)
    return invoice(vendor_name="Blue Ridge Industrial Parts LLC", po_reference="PO-26-1002", subtotal=amount,
                   tax_amount=0, total=amount,
                   line_items=[{"description": "Ball bearing 6205-2RS sealed", "quantity": units,
                                "unit_price": unit_price, "amount": amount}])


def _validate(inv, po):
    return validate_against_po(inv, po, {"subtotal": inv["subtotal"], "total": inv["total"]}, run_id="this-run")


def test_invoice_matching_po_prices_and_balance_passes(invoice, po):
    detail, issues = _validate(invoice(), po("PO-26-1001"))
    assert issues == []
    assert detail["expected_subtotal"] == pytest.approx(5090.00)


# --- line level --------------------------------------------------------------
@pytest.mark.parametrize("unit_price, flagged", [(12.20, False), (12.40, True)])   # +1.7% ok, +3.3% not
def test_unit_price_variance_beyond_two_percent_needs_review(invoice, po, unit_price, flagged):
    _, issues = _validate(_bearings(invoice, 100, unit_price), po("PO-26-1002"))
    assert ("PRICE_VARIANCE" in codes(issues)) is flagged


def test_charge_not_on_the_po_is_routed_to_the_buyer(invoice, po):
    lines = invoice()["line_items"] + [{"description": "Expedited weekend delivery surcharge", "quantity": 1,
                                       "unit_price": 640.00, "amount": 640.00}]
    _, issues = _validate(invoice(line_items=lines, subtotal=5730.00, total=5730.00), po("PO-26-1001"))
    unplanned = next(i for i in issues if i["code"] == "UNPLANNED_LINE")
    assert "J. Patel" in unplanned["message"]


def test_quantity_above_po_line_is_held(invoice, po):
    lines = invoice()["line_items"]
    lines[2] = {"description": "Ergonomic mesh office chair", "quantity": 9, "unit_price": 289.00, "amount": 2601.00}
    _, issues = _validate(invoice(line_items=lines, subtotal=5957.00, total=5957.00), po("PO-26-1001"))
    assert "QTY_EXCEEDS_PO_LINE" in codes(issues)


def test_single_bundled_line_covering_a_multi_line_po_is_accepted(invoice, po):
    # PO-26-1003 has two lines; $11,250 already paid, so $22,500 remains.
    inv = invoice(vendor_name="Apex Cloud Services Inc.", po_reference="PO-26-1003", subtotal=22500.00,
                  total=22500.00, line_items=[{"description": "Services Oct-Nov per agreement", "quantity": 1,
                                               "unit_price": 22500.00, "amount": 22500.00}])
    _, issues = _validate(inv, po("PO-26-1003"))
    assert codes(issues) == {"BUNDLED_LINES"}


# --- cumulative budget (split invoicing) ------------------------------------
def test_partial_invoice_within_remaining_balance_passes(invoice, po):
    _, issues = _validate(_bearings(invoice, 1000), po("PO-26-1002"))          # 30k + 12k <= 48k
    assert "PO_OVERRUN" not in codes(issues)


def test_overrun_within_two_percent_tolerance_passes(invoice, po):
    _, issues = _validate(_bearings(invoice, 1575), po("PO-26-1002"))          # 30k + 18.9k = 48.9k <= 48.96k
    assert "PO_OVERRUN" not in codes(issues)


def test_invoice_that_is_fine_alone_but_overbills_cumulatively_is_held(invoice, po):
    detail, issues = _validate(_bearings(invoice, 1800), po("PO-26-1002"))     # 30k + 21.6k = 51.6k
    overrun = next(i for i in issues if i["code"] == "PO_OVERRUN")
    assert overrun["severity"] == "HOLD"
    assert overrun["detail"]["over_by"] == pytest.approx(3600.00)
    assert detail["po_balance"]["billed_before"] == pytest.approx(30000.00)


def test_only_approved_or_paid_invoices_consume_po_budget(invoice, po):
    # An invoice on hold or under review must not reserve budget; an approved one must.
    for status, number in (("on_hold", "BR-1"), ("pending_review", "BR-2")):
        db.add_ledger("V002", number, normalize_invoice_number(number), "2026-10-01", "PO-26-1002",
                      12000.00, 12000.00, status, run_id=f"prior-{number}")
    _, issues = _validate(_bearings(invoice, 1000), po("PO-26-1002"))
    assert "PO_OVERRUN" not in codes(issues)

    db.add_ledger("V002", "BR-3", "BR-3", "2026-10-02", "PO-26-1002", 12000.00, 12000.00, "approved", run_id="prior-3")
    _, issues = _validate(_bearings(invoice, 1000), po("PO-26-1002"))          # 30k + 12k + 12k = 54k
    assert "PO_OVERRUN" in codes(issues)


def test_this_runs_own_ledger_entry_is_not_counted_twice(invoice, po):
    db.add_ledger("V002", "BR-9", "BR-9", "2026-10-02", "PO-26-1002", 12000.00, 12000.00, "approved", run_id="this-run")
    detail, _ = _validate(_bearings(invoice, 1000), po("PO-26-1002"))
    assert detail["po_balance"]["billed_before"] == pytest.approx(30000.00)


def test_invoice_above_auto_approval_limit_needs_review(invoice, po):
    big = invoice(total=50000.01)
    _, issues = validate_against_po(big, po("PO-26-1001"), {"subtotal": 5090.00, "total": 50000.01}, "this-run")
    assert "ABOVE_AUTO_LIMIT" in codes(issues)


def test_no_po_skips_validation(invoice):
    detail, issues = validate_against_po(invoice(), None, {"subtotal": 5090}, "this-run")
    assert issues == [] and "skipped" in detail
