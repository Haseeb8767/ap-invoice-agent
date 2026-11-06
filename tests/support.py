"""Plain helpers shared by the tests (importable as `from support import ...`)."""
from __future__ import annotations

import copy
import json
import os
from datetime import date, timedelta

from app import config

TODAY = date.fromisoformat(os.environ.get("AS_OF_DATE", "2026-11-06"))
SAMPLES = config.SAMPLES_DIR
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text())


def clean_invoice(**overrides) -> dict:
    """A valid Northwind invoice that matches PO-26-1001 exactly and should be APPROVED.

    Each test overrides only the fields its scenario is about, e.g.
        clean_invoice(invoice_number=None)
        clean_invoice(remit_bank_last4="8823")
    """
    inv = {
        "vendor_name": "Northwind Office Supplies Inc.",
        "vendor_address": "2100 Harbor Blvd, Oakland, CA 94607",
        "vendor_tax_id": "94-3812207",
        "invoice_number": "INV-20001",
        "invoice_date": (TODAY - timedelta(days=7)).isoformat(),
        "due_terms": "Net 30",
        "po_reference": "PO-26-1001",
        "currency": "USD",
        "line_items": [
            {"description": "Copy paper letter size 10-ream case", "quantity": 40, "unit_price": 42.50, "amount": 1700.00},
            {"description": "Toner cartridge HP 26X black", "quantity": 12, "unit_price": 138.00, "amount": 1656.00},
            {"description": "Ergonomic mesh office chair", "quantity": 6, "unit_price": 289.00, "amount": 1734.00},
        ],
        "subtotal": 5090.00,
        "tax_amount": 419.93,
        "tax_rate_pct": 8.25,
        "tax_inclusive_prices": False,
        "total": 5509.93,
        "remit_bank_last4": "4471",
        "bank_change_notice": False,
        "payment_urgency_language": False,
        "field_confidence": {k: 0.95 for k in
                             ("vendor_name", "invoice_number", "invoice_date", "po_reference", "total", "line_items")},
        "extraction_notes": [],
        "_meta": {"extractor": "test-fixture"},
    }
    inv.update(copy.deepcopy(overrides))
    return inv


def codes(issues) -> set[str]:
    """Set of issue codes, e.g. {'PO_OVERRUN', 'LOW_CONFIDENCE'}."""
    return {i["code"] for i in issues}


def issue_codes(run: dict) -> set[str]:
    """Issue codes recorded on a finished pipeline run."""
    return codes((run.get("result") or {}).get("issues", []))
