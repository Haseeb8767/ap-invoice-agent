"""Spec: the HTTP API the UI is built on (FastAPI TestClient, no real server needed)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from support import SAMPLES

HAPPY = "01_happy_northwind_INV-10421.pdf"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _wait_until_finished(client, run_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    raise AssertionError("run did not finish in time")


def test_health_reports_mode_and_readers(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["mode"] == "offline"
    assert "local_ocr" in body and "claude_error" in body


def test_sample_catalogue_lists_all_scenarios(client):
    assert len(client.get("/api/samples").json()) == 9


def test_running_a_sample_produces_a_decision(client):
    run_id = client.post("/api/runs", data={"sample": HAPPY}).json()["run_id"]
    run = _wait_until_finished(client, run_id)
    assert run["decision"] == "APPROVE"
    assert "file_path" not in run, "server paths are not leaked to the browser"


def test_uploading_a_pdf_runs_it(client):
    with open(SAMPLES / HAPPY, "rb") as f:
        resp = client.post("/api/runs", files={"file": ("my invoice.pdf", f, "application/pdf")})
    run = _wait_until_finished(client, resp.json()["run_id"])
    assert run["filename"] == "my_invoice.pdf", "uploaded filenames are sanitised"


def test_non_pdf_upload_is_refused_with_a_clear_message(client):
    resp = client.post("/api/runs", files={"file": ("invoice.pdf", b"hello, not a pdf", "application/pdf")})
    assert resp.status_code == 400
    assert "PDF" in resp.json()["detail"]


@pytest.mark.parametrize("name", ["nope.pdf", "../app/config.py", "..\\README.md"])
def test_unknown_or_path_traversal_sample_is_not_found(client, name):
    assert client.post("/api/runs", data={"sample": name}).status_code == 404


def test_live_event_stream_delivers_stages_then_run_done(client):
    run_id = client.post("/api/runs", data={"sample": HAPPY}).json()["run_id"]
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1]["type"] == "run_done":
                    break
    assert [e["key"] for e in events if e["type"] == "stage_done"][0] == "ingest"
    assert events[-1]["type"] == "run_done"


def test_human_action_validates_input(client):
    run_id = client.post("/api/runs", data={"sample": HAPPY}).json()["run_id"]
    _wait_until_finished(client, run_id)
    assert client.post(f"/api/runs/{run_id}/action", json={"action": "pay-now"}).status_code == 400
    assert client.post("/api/runs/does-not-exist/action", json={"action": "approve"}).status_code == 404


def test_dashboard_stats_reflect_finished_runs(client):
    run_id = client.post("/api/runs", data={"sample": HAPPY}).json()["run_id"]
    _wait_until_finished(client, run_id)
    stats = client.get("/api/stats").json()
    assert stats["total_runs"] >= 1
    assert set(stats) >= {"auto_approval_rate", "value_processed", "open_exceptions", "by_status"}


def test_reference_data_exposes_po_balances_and_policy(client):
    ref = client.get("/api/reference").json()
    po_1002 = next(p for p in ref["purchase_orders"] if p["po_number"] == "PO-26-1002")
    assert po_1002["billed"] == 30000.00 and po_1002["remaining"] == 18000.00
    assert ref["policy"]["price_tolerance_pct"] == 2.0
