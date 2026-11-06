"""Central configuration: paths, AP policy thresholds, model settings.

Every threshold the decision engine uses lives here so the policy is
auditable in one place (and easy to discuss with a finance controller).
"""
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", ROOT / "runtime"))
UPLOAD_DIR = RUNTIME_DIR / "uploads"
DB_PATH = RUNTIME_DIR / "ap.db"

# ---- Claude ---------------------------------------------------------------
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "medium")
# Server-side refusal fallbacks (on by default; set CLAUDE_FALLBACKS=0 to disable)
CLAUDE_FALLBACKS = os.getenv("CLAUDE_FALLBACKS", "1") == "1"


def extraction_mode() -> str:
    """'claude' when credentials are present, otherwise the offline parser."""
    forced = os.getenv("EXTRACTION_MODE")
    if forced in ("claude", "offline"):
        return forced
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "claude"
    return "offline"


# ---- AP policy ------------------------------------------------------------
POLICY = {
    # Line-level unit price may differ from PO price by at most this much
    "price_tolerance_pct": 2.0,
    # Invoice subtotal vs expected (qty x PO price): within pct AND abs cap
    "amount_tolerance_pct": 2.0,
    "amount_tolerance_abs": 250.00,
    # Cumulative billing may exceed PO value by at most this much
    "po_overrun_tolerance_pct": 2.0,
    # Arithmetic checks (rounding)
    "math_tolerance_abs": 0.05,
    # Extraction confidence below this on a key field -> human review
    "min_field_confidence": 0.80,
    # Inferred PO match needs at least this score to be suggested
    "min_inferred_po_score": 0.60,
    # Same vendor + same total within N days, different number -> suspected dup
    "fuzzy_duplicate_window_days": 14,
    # Invoice dated more than N days ago -> stale, review
    "stale_invoice_days": 120,
    # Auto-approval ceiling: anything above routes to review regardless
    "auto_approve_limit": 50000.00,
    "base_currency": "USD",
}

def business_date() -> date:
    """'Today' for date rules (stale / future-dated invoices).

    Defaults to the real date. Set AS_OF_DATE=YYYY-MM-DD to pin it, which keeps tests and demos
    deterministic as the sample invoices age.
    """
    pinned = os.getenv("AS_OF_DATE")
    return date.fromisoformat(pinned) if pinned else date.today()


# Pacing between stages so the live view is watchable in a demo (seconds).
STAGE_DELAY = float(os.getenv("STAGE_DELAY", "0.45"))
