"""Shared test setup.

Environment is fixed *before* the app is imported, because app.config reads it at import time:
  * a throwaway runtime dir, so tests never touch your real runtime/ap.db
  * no pause between stages
  * offline extraction (no network, no API key, no cost), unless a test opts in explicitly
  * a pinned business date, so date rules don't drift as the sample invoices age
"""
from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

os.environ["RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ap-tests-")
os.environ["STAGE_DELAY"] = "0"
os.environ["EXTRACTION_MODE"] = "offline"
os.environ["AS_OF_DATE"] = "2026-11-06"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

import pytest  # noqa: E402

from app import config, data, db, pipeline  # noqa: E402

from support import SAMPLES, clean_invoice  # noqa: E402


# ---------------------------------------------------------------------------
# Isolation: every test starts from the seeded ledger (2 previously paid invoices)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fresh_ledger():
    db.reset()
    pipeline.EVENTS.clear()
    yield


# ---------------------------------------------------------------------------
# Data fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def invoice():
    """Factory fixture: invoice(**overrides) -> extracted-invoice dict."""
    return clean_invoice


@pytest.fixture
def vendor():
    """vendor('V001') -> vendor master record."""
    return data.vendor_by_id


@pytest.fixture
def po():
    """po('PO-26-1001') -> purchase order with lines."""
    return lambda number: data.purchase_orders()[number]


# ---------------------------------------------------------------------------
# Pipeline runners
# ---------------------------------------------------------------------------
def _run_sync(path: Path, name: str, run_id: str) -> dict:
    db.create_run(run_id, name, path, "test", config.extraction_mode())
    pipeline._run(run_id, path, name)        # synchronous: no background thread in tests
    return db.get_run(run_id)


@pytest.fixture
def run_sample():
    """run_sample('01_happy_northwind_INV-10421.pdf') -> stored run, after the full real pipeline."""
    counter = iter(range(10_000))

    def _run(filename: str) -> dict:
        return _run_sync(SAMPLES / filename, filename, f"test-{filename[:2]}-{next(counter)}")
    return _run


@pytest.fixture
def run_with_reader(monkeypatch):
    """run_with_reader(reader) -> stored run.

    Runs the real pipeline (classify, rules, decision, ledger, events) with the document reader
    replaced by `reader(path, cls)`, which may return an extraction dict or raise.
    """
    counter = iter(range(10_000))

    def _run(reader) -> dict:
        monkeypatch.setattr(pipeline, "extract", reader)
        pdf = SAMPLES / "01_happy_northwind_INV-10421.pdf"   # any real PDF; the reader decides what it "says"
        return _run_sync(pdf, "injected.pdf", f"inj-{next(counter)}")
    return _run


@pytest.fixture
def run_extracted(run_with_reader):
    """run_extracted(invoice_dict) -> stored run.

    Lets a spec say "given an invoice that reads like X, the decision is Y" without generating a PDF
    for every case.
    """
    return lambda extracted: run_with_reader(lambda path, cls: copy.deepcopy(extracted))
