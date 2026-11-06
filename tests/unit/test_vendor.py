"""Spec: stage 5, vendor verification and payment-fraud signals.

Payments only go to onboarded vendors, and a changed bank account is verified by phone using the
number on file, never contact details printed on the invoice itself.
"""
import pytest

from app.rules import match_vendor
from support import codes


@pytest.mark.parametrize("printed_name", [
    "Northwind Office Supplies Inc.",
    "NORTHWIND OFFICE SUPPLIES",          # case, no suffix
    "Northwind Supplies",                 # known alias
])
def test_vendor_is_recognised_by_name_or_alias(invoice, printed_name):
    _, issues, vendor = match_vendor(invoice(vendor_name=printed_name))
    assert vendor["vendor_id"] == "V001"
    assert issues == []


def test_matching_tax_id_identifies_vendor_even_with_unusual_name(invoice):
    _, _, vendor = match_vendor(invoice(vendor_name="NW Office Supply Div.", vendor_tax_id="94-3812207"))
    assert vendor["vendor_id"] == "V001"


def test_unknown_vendor_is_rejected(invoice):
    detail, issues, vendor = match_vendor(invoice(vendor_name="Shadowline Trading Ltd", vendor_tax_id=None))
    assert vendor is None
    assert "VENDOR_UNKNOWN" in codes(issues)
    assert next(i for i in issues if i["code"] == "VENDOR_UNKNOWN")["severity"] == "REJECT"
    assert detail["candidates"], "closest candidates are shown to the reviewer"


def test_urgent_payment_language_from_unknown_vendor_is_flagged_as_fraud_signal(invoice):
    _, issues, _ = match_vendor(invoice(vendor_name="Shadowline Trading Ltd", vendor_tax_id=None,
                                        payment_urgency_language=True))
    assert "URGENCY_PRESSURE" in codes(issues)


def test_suspended_vendor_is_rejected(invoice):
    _, issues, vendor = match_vendor(invoice(vendor_name="Harborview Janitorial Services", vendor_tax_id=None,
                                             remit_bank_last4="3390"))
    assert vendor["vendor_id"] == "V006"
    assert "VENDOR_INACTIVE" in codes(issues)


def test_changed_bank_account_is_held_and_verified_using_phone_on_file(invoice):
    _, issues, _ = match_vendor(invoice(remit_bank_last4="8823"))
    bank = next(i for i in issues if i["code"] == "BANK_DETAILS_CHANGED")
    assert bank["severity"] == "HOLD"
    assert "(415) 555-0142" in bank["message"], "must use the vendor-master phone number"


def test_bank_change_notice_is_held_even_if_account_digits_match(invoice):
    _, issues, _ = match_vendor(invoice(bank_change_notice=True))
    assert "BANK_DETAILS_CHANGED" in codes(issues)


def test_invoice_without_bank_details_is_not_treated_as_a_bank_change(invoice):
    _, issues, _ = match_vendor(invoice(remit_bank_last4=None))
    assert "BANK_DETAILS_CHANGED" not in codes(issues)
