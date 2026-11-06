"""Spec: stage 7, purchase-order matching (explicit or inferred).

Summit Logistics (V004) has three open freight POs, two of them near-identical LTL lanes:
  PO-26-1004  LTL Dallas TX -> Denver CO   $3,450
  PO-26-1008  LTL Houston TX -> Phoenix AZ $3,610
  PO-26-1007  FTL Chicago IL -> Atlanta GA $8,200
"""
import pytest

from app.rules import match_po
from support import codes


def _freight(invoice, description, amount):
    return invoice(vendor_name="Summit Logistics Co.", po_reference=None, subtotal=amount, total=amount,
                   line_items=[{"description": description, "quantity": 1, "unit_price": amount, "amount": amount}])


@pytest.mark.parametrize("printed", ["PO-26-1001", "PO 26 1001", "po#26-1001", "PO: 26-1001"])
def test_printed_po_is_found_in_any_common_format(invoice, vendor, printed):
    detail, issues, po = match_po(invoice(po_reference=printed), vendor("V001"), {"subtotal": 5090})
    assert po["po_number"] == "PO-26-1001"
    assert detail["method"] == "explicit"
    assert issues == []


def test_po_that_does_not_exist_holds_a_known_vendor(invoice, vendor):
    _, issues, po = match_po(invoice(po_reference="PO-26-9999"), vendor("V001"), {"subtotal": 5090})
    assert po is None
    assert next(i for i in issues if i["code"] == "PO_NOT_FOUND")["severity"] == "HOLD"


def test_po_that_does_not_exist_rejects_an_unknown_vendor(invoice):
    _, issues, _ = match_po(invoice(po_reference="PO-26-1009"), None, {"subtotal": 5090})
    assert next(i for i in issues if i["code"] == "PO_NOT_FOUND")["severity"] == "REJECT"


def test_po_belonging_to_another_vendor_is_held(invoice, vendor):
    _, issues, _ = match_po(invoice(po_reference="PO-26-1001"), vendor("V002"), {"subtotal": 5090})
    assert "PO_VENDOR_MISMATCH" in codes(issues)


@pytest.mark.parametrize("description, amount, expected_po", [
    ("LTL freight Dallas TX to Denver CO", 3450.00, "PO-26-1004"),
    ("LTL freight Houston TX to Phoenix AZ", 3610.00, "PO-26-1008"),
    ("FTL freight Chicago IL to Atlanta GA", 8200.00, "PO-26-1007"),
])
def test_missing_po_is_inferred_from_route_and_amount(invoice, vendor, description, amount, expected_po):
    detail, issues, po = match_po(_freight(invoice, description, amount), vendor("V004"), {"subtotal": amount})
    assert po["po_number"] == expected_po
    assert detail["method"] == "inferred"
    assert detail["candidates"][0]["score"] > detail["candidates"][1]["score"]


def test_inferred_po_is_never_auto_approved(invoice, vendor):
    _, issues, _ = match_po(_freight(invoice, "LTL freight Dallas TX to Denver CO", 3450), vendor("V004"),
                            {"subtotal": 3450})
    inferred = next(i for i in issues if i["code"] == "PO_INFERRED")
    assert inferred["severity"] == "REVIEW"
    assert inferred["detail"]["suggested_po"] == "PO-26-1004"


def test_no_plausible_po_asks_vendor_for_the_number(invoice, vendor):
    _, issues, po = match_po(_freight(invoice, "Warehouse racking installation", 97000), vendor("V004"),
                             {"subtotal": 97000})
    assert po is None
    assert "PO_MISSING" in codes(issues)


def test_no_po_and_unknown_vendor_cannot_be_matched(invoice):
    _, issues, po = match_po(invoice(po_reference=None), None, {"subtotal": 5090})
    assert po is None
    assert "PO_MISSING" in codes(issues)
