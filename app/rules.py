"""Deterministic AP rules engine.

Every check produces zero or more Issues: (code, severity, message). The final
decision is simply the most severe issue found, so it's always explainable:

    REJECT  > HOLD  > REVIEW  > (none) = APPROVE

  REJECT  must not be paid (duplicate of a paid invoice, unknown/suspended vendor)
  HOLD    cannot be processed until something external is fixed (vendor must
          re-issue, buyer must amend PO, bank change must be verified)
  REVIEW  probably fine, but a human must confirm a judgement call
  APPROVE all checks passed within policy tolerance
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from rapidfuzz import fuzz

from . import data, db
from .config import POLICY

SEVERITY_RANK = {"INFO": 0, "REVIEW": 1, "HOLD": 2, "REJECT": 3}
DECISION_FOR = {0: "APPROVE", 1: "REVIEW", 2: "HOLD", 3: "REJECT"}


def issue(code, severity, message, short=None, **detail):
    return {"code": code, "severity": severity, "message": message, "short": short or message.split(". ")[0],
            "detail": detail}


def fmt(x):
    return "—" if x is None else f"${x:,.2f}"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
_PREFIX_WORDS = {"INV", "INVOICE", "NO", "NUM", "NUMBER", "BILL"}


def normalize_invoice_number(s: str | None) -> str | None:
    """'APX/2026/388' == 'APX-2026-0388';  'INV-0042' == '42'."""
    if not s:
        return None
    tokens = re.findall(r"[A-Za-z]+|\d+", s.upper())
    out = []
    for t in tokens:
        if t.isdigit():
            out.append(str(int(t)))
        elif t not in _PREFIX_WORDS:
            out.append(t)
    return "-".join(out) or None


_SUFFIX = re.compile(r"\b(inc|incorporated|llc|ltd|limited|co|corp|corporation|company|plc)\b\.?", re.I)


def normalize_company(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("&", " and ")
    s = _SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def parse_iso(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").date() if d else None
    except ValueError:
        return None


def terms_days(terms: str | None) -> int:
    m = re.search(r"(\d+)", terms or "")
    return int(m.group(1)) if m else 30


# ---------------------------------------------------------------------------
# Stage: document integrity (completeness + arithmetic + dates)
# ---------------------------------------------------------------------------
def check_integrity(ext: dict, today: date | None = None) -> tuple[dict, list]:
    today = today or date.today()
    issues, checks = [], []
    tol = POLICY["math_tolerance_abs"]

    required = {"vendor_name": "vendor name", "invoice_number": "invoice number",
                "invoice_date": "invoice date", "total": "invoice total"}
    missing = [label for k, label in required.items() if ext.get(k) in (None, "")]
    for label in missing:
        issues.append(issue("MISSING_" + label.upper().replace(" ", "_"), "HOLD",
                            f"The {label} is not on the invoice. The vendor must re-issue a complete invoice "
                            f"before it can be booked.", short=f"{label} missing"))
    checks.append({"check": "Required fields present", "ok": not missing,
                   "info": "all present" if not missing else "missing: " + ", ".join(missing)})

    items = ext.get("line_items") or []
    subtotal, tax, total = ext.get("subtotal"), ext.get("tax_amount") or 0.0, ext.get("total")

    # line arithmetic
    bad_lines = [l for l in items if None not in (l.get("quantity"), l.get("unit_price"), l.get("amount"))
                 and abs(l["quantity"] * l["unit_price"] - l["amount"]) > tol]
    for l in bad_lines:
        issues.append(issue("LINE_MATH", "REVIEW",
                            f"Line '{l['description']}': {l['quantity']:g} x {fmt(l['unit_price'])} "
                            f"= {fmt(l['quantity'] * l['unit_price'])}, but the invoice shows {fmt(l['amount'])}."))
    checks.append({"check": "Line qty x price = amount", "ok": not bad_lines,
                   "info": f"{len(items)} line(s) checked"})

    line_sum = round(sum(l.get("amount") or 0 for l in items), 2) if items else None
    if subtotal is None and line_sum is not None:
        subtotal = line_sum
        checks.append({"check": "Subtotal", "ok": True, "info": f"not printed; derived from lines = {fmt(line_sum)}"})
    elif line_sum is not None and subtotal is not None:
        ok = abs(line_sum - subtotal) <= tol
        checks.append({"check": "Lines sum to subtotal", "ok": ok, "info": f"{fmt(line_sum)} vs {fmt(subtotal)}"})
        if not ok:
            issues.append(issue("SUBTOTAL_MISMATCH", "HOLD",
                                f"Line items add up to {fmt(line_sum)} but the subtotal says {fmt(subtotal)}."))

    if subtotal is not None and total is not None:
        expected_total = round(subtotal + tax, 2)
        ok = abs(expected_total - total) <= tol
        info = f"{fmt(subtotal)} + tax {fmt(tax)} = {fmt(expected_total)}; printed {fmt(total)}"
        checks.append({"check": "Subtotal + tax = total", "ok": ok, "info": info})
        if not ok:
            diff = round(total - expected_total, 2)
            cause = ""
            if tax and abs(diff - tax) <= tol:
                cause = " The difference equals the tax amount exactly — tax appears to have been added twice."
            if ext.get("tax_inclusive_prices") and tax:
                cause += " The invoice also states prices already include tax, yet tax is charged on top."
            issues.append(issue("TOTAL_MISMATCH", "HOLD",
                                f"Printed total {fmt(total)} does not equal subtotal + tax "
                                f"({fmt(expected_total)}); off by {fmt(abs(diff))}.{cause}",
                                short=f"printed total is off by {fmt(abs(diff))}",
                                expected_total=expected_total, printed_total=total, difference=diff))
    elif ext.get("tax_inclusive_prices") and tax:
        issues.append(issue("TAX_INCLUSIVE_CONFLICT", "REVIEW",
                            "Invoice says prices include tax but also lists separate tax."))

    # dates
    d = parse_iso(ext.get("invoice_date"))
    if d:
        if d > today:
            issues.append(issue("FUTURE_DATED", "REVIEW", f"Invoice is dated in the future ({d})."))
        elif (today - d).days > POLICY["stale_invoice_days"]:
            issues.append(issue("STALE_INVOICE", "REVIEW",
                                f"Invoice is {(today - d).days} days old — check it wasn't already paid."))
        checks.append({"check": "Invoice date sane", "ok": d <= today, "info": str(d)})

    cur = (ext.get("currency") or "USD").upper()
    if cur != POLICY["base_currency"]:
        issues.append(issue("FOREIGN_CURRENCY", "REVIEW", f"Invoice is in {cur}; POs are in USD — FX review needed."))
    checks.append({"check": "Currency", "ok": cur == "USD", "info": cur})

    # extraction confidence
    conf = ext.get("field_confidence") or {}
    low = [k for k, v in conf.items() if ext.get(k) not in (None, "", []) and v < POLICY["min_field_confidence"]]
    if low:
        issues.append(issue("LOW_CONFIDENCE", "REVIEW",
                            "Low read-confidence on: " + ", ".join(low) + ". A human should confirm these values.",
                            fields=low))
    checks.append({"check": "Extraction confidence", "ok": not low,
                   "info": "all key fields >= " + str(POLICY["min_field_confidence"]) if not low else ", ".join(low)})

    normalized = {"subtotal": subtotal, "tax": tax, "total": total, "line_sum": line_sum}
    return {"checks": checks, "normalized": normalized}, issues


# ---------------------------------------------------------------------------
# Stage: vendor verification
# ---------------------------------------------------------------------------
def match_vendor(ext: dict) -> tuple[dict, list, dict | None]:
    issues = []
    name = ext.get("vendor_name")
    tax_id = (ext.get("vendor_tax_id") or "").strip()
    n = normalize_company(name)
    scored = []
    for v in data.vendors():
        best = max(fuzz.token_sort_ratio(n, normalize_company(c)) for c in [v["name"], *v["aliases"]])
        if tax_id and tax_id == v["tax_id"]:
            best = max(best, 100)
        scored.append((best, v))
    scored.sort(key=lambda t: -t[0])
    score, vendor = scored[0] if scored else (0, None)
    candidates = [{"vendor_id": v["vendor_id"], "name": v["name"], "score": round(s)} for s, v in scored[:3]]

    if score < 85:
        issues.append(issue("VENDOR_UNKNOWN", "REJECT",
                            f"'{name}' is not in the approved vendor master (closest: {vendor['name']}, "
                            f"{score:.0f}% similar). Payments only go to onboarded vendors.",
                            short=f"'{name}' is not an approved vendor", closest=candidates[0]))
        if ext.get("payment_urgency_language"):
            issues.append(issue("URGENCY_PRESSURE", "INFO",
                                "Invoice uses urgent payment pressure language — a common fraud signal."))
        return {"input_name": name, "matched": None, "candidates": candidates}, issues, None

    detail = {"input_name": name, "matched": {"vendor_id": vendor["vendor_id"], "name": vendor["name"],
                                              "status": vendor["status"], "score": round(score),
                                              "tax_id_match": bool(tax_id) and tax_id == vendor["tax_id"]},
              "candidates": candidates}
    if vendor["status"] != "approved":
        issues.append(issue("VENDOR_INACTIVE", "REJECT", f"{vendor['name']} is {vendor['status']} in the vendor master."))

    bank = ext.get("remit_bank_last4")
    bank_ok = not bank or bank == vendor["bank_last4"]
    detail["bank"] = {"invoice": bank, "master": vendor["bank_last4"], "ok": bank_ok}
    if not bank_ok or ext.get("bank_change_notice"):
        issues.append(issue("BANK_DETAILS_CHANGED", "HOLD",
                            f"Remit-to account ends in {bank or '?'} but the vendor master has ****{vendor['bank_last4']}"
                            + (" and the invoice announces new bank details" if ext.get("bank_change_notice") else "")
                            + ". Bank-change requests by invoice/email are the #1 AP fraud pattern — verify by phone "
                              f"using the number on file ({vendor['phone']}), not contact details on the invoice.",
                            short="remit-to bank account differs from the vendor master (possible payment fraud)"))
    return detail, issues, vendor


# ---------------------------------------------------------------------------
# Stage: duplicate detection
# ---------------------------------------------------------------------------
def check_duplicates(ext: dict, vendor: dict | None, totals: dict) -> tuple[dict, list]:
    issues = []
    norm = normalize_invoice_number(ext.get("invoice_number"))
    detail = {"invoice_number": ext.get("invoice_number"), "normalized": norm, "compared_against": 0, "matches": []}
    if not vendor:
        detail["note"] = "Vendor unknown — duplicate check limited to invoice number across all vendors."
    rows = db.ledger_rows(vendor_id=vendor["vendor_id"]) if vendor else db.ledger_rows()
    detail["compared_against"] = len(rows)
    inv_date = parse_iso(ext.get("invoice_date"))
    total = totals.get("total")

    for r in rows:
        if norm and r["invoice_number_norm"] == norm:
            exact = r["invoice_number"] == ext.get("invoice_number")
            detail["matches"].append({"type": "exact" if exact else "normalized", **_ledger_view(r)})
            how = "" if exact else (f" (printed as '{ext.get('invoice_number')}', "
                                    f"normalizes to the same number as '{r['invoice_number']}')")
            if r["status"] in ("paid", "approved"):
                issues.append(issue("DUPLICATE_PAID", "REJECT",
                                    f"Duplicate of invoice {r['invoice_number']} dated {r['invoice_date']}, "
                                    f"already {r['status']} for {fmt(r['total'])}{how}. Paying again would be a "
                                    f"double payment.", short=f"duplicate of invoice {r['invoice_number']}, already {r['status']}",
                                    original=r["invoice_number"]))
            elif r["status"] != "rejected":
                issues.append(issue("DUPLICATE_IN_PROCESS", "REVIEW",
                                    f"Invoice {r['invoice_number']} is already in the queue ({r['status']}){how}."))
        elif vendor and total is not None and r["total"] is not None and abs(r["total"] - total) <= 0.01:
            rd = parse_iso(r["invoice_date"])
            if inv_date and rd and abs((inv_date - rd).days) <= POLICY["fuzzy_duplicate_window_days"]:
                detail["matches"].append({"type": "same_amount_near_date", **_ledger_view(r)})
                issues.append(issue("SUSPECTED_DUPLICATE", "REVIEW",
                                    f"Same vendor and identical amount {fmt(total)} as invoice {r['invoice_number']} "
                                    f"({r['invoice_date']}), {abs((inv_date - rd).days)} days apart but a different "
                                    f"invoice number. Possibly a re-issued copy.",
                                    short="possible duplicate (same amount, nearby date)"))
    return detail, issues


def _ledger_view(r):
    return {k: r[k] for k in ("invoice_number", "invoice_date", "po_number", "total", "status")}


# ---------------------------------------------------------------------------
# Stage: PO matching (explicit or inferred)
# ---------------------------------------------------------------------------
def match_po(ext: dict, vendor: dict | None, totals: dict) -> tuple[dict, list, dict | None]:
    issues = []
    pos = data.purchase_orders()
    raw = ext.get("po_reference")
    m = re.search(r"PO[\s\-#:]*(\d{2})[\s\-]*(\d{4})", raw or "", re.I)
    ref = f"PO-{m.group(1)}-{m.group(2)}" if m else (raw.strip().upper() if raw else None)

    if ref:
        po = pos.get(ref)
        if not po:
            sev = "REJECT" if not vendor else "HOLD"
            issues.append(issue("PO_NOT_FOUND", sev, f"PO '{raw}' does not exist in the procurement system."))
            return {"method": "explicit", "reference": raw, "po": None}, issues, None
        if vendor and po["vendor_id"] != vendor["vendor_id"]:
            issues.append(issue("PO_VENDOR_MISMATCH", "HOLD",
                                f"{ref} belongs to a different vendor ({data.vendor_by_id(po['vendor_id'])['name']})."))
        if po["status"] != "open":
            issues.append(issue("PO_NOT_OPEN", "HOLD", f"{ref} is {po['status']}."))
        return {"method": "explicit", "reference": raw, "po": _po_view(po)}, issues, po

    # No PO printed — infer from vendor's open POs
    if not vendor:
        issues.append(issue("PO_MISSING", "HOLD", "No PO reference and vendor unknown — cannot match."))
        return {"method": "none", "po": None, "candidates": []}, issues, None

    subtotal = totals.get("subtotal") or 0
    inv_desc = " ".join(l["description"] for l in ext.get("line_items") or [])
    cands = []
    for po in pos.values():
        if po["vendor_id"] != vendor["vendor_id"] or po["status"] != "open":
            continue
        remaining = po["amount"] - db.po_billed(po["po_number"])
        amt_score = max(0.0, 1 - abs(remaining - subtotal) / max(remaining, subtotal, 1))
        desc_score = fuzz.token_set_ratio(inv_desc.lower(),
                                          " ".join(l["description"] for l in po["lines"]).lower()) / 100
        score = round(0.5 * amt_score + 0.5 * desc_score, 3)
        cands.append({"po_number": po["po_number"], "description": po["description"], "remaining": round(remaining, 2),
                      "amount_score": round(amt_score, 2), "description_score": round(desc_score, 2), "score": score})
    cands.sort(key=lambda c: -c["score"])
    detail = {"method": "inferred", "reference": None, "candidates": cands, "po": None}
    if not cands or cands[0]["score"] < POLICY["min_inferred_po_score"]:
        issues.append(issue("PO_MISSING", "HOLD",
                            f"No PO reference on the invoice and no open {vendor['name']} PO fits "
                            f"(best score {cands[0]['score'] if cands else 0:.2f}). Ask the vendor for the PO number."))
        return detail, issues, None

    best = cands[0]
    margin = best["score"] - (cands[1]["score"] if len(cands) > 1 else 0)
    po = pos[best["po_number"]]
    detail["po"] = _po_view(po)
    detail["confidence"] = best["score"]
    issues.append(issue("PO_INFERRED", "REVIEW",
                        f"No PO number printed. Best match is {best['po_number']} ({po['description']}) with "
                        f"score {best['score']:.2f}"
                        + (f", {margin:.2f} ahead of the next candidate" if len(cands) > 1 else "")
                        + ". Policy: an inferred PO is never auto-approved — a reviewer confirms the suggested match.",
                        short=f"no PO on invoice; suggested match {best['po_number']}",
                        suggested_po=best["po_number"]))
    return detail, issues, po


def _po_view(po):
    return {k: po[k] for k in ("po_number", "vendor_id", "po_date", "status", "amount", "buyer", "description")} | {
        "lines": po["lines"]}


# ---------------------------------------------------------------------------
# Stage: line-level + cumulative PO validation
# ---------------------------------------------------------------------------
def validate_against_po(ext: dict, po: dict | None, totals: dict, run_id: str) -> tuple[dict, list]:
    issues = []
    if not po:
        return {"skipped": "No PO to validate against."}, issues
    items = ext.get("line_items") or []
    subtotal = totals.get("subtotal") or 0.0
    lines_out, expected, unmatched = [], 0.0, []

    for it in items:
        best, best_s = None, 0
        for pl in po["lines"]:
            s = fuzz.token_set_ratio(it["description"].lower(), pl["description"].lower())
            if s > best_s:
                best, best_s = pl, s
        row = {"description": it["description"], "qty": it.get("quantity"), "unit_price": it.get("unit_price"),
               "amount": it.get("amount"), "po_line": None, "similarity": best_s}
        if best and best_s >= 70:
            qty = it.get("quantity") or 1
            unit = it.get("unit_price") if it.get("unit_price") is not None else (it.get("amount") or 0) / qty
            var = (unit - best["unit_price"]) / best["unit_price"] * 100 if best["unit_price"] else 0
            row.update({"po_line": best["line_no"], "po_description": best["description"],
                        "po_unit_price": best["unit_price"], "po_quantity": best["quantity"],
                        "price_variance_pct": round(var, 2), "price_ok": abs(var) <= POLICY["price_tolerance_pct"],
                        "qty_ok": qty <= best["quantity"]})
            expected += qty * best["unit_price"]
            if not row["price_ok"]:
                issues.append(issue("PRICE_VARIANCE", "REVIEW",
                                    f"'{it['description']}' billed at {fmt(unit)} vs PO price {fmt(best['unit_price'])} "
                                    f"({var:+.1f}%, tolerance ±{POLICY['price_tolerance_pct']}%)."))
            if not row["qty_ok"]:
                issues.append(issue("QTY_EXCEEDS_PO_LINE", "HOLD",
                                    f"'{it['description']}' quantity {qty:g} exceeds PO line quantity "
                                    f"{best['quantity']:g}."))
        else:
            unmatched.append(row)
        lines_out.append(row)

    # bundled invoice: one summary line covering a multi-line PO
    if unmatched and len(items) == 1 and len(po["lines"]) > 1:
        remaining = po["amount"] - db.po_billed(po["po_number"], exclude_run=run_id)
        if abs(subtotal - remaining) <= max(POLICY["amount_tolerance_abs"], remaining * POLICY["amount_tolerance_pct"] / 100):
            issues.append(issue("BUNDLED_LINES", "INFO",
                                "Invoice bills the PO as a single bundled line; amount matches remaining PO value."))
            expected, unmatched = subtotal, []

    for row in unmatched:
        issues.append(issue("UNPLANNED_LINE", "REVIEW",
                            f"'{row['description']}' ({fmt(row['amount'])}) is not on {po['po_number']}. "
                            f"Needs budget-owner ({po['buyer']}) approval or a PO amendment.",
                            short=f"{fmt(row['amount'])} charge not on {po['po_number']}"))

    variance = round(subtotal - expected, 2)
    var_pct = round(variance / expected * 100, 2) if expected else None
    within = expected and abs(variance) <= POLICY["amount_tolerance_abs"] and abs(var_pct) <= POLICY["amount_tolerance_pct"]
    if expected and not within and not unmatched:
        issues.append(issue("AMOUNT_VARIANCE", "REVIEW",
                            f"Invoice subtotal {fmt(subtotal)} vs expected {fmt(expected)} from PO prices "
                            f"({var_pct:+.1f}%)."))

    billed_before = db.po_billed(po["po_number"], exclude_run=run_id)
    billed_after = round(billed_before + subtotal, 2)
    limit = round(po["amount"] * (1 + POLICY["po_overrun_tolerance_pct"] / 100), 2)
    history = [_ledger_view(r) for r in db.ledger_rows(po_number=po["po_number"]) if r["run_id"] != run_id]
    if billed_after > limit:
        over = round(billed_after - po["amount"], 2)
        issues.append(issue("PO_OVERRUN", "HOLD",
                            f"{po['po_number']} is for {fmt(po['amount'])}. Already billed {fmt(billed_before)} across "
                            f"{len([h for h in history if h['status'] in db.COMMITTED])} invoice(s); this invoice "
                            f"brings it to {fmt(billed_after)} — {fmt(over)} over the PO "
                            f"({over / po['amount'] * 100:.1f}%, tolerance {POLICY['po_overrun_tolerance_pct']}%). "
                            f"Each invoice looks fine on its own; the over-billing only shows cumulatively.",
                            short=f"cumulative billing on {po['po_number']} would exceed the PO by {fmt(over)}",
                            over_by=over))

    if (totals.get("total") or 0) > POLICY["auto_approve_limit"]:
        issues.append(issue("ABOVE_AUTO_LIMIT", "REVIEW",
                            f"Total exceeds the auto-approval limit of {fmt(POLICY['auto_approve_limit'])}."))

    return {"lines": lines_out, "expected_subtotal": round(expected, 2), "invoice_subtotal": subtotal,
            "variance": variance, "variance_pct": var_pct,
            "po_balance": {"po_amount": po["amount"], "billed_before": billed_before, "this_invoice": subtotal,
                           "billed_after": billed_after, "remaining_after": round(po["amount"] - billed_after, 2),
                           "limit_with_tolerance": limit, "history": history}}, issues


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def decide(all_issues: list, ext: dict, vendor: dict | None, po: dict | None, po_detail: dict, totals: dict) -> dict:
    rank = max((SEVERITY_RANK[i["severity"]] for i in all_issues), default=0)
    decision = DECISION_FOR[rank]
    blocking = [i for i in all_issues if SEVERITY_RANK[i["severity"]] == rank and rank > 0]
    others = [i for i in all_issues if 0 < SEVERITY_RANK[i["severity"]] < rank]
    codes = {i["code"] for i in all_issues}
    total = totals.get("total")
    po_no = po["po_number"] if po else None

    inv_d = parse_iso(ext.get("invoice_date"))
    due = (inv_d + timedelta(days=terms_days(vendor["payment_terms"] if vendor else ext.get("due_terms")))
           ) if inv_d else None

    if decision == "APPROVE":
        headline = f"Approved — matches {po_no} within tolerance. {fmt(total)} scheduled for payment" + (
            f" on {due.isoformat()}." if due else ".")
        next_action = {"owner": "System", "action": f"Post to ledger against {po_no} and schedule payment"
                                                    + (f" for {due.isoformat()}" if due else "") + "."}
    elif decision == "REVIEW":
        headline = "Needs review — " + _lead(blocking)
        owner = po["buyer"] if po and codes & {"UNPLANNED_LINE", "PRICE_VARIANCE", "AMOUNT_VARIANCE"} else "AP reviewer"
        next_action = {"owner": owner, "action": "Confirm or reject: " + "; ".join(i["code"] for i in blocking) + "."}
        if "PO_INFERRED" in codes:
            next_action["action"] = (f"Confirm suggested PO {po_no} (one click approves with that PO), "
                                     "or reassign.")
    elif decision == "HOLD":
        headline = "On hold — " + _lead(blocking)
        if "BANK_DETAILS_CHANGED" in codes:
            next_action = {"owner": "AP lead", "action": f"Call {vendor['name']} on {vendor['phone']} (from vendor "
                                                         "master) to verify the bank change. Do NOT reply to the "
                                                         "invoice or use contact details printed on it."}
        elif "PO_OVERRUN" in codes:
            next_action = {"owner": po["buyer"] if po else "Procurement",
                           "action": "Either raise a PO change order for the extra quantity or ask the vendor for a "
                                     "credit note / corrected invoice. Vendor query drafted below."}
        else:
            next_action = {"owner": "AP clerk", "action": "Send the drafted query to the vendor and wait for a "
                                                         "corrected invoice."}
    else:  # REJECT
        headline = "Rejected — " + _lead(blocking)
        if codes & {"DUPLICATE_PAID"}:
            next_action = {"owner": "AP clerk", "action": "Do not pay. Notify vendor the invoice was already paid "
                                                         "(draft below)."}
        else:
            next_action = {"owner": "Procurement / Risk", "action": "Do not pay. Forward to procurement for vendor "
                                                                    "verification and possible fraud review."}

    email = None
    if vendor and (decision == "HOLD" and "BANK_DETAILS_CHANGED" not in codes
                   or decision == "REJECT" and "DUPLICATE_PAID" in codes):
        email = _vendor_email(decision, blocking + others, ext, vendor, po_no)

    passed = []
    if "VENDOR_UNKNOWN" not in codes and "VENDOR_INACTIVE" not in codes and vendor:
        passed.append(f"Vendor verified: {vendor['name']} ({vendor['vendor_id']})")
    if po and "PO_INFERRED" not in codes:
        passed.append(f"{po_no} found and open")
    if not codes & {"DUPLICATE_PAID", "DUPLICATE_IN_PROCESS", "SUSPECTED_DUPLICATE"}:
        passed.append("No duplicate found")
    if not codes & {"TOTAL_MISMATCH", "SUBTOTAL_MISMATCH", "LINE_MATH"}:
        passed.append("Invoice arithmetic reconciles")
    if po and not codes & {"PRICE_VARIANCE", "UNPLANNED_LINE", "AMOUNT_VARIANCE"}:
        passed.append("Lines match PO prices within tolerance")
    if po and "PO_OVERRUN" not in codes:
        passed.append("Within remaining PO balance")

    return {"decision": decision, "headline": headline, "reasons": blocking, "other_flags": others,
            "info": [i for i in all_issues if i["severity"] == "INFO"], "passed_checks": passed,
            "next_action": next_action, "vendor_email": email, "due_date": due.isoformat() if due else None}


def _lead(blocking):
    s = blocking[0]["short"].rstrip(".")
    return s + (f" (+{len(blocking) - 1} more)" if len(blocking) > 1 else "") + "."


def _lead(blocking):
    s = blocking[0]["short"].rstrip(".")
    return s + (f" (+{len(blocking) - 1} more)" if len(blocking) > 1 else "") + "."


def _vendor_email(decision, issues, ext, vendor, po_no):
    ref = ext.get("invoice_number") or f"your invoice dated {ext.get('invoice_date') or '(undated)'}"
    bullets = "\n".join(f"  - {i['message']}" for i in issues if i["severity"] in ("HOLD", "REJECT"))
    if decision == "REJECT":
        body = (f"Hello {vendor['name']} team,\n\nWe received invoice {ref} for {fmt(ext.get('total'))}. "
                f"Our records show this invoice has already been processed and paid:\n\n{bullets}\n\n"
                "No further payment will be made. If you believe this is a different invoice, please reply with "
                "supporting details.\n\nRegards,\nAccounts Payable — Meridian Health Partners")
        subject = f"Duplicate invoice {ref} — already paid"
    else:
        body = (f"Hello {vendor['name']} team,\n\nWe could not process {ref}"
                + (f" (PO {po_no})" if po_no else "") + f" for {fmt(ext.get('total'))} because:\n\n{bullets}\n\n"
                "Please send a corrected invoice (or credit note) addressing the points above and we will process "
                "it promptly.\n\nRegards,\nAccounts Payable — Meridian Health Partners")
        subject = f"Action needed: invoice {ref} on hold"
    return {"to": vendor["email"], "subject": subject, "body": body}
