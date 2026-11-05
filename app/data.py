"""Reference data: vendor master and purchase orders (the 'procurement system')."""
from __future__ import annotations

import csv
from functools import lru_cache

from .config import DATA_DIR


def _read(name):
    with open(DATA_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@lru_cache
def vendors() -> list[dict]:
    out = []
    for r in _read("vendors.csv"):
        r["aliases"] = [a for a in r["aliases"].split("|") if a]
        out.append(r)
    return out


@lru_cache
def purchase_orders() -> dict[str, dict]:
    lines: dict[str, list] = {}
    for l in _read("po_lines.csv"):
        lines.setdefault(l["po_number"], []).append({
            "line_no": int(l["line_no"]), "description": l["description"],
            "quantity": float(l["quantity"]), "unit_price": float(l["unit_price"]),
        })
    pos = {}
    for p in _read("purchase_orders.csv"):
        p["amount"] = float(p["amount"])
        p["lines"] = lines.get(p["po_number"], [])
        pos[p["po_number"]] = p
    return pos


def vendor_by_id(vid):
    return next((v for v in vendors() if v["vendor_id"] == vid), None)
