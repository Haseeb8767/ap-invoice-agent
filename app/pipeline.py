"""Pipeline orchestrator: runs each stage, streams live events, persists the audit trail.

Stages never short-circuit (except when extraction itself is impossible): we
want every problem found in one pass so the vendor gets ONE complete query,
not a drip of back-and-forth emails.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from pathlib import Path

from . import config, db, rules
from .extract import ExtractionUnavailable, classify, extract

STAGES = [
    ("ingest", "Ingest document"),
    ("classify", "Classify document"),
    ("extract", "Extract fields"),
    ("integrity", "Completeness & arithmetic"),
    ("vendor", "Vendor verification"),
    ("duplicate", "Duplicate detection"),
    ("po_match", "Purchase order match"),
    ("validate", "Line & balance validation"),
    ("decide", "Decision"),
]

# run_id -> list of events (in-memory, for the live SSE stream)
EVENTS: dict[str, list] = {}
_ev_lock = threading.Lock()
# One extraction at a time: OCR/rendering is the memory-heavy step, and free hosting has 512 MB.
_extract_lock = threading.Lock()


def _emit(run_id, event):
    event["ts"] = time.time()
    with _ev_lock:
        EVENTS.setdefault(run_id, []).append(event)


def start_run(file_path: Path, filename: str, source: str) -> str:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    db.create_run(run_id, filename, file_path, source, config.extraction_mode())
    EVENTS[run_id] = []
    threading.Thread(target=_run, args=(run_id, Path(file_path), filename), daemon=True).start()
    return run_id


def _status_from(issues):
    rank = max((rules.SEVERITY_RANK[i["severity"]] for i in issues), default=0)
    return {0: "pass", 1: "warn", 2: "fail", 3: "fail"}[rank]


class Stage:
    def __init__(self, run_id, key, title, stages):
        self.run_id, self.key, self.title, self.stages = run_id, key, title, stages

    def __enter__(self):
        self.t0 = time.perf_counter()
        _emit(self.run_id, {"type": "stage_start", "key": self.key, "title": self.title})
        return self

    def done(self, summary, details=None, issues=None, status=None):
        issues = issues or []
        rec = {"key": self.key, "title": self.title, "status": status or _status_from(issues),
               "summary": summary, "details": details or {}, "issues": issues,
               "ms": int((time.perf_counter() - self.t0) * 1000)}
        self.stages.append(rec)
        _emit(self.run_id, {"type": "stage_done", **rec})
        time.sleep(config.STAGE_DELAY)
        return rec

    def __exit__(self, exc_type, exc, tb):
        # If a stage blows up, close it as failed so the UI never shows a stuck spinner.
        if exc_type is not None and not any(s["key"] == self.key for s in self.stages):
            rec = {"key": self.key, "title": self.title, "status": "fail", "summary": f"Failed: {exc}",
                   "details": {}, "issues": [], "ms": int((time.perf_counter() - self.t0) * 1000)}
            self.stages.append(rec)
            _emit(self.run_id, {"type": "stage_done", **rec})
        return False


def _run(run_id: str, path: Path, filename: str):
    t_start = time.perf_counter()
    stages, all_issues = [], []
    ext, vendor, po, po_detail, totals = {}, None, None, {}, {}
    try:
        # 1. Ingest -------------------------------------------------------------
        with Stage(run_id, *STAGES[0], stages) as s:
            import hashlib
            raw = path.read_bytes()
            s.done(f"Received {filename} ({len(raw) / 1024:.0f} KB)",
                   {"filename": filename, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:16]})

        # 2. Classify -----------------------------------------------------------
        with Stage(run_id, *STAGES[1], stages) as s:
            cls = classify(path)
            claude = config.extraction_mode() == "claude"
            if cls["kind"] == "native_text":
                human = "Native PDF with a text layer → " + (
                    "send PDF to Claude as a document" if claude else "parse text layer (offline parser)")
            else:
                human = "Image-only scan (no text layer) → " + (
                    "render pages and read with Claude vision" if claude else "render page and read with local OCR")
            s.done(human, {k: v for k, v in cls.items() if k != "text"},
                   status="pass" if cls["kind"] == "native_text" else "warn")

        # 3. Extract ------------------------------------------------------------
        with Stage(run_id, *STAGES[2], stages) as s:
            try:
                with _extract_lock:
                    ext = extract(path, cls)
            except ExtractionUnavailable as e:
                iss = [rules.issue("UNREADABLE_SCAN", "HOLD", str(e))]
                s.done("Could not read document automatically", {"error": str(e)}, iss)
                all_issues += iss
                return _finish(run_id, stages, all_issues, ext, None, None, {}, {}, t_start, filename)
            meta = ext.get("_meta", {})
            fb = []
            if meta.get("fallback_reason"):
                fb = [rules.issue("CLAUDE_UNAVAILABLE", "INFO",
                                  f"Claude could not be used — {meta['fallback_reason']}. Fields were read by the "
                                  + ("local OCR" if meta.get("extractor") == "local-ocr" else "offline parser")
                                  + " instead.", short="Claude unavailable; offline parser used")]
                all_issues += fb
            n_fields = sum(1 for k, v in ext.items() if not k.startswith("_") and v not in (None, "", []))
            s.done(f"{n_fields} fields, {len(ext.get('line_items') or [])} line item(s) via "
                   f"{meta.get('extractor')}" + (f" ({meta.get('model')})" if meta.get("model") else ""),
                   {"fields": {k: v for k, v in ext.items() if not k.startswith("_")}, "meta": meta},
                   fb, status="warn" if fb else "pass")

        # 4. Integrity ----------------------------------------------------------
        with Stage(run_id, *STAGES[3], stages) as s:
            det, iss = rules.check_integrity(ext)
            totals = det["normalized"]
            all_issues += iss
            ok = sum(1 for c in det["checks"] if c["ok"])
            s.done(f"{ok}/{len(det['checks'])} checks passed", det, iss)

        # 5. Vendor -------------------------------------------------------------
        with Stage(run_id, *STAGES[4], stages) as s:
            det, iss, vendor = rules.match_vendor(ext)
            all_issues += iss
            s.done(f"Matched {vendor['name']} ({det['matched']['score']}%)" if vendor
                   else f"'{ext.get('vendor_name')}' not found in vendor master", det, iss)

        # 6. Duplicates ---------------------------------------------------------
        with Stage(run_id, *STAGES[5], stages) as s:
            det, iss = rules.check_duplicates(ext, vendor, totals)
            all_issues += iss
            s.done(f"Compared against {det['compared_against']} prior invoice(s) — "
                   + ("no duplicates" if not det["matches"] else f"{len(det['matches'])} match(es)"), det, iss)

        # 7. PO match -----------------------------------------------------------
        with Stage(run_id, *STAGES[6], stages) as s:
            po_detail, iss, po = rules.match_po(ext, vendor, totals)
            all_issues += iss
            if po and po_detail["method"] == "explicit":
                summ = f"{po['po_number']} referenced on invoice — {po['description']}"
            elif po:
                summ = f"No PO printed; inferred {po['po_number']} (score {po_detail['confidence']:.2f})"
            else:
                summ = "No usable PO"
            s.done(summ, po_detail, iss)

        # 8. Validate -----------------------------------------------------------
        with Stage(run_id, *STAGES[7], stages) as s:
            det, iss = rules.validate_against_po(ext, po, totals, run_id)
            all_issues += iss
            if po:
                b = det["po_balance"]
                summ = (f"Expected {rules.fmt(det['expected_subtotal'])} vs invoiced "
                        f"{rules.fmt(det['invoice_subtotal'])}; PO billed {rules.fmt(b['billed_after'])} of "
                        f"{rules.fmt(b['po_amount'])} after this invoice")
            else:
                summ = "Skipped — no PO to validate against"
            s.done(summ, det, iss, status=None if po else "skipped")

        return _finish(run_id, stages, all_issues, ext, vendor, po, po_detail, totals, t_start, filename)
    except Exception as e:  # noqa: BLE001 — surface any failure in the UI rather than hanging
        tb = traceback.format_exc()
        rec = {"key": "error", "title": "Pipeline error", "status": "fail", "summary": str(e),
               "details": {"traceback": tb}, "issues": [], "ms": 0}
        stages.append(rec)
        _emit(run_id, {"type": "stage_done", **rec})
        msg = str(e)
        try:
            import anthropic
            from .extract import claude_error_reason
            if isinstance(e, anthropic.APIError):
                msg = claude_error_reason(e)
        except Exception:  # noqa: BLE001
            pass
        result = {"summary": {"decision": "REVIEW", "headline": f"Could not complete — {msg}. Fix and re-run."},
                  "error": msg}
        db.finish_run(run_id, status="error", stages=stages, result=result,
                      duration_ms=int((time.perf_counter() - t_start) * 1000))
        _emit(run_id, {"type": "run_done", "decision": "REVIEW", "status": "error"})


def _finish(run_id, stages, all_issues, ext, vendor, po, po_detail, totals, t_start, filename):
    with Stage(run_id, *STAGES[8], stages) as s:
        if "UNREADABLE_SCAN" in {i["code"] for i in all_issues}:
            dec = {"decision": "HOLD", "headline": "On hold — scanned image could not be read automatically.",
                   "reasons": all_issues, "other_flags": [], "info": [], "passed_checks": [],
                   "next_action": {"owner": "AP clerk", "action": "Enable Claude vision or key in the invoice manually."},
                   "vendor_email": None, "due_date": None}
        else:
            dec = rules.decide(all_issues, ext, vendor, po, po_detail, totals)
        status = {"APPROVE": "pass", "REVIEW": "warn", "HOLD": "fail", "REJECT": "fail"}[dec["decision"]]
        s.done(dec["headline"], dec, [], status=status)

    summary = {
        "decision": dec["decision"], "headline": dec["headline"],
        "vendor_name": vendor["name"] if vendor else ext.get("vendor_name"),
        "vendor_id": vendor["vendor_id"] if vendor else None,
        "invoice_number": ext.get("invoice_number"), "invoice_date": ext.get("invoice_date"),
        "po_number": po["po_number"] if po else None, "po_method": po_detail.get("method"),
        "total": totals.get("total"), "currency": ext.get("currency") or "USD",
    }
    ledger_status = {"APPROVE": "approved", "REVIEW": "pending_review", "HOLD": "on_hold", "REJECT": "rejected"}
    if ext:
        db.add_ledger(vendor["vendor_id"] if vendor else None, ext.get("invoice_number"),
                      rules.normalize_invoice_number(ext.get("invoice_number")), ext.get("invoice_date"),
                      po["po_number"] if po else None, totals.get("subtotal"), totals.get("total"),
                      ledger_status[dec["decision"]], run_id)
    result = {"summary": summary, "decision": dec, "extraction": {k: v for k, v in ext.items()},
              "issues": all_issues, "policy": config.POLICY}
    db.finish_run(run_id, status="done", stages=stages, result=result,
                  duration_ms=int((time.perf_counter() - t_start) * 1000))
    _emit(run_id, {"type": "run_done", "decision": dec["decision"], "status": "done"})


def human_action(run_id: str, action: str, note: str, reviewer: str = "AP reviewer") -> dict:
    run = db.get_run(run_id)
    if not run:
        raise KeyError(run_id)
    final = {"approve": "APPROVE", "reject": "REJECT"}[action]
    po = None
    if action == "approve":
        po = (run.get("result") or {}).get("summary", {}).get("po_number")
    db.update_ledger_status(run_id, "approved" if action == "approve" else "rejected", po)
    rec = {"action": action, "note": note, "by": reviewer, "at": db.now_iso(), "previous": run["decision"]}
    db.set_human_action(run_id, final, rec)
    return rec
