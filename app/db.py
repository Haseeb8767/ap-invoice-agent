"""SQLite persistence: run history + invoice ledger (what has been billed per PO)."""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DATA_DIR, DB_PATH, RUNTIME_DIR, UPLOAD_DIR

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    source TEXT,
    mode TEXT,
    status TEXT NOT NULL,           -- running | done | error
    decision TEXT,                  -- APPROVE | REVIEW | HOLD | REJECT
    final_status TEXT,              -- decision after any human action
    vendor_name TEXT,
    vendor_id TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    po_number TEXT,
    total REAL,
    currency TEXT,
    headline TEXT,
    duration_ms INTEGER,
    stages_json TEXT,
    result_json TEXT,
    human_action_json TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id TEXT,
    invoice_number TEXT,
    invoice_number_norm TEXT,
    invoice_date TEXT,
    po_number TEXT,
    subtotal REAL,
    total REAL,
    status TEXT,                    -- paid | approved | pending_review | on_hold | rejected
    run_id TEXT,
    source TEXT,                    -- seed | pipeline
    created_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        if _conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0:
            _seed()
    return _conn


def _seed():
    from .rules import normalize_invoice_number  # local import avoids cycle

    with open(DATA_DIR / "seed_invoices.csv", newline="") as f:
        for r in csv.DictReader(f):
            _conn.execute(
                "INSERT INTO ledger (vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number,"
                " subtotal, total, status, run_id, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["vendor_id"], r["invoice_number"], normalize_invoice_number(r["invoice_number"]),
                 r["invoice_date"], r["po_number"], float(r["subtotal"]), float(r["total"]), r["status"],
                 None, "seed", now_iso()),
            )
    _conn.commit()


def reset():
    with _lock:
        c = conn()
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM ledger")
        c.commit()
        _seed()


# ---- runs -------------------------------------------------------------------
def create_run(run_id, filename, file_path, source, mode):
    with _lock:
        conn().execute(
            "INSERT INTO runs (id, created_at, filename, file_path, source, mode, status) VALUES (?,?,?,?,?,?,?)",
            (run_id, now_iso(), filename, str(file_path), source, mode, "running"),
        )
        conn().commit()


def finish_run(run_id, *, status, stages, result, duration_ms):
    s = result.get("summary", {}) if result else {}
    with _lock:
        conn().execute(
            """UPDATE runs SET finished_at=?, status=?, decision=?, final_status=?, vendor_name=?, vendor_id=?,
               invoice_number=?, invoice_date=?, po_number=?, total=?, currency=?, headline=?, duration_ms=?,
               stages_json=?, result_json=? WHERE id=?""",
            (now_iso(), status, s.get("decision"), s.get("decision"), s.get("vendor_name"), s.get("vendor_id"),
             s.get("invoice_number"), s.get("invoice_date"), s.get("po_number"), s.get("total"),
             s.get("currency"), s.get("headline"), duration_ms, json.dumps(stages, default=str),
             json.dumps(result, default=str), run_id),
        )
        conn().commit()


def set_human_action(run_id, final_status, action):
    with _lock:
        conn().execute("UPDATE runs SET final_status=?, human_action_json=? WHERE id=?",
                       (final_status, json.dumps(action), run_id))
        conn().commit()


def get_run(run_id):
    r = conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_dict(r) if r else None


def list_runs(limit=200):
    rows = conn().execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_run_dict(r, full=False) for r in rows]


def _run_dict(r, full=True):
    d = dict(r)
    for k in ("stages_json", "result_json", "human_action_json"):
        v = d.pop(k, None)
        if full:
            d[k.replace("_json", "")] = json.loads(v) if v else None
        elif k == "human_action_json":
            d["human_action"] = json.loads(v) if v else None
    return d


# ---- ledger -----------------------------------------------------------------
def ledger_rows(vendor_id=None, po_number=None):
    q, args = "SELECT * FROM ledger WHERE 1=1", []
    if vendor_id:
        q += " AND vendor_id=?"
        args.append(vendor_id)
    if po_number:
        q += " AND po_number=?"
        args.append(po_number)
    return [dict(r) for r in conn().execute(q + " ORDER BY id", args).fetchall()]


def add_ledger(vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number, subtotal, total,
               status, run_id):
    with _lock:
        conn().execute(
            "INSERT INTO ledger (vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number, subtotal,"
            " total, status, run_id, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number, subtotal, total, status,
             run_id, "pipeline", now_iso()),
        )
        conn().commit()


def update_ledger_status(run_id, status, po_number=None):
    with _lock:
        if po_number:
            conn().execute("UPDATE ledger SET status=?, po_number=? WHERE run_id=?", (status, po_number, run_id))
        else:
            conn().execute("UPDATE ledger SET status=? WHERE run_id=?", (status, run_id))
        conn().commit()


# Ledger statuses that consume PO budget
COMMITTED = ("paid", "approved")


def po_billed(po_number, exclude_run=None):
    rows = ledger_rows(po_number=po_number)
    return round(sum(r["subtotal"] or 0 for r in rows
                     if r["status"] in COMMITTED and (exclude_run is None or r["run_id"] != exclude_run)), 2)
