"""Spec: the orchestrated pipeline, its audit trail, live events and the human in the loop.

`run_extracted` runs the real pipeline but injects what the reader "saw", so these specs read as:
given an invoice that says X, the process does Y.
"""
from app import db, pipeline
from app.extract import ExtractionUnavailable
from support import issue_codes

STAGE_KEYS = [k for k, _ in pipeline.STAGES]


def test_every_stage_runs_in_order_and_is_recorded(invoice, run_extracted):
    run = run_extracted(invoice())
    assert [s["key"] for s in run["stages"]] == STAGE_KEYS
    assert all(s["status"] in ("pass", "warn", "fail", "skipped") and s["summary"] for s in run["stages"])
    assert run["status"] == "done" and run["decision"] == "APPROVE"


def test_live_event_stream_opens_and_closes_every_stage(invoice, run_extracted):
    run = run_extracted(invoice())
    events = pipeline.EVENTS[run["id"]]
    started = [e["key"] for e in events if e["type"] == "stage_start"]
    finished = [e["key"] for e in events if e["type"] == "stage_done"]
    assert started == finished == STAGE_KEYS, "no stage may be left spinning in the UI"
    assert events[-1]["type"] == "run_done"


def test_all_problems_are_collected_in_one_pass(invoice, run_extracted):
    run = run_extracted(invoice(invoice_number=None, total=5929.86, remit_bank_last4="8823"))
    assert {"MISSING_INVOICE_NUMBER", "TOTAL_MISMATCH", "BANK_DETAILS_CHANGED"} <= issue_codes(run)
    assert run["decision"] == "HOLD"


def test_decision_is_written_to_the_ledger(invoice, run_extracted):
    run = run_extracted(invoice())
    [row] = [r for r in db.ledger_rows(po_number="PO-26-1001") if r["run_id"] == run["id"]]
    assert row["status"] == "approved"
    assert db.po_billed("PO-26-1001") == 5090.00, "an approval consumes PO budget"


def test_same_invoice_submitted_twice_is_rejected_the_second_time(invoice, run_extracted):
    assert run_extracted(invoice())["decision"] == "APPROVE"
    second = run_extracted(invoice())
    assert second["decision"] == "REJECT"
    assert "DUPLICATE_PAID" in issue_codes(second)


def test_reviewer_confirming_an_inferred_po_commits_the_budget(invoice, run_extracted):
    freight = invoice(vendor_name="Summit Logistics Co.", vendor_tax_id="75-3301876", po_reference=None,
                      invoice_number="SL-3000", subtotal=3450.00, tax_amount=0, total=3450.00,
                      remit_bank_last4="2285",
                      line_items=[{"description": "LTL freight Dallas TX to Denver CO", "quantity": 1,
                                   "unit_price": 3450.00, "amount": 3450.00}])
    run = run_extracted(freight)
    assert run["decision"] == "REVIEW" and run["po_number"] == "PO-26-1004"
    assert db.po_billed("PO-26-1004") == 0, "a suggestion reserves nothing"

    action = pipeline.human_action(run["id"], "approve", "Confirmed with M. Okafor")
    assert action["previous"] == "REVIEW"
    assert db.get_run(run["id"])["final_status"] == "APPROVE"
    assert db.po_billed("PO-26-1004") == 3450.00


def test_reviewer_rejection_is_recorded_and_frees_nothing(invoice, run_extracted):
    run = run_extracted(invoice(remit_bank_last4="8823"))
    pipeline.human_action(run["id"], "reject", "Vendor confirmed bank change was not theirs")
    stored = db.get_run(run["id"])
    assert stored["final_status"] == "REJECT"
    assert stored["human_action"]["note"].startswith("Vendor confirmed")
    assert db.po_billed("PO-26-1001") == 0


def test_unreadable_document_is_held_for_manual_entry(run_with_reader):
    def unreadable(path, cls):
        raise ExtractionUnavailable("Scanned image could not be read.")
    run = run_with_reader(unreadable)
    assert run["decision"] == "HOLD"
    assert "UNREADABLE_SCAN" in issue_codes(run)


def test_unexpected_crash_is_reported_as_an_error_not_a_stuck_run(run_with_reader):
    def crash(path, cls):
        raise RuntimeError("boom")
    run = run_with_reader(crash)
    assert run["status"] == "error"
    extract_stage = next(s for s in run["stages"] if s["key"] == "extract")
    assert extract_stage["status"] == "fail", "the failing stage is closed, so the UI spinner stops"
    assert "Could not complete" in run["headline"]
