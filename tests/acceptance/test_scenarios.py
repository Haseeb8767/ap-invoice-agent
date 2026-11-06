"""Acceptance specs: every demo scenario, end to end, from the real PDF to the decision.

These are the executable version of samples/manifest.json and of the README scenario table.
Adding a new edge case starts here: add the PDF and its manifest entry, watch this file go red,
then make it green.
"""
import pytest

from support import MANIFEST, issue_codes

# The findings that must drive each decision (not merely the right label for the wrong reason).
MUST_FIND = {
    "01_happy_northwind_INV-10421.pdf": set(),
    "02_split_po_blueridge_BR-77931.pdf": set(),
    "03_split_po_overbill_blueridge_BR-78002.pdf": {"PO_OVERRUN"},
    "04_scanned_no_po_summit_SL-2291.pdf": {"PO_INFERRED"},
    "05_duplicate_apex_APX-2026-388.pdf": {"DUPLICATE_PAID"},
    "06_missing_fields_quantum.pdf": {"MISSING_INVOICE_NUMBER", "TOTAL_MISMATCH"},
    "07_price_variance_apex_APX-2026-0412.pdf": {"UNPLANNED_LINE"},
    "08_bank_change_northwind_INV-10488.pdf": {"BANK_DETAILS_CHANGED"},
    "09_unknown_vendor_shadowline.pdf": {"VENDOR_UNKNOWN", "PO_NOT_FOUND"},
}


def _params():
    for s in MANIFEST:
        marks = [pytest.mark.slow] if "scanned" in s["scenario"] else []
        yield pytest.param(s, id=s["file"][:2] + "-" + s["scenario"], marks=marks)


def test_every_sample_has_an_acceptance_spec():
    assert {s["file"] for s in MANIFEST} == set(MUST_FIND)


@pytest.mark.parametrize("scenario", _params())
def test_sample_invoice_gets_the_expected_decision_for_the_right_reasons(run_sample, scenario):
    run = run_sample(scenario["file"])
    assert run["status"] == "done"
    assert run["decision"] == scenario["expected_decision"], run["headline"]
    assert MUST_FIND[scenario["file"]] <= issue_codes(run)


def test_split_po_story_second_invoice_fits_third_overbills(run_sample):
    """Demo order matters: 02 is approved and consumes budget, so 03 tips the PO over."""
    assert run_sample("02_split_po_blueridge_BR-77931.pdf")["decision"] == "APPROVE"
    third = run_sample("03_split_po_overbill_blueridge_BR-78002.pdf")
    overrun = next(i for i in third["result"]["issues"] if i["code"] == "PO_OVERRUN")
    assert overrun["detail"]["over_by"] == pytest.approx(15600.00)


def test_happy_path_resubmitted_is_caught_as_duplicate(run_sample):
    assert run_sample("01_happy_northwind_INV-10421.pdf")["decision"] == "APPROVE"
    again = run_sample("01_happy_northwind_INV-10421.pdf")
    assert again["decision"] == "REJECT" and "DUPLICATE_PAID" in issue_codes(again)


@pytest.mark.slow
def test_scanned_invoice_suggests_the_right_lane_over_a_lookalike(run_sample):
    run = run_sample("04_scanned_no_po_summit_SL-2291.pdf")
    candidates = run["result"]["decision"]["reasons"]
    assert run["po_number"] == "PO-26-1004"                       # Dallas -> Denver
    assert any("PO-26-1004" in r["message"] for r in candidates)
    po_stage = next(s for s in run["stages"] if s["key"] == "po_match")
    ranked = [c["po_number"] for c in po_stage["details"]["candidates"]]
    assert ranked.index("PO-26-1004") < ranked.index("PO-26-1008")  # beats Houston -> Phoenix
