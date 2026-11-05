# 5-minute demo script

**Before recording:**
1. Optional: set `ANTHROPIC_API_KEY` so the badge says **Claude**. Without it, the app uses the offline parser plus local OCR, and all 9 scenarios still work.
2. Click **Reset demo data**.
3. Open the Dashboard once so it's warm.
4. Run order matters: run 02 before 03 so the split-PO story builds.

---

**0:00–0:35 · The problem and the approach** (on the Process page)
> "AP teams open each invoice PDF, find the PO in a spreadsheet, check the numbers and decide. Vendors format invoices differently, some are scans, and the costly mistakes come from fatigue. My design splits the job in two. Claude reads the document, because layouts vary endlessly. A deterministic rules engine makes the decision, because a payment decision has to be explainable and the same every time. Claude is told to transcribe what's printed and never fix the maths, so errors get caught instead of hidden."

**0:35–1:40 · Happy path: card 01**
- Click **Clean invoice**. Talk over the stages as they tick: "classify finds a text layer, so the PDF goes to Claude; extraction returns fields with confidence scores." Expand **Extract fields** and show the confidence bars.
- "Vendor matched by name and tax ID, bank account matches the master, no duplicates, PO-26-1001 printed, each line matched to PO prices, within the PO balance."
- On the decision card: "Approved. It explains why, says who acts next, and schedules payment by the vendor's terms."

**1:40–2:40 · Edge case 1: split PO and cumulative over-billing (02 then 03)**
- Run 02: "a second partial invoice against a $48k blanket PO. It fits, so it's approved."
- Run 03: "this one looks perfect on its own: right price, right PO, the maths adds up. But expand validation." Show the balance bar going past the PO line. "The process tracks billing across invoices. That's how split invoicing over-bills without anyone noticing. Held, with the buyer named and a vendor email drafted."

**2:40–3:40 · Edge case 2: scanned invoice with no PO (04)**
- "No text layer, so the classifier routes it to Claude vision, or to local OCR if Claude isn't available." Show the extracted fields. In offline mode, point to the notes: "the quantity was faint, so the reader re-read that cell and reported low confidence. The rules engine sends low-confidence reads to a human rather than trusting them."
- Expand **PO match**: "no PO printed, so it scores the vendor's open POs on amount fit and route. Dallas→Denver wins over a similar Houston→Phoenix PO. But my policy is that an inferred match is never auto-approved, so it goes to review with the suggestion pre-filled." Click **Approve with PO-26-1004**. "The human decision is recorded and the PO ledger updates."

**3:40–4:30 · Edge case 3: disguised duplicate (05) and missing fields (06)**
- 05: "APX/2026/388 vs APX-2026-0388, already paid. An exact-match check misses this. I normalise invoice numbers, so it's rejected before a double payment."
- 06: "No invoice number, and the total is off by exactly the tax amount. The invoice says prices include tax, then adds tax again. The engine finds both problems in one pass and drafts one email."

**4:30–5:00 · Dashboard**
- "Every run: straight-through rate, open exceptions, value stopped. Click any run to replay its full audit trail. All thresholds live in one policy file, shown here. Next steps: email-inbox intake, three-way match with goods receipts, approval routing by amount."

**If time allows:** run 08 (bank change, held for a phone callback) or re-run 01 (instant duplicate rejection).
