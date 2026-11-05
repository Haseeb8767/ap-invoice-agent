/* AP Invoice Agent — front end (no build step, no dependencies). */
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v === null || v === undefined || v === "" ? "—" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const toast = (msg) => { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 2600); };

const STAGES = [
  ["ingest", "Ingest document"], ["classify", "Classify document"], ["extract", "Extract fields"],
  ["integrity", "Completeness & arithmetic"], ["vendor", "Vendor verification"], ["duplicate", "Duplicate detection"],
  ["po_match", "Purchase order match"], ["validate", "Line & balance validation"], ["decide", "Decision"],
];
const ICONS = {
  pass: '<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg>',
  warn: "!", fail: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>', skipped: "–",
};
const DECISION_LABEL = { APPROVE: "APPROVED", REVIEW: "NEEDS REVIEW", HOLD: "ON HOLD", REJECT: "REJECTED" };

/* ============================== routing ============================== */
let currentLive = null;
function route() {
  const h = location.hash || "#/process";
  const [, view, id] = h.split("/");
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  document.querySelectorAll(".nav-item").forEach((a) => a.classList.toggle("active", a.dataset.view === (view === "run" ? "dashboard" : view)));
  if (view === "dashboard") { $("#view-dashboard").classList.remove("hidden"); loadDashboard(); }
  else if (view === "reference") { $("#view-reference").classList.remove("hidden"); loadReference(); }
  else if (view === "run" && id) { $("#view-run").classList.remove("hidden"); loadRunDetail(id); }
  else { $("#view-process").classList.remove("hidden"); }
}
window.addEventListener("hashchange", route);

/* ============================== health / samples ============================== */
async function loadHealth() {
  const h = await api("/api/health");
  const b = $("#mode-badge");
  b.classList.toggle("offline", h.mode !== "claude");
  b.classList.toggle("error", !!h.claude_error);
  b.innerHTML = h.mode === "claude"
    ? (h.claude_error
      ? `<span class="dot"></span>Claude <b>failing</b> — using offline parser + OCR<br><span style="font-size:11.5px">${esc(h.claude_error)}</span>`
      : `<span class="dot"></span>Extraction: <b>Claude</b><br><span class="mono">${esc(h.model)}</span>`)
    : `<span class="dot"></span>Extraction: <b>offline parser + local OCR</b><br>set ANTHROPIC_API_KEY for Claude`;
}

async function loadSamples() {
  const samples = await api("/api/samples");
  $("#samples").innerHTML = samples.map((s) => {
    const kind = s.scenario === "happy_path" ? ["happy", "Happy path"] : s.scenario.startsWith("edge") || s.scenario === "split_po" ? ["edge", "Edge case"] : ["extra", "Extra"];
    return `<button class="sample" data-file="${esc(s.file)}">
      <div class="row"><span class="tag ${kind[0]}">${kind[1]}</span><span class="status ${s.expected_decision}">${s.expected_decision}</span></div>
      <div class="t">${esc(s.title)}</div><div class="w">${esc(s.why)}</div></button>`;
  }).join("");
  document.querySelectorAll(".sample").forEach((b) => b.addEventListener("click", () => {
    const fd = new FormData(); fd.append("sample", b.dataset.file); startRun(fd);
  }));
}

/* ============================== start + live ============================== */
async function startRun(fd) {
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: fd });
    location.hash = "#/process";
    const area = $("#run-area");
    area.classList.remove("hidden");
    const run = await api(`/api/runs/${run_id}`);
    renderRunShell(area, run);
    area.scrollIntoView({ behavior: "smooth", block: "start" });
    streamRun(area, run_id);
  } catch (e) { toast("Could not start run: " + e.message); }
}

function renderRunShell(el, run) {
  el.innerHTML = `
    <div class="run-head">
      <div><div class="eyebrow">Run <span class="mono">${esc(run.id)}</span> · ${esc(run.mode === "claude" ? "Claude extraction" : "offline parser + OCR")}</div>
      <h2>${esc(run.filename)}</h2></div>
      <div class="run-meta"><span class="timer">${run.duration_ms ? (run.duration_ms / 1000).toFixed(1) + "s" : "0.0s"}</span>
      <span class="pill">${run.status === "running" ? "running" : "complete"}</span></div>
    </div>
    <div class="run-grid">
      <ol class="stages">${STAGES.map(([k, t]) => `
        <li class="stage pending" data-key="${k}">
          <div class="stage-row"><div class="icon"></div>
            <div><div class="stage-title">${esc(t)}</div><div class="stage-sum">Waiting…</div></div>
            <div class="stage-ms"></div></div>
          <div class="stage-body"></div></li>`).join("")}
      </ol>
      <div class="side-col">
        <div class="decision hidden"></div>
        <div class="card pdf-card"><div class="card-head"><span>Source document</span>
          <a target="_blank" class="link" href="/api/runs/${run.id}/file">Open ↗</a></div>
          <iframe title="Invoice PDF" src="/api/runs/${run.id}/file#view=FitH&toolbar=0"></iframe></div>
      </div>
    </div>`;
  el.querySelectorAll(".stage-row").forEach((r) => r.addEventListener("click", () => {
    const li = r.parentElement; if (!li.classList.contains("pending")) li.classList.toggle("open");
  }));
}

function streamRun(el, runId) {
  if (currentLive) currentLive.close();
  const t0 = performance.now();
  const timer = el.querySelector(".timer");
  const tick = setInterval(() => (timer.textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s"), 100);
  const es = new EventSource(`/api/runs/${runId}/events`);
  currentLive = es;
  es.onmessage = async (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "stage_start") setStage(el, { key: ev.key, status: "running", summary: "Running…" });
    else if (ev.type === "stage_done") setStage(el, ev, true);
    else if (ev.type === "run_done") {
      es.close(); clearInterval(tick);
      const run = await api(`/api/runs/${runId}`);
      timer.textContent = (run.duration_ms / 1000).toFixed(1) + "s";
      el.querySelector(".pill").textContent = "complete";
      renderDecision(el, run);
      loadHealth();
    }
  };
  es.onerror = () => { es.close(); clearInterval(tick); };
}

function setStage(el, st, autoOpen) {
  let li = el.querySelector(`.stage[data-key="${st.key}"]`);
  if (!li) { // e.g. pipeline error stage
    li = document.createElement("li"); li.className = "stage"; li.dataset.key = st.key;
    li.innerHTML = `<div class="stage-row"><div class="icon"></div><div><div class="stage-title">${esc(st.title)}</div><div class="stage-sum"></div></div><div class="stage-ms"></div></div><div class="stage-body"></div>`;
    li.querySelector(".stage-row").addEventListener("click", () => li.classList.toggle("open"));
    el.querySelector(".stages").appendChild(li);
  }
  li.className = "stage " + st.status;
  li.querySelector(".icon").innerHTML = ICONS[st.status] || "";
  li.querySelector(".stage-sum").textContent = st.summary || "";
  if (st.ms !== undefined) li.querySelector(".stage-ms").textContent = st.ms >= 1000 ? (st.ms / 1000).toFixed(1) + "s" : st.ms + "ms";
  if (st.details) {
    li.querySelector(".stage-body").innerHTML = renderIssues(st.issues) + (st.key === "decide" ? "<p class='muted'>See the decision panel →</p>" : renderDetails(st.key, st.details));
    if (autoOpen && (st.issues || []).some((i) => i.severity !== "INFO")) li.classList.add("open");
  }
}

function renderIssues(issues) {
  if (!issues || !issues.length) return "";
  return `<div class="issues">${issues.map((i) => `<div class="issue ${i.severity}"><span class="code">${esc(i.code)}</span><span>${esc(i.message)}</span></div>`).join("")}</div>`;
}

/* ============================== stage detail renderers ============================== */
const kv = (pairs) => `<dl class="kv">${pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
const val = (v) => (v === null || v === undefined || v === "" ? '<span class="null">not found</span>' : esc(v));

function renderDetails(key, d) {
  if (!d) return "";
  if (d.skipped) return `<p class="muted">${esc(d.skipped)}</p>`;
  switch (key) {
    case "ingest": return kv([["File", esc(d.filename)], ["Size", (d.bytes / 1024).toFixed(1) + " KB"], ["SHA-256", `<span class="mono">${esc(d.sha256)}…</span>`]]);
    case "classify": return kv([["Document type", d.kind === "native_text" ? "Native PDF (text layer)" : "<b>Scanned image</b> — no text layer"],
      ["Pages", d.pages], ["Text characters", d.text_chars], ["Embedded images", d.embedded_images],
      ["Route", d.route === "pdf_document" ? "PDF document → Claude / text parser" : "Page images → Claude vision / local OCR"]]);
    case "extract": return renderExtract(d);
    case "integrity": return `<ul class="checks">${d.checks.map((c) => `<li><span class="${c.ok ? "ok-c" : "bad-c"}">${c.ok ? "✓" : "✗"}</span><span>${esc(c.check)}</span><span class="muted">${esc(c.info)}</span></li>`).join("")}</ul>`;
    case "vendor": {
      const m = d.matched;
      let h = m ? kv([["Invoice says", esc(d.input_name)], ["Matched", `<b>${esc(m.name)}</b> (${esc(m.vendor_id)})`], ["Name similarity", m.score + "%"],
        ["Tax ID match", m.tax_id_match ? "yes" : "no / not printed"], ["Status", esc(m.status)],
        ["Bank (invoice / master)", d.bank ? `${val(d.bank.invoice)} / ****${esc(d.bank.master)} ${d.bank.ok ? '<span class="ok-c">✓</span>' : '<span class="bad-c">✗ changed</span>'}` : "—"]])
        : kv([["Invoice says", esc(d.input_name)], ["Matched", '<span class="null">no approved vendor</span>']]);
      h += `<div class="sub-h">Closest vendors in master</div><table class="mini"><tr><th>Vendor</th><th class="num">Similarity</th></tr>${d.candidates.map((c) => `<tr><td>${esc(c.name)}</td><td class="num">${c.score}%</td></tr>`).join("")}</table>`;
      return h;
    }
    case "duplicate": return kv([["Invoice # (printed)", val(d.invoice_number)], ["Normalized key", `<span class="mono">${val(d.normalized)}</span>`], ["Compared against", d.compared_against + " prior invoice(s) from this vendor"]])
      + (d.matches.length ? `<div class="sub-h">Matches</div><table class="mini"><tr><th>Type</th><th>Invoice</th><th>Date</th><th>PO</th><th class="num">Total</th><th>Status</th></tr>${d.matches.map((m) => `<tr><td>${esc(m.type)}</td><td class="mono">${esc(m.invoice_number)}</td><td>${esc(m.invoice_date)}</td><td>${esc(m.po_number)}</td><td class="num">${money(m.total)}</td><td>${esc(m.status)}</td></tr>`).join("")}</table>` : "");
    case "po_match": {
      let h = kv([["Method", d.method === "explicit" ? "PO number printed on invoice" : d.method === "inferred" ? "<b>Inferred</b> — no PO printed" : "none"],
        ["Reference", val(d.reference)], ...(d.po ? [["PO", `<b>${esc(d.po.po_number)}</b> — ${esc(d.po.description)}`], ["PO value", money(d.po.amount)], ["Buyer", esc(d.po.buyer)]] : [])]);
      if (d.candidates && d.candidates.length) h += `<div class="sub-h">Candidate POs (score = 50% amount fit + 50% description fit)</div><table class="mini"><tr><th>PO</th><th>Description</th><th class="num">Remaining</th><th class="num">Amount</th><th class="num">Desc.</th><th class="num">Score</th></tr>${d.candidates.map((c, i) => `<tr${i === 0 ? ' style="font-weight:600"' : ""}><td class="mono">${esc(c.po_number)}</td><td>${esc(c.description)}</td><td class="num">${money(c.remaining)}</td><td class="num">${c.amount_score.toFixed(2)}</td><td class="num">${c.description_score.toFixed(2)}</td><td class="num">${c.score.toFixed(2)}</td></tr>`).join("")}</table>`;
      return h;
    }
    case "validate": return renderValidate(d);
    default: return `<pre class="mono">${esc(JSON.stringify(d, null, 2))}</pre>`;
  }
}

function renderExtract(d) {
  const f = d.fields || {}, conf = f.field_confidence || {}, meta = d.meta || {};
  const row = (label, key, fmt = esc) => {
    const c = conf[key];
    const bar = c !== undefined ? `<span class="conf ${c < 0.8 ? "low" : ""}"><i style="width:${Math.round(c * 100)}%"></i></span>${Math.round(c * 100)}%` : "";
    return `<tr><td>${label}</td><td>${f[key] === null || f[key] === undefined || f[key] === "" ? '<span class="null">not on document</span>' : fmt(f[key])}</td><td class="num">${bar}</td></tr>`;
  };
  let h = `<table class="mini"><tr><th>Field</th><th>Value</th><th class="num">Confidence</th></tr>
    ${row("Vendor", "vendor_name")}${row("Vendor tax ID", "vendor_tax_id")}${row("Invoice #", "invoice_number")}${row("Invoice date", "invoice_date")}
    ${row("PO reference", "po_reference")}${row("Currency", "currency")}${row("Subtotal", "subtotal", money)}${row("Tax", "tax_amount", money)}
    ${row("Total", "total", money)}${row("Terms", "due_terms")}${row("Remit bank (last 4)", "remit_bank_last4")}
    <tr><td>Prices tax-inclusive</td><td>${f.tax_inclusive_prices ? "<b>yes</b>" : "no"}</td><td></td></tr>
    <tr><td>Bank-change notice</td><td>${f.bank_change_notice ? "<b class='bad-c'>yes</b>" : "no"}</td><td></td></tr></table>`;
  if ((f.line_items || []).length) h += `<div class="sub-h">Line items</div><table class="mini"><tr><th>Description</th><th class="num">Qty</th><th class="num">Unit</th><th class="num">Amount</th></tr>${f.line_items.map((l) => `<tr><td>${esc(l.description)}</td><td class="num">${l.quantity ?? "—"}</td><td class="num">${money(l.unit_price)}</td><td class="num">${money(l.amount)}</td></tr>`).join("")}</table>`;
  if ((f.extraction_notes || []).length) h += `<div class="sub-h">Extractor notes</div><ul class="notes">${f.extraction_notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`;
  h += `<div class="sub-h">Extractor</div>` + kv([["Engine", esc(meta.extractor)], ...(meta.model ? [["Model", `<span class="mono">${esc(meta.model)}</span>`], ["Tokens (in / out)", `${meta.input_tokens} / ${meta.output_tokens}`]] : []), ...(meta.engine ? [["OCR engine", esc(meta.engine)], ["Text regions read", meta.ocr_regions]] : [])]);
  return h;
}

function renderValidate(d) {
  let h = `<div class="sub-h">Line-by-line vs PO</div><table class="mini"><tr><th>Invoice line</th><th>PO line</th><th class="num">Qty</th><th class="num">Inv. price</th><th class="num">PO price</th><th class="num">Var.</th></tr>
    ${d.lines.map((l) => `<tr><td>${esc(l.description)}</td><td>${l.po_line ? esc(l.po_description) : '<span class="null">not on PO</span>'}</td><td class="num">${l.qty ?? "—"}</td><td class="num">${money(l.unit_price)}</td><td class="num">${l.po_line ? money(l.po_unit_price) : "—"}</td><td class="num">${l.po_line ? `<span class="${l.price_ok ? "ok-c" : "bad-c"}">${l.price_variance_pct > 0 ? "+" : ""}${l.price_variance_pct}%</span>` : "—"}</td></tr>`).join("")}</table>`;
  h += kv([["Expected (qty × PO price)", money(d.expected_subtotal)], ["Invoiced subtotal", money(d.invoice_subtotal)], ["Variance", `${money(d.variance)}${d.variance_pct !== null ? ` (${d.variance_pct > 0 ? "+" : ""}${d.variance_pct}%)` : ""}`]]);
  const b = d.po_balance;
  if (b) {
    const scale = Math.max(b.limit_with_tolerance, b.billed_after) * 1.04;
    const pct = (x) => (x / scale) * 100;
    const over = b.billed_after > b.limit_with_tolerance;
    h += `<div class="sub-h">Cumulative PO balance</div><div class="balance"><div class="bar">
      <div class="seg billed" style="width:${pct(b.billed_before)}%"></div>
      <div class="seg this ${over ? "over" : ""}" style="width:${pct(b.this_invoice)}%"></div>
      <div class="limit" style="left:${pct(b.po_amount)}%" title="PO value"></div></div>
      <div class="legend"><span><i style="background:#9fb3f5"></i>Previously billed ${money(b.billed_before)}</span>
      <span><i style="background:${over ? "var(--bad)" : "var(--accent)"}"></i>This invoice ${money(b.this_invoice)}</span>
      <span><i style="background:var(--ink)"></i>PO value ${money(b.po_amount)}</span>
      <span>Remaining after: <b class="${b.remaining_after < 0 ? "bad-c" : ""}">${money(b.remaining_after)}</b></span></div></div>`;
    if (b.history.length) h += `<table class="mini"><tr><th>Prior invoice</th><th>Date</th><th class="num">Total</th><th>Status</th></tr>${b.history.map((r) => `<tr><td class="mono">${esc(r.invoice_number)}</td><td>${esc(r.invoice_date)}</td><td class="num">${money(r.total)}</td><td>${esc(r.status)}</td></tr>`).join("")}</table>`;
  }
  return h;
}

/* ============================== decision card ============================== */
function renderDecision(el, run) {
  const box = el.querySelector(".decision");
  const res = run.result || {}, d = res.decision || {}, s = res.summary || {};
  const dec = s.decision || run.decision || "REVIEW";
  box.className = "decision " + (run.final_status || dec);
  const reasons = (d.reasons || []).concat(d.other_flags || []);
  let h = `<div class="band"><span class="badge">${DECISION_LABEL[dec] || dec}</span>
    <div><div style="font-weight:600">${esc(s.vendor_name || "Unknown vendor")}</div><div class="muted mono">${esc(s.invoice_number || "no invoice #")} · ${esc(s.po_number || "no PO")}</div></div>
    <div class="amount">${money(s.total)}</div></div><div class="body">
    <div class="headline">${esc(d.headline || s.headline || "")}</div>`;
  if (reasons.length) h += `<div class="sub-h">Why</div>${renderIssues(reasons)}`;
  if ((d.passed_checks || []).length) h += `<div class="sub-h">Checks passed</div><ul class="passed">${d.passed_checks.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`;
  if (d.next_action) h += `<div class="sub-h">Next action</div><div class="next"><b>${esc(d.next_action.owner)}:</b> ${esc(d.next_action.action)}</div>`;
  if (d.vendor_email) h += `<div class="sub-h">Drafted vendor email</div><div class="email"><div class="eh"><span><b>To:</b> ${esc(d.vendor_email.to)} — ${esc(d.vendor_email.subject)}</span><a class="link" href="#" data-copy>Copy</a></div><pre>${esc(d.vendor_email.body)}</pre></div>`;
  if (run.status === "error") {
    h += `<div class="sub-h">What to do</div><div class="next">Nothing was decided — the invoice could not be processed. Fix the cause above, then re-run.</div>
      <div class="actions" style="margin-top:8px"><button class="btn small" data-rerun>Re-run this invoice</button></div>`;
  } else if (run.human_action) {
    const a = run.human_action;
    h += `<div class="sub-h">Human decision</div><div class="human"><b>${a.action === "approve" ? "Approved" : "Rejected"}</b> by ${esc(a.by)} · ${new Date(a.at).toLocaleString()}${a.note ? ` — “${esc(a.note)}”` : ""}<br><span class="muted">Original agent decision: ${esc(a.previous)}</span></div>`;
  } else if (dec === "REVIEW" || dec === "HOLD") {
    h += `<div class="sub-h">Reviewer action</div><div class="actions"><input placeholder="Note (optional)" data-note>
      <button class="btn ok small" data-act="approve">Approve${s.po_number && s.po_method === "inferred" ? " with " + esc(s.po_number) : ""}</button>
      <button class="btn bad small" data-act="reject">Reject</button></div>`;
  }
  h += "</div>";
  box.innerHTML = h;
  box.classList.remove("hidden");
  box.querySelector("[data-rerun]")?.addEventListener("click", async () => {
    const { run_id } = await api(`/api/runs/${run.id}/rerun`, { method: "POST" });
    location.hash = "#/process";
    const area = $("#run-area"); area.classList.remove("hidden");
    renderRunShell(area, await api(`/api/runs/${run_id}`)); streamRun(area, run_id);
  });
  box.querySelector("[data-copy]")?.addEventListener("click", (e) => { e.preventDefault(); navigator.clipboard.writeText(`Subject: ${d.vendor_email.subject}\n\n${d.vendor_email.body}`); toast("Email copied"); });
  box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/runs/${run.id}/action`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: b.dataset.act, note: box.querySelector("[data-note]").value }) });
    toast(b.dataset.act === "approve" ? "Approved and posted to PO ledger" : "Rejected");
    renderDecision(el, await api(`/api/runs/${run.id}`));
  }));
}

/* ============================== run detail (replay) ============================== */
async function loadRunDetail(id) {
  const el = $("#view-run");
  el.innerHTML = `<header class="page-head"><div><a class="link" href="#/dashboard">← Dashboard</a></div></header><div class="run-area"></div>`;
  const run = await api(`/api/runs/${id}`);
  const area = el.querySelector(".run-area");
  renderRunShell(area, run);
  if (run.status === "running") return streamRun(area, id);
  (run.stages || []).forEach((st) => setStage(area, st, true));
  renderDecision(area, run);
}

/* ============================== dashboard ============================== */
let filter = "ALL";
async function loadDashboard() {
  const [stats, runs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
  const by = stats.by_status, tot = stats.total_runs || 1;
  const colors = { APPROVE: "var(--ok)", REVIEW: "#f5a524", HOLD: "var(--hold)", REJECT: "var(--bad)" };
  $("#kpis").innerHTML = [
    ["Invoices processed", stats.total_runs, `<div class="dist">${["APPROVE", "REVIEW", "HOLD", "REJECT"].map((k) => `<span style="width:${((by[k] || 0) / tot) * 100}%;background:${colors[k]}" title="${k}: ${by[k] || 0}"></span>`).join("")}</div>`],
    ["Straight-through rate", stats.auto_approval_rate + "%", "auto-approved, no human touch"],
    ["Open exceptions", stats.open_exceptions, "review + hold queue"],
    ["Value approved", money(stats.value_approved), "ready to pay"],
    ["Value stopped", money(stats.value_blocked), "held or rejected"],
    ["Avg. processing", stats.avg_seconds + "s", "per invoice, end to end"],
  ].map(([l, v, s]) => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("");

  const counts = { ALL: runs.length };
  runs.forEach((r) => { const k = r.status === "running" ? "RUNNING" : r.final_status || r.decision; counts[k] = (counts[k] || 0) + 1; });
  $("#filters").innerHTML = ["ALL", "APPROVE", "REVIEW", "HOLD", "REJECT"].map((k) => `<button class="chip ${filter === k ? "on" : ""}" data-f="${k}">${k === "ALL" ? "All" : DECISION_LABEL[k].toLowerCase()} · ${counts[k] || 0}</button>`).join("");
  document.querySelectorAll("#filters .chip").forEach((c) => c.addEventListener("click", () => { filter = c.dataset.f; loadDashboard(); }));

  const rows = runs.filter((r) => filter === "ALL" || (r.final_status || r.decision) === filter);
  $("#runs-table").innerHTML = rows.length ? `<tr><th>When</th><th>Status</th><th>Vendor</th><th>Invoice #</th><th>PO</th><th class="num">Total</th><th>Outcome</th><th class="num">Time</th></tr>` +
    rows.map((r) => {
      const st = r.status === "running" ? "RUNNING" : r.final_status || r.decision || "REVIEW";
      const over = r.final_status && r.final_status !== r.decision ? `<span class="overridden">agent: ${esc(r.decision)} → human</span>` : "";
      return `<tr class="click" onclick="location.hash='#/run/${r.id}'"><td>${new Date(r.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
        <td><span class="status ${st}">${DECISION_LABEL[st] || st}</span>${over}</td><td>${esc(r.vendor_name || "—")}</td><td class="mono">${esc(r.invoice_number || "—")}</td>
        <td class="mono">${esc(r.po_number || "—")}</td><td class="num">${money(r.total)}</td><td class="headline-cell">${esc(r.headline || r.filename)}</td>
        <td class="num">${r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : ""}</td></tr>`;
    }).join("") : `<tr><td class="empty">No runs yet — process an invoice to see it here.</td></tr>`;
  if (runs.some((r) => r.status === "running") && location.hash.startsWith("#/dashboard")) setTimeout(loadDashboard, 2000);
}

/* ============================== reference ============================== */
async function loadReference() {
  const ref = await api("/api/reference");
  $("#po-table").innerHTML = `<tr><th>PO</th><th>Vendor</th><th>Description</th><th>Buyer</th><th class="num">PO value</th><th>Billed</th><th class="num">Remaining</th><th>Invoices</th></tr>` +
    ref.purchase_orders.map((p) => {
      const pct = Math.min(100, (p.billed / p.amount) * 100);
      return `<tr><td class="mono">${esc(p.po_number)}</td><td>${esc(p.vendor)}</td><td>${esc(p.description)}</td><td>${esc(p.buyer)}</td><td class="num">${money(p.amount)}</td>
      <td><span class="pbar"><i class="${p.billed > p.amount ? "full" : ""}" style="width:${pct}%"></i></span>${money(p.billed)}</td><td class="num">${money(p.remaining)}</td>
      <td class="mono" style="font-size:12px">${p.invoices.map((i) => `${esc(i.invoice_number || "(no #)")} <span class="muted">${esc(i.status)}</span>`).join("<br>") || "—"}</td></tr>`;
    }).join("");
  $("#vendor-table").innerHTML = `<tr><th>ID</th><th>Vendor</th><th>Status</th><th>Tax ID</th><th>Bank</th><th>Terms</th></tr>` +
    ref.vendors.map((v) => `<tr><td class="mono">${esc(v.vendor_id)}</td><td>${esc(v.name)}</td><td><span class="status ${v.status === "approved" ? "APPROVE" : "REJECT"}">${esc(v.status)}</span></td><td class="mono">${esc(v.tax_id)}</td><td class="mono">****${esc(v.bank_last4)}</td><td>${esc(v.payment_terms)}</td></tr>`).join("");
  const p = ref.policy;
  $("#policy").innerHTML = `<div class="policy">${kv([
    ["Unit price tolerance", "±" + p.price_tolerance_pct + "%"], ["Invoice vs expected amount", `±${p.amount_tolerance_pct}% and ≤ ${money(p.amount_tolerance_abs)}`],
    ["Cumulative PO overrun allowed", p.po_overrun_tolerance_pct + "%"], ["Min. extraction confidence", p.min_field_confidence],
    ["Min. inferred-PO score", p.min_inferred_po_score], ["Fuzzy duplicate window", p.fuzzy_duplicate_window_days + " days"],
    ["Stale invoice after", p.stale_invoice_days + " days"], ["Auto-approve ceiling", money(p.auto_approve_limit)], ["Currency", p.base_currency]])}
    <p class="muted" style="margin-top:12px;font-size:12.5px">Decision = most severe finding: REJECT › HOLD › REVIEW › APPROVE. Inferred POs and bank-detail changes are never auto-approved.</p></div>`;
}

/* ============================== upload + init ============================== */
const dz = $("#dropzone");
$("#file-input").addEventListener("change", (e) => { if (e.target.files[0]) { const fd = new FormData(); fd.append("file", e.target.files[0]); startRun(fd); e.target.value = ""; } });
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) { const fd = new FormData(); fd.append("file", f); startRun(fd); } });
$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Clear all runs and restore the PO ledger to its starting state?")) return;
  await api("/api/reset", { method: "POST" }); $("#run-area").classList.add("hidden"); toast("Demo data reset"); route();
});

loadHealth(); loadSamples(); route();
