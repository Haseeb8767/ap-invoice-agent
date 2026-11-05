"""Generate realistic test invoice PDFs for the AP demo.

Each vendor gets its own visual layout and its own label vocabulary
("Invoice No" vs "Bill #", "PO Number" vs "Order Ref" ...), because real
vendor invoices never agree on format. One invoice is rasterised into an
image-only PDF to simulate a scanned document.

Run:  python scripts/generate_samples.py
"""
from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples"
W, H = LETTER


def money(x: float) -> str:
    return f"${x:,.2f}"


# --------------------------------------------------------------------------
# Layout helpers
# --------------------------------------------------------------------------
BOLD = {"Helvetica": "Helvetica-Bold", "Times-Roman": "Times-Bold", "Courier": "Courier-Bold"}


def draw_table(c, x, y, cols, rows, widths, header_fill, font="Helvetica", size=9.5, row_h=18):
    c.setFillColor(header_fill)
    c.rect(x, y - row_h + 4, sum(widths), row_h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont(BOLD[font], size)
    cx = x
    for i, col in enumerate(cols):
        if i == 0:
            c.drawString(cx + 6, y - 9, col)
        else:
            c.drawRightString(cx + widths[i] - 6, y - 9, col)
        cx += widths[i]
    y -= row_h
    c.setFillColor(colors.black)
    c.setFont(font, size)
    for r in rows:
        cx = x
        for i, val in enumerate(r):
            if i == 0:
                c.drawString(cx + 6, y - 9, str(val))
            else:
                c.drawRightString(cx + widths[i] - 6, y - 9, str(val))
            cx += widths[i]
        c.setStrokeColor(colors.HexColor("#DDDDDD"))
        c.line(x, y - row_h + 4, x + sum(widths), y - row_h + 4)
        y -= row_h
    return y


def totals_block(c, x_label, x_val, y, items, bold_last=True, size=10):
    for i, (label, val) in enumerate(items):
        last = i == len(items) - 1
        c.setFont("Helvetica-Bold" if (last and bold_last) else "Helvetica", size + (1 if last else 0))
        c.drawRightString(x_label, y, label)
        c.drawRightString(x_val, y, val)
        y -= 16
    return y


def line_rows(lines):
    return [[d, f"{q:,}", money(p), money(q * p)] for d, q, p in lines]


# --------------------------------------------------------------------------
# Layout A — Northwind (classic, blue header bar)
# --------------------------------------------------------------------------
def layout_northwind(path, inv_no, date_str, po, lines, tax_rate, bank_note=None, remit_last4="4471"):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle(f"Invoice {inv_no}")
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, H - 60, "Northwind Office Supplies Inc.")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 76, "2100 Harbor Blvd, Oakland, CA 94607  |  Tax ID 94-3812207")
    c.drawString(50, H - 88, "ar@northwind-supplies.com  |  (415) 555-0142")
    c.setFillColor(colors.HexColor("#1F4E99"))
    c.setFont("Helvetica-Bold", 26)
    c.drawRightString(W - 50, H - 64, "INVOICE")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    meta = [("Invoice No:", inv_no), ("Invoice Date:", date_str), ("PO Number:", po), ("Terms:", "Net 30")]
    y = H - 120
    for k, v in meta:
        c.drawRightString(W - 150, y, k)
        c.drawString(W - 140, y, v)
        y -= 14
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, H - 120, "Bill To")
    c.setFont("Helvetica", 10)
    for i, t in enumerate(["Meridian Health Partners", "Accounts Payable", "500 Market St, Suite 1200", "San Francisco, CA 94105"]):
        c.drawString(50, H - 134 - i * 13, t)

    sub = round(sum(q * p for _, q, p in lines), 2)
    tax = round(sub * tax_rate, 2)
    y = draw_table(c, 50, H - 210, ["Description", "Qty", "Unit Price", "Amount"], line_rows(lines),
                   [270, 60, 90, 92], colors.HexColor("#1F4E99"))
    y -= 14
    totals_block(c, W - 150, W - 52, y, [("Subtotal", money(sub)),
                                          (f"Sales Tax ({tax_rate*100:.2f}%)", money(tax)),
                                          ("Total Due", money(sub + tax))])
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, 130, "Remit To")
    c.setFont("Helvetica", 9)
    if bank_note:
        c.setFillColor(colors.HexColor("#B00020"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(50, 116, bank_note)
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 9)
        c.drawString(50, 102, f"First Coastal Bank  |  Routing 121000358  |  Account ending {remit_last4}")
    else:
        c.drawString(50, 116, f"Pacific Western Bank  |  Routing 122238420  |  Account ending {remit_last4}")
    c.drawString(50, 70, "Thank you for your business. Please reference the invoice number with your payment.")
    c.save()
    return sub, tax


# --------------------------------------------------------------------------
# Layout B — Blue Ridge (industrial, dark banner, meta box)
# --------------------------------------------------------------------------
def layout_blueridge(path, inv_no, date_str, po, lines, ship_note):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle(f"Invoice {inv_no}")
    c.setFillColor(colors.HexColor("#263238"))
    c.rect(0, H - 95, W, 95, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 50, "BLUE RIDGE INDUSTRIAL PARTS LLC")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 68, "88 Foundry Rd, Asheville, NC 28801  -  EIN 58-2290114  -  billing@blueridgeparts.com")
    c.setFillColor(colors.black)
    c.setStrokeColor(colors.HexColor("#263238"))
    c.rect(W - 230, H - 210, 190, 92, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(W - 222, H - 136, f"INVOICE # {inv_no}")
    c.setFont("Helvetica", 9.5)
    c.drawString(W - 222, H - 154, f"Date {date_str}")
    c.drawString(W - 222, H - 170, f"Customer PO {po}")
    c.drawString(W - 222, H - 186, "Terms Net 45")
    c.drawString(W - 222, H - 202, "Ship Via: Ground")
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(40, H - 130, "SOLD TO:")
    c.setFont("Helvetica", 9.5)
    for i, t in enumerate(["Meridian Health Partners", "Facilities & Engineering", "500 Market St, Suite 1200", "San Francisco, CA 94105"]):
        c.drawString(40, H - 144 - i * 12, t)
    sub = round(sum(q * p for _, q, p in lines), 2)
    y = draw_table(c, 40, H - 240, ["Item / Description", "Qty Shipped", "Unit", "Ext. Price"], line_rows(lines),
                   [260, 90, 85, 97], colors.HexColor("#455A64"))
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(40, y - 6, ship_note)
    y -= 30
    totals_block(c, W - 140, W - 45, y, [("Net Amount", money(sub)), ("Tax (resale exempt)", money(0)),
                                         ("Invoice Total", money(sub))])
    c.setFont("Helvetica", 8.5)
    c.drawString(40, 80, "Remit: Blue Ridge Industrial Parts LLC, First Citizens Bank, account ending 9032")
    c.save()
    return sub, 0.0


# --------------------------------------------------------------------------
# Layout C — Apex Cloud (modern SaaS, "Bill #" and "Order Ref")
# --------------------------------------------------------------------------
def layout_apex(path, bill_no, date_str, order_ref, lines, period):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle(f"Bill {bill_no}")
    c.setFillColor(colors.HexColor("#6C2BD9"))
    c.circle(62, H - 62, 14, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(84, H - 68, "apex cloud")
    c.setFillColor(colors.HexColor("#555555"))
    c.setFont("Helvetica", 9)
    c.drawRightString(W - 50, H - 52, "Apex Cloud Services Inc.")
    c.drawRightString(W - 50, H - 64, "600 Congress Ave, Austin, TX 78701")
    c.drawRightString(W - 50, H - 76, "invoices@apexcloud.io  |  Tax ID 47-1188325")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(50, H - 130, "Bill")
    c.setFont("Helvetica", 10)
    rows = [("Bill #", bill_no), ("Issued", date_str), ("Order Ref", order_ref), ("Service period", period), ("Due", "Net 30")]
    y = H - 155
    for k, v in rows:
        c.setFillColor(colors.HexColor("#777777"))
        c.drawString(50, y, k)
        c.setFillColor(colors.black)
        c.drawString(150, y, v)
        y -= 15
    c.drawString(360, H - 155, "Customer: Meridian Health Partners")
    c.drawString(360, H - 170, "Account ID: MHP-00417")
    sub = round(sum(q * p for _, q, p in lines), 2)
    y = draw_table(c, 50, H - 260, ["Service", "Qty", "Rate", "Amount"], line_rows(lines),
                   [280, 50, 90, 92], colors.HexColor("#6C2BD9"))
    y -= 14
    totals_block(c, W - 150, W - 52, y, [("Subtotal", money(sub)), ("Tax", money(0)), ("Amount Due (USD)", money(sub))])
    c.setFont("Helvetica", 8.5)
    c.setFillColor(colors.HexColor("#777777"))
    c.drawString(50, 80, "Pay by ACH to Apex Cloud Services Inc., Silicon Valley Bank, account ending 6610.")
    c.save()
    return sub, 0.0


# --------------------------------------------------------------------------
# Layout D — Quantum Print (minimal, missing invoice number, bad total)
# --------------------------------------------------------------------------
def layout_quantum(path, date_str, po_line, lines, tax_rate, printed_total):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle("Quantum Print invoice")
    c.setFont("Times-Bold", 22)
    c.drawString(50, H - 60, "Quantum Print & Design LLC")
    c.setFont("Times-Roman", 10)
    c.drawString(50, H - 76, "1450 Wazee St, Denver, CO 80202  -  accounts@quantumprint.co  -  Tax ID 36-4472019")
    c.setFont("Times-Bold", 14)
    c.drawString(50, H - 115, "INVOICE")
    c.setFont("Times-Roman", 11)
    c.drawString(50, H - 135, f"Date: {date_str}")
    c.drawString(50, H - 150, po_line)
    c.drawString(50, H - 165, "Customer: Meridian Health Partners - Marketing Dept.")
    sub = round(sum(q * p for _, q, p in lines), 2)
    tax = round(sub * tax_rate, 2)
    y = draw_table(c, 50, H - 200, ["Item", "Qty", "Each", "Line Total"], line_rows(lines),
                   [270, 60, 90, 92], colors.HexColor("#00897B"), font="Times-Roman", size=10.5)
    c.setFont("Times-Italic", 9.5)
    c.drawString(50, y - 8, "Note: all unit prices include 8% Colorado sales tax.")
    y -= 34
    c.setFont("Times-Roman", 11)
    for label, val, bold in [("Subtotal", money(sub), False), (f"Sales Tax {tax_rate*100:.0f}%", money(tax), False),
                             ("TOTAL", money(printed_total), True)]:
        c.setFont("Times-Bold" if bold else "Times-Roman", 12 if bold else 11)
        c.drawRightString(W - 150, y, label)
        c.drawRightString(W - 52, y, val)
        y -= 17
    c.setFont("Times-Roman", 9)
    c.drawString(50, 80, "Payment by check or ACH (account ending 7754). Thank you!")
    c.save()
    return sub, tax


# --------------------------------------------------------------------------
# Layout E — Summit Logistics (freight bill, later rasterised as a "scan")
# --------------------------------------------------------------------------
def layout_summit(path, pro_no, date_str, lines):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Courier-Bold", 18)
    c.drawString(50, H - 60, "SUMMIT LOGISTICS CO.")
    c.setFont("Courier", 9.5)
    c.drawString(50, H - 76, "4400 Irving Blvd, Dallas TX 75247   Tel (214) 555-0123")
    c.drawString(50, H - 88, "Federal Tax ID 75-3301876")
    c.setFont("Courier-Bold", 14)
    c.drawRightString(W - 50, H - 60, "FREIGHT INVOICE")
    c.setFont("Courier", 10)
    c.drawRightString(W - 50, H - 80, f"Invoice No: {pro_no}")
    c.drawRightString(W - 50, H - 94, f"Invoice Date: {date_str}")
    c.drawRightString(W - 50, H - 108, "Terms: Net 15")
    c.drawString(50, H - 130, "Bill To: Meridian Health Partners, 500 Market St, San Francisco CA")
    c.drawString(50, H - 145, "Shipper: Meridian Distribution Ctr, Dallas TX   Consignee: Meridian Denver Clinic")
    c.drawString(50, H - 160, "BOL # 5583012     Pickup 10/16/2026     Delivered 10/19/2026     Wt 6,420 lb")
    sub = round(sum(q * p for _, q, p in lines), 2)
    y = draw_table(c, 50, H - 190, ["Description", "Qty", "Rate", "Charge"], line_rows(lines),
                   [270, 60, 90, 92], colors.HexColor("#333333"), font="Courier", size=9.5)
    y -= 14
    totals_block(c, W - 150, W - 52, y, [("Subtotal", money(sub)), ("Tax", money(0)), ("Amount Due", money(sub))])
    c.setFont("Courier", 9)
    c.drawString(50, 80, "Remit to Summit Logistics Co., Comerica Bank, account ending 2285.")
    c.save()
    return sub, 0.0


def rasterise_as_scan(pdf_path: Path, dpi=110, angle=0.9):
    """Turn a clean PDF into an image-only PDF that looks scanned."""
    src = fitz.open(pdf_path)
    pix = src[0].get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    img = img.rotate(angle, expand=False, fillcolor=255, resample=Image.BICUBIC)
    rnd = random.Random(7)
    px = img.load()
    for _ in range(int(img.width * img.height * 0.004)):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, y] = rnd.choice([150, 180, 200])
    img = img.filter(ImageFilter.GaussianBlur(0.5))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    src.close()
    out = fitz.open()
    page = out.new_page(width=W, height=H)
    page.insert_image(page.rect, stream=buf.getvalue())
    out.save(pdf_path)
    out.close()


# --------------------------------------------------------------------------
# Layout F — Shadowline (unknown vendor)
# --------------------------------------------------------------------------
def layout_shadowline(path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, H - 60, "Shadowline Trading Ltd")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 76, "Suite 9, 14 Harbour Rd, Wan Chai, Hong Kong  |  accounts@shadowline-trading.net")
    c.setFont("Helvetica-Bold", 20)
    c.drawRightString(W - 50, H - 60, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    for i, (k, v) in enumerate([("Invoice Number", "SLT-0091"), ("Invoice Date", "November 1, 2026"),
                                ("PO Number", "PO-26-1009"), ("Terms", "Due on receipt")]):
        c.drawRightString(W - 150, H - 100 - i * 14, k + ":")
        c.drawString(W - 140, H - 100 - i * 14, v)
    c.drawString(50, H - 110, "Bill To: Meridian Health Partners")
    lines = [("Strategic sourcing advisory services - October", 1, 7500.00)]
    y = draw_table(c, 50, H - 180, ["Description", "Qty", "Unit Price", "Amount"], line_rows(lines),
                   [270, 60, 90, 92], colors.HexColor("#424242"))
    y -= 14
    totals_block(c, W - 150, W - 52, y, [("Subtotal", money(7500)), ("Tax", money(0)), ("Total Due", money(7500))])
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, 100, "URGENT: Please wire payment today to avoid late fees.")
    c.setFont("Helvetica", 9)
    c.drawString(50, 86, "Wire to: Global Asia Bank, SWIFT GABKHKHH, account ending 5521")
    c.save()
    return 7500.0, 0.0


# --------------------------------------------------------------------------
def main():
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*.pdf"):
        f.unlink()
    manifest = []

    def add(fname, title, scenario, expected, why):
        manifest.append({"file": fname, "title": title, "scenario": scenario,
                         "expected_decision": expected, "why": why})

    # 1. Happy path
    layout_northwind(OUT / "01_happy_northwind_INV-10421.pdf", "INV-10421", "Oct 28, 2026", "PO-26-1001",
                     [("Copy paper letter size 10-ream case", 40, 42.50),
                      ("Toner cartridge HP 26X black", 12, 138.00),
                      ("Ergonomic mesh office chair", 6, 289.00)], 0.0825)
    add("01_happy_northwind_INV-10421.pdf", "Clean invoice, exact PO match", "happy_path", "APPROVE",
        "Text PDF, explicit PO, every line matches PO price and quantity, maths and tax reconcile.")

    # 2. Split PO — second partial invoice (fits remaining balance)
    layout_blueridge(OUT / "02_split_po_blueridge_BR-77931.pdf", "BR-77931", "10/21/2026", "PO-26-1002",
                     [("Ball bearing 6205-2RS sealed", 1000, 12.00)],
                     "Partial shipment 2 of blanket order. Backorder balance to follow.")
    add("02_split_po_blueridge_BR-77931.pdf", "Split PO — partial invoice #2", "split_po", "APPROVE",
        "PO-26-1002 already has $30,000 invoiced. This $12,000 partial fits inside the $48,000 PO.")

    # 3. Split PO — over-billing invoice
    layout_blueridge(OUT / "03_split_po_overbill_blueridge_BR-78002.pdf", "BR-78002", "11/02/2026", "PO-26-1002",
                     [("Ball bearing 6205-2RS sealed", 1800, 12.00)],
                     "Final shipment of blanket order.")
    add("03_split_po_overbill_blueridge_BR-78002.pdf", "Split PO — cumulative over-billing", "edge_overbill", "HOLD",
        "Each invoice looks fine alone, but cumulative billing on PO-26-1002 now exceeds the PO value.")

    # 4. Scanned, no PO reference
    p4 = OUT / "04_scanned_no_po_summit_SL-2291.pdf"
    layout_summit(p4, "SL-2291", "10/24/2026", [("LTL freight Dallas TX to Denver CO", 1, 3450.00)])
    rasterise_as_scan(p4)
    add(p4.name, "Scanned image, no PO number", "edge_scanned_no_po", "REVIEW",
        "Image-only PDF (needs vision). No PO printed — the process must infer the PO from vendor, amount and route.")

    # 5. Disguised duplicate
    layout_apex(OUT / "05_duplicate_apex_APX-2026-388.pdf", "APX/2026/388", "30 Sep 2026", "PO-26-1003",
                [("Enterprise platform subscription (monthly)", 1, 9800.00),
                 ("Premium support plan (monthly)", 1, 1450.00)], "Sep 1 - Sep 30, 2026")
    add("05_duplicate_apex_APX-2026-388.pdf", "Disguised duplicate", "edge_duplicate", "REJECT",
        "'APX/2026/388' is the already-paid 'APX-2026-0388' with different formatting. Exact-match checks miss it.")

    # 6. Missing invoice number + broken maths
    layout_quantum(OUT / "06_missing_fields_quantum.pdf", "2026-10-27", "Re: PO-26-1005",
                   [("Retractable trade show banner 8ft", 4, 375.00),
                    ("Tri-fold brochures (box of 500)", 10, 185.00)], 0.08, printed_total=3886.00)
    add("06_missing_fields_quantum.pdf", "Missing invoice # and total that doesn't add up", "edge_missing_math", "HOLD",
        "No invoice number. Prices say tax-inclusive yet tax is added again, and the printed total matches neither.")

    # 7. Unplanned line item / price variance
    layout_apex(OUT / "07_price_variance_apex_APX-2026-0412.pdf", "APX-2026-0412", "31 Oct 2026", "PO-26-1003",
                [("Enterprise platform subscription (monthly)", 1, 9800.00),
                 ("Premium support plan (monthly)", 1, 1450.00),
                 ("API overage - 1.2M calls above plan", 1, 640.00)], "Oct 1 - Oct 31, 2026")
    add("07_price_variance_apex_APX-2026-0412.pdf", "Unplanned charge above PO", "extra_variance", "REVIEW",
        "Two lines match the PO; an extra $640 overage line is not on the PO (5.7% over expected).")

    # 8. Bank detail change
    layout_northwind(OUT / "08_bank_change_northwind_INV-10488.pdf", "INV-10488", "Oct 30, 2026", "PO-26-1006",
                     [("27-inch 4K monitor", 20, 219.00)], 0.0825,
                     bank_note="IMPORTANT: Our bank details have changed. Please update your records.",
                     remit_last4="8823")
    add("08_bank_change_northwind_INV-10488.pdf", "Remit-to bank account changed", "extra_bank_change", "HOLD",
        "Everything matches except the bank account differs from the vendor master — classic payment-fraud pattern.")

    # 9. Unknown vendor
    layout_shadowline(OUT / "09_unknown_vendor_shadowline.pdf")
    add("09_unknown_vendor_shadowline.pdf", "Vendor not in master file", "extra_unknown_vendor", "REJECT",
        "Vendor is not approved, PO number does not exist, urgent wire request.")

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest)} invoices to {OUT}")


if __name__ == "__main__":
    main()
