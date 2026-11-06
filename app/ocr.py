"""Local OCR extractor for scanned invoices (no API key needed).

Uses RapidOCR (ONNX models, pip-installable, runs on CPU) to read text boxes
from the page image, groups them into rows, and parses invoice fields with
OCR-tolerant patterns. Like every extractor here it reports what is printed:
it does not fix totals or guess a PO. Confidence per field comes from the OCR
engine's own score, so shaky reads are flagged for a human by the rules engine.

Claude vision remains the preferred reader for scans; this is the fallback
when Claude is not configured or not reachable.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pymupdf

# Pages are rendered so the long side is this many pixels, whatever the PDF's page size claims
# (phone-scanner PDFs often have enormous pages). 2200 px = a US Letter page at 200 dpi.
PAGE_LONG_SIDE_PX = 2200
DET_MAX_SIDE = int(os.getenv("OCR_DET_MAX_SIDE", "1024"))
_ENGINE = None


class OCRUnavailable(Exception):
    pass


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _engine():
    global _ENGINE
    if _ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as e:  # noqa: BLE001
            raise OCRUnavailable("local OCR engine not installed (pip install rapidocr-onnxruntime)") from e
        engine = RapidOCR()
        # Run text *detection* on a downscaled copy (recognition still uses full-resolution crops).
        # Cuts peak memory from ~670 MB to ~250 MB so it fits free 512 MB hosting.
        for op in engine.text_detector.preprocess_op:
            if hasattr(op, "limit_type"):
                op.limit_type, op.limit_side_len = "max", DET_MAX_SIDE
        _ENGINE = engine
    return _ENGINE


def _img(pix):
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    return np.ascontiguousarray(a[:, :, :3])


def _zoom(page):
    return PAGE_LONG_SIDE_PX / max(page.rect.width, page.rect.height, 1)


def _read_boxes(page):
    z = _zoom(page)
    res, _ = _engine()(_img(page.get_pixmap(matrix=pymupdf.Matrix(z, z))))
    boxes = []
    for pts, txt, score in res or []:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        boxes.append({"x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
                      "yc": (min(ys) + max(ys)) / 2, "text": txt.strip(), "score": float(score)})
    return boxes


def _rows(boxes, tol=None):
    """Group boxes into visual rows by vertical centre."""
    rows = []
    for b in sorted(boxes, key=lambda b: b["yc"]):
        t = tol or max(8, (b["y1"] - b["y0"]) * 0.5)
        if rows and abs(rows[-1]["yc"] - b["yc"]) <= t:
            r = rows[-1]
            r["boxes"].append(b)
            r["yc"] = sum(x["yc"] for x in r["boxes"]) / len(r["boxes"])
        else:
            rows.append({"yc": b["yc"], "boxes": [b]})
    for r in rows:
        r["boxes"].sort(key=lambda b: b["x0"])
        r["text"] = "  ".join(b["text"] for b in r["boxes"])
    return rows


# --- OCR-tolerant value parsing --------------------------------------------
AMOUNT_RE = re.compile(r"\$\s?(\d[\d,.:;' ]*\d)")


def _amount(s):
    """'$3,450:00' / '$3.450.00' / '$3 450.00' -> 3450.0 (last two digits are cents)."""
    m = AMOUNT_RE.search(s or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    sep = re.search(r"[.,:;]\d{2}$", m.group(1))
    if not sep or len(digits) < 3:
        return float(digits) if digits else None
    return int(digits[:-2]) + int(digits[-2:]) / 100


def _date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            pass
    return None


def _box_score(rows, needle):
    for r in rows:
        for b in r["boxes"]:
            if needle and needle.replace(" ", "")[:4].lower() in b["text"].replace(" ", "").lower():
                return b["score"]
    return 0.0


def _labelled_amount(rows, label):
    for r in rows:
        if re.search(label, r["text"].replace(" ", ""), re.I):
            amts = [(b, _amount(b["text"])) for b in r["boxes"] if _amount(b["text"]) is not None]
            if amts:
                b, v = amts[-1]
                return v, b["score"]
    return None, 0.0


def _reread_cell(page, x0, x1, y0, y1):
    """Second, higher-resolution pass on one table cell (small digits like a lone '1' are often missed).
    Returns (value, score) or (None, 0). The text detector skips lone digits, so we isolate the ink
    ourselves and run only the recogniser on it."""
    z = _zoom(page)
    clip = pymupdf.Rect(x0 / z, y0 / z, x1 / z, y1 / z)
    if clip.width * z < 4 or clip.height * z < 4:
        return None, 0.0
    a = _img(page.get_pixmap(matrix=pymupdf.Matrix(1.5 * z, 1.5 * z), clip=clip))
    g = a.mean(axis=2)
    dark = g < min(170, np.median(g) - 60)
    dark[dark.mean(axis=1) > 0.5] = False          # ignore table rules / header bands
    cols = np.where(dark.sum(axis=0) >= 6)[0]
    if not len(cols):
        return None, 0.0
    rows = np.where(dark[:, cols.min():cols.max() + 1].any(axis=1))[0]
    crop = a[max(rows.min() - 8, 0):rows.max() + 8, max(cols.min() - 14, 0):cols.max() + 14]
    crop = np.pad(crop, ((6, 6), (20, 20), (0, 0)), constant_values=255)
    (txt, score), = _engine().text_recognizer([crop])[0]
    txt = txt.replace("l", "1").replace("I", "1").replace("O", "0").strip()
    if re.fullmatch(r"[\d,]+(?:\.\d+)?", txt):
        return float(txt.replace(",", "")), float(score)
    return None, 0.0


def extract_ocr(pdf_path: Path) -> dict:
    doc = pymupdf.open(pdf_path)
    page = doc[0]
    boxes = _read_boxes(page)
    rows = _rows(boxes)
    notes = [f"Scanned image read with local OCR (RapidOCR, {len(boxes)} text regions, "
             f"mean confidence {np.mean([b['score'] for b in boxes]):.2f}). Values should be confirmed by a human."]
    text = "\n".join(r["text"] for r in rows)

    # header fields
    top = rows[0]["boxes"] if rows else []
    vendor = re.sub(r"\s*(TAX|FREIGHT)?\s*INVOICE.*$", "", top[0]["text"], flags=re.I).strip() if top else None
    vendor_score = top[0]["score"] if top else 0.0

    inv, inv_s = None, 0.0
    m = re.search(r"invoice\s*(?:no|number|#)[\s:.#]*([A-Z0-9][A-Z0-9\-/]{2,})", text, re.I)
    if m:
        inv, inv_s = m.group(1).upper(), _box_score(rows, m.group(1))

    date, date_s = None, 0.0
    m = re.search(r"invoice\s*date[\s:]*(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", text, re.I)
    if m:
        date, date_s = _date(m.group(1)), _box_score(rows, m.group(1))

    m = re.search(r"tax\s*id[\s:]*(\d{2}-\d{7})", text, re.I)
    tax_id = m.group(1) if m else None
    m = re.search(r"terms[\s:]*(net\s*\d+|due\s*on\s*receipt)", text, re.I)
    terms = re.sub(r"(?i)net\s*", "Net ", m.group(1)) if m else None
    m = re.search(r"(PO-\d{2}-\d{4})", text.replace(" ", ""), re.I)
    po = m.group(1).upper() if m else None

    subtotal, sub_s = _labelled_amount(rows, r"subtotal")
    tax, _ = _labelled_amount(rows, r"^tax|salestax")
    total, total_s = _labelled_amount(rows, r"amountdue|totaldue|invoicetotal|^total")
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    rate = float(m.group(1)) if m else None
    m = re.search(r"account\s*ending\s*(\d{4})", text, re.I)
    bank = m.group(1) if m else None

    # line items: rows between the table header and the subtotal row
    # (header cells may be detected as slightly different rows, so collect them by proximity)
    qty_col = next((b for b in sorted(boxes, key=lambda b: b["yc"])
                    if re.search(r"\bqty|quantity", b["text"], re.I)), None)
    sub_row = next((r for r in rows if re.search(r"subtotal", r["text"].replace(" ", ""), re.I)), None)
    items, item_scores = [], []
    if qty_col and sub_row:
        hdr_words = r"description|item|service|qty|quantity|rate|unit|price|amount|charge|total|each"
        hdr_bottom = max(b["yc"] for b in boxes
                         if abs(b["yc"] - qty_col["yc"]) < 30 and re.search(hdr_words, b["text"], re.I))
        body = [b for b in boxes if hdr_bottom + 10 < b["yc"] < sub_row["yc"] - 10]
        for r in _rows(body, tol=25):
            amts = [b for b in r["boxes"] if _amount(b["text"]) is not None]
            words = [b for b in r["boxes"] if _amount(b["text"]) is None and not re.fullmatch(r"[\d,.]+", b["text"])]
            nums = [b for b in r["boxes"] if re.fullmatch(r"[\d,.]+", b["text"])]
            if not amts or not words:
                continue
            desc = " ".join(b["text"] for b in words)
            unit_price = _amount(amts[0]["text"]) if len(amts) > 1 else None
            amount = _amount(amts[-1]["text"])
            qty = float(nums[0]["text"].replace(",", "")) if nums else None
            row_score = min(b["score"] for b in r["boxes"])
            if qty is None and qty_col is not None:  # targeted re-read of the qty cell
                qty, q_score = _reread_cell(page, qty_col["x0"] - 40, amts[0]["x0"] - 5,
                                            min(b["y0"] for b in r["boxes"]) - 2,
                                            max(b["y1"] for b in r["boxes"]) + 6)
                if qty is not None:
                    row_score = min(row_score, q_score)
                    notes.append(f"Quantity for '{desc}' was faint; recovered by re-reading the Qty cell "
                                 f"(read '{qty:g}', confidence {q_score:.2f}).")
            if qty is None:
                notes.append(f"Quantity for '{desc}' is not legible on the scan.")
            items.append({"description": desc, "quantity": qty, "unit_price": unit_price, "amount": amount})
            item_scores.append(row_score)
    doc.close()

    if not inv:
        notes.append("No invoice number found on the scan.")
    if subtotal is not None and total is not None and abs(subtotal + (tax or 0) - total) > 0.05:
        notes.append("Printed total does not equal subtotal + tax.")

    r2 = lambda v: round(float(v), 2)  # noqa: E731
    return {
        "vendor_name": vendor, "vendor_address": rows[1]["text"] if len(rows) > 1 else None,
        "vendor_tax_id": tax_id, "invoice_number": inv, "invoice_date": date, "due_terms": terms,
        "po_reference": po, "currency": "USD" if "$" in text or "USD" in text else None,
        "line_items": items, "subtotal": subtotal, "tax_amount": tax, "tax_rate_pct": rate,
        "tax_inclusive_prices": bool(re.search(r"prices?\s*include.{0,20}tax", text, re.I)),
        "total": total, "remit_bank_last4": bank,
        "bank_change_notice": bool(re.search(r"bank\s*(details|account)\s*(have|has)?\s*changed|new\s*bank", text, re.I)),
        "payment_urgency_language": bool(re.search(r"urgent|wire\s*payment\s*today|immediately", text, re.I)),
        "field_confidence": {
            "vendor_name": r2(vendor_score), "invoice_number": r2(inv_s), "invoice_date": r2(date_s),
            "po_reference": 0.0 if not po else 0.8, "total": r2(total_s),
            "line_items": r2(min(item_scores)) if item_scores else 0.0},
        "extraction_notes": notes,
        "_meta": {"extractor": "local-ocr", "engine": "RapidOCR (ONNX)", "ocr_regions": len(boxes),
                  "ocr_text": text},
    }
