"""Document classification + field extraction.

Design rule: the extractor REPORTS what is printed on the invoice. It never
"fixes" numbers or guesses a PO that isn't there — that's the rules engine's
job, so every decision stays deterministic and explainable.

Two extractors:
  * Claude (default when credentials exist) — handles any layout, and scanned
    images through vision.
  * Offline fallbacks (no API key, or Claude unreachable):
      - text-layer PDFs  -> heuristic regex parser over the text layer
      - scanned images   -> local OCR (RapidOCR, app/ocr.py) + OCR-tolerant parser
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path

import pdfplumber
import pymupdf

from . import config

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(pdf_path: Path) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        pages = len(pdf.pages)
        texts = [(p.extract_text() or "") for p in pdf.pages]
        images = sum(len(p.images) for p in pdf.pages)
    chars = sum(len(t.strip()) for t in texts)
    per_page = chars / max(pages, 1)
    kind = "native_text" if per_page >= 80 else "scanned_image"
    return {
        "pages": pages,
        "text_chars": chars,
        "embedded_images": images,
        "kind": kind,
        "route": "pdf_document" if kind == "native_text" else "vision_images",
        "text": "\n".join(texts),
    }


# ---------------------------------------------------------------------------
# Extraction schema (shared by both extractors)
# ---------------------------------------------------------------------------
def _nullable(t):
    return {"anyOf": [{"type": t}, {"type": "null"}]}


CONF_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "po_reference", "total", "line_items"]

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": _nullable("string"),
        "vendor_address": _nullable("string"),
        "vendor_tax_id": _nullable("string"),
        "invoice_number": _nullable("string"),
        "invoice_date": _nullable("string"),
        "due_terms": _nullable("string"),
        "po_reference": _nullable("string"),
        "currency": _nullable("string"),
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": _nullable("number"),
                    "unit_price": _nullable("number"),
                    "amount": _nullable("number"),
                },
                "required": ["description", "quantity", "unit_price", "amount"],
                "additionalProperties": False,
            },
        },
        "subtotal": _nullable("number"),
        "tax_amount": _nullable("number"),
        "tax_rate_pct": _nullable("number"),
        "tax_inclusive_prices": {"type": "boolean"},
        "total": _nullable("number"),
        "remit_bank_last4": _nullable("string"),
        "bank_change_notice": {"type": "boolean"},
        "payment_urgency_language": {"type": "boolean"},
        "field_confidence": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in CONF_FIELDS},
            "required": CONF_FIELDS,
            "additionalProperties": False,
        },
        "extraction_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "vendor_name", "vendor_address", "vendor_tax_id", "invoice_number", "invoice_date", "due_terms",
        "po_reference", "currency", "line_items", "subtotal", "tax_amount", "tax_rate_pct",
        "tax_inclusive_prices", "total", "remit_bank_last4", "bank_change_notice",
        "payment_urgency_language", "field_confidence", "extraction_notes",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the document-reading step of an accounts-payable pipeline. You read one vendor \
invoice and transcribe its fields into the JSON schema provided. A separate rules engine validates \
everything afterwards, so your job is faithful transcription, not judgement.

Rules:
- Report values exactly as printed. Do NOT correct arithmetic, do NOT compute a missing total, do NOT \
reconcile numbers that disagree. If the printed total is wrong, report the printed total.
- If a field is not printed on the document, return null. Never invent an invoice number or PO number. \
A PO reference may be labelled "PO", "Purchase Order", "Customer PO", "Order Ref", "Re:" etc. — only fill \
po_reference when a purchase-order identifier is actually printed.
- Dates: return ISO format YYYY-MM-DD.
- Amounts: plain numbers, no currency symbols or thousands separators. Currency as ISO code (e.g. USD).
- vendor_name is the company that ISSUED the invoice (not the bill-to customer).
- tax_inclusive_prices: true only if the document states prices already include tax.
- remit_bank_last4: last 4 digits of the payee bank account if printed.
- bank_change_notice: true if the invoice announces new/changed bank or payment details.
- payment_urgency_language: true if it pressures for immediate payment (e.g. "wire today", "urgent").
- field_confidence: 0.0-1.0 for how confident you are each field was read correctly (use 0 when the field \
is absent; lower it for blurry scans or ambiguous labels).
- extraction_notes: short factual observations a human AP clerk would want to know (e.g. "printed total \
does not equal subtotal + tax", "document is a low-quality scan", "no invoice number printed")."""


# ---------------------------------------------------------------------------
# Claude extractor
# ---------------------------------------------------------------------------
def _content_blocks(pdf_path: Path, route: str) -> list[dict]:
    if route == "pdf_document":
        data = base64.standard_b64encode(pdf_path.read_bytes()).decode()
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}]
    blocks = []
    doc = pymupdf.open(pdf_path)
    for page in doc:
        png = page.get_pixmap(dpi=150).tobytes("png")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.standard_b64encode(png).decode()}})
    doc.close()
    return blocks


def extract_with_claude(pdf_path: Path, route: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    content = _content_blocks(pdf_path, route) + [
        {"type": "text", "text": "Extract this invoice into the schema."}]
    params = dict(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": config.CLAUDE_EFFORT,
                       "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
    )
    response = None
    if config.CLAUDE_FALLBACKS:
        try:
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        except anthropic.BadRequestError:
            response = None  # account/platform doesn't support fallbacks; retry plain
    if response is None:
        response = client.messages.create(**params)

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this document.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Extraction output was truncated (max_tokens).")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    usage = response.usage
    data["_meta"] = {
        "extractor": "claude",
        "model": response.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "request_id": getattr(response, "_request_id", None),
    }
    return data


# ---------------------------------------------------------------------------
# Offline heuristic extractor (text-layer PDFs only)
# ---------------------------------------------------------------------------
AMT = r"\$?\s?([\d,]+\.\d{2})"
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"]


def _num(s):
    return float(s.replace(",", "").replace("$", "").strip()) if s else None


def _parse_date(s):
    s = s.strip().rstrip(".")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _find(pattern, text, flags=re.I):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def extract_offline(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    notes = ["Offline heuristic parser used (no Claude credentials configured)."]

    vendor = re.sub(r"\s*\b(TAX |FREIGHT )?INVOICE\b.*$", "", lines[0], flags=re.I).strip() if lines else None
    inv = _find(r"(?:invoice\s*(?:no\.?|number|#)|bill\s*#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", text)

    date = None
    for m in re.finditer(r"(?:invoice date|issued|\bdate)\s*:?\s*([A-Za-z]{3,9}\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4}"
                         r"|\d{4}-\d{2}-\d{2}|\d{1,2} [A-Za-z]{3,9} \d{4})", text, re.I):
        date = _parse_date(m.group(1))
        if date:
            break

    po = _find(r"(?:PO Number|Customer PO|Order Ref|Purchase Order|Re:|PO)\s*:?\s*#?\s*(PO-\d{2}-\d{4})", text)

    items = []
    row = re.compile(r"^(.+?)\s+([\d,]+)\s+\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})$")
    for l in lines:
        m = row.match(l)
        if m and not re.match(r"(sub)?total|tax", m.group(1), re.I):
            items.append({"description": m.group(1), "quantity": _num(m.group(2)),
                          "unit_price": _num(m.group(3)), "amount": _num(m.group(4))})

    subtotal = _num(_find(r"(?:subtotal|net amount)\s*" + AMT, text))
    tax_line = next((l for l in lines if re.match(r"^(sales\s+)?tax\b", l, re.I)), None)
    tax = _num(_find(AMT + r"\s*$", tax_line)) if tax_line else None
    rate = _num(_find(r"([\d.]+)\s*%", tax_line)) if tax_line else None

    total = None
    for l in lines:
        if re.match(r"^(total due|invoice total|amount due(?: \(usd\))?|total)\b", l, re.I):
            total = _num(_find(AMT + r"\s*$", l)) or total

    bank = _find(r"account ending\s*(\d{4})", text)
    currency = "USD" if "$" in text or "USD" in text else None

    if not inv:
        notes.append("No invoice number found on document.")
    if subtotal is not None and total is not None and abs((subtotal + (tax or 0)) - total) > 0.05:
        notes.append("Printed total does not equal subtotal + tax.")

    conf = lambda v: 0.9 if v not in (None, [], "") else 0.0  # noqa: E731
    return {
        "vendor_name": vendor, "vendor_address": lines[1] if len(lines) > 1 else None,
        "vendor_tax_id": _find(r"(?:tax id|ein)\s*:?\s*(\d{2}-\d{7})", text),
        "invoice_number": inv, "invoice_date": date,
        "due_terms": _find(r"terms:?\s*(net \d+|due on receipt)", text),
        "po_reference": po, "currency": currency, "line_items": items,
        "subtotal": subtotal, "tax_amount": tax, "tax_rate_pct": rate,
        "tax_inclusive_prices": bool(re.search(r"prices? include .{0,20}tax", text, re.I)),
        "total": total, "remit_bank_last4": bank,
        "bank_change_notice": bool(re.search(r"bank (details|account) (have |has )?changed|new bank", text, re.I)),
        "payment_urgency_language": bool(re.search(r"urgent|wire payment today|immediately", text, re.I)),
        "field_confidence": {"vendor_name": conf(vendor), "invoice_number": conf(inv), "invoice_date": conf(date),
                             "po_reference": conf(po), "total": conf(total), "line_items": conf(items)},
        "extraction_notes": notes,
        "_meta": {"extractor": "offline-heuristic"},
    }


def claude_error_reason(e: Exception) -> str:
    """Plain-English reason for a Claude API failure (most specific first)."""
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return "the Anthropic API key was rejected (401 invalid x-api-key) — check ANTHROPIC_API_KEY"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "the API key does not have permission to use this model (403)"
    if isinstance(e, anthropic.NotFoundError):
        return f"model '{config.CLAUDE_MODEL}' was not found for this key (404)"
    if isinstance(e, anthropic.RateLimitError):
        return "Claude API rate limit reached (429)"
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API error {e.status_code}"
    if isinstance(e, anthropic.APIConnectionError):
        return "could not reach the Claude API (network)"
    return str(e)


# Last Claude failure, surfaced in /api/health so the UI badge can warn.
LAST_CLAUDE_ERROR: dict = {}


def _ocr_or_unavailable(pdf_path: Path, why: str) -> dict:
    from . import ocr
    try:
        return ocr.extract_ocr(pdf_path)
    except Exception as e:  # noqa: BLE001
        raise ExtractionUnavailable(f"{why} Local OCR also failed ({e}). Routed to manual data entry.") from e


def extract(pdf_path: Path, cls: dict) -> dict:
    import anthropic

    scanned = cls["kind"] == "scanned_image"
    if config.extraction_mode() == "claude":
        try:
            data = extract_with_claude(pdf_path, cls["route"])
            LAST_CLAUDE_ERROR.clear()
            return data
        except anthropic.APIError as e:
            reason = claude_error_reason(e)
            LAST_CLAUDE_ERROR.update({"reason": reason})
            if scanned:
                data = _ocr_or_unavailable(pdf_path, f"Claude vision is unavailable ({reason}).")
                data["_meta"]["fallback_reason"] = reason
                data["extraction_notes"].insert(0, f"Claude unavailable: {reason}. Read with local OCR instead.")
                return data
            data = extract_offline(cls["text"])
            data["_meta"]["fallback_reason"] = reason
            data["extraction_notes"].insert(0, f"Claude unavailable: {reason}. Fell back to the offline parser.")
            return data
    if scanned:
        return _ocr_or_unavailable(pdf_path, "This is a scanned image with no text layer.")
    return extract_offline(cls["text"])


class ExtractionUnavailable(Exception):
    pass
