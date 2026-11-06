"""Spec: stage 9, the decision.

The most severe finding wins (REJECT > HOLD > REVIEW > APPROVE), and every decision says why,
who acts next, and what (if anything) to send the vendor.
"""
from datetime import date, timedelta

import pytest

from app.rules import decide, issue


def _decide(invoice, vendor, po, issues, **kw):
    inv = kw.pop("ext", None) or invoice()
    return decide(issues, inv, kw.pop("vendor_rec", vendor("V001")), kw.pop("po_rec", po("PO-26-1001")),
                  {"method": "explicit"}, {"subtotal": inv["subtotal"], "total": inv["total"]})


@pytest.mark.parametrize("severities, expected", [
    ([], "APPROVE"),
    (["INFO"], "APPROVE"),                       # informational notes never block
    (["REVIEW"], "REVIEW"),
    (["REVIEW", "HOLD"], "HOLD"),
    (["HOLD", "REVIEW", "REJECT", "INFO"], "REJECT"),
])
def test_most_severe_finding_wins(invoice, vendor, po, severities, expected):
    issues = [issue(f"X{n}", sev, f"finding {n}") for n, sev in enumerate(severities)]
    assert _decide(invoice, vendor, po, issues)["decision"] == expected


def test_approval_schedules_payment_by_vendor_terms(invoice, vendor, po):
    inv = invoice(invoice_date="2026-10-28")
    out = _decide(invoice, vendor, po, [], ext=inv)
    assert out["decision"] == "APPROVE"
    assert out["due_date"] == (date(2026, 10, 28) + timedelta(days=30)).isoformat()   # Northwind: Net 30
    assert out["next_action"]["owner"] == "System"
    assert out["passed_checks"], "an approval lists what was verified"


def test_headline_leads_with_first_reason_and_counts_the_rest(invoice, vendor, po):
    issues = [issue("A", "HOLD", "first problem", short="first problem"), issue("B", "HOLD", "second problem")]
    assert _decide(invoice, vendor, po, issues)["headline"] == "On hold — first problem (+1 more)."


def test_hold_drafts_one_vendor_email_listing_every_problem(invoice, vendor, po):
    issues = [issue("MISSING_INVOICE_NUMBER", "HOLD", "The invoice number is not on the invoice."),
              issue("TOTAL_MISMATCH", "HOLD", "Printed total is off by $268.00.")]
    email = _decide(invoice, vendor, po, issues)["vendor_email"]
    assert email["to"] == "ar@northwind-supplies.com"
    assert "invoice number is not on the invoice" in email["body"]
    assert "off by $268.00" in email["body"]


def test_bank_change_is_verified_by_phone_and_never_by_replying_to_the_invoice(invoice, vendor, po):
    out = _decide(invoice, vendor, po, [issue("BANK_DETAILS_CHANGED", "HOLD", "Remit-to account changed.")])
    assert out["next_action"]["owner"] == "AP lead"
    assert "(415) 555-0142" in out["next_action"]["action"]
    assert out["vendor_email"] is None, "no email to a possibly compromised vendor address"


def test_inferred_po_review_offers_one_click_confirmation(invoice, vendor, po):
    out = _decide(invoice, vendor, po, [issue("PO_INFERRED", "REVIEW", "No PO printed.")],
                  vendor_rec=vendor("V004"), po_rec=po("PO-26-1004"))
    assert "PO-26-1004" in out["next_action"]["action"]


def test_price_or_scope_question_is_owned_by_the_po_buyer(invoice, vendor, po):
    out = _decide(invoice, vendor, po, [issue("UNPLANNED_LINE", "REVIEW", "Charge not on PO.")])
    assert out["next_action"]["owner"] == "J. Patel"


def test_duplicate_of_paid_invoice_tells_vendor_it_was_already_paid(invoice, vendor, po):
    out = _decide(invoice, vendor, po, [issue("DUPLICATE_PAID", "REJECT", "Already paid.")])
    assert out["decision"] == "REJECT"
    assert "already paid" in out["vendor_email"]["subject"]


def test_unknown_vendor_is_escalated_to_risk_without_contacting_the_sender(invoice, vendor, po):
    out = decide([issue("VENDOR_UNKNOWN", "REJECT", "Not an approved vendor.")], invoice(), None, None,
                 {"method": "none"}, {"subtotal": 1, "total": 1})
    assert out["next_action"]["owner"] == "Procurement / Risk"
    assert out["vendor_email"] is None
