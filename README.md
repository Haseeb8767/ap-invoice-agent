---
title: AP Invoice Agent
emoji: 🧾
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# AP Invoice Agent: from PDF to decision

**Case study PS-1 (Finance / AP).** This app takes a vendor invoice PDF and returns a reasoned decision:
**APPROVE / REVIEW / HOLD / REJECT**. Every stage is shown live in the UI, and a dashboard shows the history of all runs.

> Reviewing with an AI agent? See [AI_REVIEW.md](AI_REVIEW.md): requirement → evidence map, edge-case matrix, and how to verify.

## Design principle: Claude reads, rules decide

| Step | Who does it | Why |
|---|---|---|
| Read the messy PDF (any layout, scanned images) | **Claude** (`claude-opus-5-5`, structured JSON output, vision for scans) | Vendors all format invoices differently. Regex templates break on layouts they haven't seen. |
| Validate and decide | **Deterministic rules engine** (`app/rules.py`) | Payment decisions must be explainable, repeatable and auditable. The same invoice always gets the same result. |

Claude is told to **transcribe exactly what is printed**. It never fixes arithmetic and never guesses a PO number. Spotting those problems is the rules engine's job, so a wrong total is caught rather than silently corrected.

## Pipeline

| # | Stage | What happens |
|---|---|---|
| 1 | Ingest | Accepts the upload and fingerprints the file (SHA-256) |
| 2 | Classify | Checks for a text layer. Native PDFs go to Claude as a document; image-only scans are rendered to images and read with Claude vision |
| 3 | Extract | Claude fills a strict JSON schema: header fields, line items, tax, totals, remit-to bank, a confidence score per field, and notes |
| 4 | Completeness & arithmetic | Required fields present; qty × price = amount; lines add up to subtotal; subtotal + tax = total (and diagnoses double-counted tax); date sanity; currency; confidence threshold |
| 5 | Vendor | Fuzzy name match plus tax ID against the vendor master; vendor status; **bank account compared with the master** |
| 6 | Duplicates | Invoice-number normalisation (`APX/2026/388` = `APX-2026-0388`), plus same vendor and amount within 14 days |
| 7 | PO match | Uses the PO number if printed. Otherwise infers it from the vendor's open POs (50% amount fit + 50% description fit) |
| 8 | Validate | Each line matched to a PO line with a price-variance check; unplanned lines; **cumulative billing against the PO value** |
| 9 | Decide | The most severe finding wins, plus a headline, reasons, next action with an owner, and a drafted vendor email |

Stages don't stop at the first problem. Every issue is collected in one pass, so the vendor gets **one** complete query instead of a back-and-forth.

## Scenarios (in `samples/`)

| File | Scenario | Decision |
|---|---|---|
| 01 | **Happy path**: clean invoice, exact PO match, 8.25% tax | APPROVE |
| 02 | **Split PO**: second partial invoice that fits the remaining PO balance | APPROVE |
| 03 | **Edge: cumulative over-billing.** Each invoice looks fine alone, but in total they exceed the PO | HOLD |
| 04 | **Edge: scanned image with no PO number.** Needs vision, then PO inference (PO-26-1004 vs a near-identical Houston route) | REVIEW |
| 05 | **Edge: disguised duplicate.** `APX/2026/388` is the already-paid `APX-2026-0388` | REJECT |
| 06 | **Edge: missing invoice number and broken maths.** Prices say tax-inclusive, tax is added again, so the total is off by exactly the tax | HOLD |
| 07 | Extra: an unplanned $640 overage line not on the PO | REVIEW |
| 08 | Extra: remit-to bank account changed (payment-fraud pattern). Verify by phone, not by email | HOLD |
| 09 | Extra: unknown vendor, PO that doesn't exist, "wire today" pressure | REJECT |

Also try running sample 01 a second time: it's rejected as a duplicate of the invoice you just approved.

## Run it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
uvicorn app.main:app --port 8000         # open http://localhost:8000
```

* With no API key (or if Claude is unreachable), the app runs in **offline mode**: a regex parser for text PDFs and **local OCR (RapidOCR, free, CPU-only) for scanned images**. OCR confidence scores flow into the rules engine, so shaky reads go to a human. All 9 scenarios still produce the expected decision. The sidebar badge shows which mode is active.
* `python scripts/run_samples.py` runs all 9 scenarios headlessly and checks the expected decisions.
* `python scripts/generate_samples.py` regenerates the invoice PDFs.
* **Reset demo data** (sidebar) clears runs and restores the PO ledger.
* Optional environment variables: `CLAUDE_MODEL` (default `claude-opus-5-5`), `CLAUDE_EFFORT` (default `medium`), `STAGE_DELAY` (pause between stages in the demo, default 0.45s), and `CLAUDE_FALLBACKS=0` to turn off server-side refusal fallbacks.

**Deploy:** a `Dockerfile` is included (port 7860). It runs as-is on **Hugging Face Spaces** (Docker SDK; the YAML header at the top of this README configures it) or on **Render** (`render.yaml`, Docker runtime). `ANTHROPIC_API_KEY` is optional: add it as a secret to enable Claude extraction.

## Assumptions

* The buyer is "Meridian Health Partners". All POs are in USD and **PO amounts are pre-tax**, so invoice subtotals are compared with PO values and tax is checked separately.
* Only **approved** invoices (and seeded paid ones) use up PO budget. Invoices under review or on hold don't reserve budget.
* An **inferred** PO match is never auto-approved, however confident. A reviewer confirms it with one click.
* Bank-detail changes are always held and verified by phone **using the number in the vendor master**, never contact details printed on the invoice.
* All policy thresholds are in one place, `app/config.py` (`POLICY`), and are shown on the "POs & vendors" page.

## Code map

```
app/config.py    policy thresholds, model settings
app/extract.py   classification, Claude extraction (schema + prompt), offline parser, fallback routing
app/ocr.py       local OCR extractor for scans (RapidOCR + row grouping + faint-digit re-read)
app/rules.py     every validation rule and the decision logic
app/pipeline.py  stage orchestration, live events, human approve/reject
app/db.py        SQLite run history + invoice ledger
app/main.py      FastAPI: upload, SSE live stream, dashboard and reference APIs
static/          UI (vanilla JS, no build step)
docs/            architecture.mmd (system layers), decision-flow.mmd (how Claude + rules decide)
data/            vendor master, POs, PO lines, previously paid invoices
```
