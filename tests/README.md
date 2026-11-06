# Tests: specs first, then code

The test suite is the **specification** of how the AP process must behave. Each test name is a
sentence about the business (`test_invoice_that_is_fine_alone_but_overbills_cumulatively_is_held`),
so the suite reads as a list of AP policies, and a failing test tells you which policy broke.

```bash
pip install -r requirements-dev.txt
python -m pytest                    # everything (~25 s, includes local OCR)
python -m pytest -m "not slow"      # fast loop while developing (~15 s)
python -m pytest tests/unit -q      # rules only (~4 s)
python -m pytest -k duplicate       # one topic
```

No API key, network or money is needed. Claude is replaced by a fake client that records the
request, so the LLM contract is still tested.

## Layout: a test pyramid

| Layer | Folder | What it pins down | Speed |
|---|---|---|---|
| Unit | `tests/unit/` | One rules-engine stage at a time: normalisation, integrity, vendor, duplicates, PO matching, PO validation, decision, OCR parsing helpers | ms |
| Integration | `tests/integration/` | The Claude request contract (model, JSON schema, guardrail prompt), refusal and truncation handling, fallback on a 401, the offline readers, pipeline orchestration, live events, human approve/reject, error paths | seconds |
| Acceptance | `tests/acceptance/` | Every demo PDF in `samples/` gets its expected decision **for the right reasons**, plus multi-invoice stories (split-PO over-billing, resubmitted duplicate) | seconds |
| API | `tests/api/` | The HTTP endpoints the UI uses: uploads, input validation, path traversal, SSE stream, stats | seconds |

Shared pieces:
- `tests/support.py`: `clean_invoice(**overrides)` builds a valid invoice, so each test states only
  what is special about its scenario.
- `tests/conftest.py`: fixtures. `run_extracted(invoice)` runs the **real** pipeline with an injected
  reader result; `run_sample(file)` runs a real PDF end to end; every test starts from a freshly
  seeded ledger.

Determinism: tests pin `AS_OF_DATE=2026-11-06`, so date rules (stale or future-dated invoices) don't
drift as the sample invoices age, and they use a throwaway database.

## Workflow: red → green → refactor

### Example: adding a new rule

Policy request: *"Invoices dated before their PO was raised are suspicious and need review."*

1. **Red: write the spec first.** In `tests/unit/test_integrity.py` (or a new file):
   ```python
   def test_invoice_dated_before_its_po_needs_review(invoice, po):
       inv = invoice(invoice_date="2026-09-01")          # PO-26-1001 was raised 2026-09-22
       _, issues = validate_against_po(inv, po("PO-26-1001"), {"subtotal": 5090, "total": 5509.93}, "r")
       assert "INVOICE_BEFORE_PO" in codes(issues)
   ```
   Run `python -m pytest -k before_its_po` and confirm it **fails for the right reason** (the code
   isn't there yet, not a typo in the test).
2. **Green:** add the smallest change to `app/rules.py` that makes it pass. Put any threshold in
   `POLICY` in `app/config.py`, never as a magic number in the rule.
3. **Refactor:** clean up while the whole suite stays green (`python -m pytest`).
4. If the rule changes a demo outcome, update `samples/manifest.json` and `MUST_FIND` in
   `tests/acceptance/test_scenarios.py`.

### Example: adding a new edge-case invoice

1. Add the PDF generator case to `scripts/generate_samples.py` and its entry (expected decision and
   why) to `samples/manifest.json`.
2. Add the file and the issue codes that must drive its decision to `MUST_FIND`.
   `test_every_sample_has_an_acceptance_spec` fails until you do, so no scenario goes unspecified.
3. Run it red, then implement until it's green.

### Rules of thumb (for humans and AI agents)

- **Never edit a test just to make it pass.** If a spec is wrong, say why in the commit message.
- Assert **why**, not only **what**: check the issue code and key detail (`over_by`, `suggested_po`),
  not just the APPROVE/HOLD label. A right decision for the wrong reason is a bug.
- The LLM is a transcriber. Tests in `test_extraction.py` pin the guardrail prompt and the schema;
  changing the prompt is a spec change and should be deliberate.
- Keep unit tests free of I/O. Use `run_extracted` instead of generating PDFs for rule scenarios.

## Proof the suite catches bugs

Injected bugs and the specs that caught them (the code was restored afterwards):

| Injected bug | Caught by |
|---|---|
| Duplicate check stops ignoring leading zeros | `test_reformatted_number_of_an_already_paid_invoice_is_rejected`, acceptance `05-edge_duplicate` |
| Held invoices start consuming PO budget | `test_only_approved_or_paid_invoices_consume_po_budget` |
| "New bank details" notice ignored when digits match | `test_bank_change_notice_is_held_even_if_account_digits_match` |
| "Do NOT correct arithmetic" removed from the LLM prompt | `test_system_prompt_keeps_the_llm_a_transcriber_not_a_decider` |
