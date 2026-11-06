"""Spec: stage 6, duplicate detection.

Seeded ledger (previously paid): BR-77810 (Blue Ridge) and APX-2026-0388 (Apex Cloud).
"""
from datetime import timedelta

from app import db
from app.rules import check_duplicates, normalize_invoice_number
from support import TODAY, codes


def _ledger(vendor_id, number, status, total=5509.93, days_ago=7, po="PO-26-1001"):
    db.add_ledger(vendor_id, number, normalize_invoice_number(number), (TODAY - timedelta(days=days_ago)).isoformat(),
                  po, total, total, status, run_id=f"prior-{number}")


def test_new_invoice_number_is_not_a_duplicate(invoice, vendor):
    _, issues = check_duplicates(invoice(), vendor("V001"), {"total": 5509.93})
    assert issues == []


def test_reformatted_number_of_an_already_paid_invoice_is_rejected(invoice, vendor):
    inv = invoice(vendor_name="Apex Cloud Services Inc.", invoice_number="APX/2026/388")
    detail, issues = check_duplicates(inv, vendor("V003"), {"total": 11250.00})
    dup = next(i for i in issues if i["code"] == "DUPLICATE_PAID")
    assert dup["severity"] == "REJECT"
    assert dup["detail"]["original"] == "APX-2026-0388"
    assert detail["matches"][0]["type"] == "normalized"


def test_invoice_already_in_review_queue_is_flagged_not_rejected(invoice, vendor):
    _ledger("V001", "INV-20001", "pending_review")
    _, issues = check_duplicates(invoice(), vendor("V001"), {"total": 5509.93})
    assert codes(issues) == {"DUPLICATE_IN_PROCESS"}


def test_previously_rejected_invoice_can_be_resubmitted(invoice, vendor):
    _ledger("V001", "INV-20001", "rejected")
    _, issues = check_duplicates(invoice(), vendor("V001"), {"total": 5509.93})
    assert issues == []


def test_same_amount_same_vendor_within_window_is_a_suspected_reissue(invoice, vendor):
    _ledger("V001", "INV-19999", "approved", days_ago=10)
    _, issues = check_duplicates(invoice(), vendor("V001"), {"total": 5509.93})
    assert "SUSPECTED_DUPLICATE" in codes(issues)


def test_same_amount_outside_window_is_normal_recurring_billing(invoice, vendor):
    _ledger("V001", "INV-19000", "paid", days_ago=60)
    _, issues = check_duplicates(invoice(), vendor("V001"), {"total": 5509.93})
    assert issues == []


def test_duplicate_check_is_scoped_to_the_vendor(invoice, vendor):
    # Another vendor happens to use the same number format; that is not our duplicate.
    _, issues = check_duplicates(invoice(invoice_number="APX-2026-0388"), vendor("V001"), {"total": 5509.93})
    assert issues == []
