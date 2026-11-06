"""Spec: document classification, the LLM extraction contract, and graceful fallback.

The Claude API is never called for real here. A fake client records the request so we can assert
the integration contract (model, structured-output schema, guardrail prompt) and simulate failures
(refusal, truncation, 401) deterministically and for free.
"""
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app import extract as ex
from support import SAMPLES, clean_invoice

TEXT_PDF = SAMPLES / "01_happy_northwind_INV-10421.pdf"
SCAN_PDF = SAMPLES / "04_scanned_no_po_summit_SL-2291.pdf"


# --- classification ----------------------------------------------------------
def test_pdf_with_text_layer_is_sent_to_claude_as_a_document():
    cls = ex.classify(TEXT_PDF)
    assert (cls["kind"], cls["route"]) == ("native_text", "pdf_document")


def test_image_only_scan_is_routed_to_vision():
    cls = ex.classify(SCAN_PDF)
    assert (cls["kind"], cls["route"]) == ("scanned_image", "vision_images")


# --- the prompt is part of the spec ------------------------------------------
@pytest.mark.parametrize("guardrail", [
    "Do NOT correct arithmetic",
    "Never invent an invoice number or PO number",
    "return null",
    "report the printed total",
])
def test_system_prompt_keeps_the_llm_a_transcriber_not_a_decider(guardrail):
    assert guardrail.lower() in ex.SYSTEM_PROMPT.lower()


def test_schema_requires_per_field_confidence_so_rules_can_route_shaky_reads():
    assert "field_confidence" in ex.EXTRACTION_SCHEMA["required"]
    assert ex.EXTRACTION_SCHEMA["additionalProperties"] is False


# --- Claude request / response contract (fake client) ------------------------
class FakeClaude:
    """Stands in for anthropic.Anthropic(); returns a canned response and records the request."""

    def __init__(self, payload=None, stop_reason="end_turn"):
        self.calls = []
        text = json.dumps(payload if payload is not None else _schema_valid(clean_invoice()))
        response = SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                                   content=[SimpleNamespace(type="text", text=text)],
                                   usage=SimpleNamespace(input_tokens=1200, output_tokens=300))

        def create(**params):
            self.calls.append(params)
            return response
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    def __call__(self, *a, **k):
        return self


def _schema_valid(inv):
    return {k: v for k, v in inv.items() if not k.startswith("_")}


def test_text_pdf_request_uses_structured_output_and_a_document_block(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    out = ex.extract_with_claude(TEXT_PDF, "pdf_document")

    params = fake.calls[0]
    assert params["model"] == ex.config.CLAUDE_MODEL
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": ex.EXTRACTION_SCHEMA}
    assert params["messages"][0]["content"][0]["type"] == "document"
    assert out["invoice_number"] == "INV-20001"
    assert out["_meta"]["extractor"] == "claude"


def test_scan_request_sends_page_images_within_size_limit(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    ex.extract_with_claude(SCAN_PDF, "vision_images")
    block = fake.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"


@pytest.mark.parametrize("stop_reason, message", [("refusal", "declined"), ("max_tokens", "truncated")])
def test_unusable_llm_response_raises_instead_of_returning_partial_data(monkeypatch, stop_reason, message):
    monkeypatch.setattr(anthropic, "Anthropic", FakeClaude(stop_reason=stop_reason))
    with pytest.raises(RuntimeError, match=message):
        ex.extract_with_claude(TEXT_PDF, "pdf_document")


# --- fallback when Claude is unavailable --------------------------------------
def _auth_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=req), body=None)


@pytest.fixture
def claude_mode(monkeypatch):
    monkeypatch.setenv("EXTRACTION_MODE", "claude")
    ex.LAST_CLAUDE_ERROR.clear()
    yield
    ex.LAST_CLAUDE_ERROR.clear()


def test_rejected_api_key_falls_back_to_offline_parser_and_says_why(monkeypatch, claude_mode):
    def broken(*a, **k):
        raise _auth_error()
    monkeypatch.setattr(ex, "extract_with_claude", broken)

    out = ex.extract(TEXT_PDF, ex.classify(TEXT_PDF))
    assert out["_meta"]["extractor"] == "offline-heuristic"
    assert "401" in out["_meta"]["fallback_reason"]
    assert out["invoice_number"] == "INV-10421", "the invoice is still read"
    assert "401" in ex.LAST_CLAUDE_ERROR["reason"], "surfaced to the UI health badge"


def test_successful_claude_call_clears_a_previous_error(monkeypatch, claude_mode):
    ex.LAST_CLAUDE_ERROR["reason"] = "old failure"
    monkeypatch.setattr(ex, "extract_with_claude", lambda *a, **k: {"_meta": {"extractor": "claude"}})
    ex.extract(TEXT_PDF, ex.classify(TEXT_PDF))
    assert ex.LAST_CLAUDE_ERROR == {}


@pytest.mark.parametrize("error, reason", [
    (_auth_error(), "401"),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), "network"),
])
def test_api_errors_are_explained_in_plain_english(error, reason):
    assert reason in ex.claude_error_reason(error)


# --- offline readers produce the same contract --------------------------------
def test_offline_parser_reads_the_clean_invoice():
    out = ex.extract_offline(ex.classify(TEXT_PDF)["text"])
    assert out["vendor_name"] == "Northwind Office Supplies Inc."
    assert (out["invoice_number"], out["po_reference"]) == ("INV-10421", "PO-26-1001")
    assert len(out["line_items"]) == 3
    assert (out["subtotal"], out["tax_amount"], out["total"]) == (5090.00, 419.93, 5509.93)


def test_offline_parser_output_satisfies_the_extraction_schema():
    out = ex.extract_offline(ex.classify(TEXT_PDF)["text"])
    assert set(ex.EXTRACTION_SCHEMA["required"]) <= set(out)


@pytest.mark.slow
def test_local_ocr_reads_the_scanned_invoice_and_reports_honest_confidence():
    from app.ocr import extract_ocr
    out = extract_ocr(SCAN_PDF)
    assert set(ex.EXTRACTION_SCHEMA["required"]) <= set(out)
    assert (out["invoice_number"], out["invoice_date"], out["total"]) == ("SL-2291", "2026-10-24", 3450.00)
    assert out["po_reference"] is None, "no PO is printed, so none may be invented"
    assert out["line_items"][0]["quantity"] == 1
    assert out["field_confidence"]["line_items"] < 0.8, "the faint quantity must be reported as uncertain"
