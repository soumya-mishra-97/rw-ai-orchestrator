/* Robert Walters AI-Orchestrator UI (vanilla JS, no build step).
 *
 * Flow: requirement → POST /api/orchestrate → follow GET /api/runs/{id}/stream (Server-Sent
 * Events: one "view" event per change). If streaming is unavailable, it falls back to polling
 * GET /api/runs/{id}/view?since=<version>, which answers 204 while nothing has changed.
 * Everything about the pipeline (agents, gates, rubric, models, examples) comes from /api/config.
 *
 * Model output is untrusted: the DOM is built with el()/fill() and textContent only.
 */
"use strict";

const POLL_MS = 1000;
const TONE = {
  pending: "waiting", queued: "running", running: "running", retrying: "retry",
  awaiting_review: "review", blocked_for_human: "review", passed: "passed", completed: "passed",
  complete: "passed", accepted_by_human: "human", aborted: "failed", failed: "failed", rejected: "failed",
  pass: "passed", retry: "retry", escalate: "review", accept_as_is: "human",
  retry_with_guidance: "retry", abort: "failed", success: "passed", warning: "retry", danger: "failed",
};
const STATUS_TEXT = {
  pending: "Waiting", running: "Running", retrying: "Retrying", awaiting_review: "Needs review",
  passed: "Passed", accepted_by_human: "Accepted by human", completed: "Completed",
  aborted: "Aborted", failed: "Failed", queued: "Queued", complete: "Complete",
  blocked_for_human: "Waiting for reviewer", rejected: "Rejected", pass: "PASS", retry: "RETRY",
  escalate: "ESCALATE", accept_as_is: "ACCEPTED", retry_with_guidance: "RETRY (human)", abort: "ABORTED",
};
const SOURCE_TEXT = { claude: "Claude (live)", example: "Curated example", simulator: "Simulated (no model)" };
const GLYPH = { passed: "✓", human: "✓", retry: "↻", review: "!", failed: "✕" };

const state = {
  config: null, runId: null, view: null, version: null, timer: null, follow: null,
  tab: null, attempt: {}, openFiles: new Set(),
};

// ------------------------------------------------------------------ helpers
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  node.append(...nodes(children));
  return node;
}
function nodes(children) {
  return children.flat(Infinity)
    .filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c))));
}
function fill(node, ...children) {
  node.replaceChildren(...nodes(children));
  return node;
}
const $ = (id) => document.getElementById(id);
const tone = (status) => TONE[status] || "waiting";
const badge = (status, text) => el("span", { class: `badge ${tone(status)}` }, text || STATUS_TEXT[status] || status);
const setBadge = (id, status, text, cls) =>
  $(id).replaceWith(Object.assign(cls ? el("span", { class: `badge ${cls}` }, text) : badge(status, text), { id }));
const usd = (n) => `$${(n || 0).toFixed(4)}`;
const pct = (x) => (x === null ? "—" : `${Math.round(x * 100)}%`);
const list = (items) => el("ul", {}, (items || []).map((i) => el("li", {}, i)));
const time = (ts) => new Date(ts).toLocaleTimeString([], { hour12: false });
const agent = (persona) => state.config.agents.find((a) => a.persona === persona);
const compact = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

function duration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}
function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* storage blocked: the preference is simply not remembered */ }
  return null;
}
function apiKey() {
  try { return sessionStorage.getItem("sdlc-api-key") || ""; } catch { return ""; }
}
function authHeaders(extra = {}) {
  const key = apiKey();
  return key ? { ...extra, "X-API-Key": key } : extra;
}
async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: authHeaders({ "Content-Type": "application/json", ...(options.headers || {}) }) });
  let body = null;
  if (res.status !== 204) {
    try { body = await res.json(); } catch { /* empty body */ }
  }
  return { ok: res.ok, status: res.status, body };
}

// ------------------------------------------------------------------ boot
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const toggle = $("theme-toggle");
  toggle.textContent = theme === "dark" ? "Light theme" : "Dark theme";
  toggle.setAttribute("aria-pressed", String(theme === "light"));
}

async function boot() {
  const params = new URLSearchParams(location.search);
  const requested = params.get("theme");
  if (requested === "light" || requested === "dark") storage("sdlc-theme", requested);
  applyTheme(storage("sdlc-theme") || "dark");
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    storage("sdlc-theme", next);
    applyTheme(next);
  });

  const res = await api("/api/config");
  if (!res.ok) {
    fill($("status-bar"), el("span", { class: "dot failed" }), el("b", {}, "Server unreachable"),
      el("span", { class: "reason" }, `(${res.status}). Check that the server is running.`));
    return;
  }
  state.config = res.body;
  renderStatusBar(state.config);
  renderGuide(state.config);

  const config = state.config;
  const demo = config.mode === "offline";
  $("pipeline").textContent = `${config.agents.filter((a) => a.persona !== "evaluator").map((a) => a.name).join(" → ")}, `
    + "with an Evaluator gate after every handoff.";
  $("scores-sub").textContent = `Five rubric dimensions per handoff (1–5). Pass rule: ${config.pass_rule}.`;
  const box = $("examples");
  box.hidden = false;
  box.append(el("span", { class: "muted" }, demo ? "Try a curated example:" : "Examples:"));
  for (const ex of config.examples) {
    box.append(el("button", {
      class: "chip", type: "button", title: ex.highlights,
      onclick: () => { $("requirement").value = ex.requirement; $("requirement").focus(); },
    }, ex.title));
  }

  if (config.auth_required) {
    $("apikey-wrap").hidden = false;
    $("apikey").value = apiKey();
    $("apikey").addEventListener("change", (e) => {
      try { sessionStorage.setItem("sdlc-api-key", e.target.value); } catch { /* blocked */ }
    });
  }
  $("submit").addEventListener("click", submit);
  $("requirement").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
  });
  document.querySelectorAll(".nav-item[data-view]").forEach((b) =>
    b.addEventListener("click", () => showView(b.dataset.view)));
  setInterval(tick, 1000);

  if (params.get("run")) openRun(params.get("run"));
  else if (params.get("view")) showView(params.get("view"));
}

function renderStatusBar(config) {
  const demo = config.mode === "offline";
  const guideLink = el("a", { href: "#g-llm", onclick: (e) => { e.preventDefault(); showView("guide", "g-llm"); } }, "How modes work");
  fill($("status-bar"), demo
    ? [el("span", { class: "dot retry" }), el("b", {}, "Demo Mode"), el("span", { class: "sep" }, "·"),
      "Local simulator active", el("span", { class: "sep" }, "·"), "No API cost",
      el("span", { class: "reason" }, config.mode_reason), guideLink]
    : [el("span", { class: "dot passed" }), el("b", {}, "Live Mode"), el("span", { class: "sep" }, "·"),
      "Claude connected", el("span", { class: "sep" }, "·"), `${config.tier1_model.name} + ${config.tier2_model.name}`,
      el("span", { class: "reason" }, config.mode_reason), guideLink]);
}

function showView(name, anchor) {
  document.querySelectorAll(".nav-item[data-view]").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  for (const view of ["workflow", "history", "guide"]) $(`view-${view}`).hidden = view !== name;
  if (name === "history") loadHistory();
  if (anchor) $(anchor).scrollIntoView({ behavior: "smooth", block: "start" });
  else window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ submit → orchestrator
function setSubmitting(busy) {
  $("submit").disabled = busy;
  $("submit").querySelector(".spinner").hidden = !busy;
  $("submit-icon").hidden = busy;
  $("submit-label").textContent = busy ? "Generating Blueprint…" : "Generate Intelligent SDLC Blueprint";
}

async function submit() {
  const requirement = $("requirement").value.trim();
  const box = $("validation");
  box.hidden = true;
  if (!requirement) {
    box.hidden = false;
    fill(box, "Enter a feature requirement first, e.g. “Build a Leave Management System”.");
    return;
  }
  setSubmitting(true);
  renderPending(requirement);
  const res = await api("/api/orchestrate", { method: "POST", body: JSON.stringify({ requirement }) });
  setSubmitting(false);

  if (res.status === 202) {
    openRun(res.body.run_id);
    return;
  }
  $("run").hidden = true;
  box.hidden = false;
  const reasons = res.body && res.body.assessment ? res.body.assessment.reasons : null;
  const detail = res.body && res.body.detail;
  fill(box, reasons
    ? [el("strong", {}, "The Master Orchestrator rejected this requirement."), list(reasons)]
    : `Request failed (${res.status}): ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
}

function openRun(runId) {
  stopFollowing();
  Object.assign(state, { runId, view: null, version: null, tab: null, attempt: {} });
  state.openFiles.clear();
  history.replaceState(null, "", `?run=${encodeURIComponent(runId)}`);
  showView("workflow");
  $("run").hidden = false;
  follow(runId);
}

// ------------------------------------------------------------------ live updates
function isSettled(v) {
  if (!v) return false;
  if (v.status === "blocked_for_human") return Boolean(v.pending_review);
  return ["complete", "aborted", "failed", "rejected"].includes(v.status);
}

function apply(runId, view) {
  if (runId !== state.runId) return;
  state.view = view;
  state.version = view.version;
  render(view);
}

function stopFollowing() {
  clearTimeout(state.timer);
  if (state.follow) state.follow.abort();
  state.follow = null;
}

async function follow(runId) {
  stopFollowing();
  const controller = new AbortController();
  state.follow = controller;
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/stream`, { headers: authHeaders(), signal: controller.signal });
    if (!res.ok || !res.body) throw new Error(`stream unavailable (${res.status})`);
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const event = (block.match(/^event: (.*)$/m) || [])[1];
        const data = block.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
        if (event === "view") apply(runId, JSON.parse(data));
      }
    }
  } catch {
    if (controller.signal.aborted) return;
  }
  if (state.runId === runId && !isSettled(state.view)) poll();
}

async function poll() {
  clearTimeout(state.timer);
  const runId = state.runId;
  const since = state.version ? `?since=${encodeURIComponent(state.version)}` : "";
  const res = await api(`/api/runs/${encodeURIComponent(runId)}/view${since}`);
  if (res.status === 200) apply(runId, res.body);
  if (runId === state.runId && !isSettled(state.view)) state.timer = setTimeout(poll, POLL_MS);
}

// ------------------------------------------------------------------ render
function render(v) {
  $("run-details").hidden = false;
  $("run-title").textContent = `${v.title} · ${v.run_id}`;
  setBadge("run-status", v.status);
  setBadge("run-source", null, SOURCE_TEXT[v.source] || "—", "outline");
  renderProgress(v);
  renderOrchestrator(v);
  renderAgents(v);
  renderReview(v);
  renderFinal(v);
  renderMetrics(v);
  renderDecisions(v);
  renderOutputs(v);
  renderScores(v);
  renderTimeline(v);
  tick();
}

function renderPending(requirement) {
  stopFollowing();
  state.runId = null;
  state.view = null;
  $("run").hidden = false;
  $("run-details").hidden = true;
  $("run-title").textContent = requirement.length > 90 ? `${requirement.slice(0, 90)}…` : requirement;
  setBadge("run-status", "queued", "Planning");
  setBadge("run-source", null, "—", "outline");
  renderProgress(null);
}

/** When a stage started: its first logged call, or else when the previous stage finished. */
function stageStart(v, stage) {
  const i = v.stages.indexOf(stage);
  return stage.started_at || (i > 0 ? v.stages[i - 1].finished_at : v.started_at) || v.started_at;
}

function currentAttempt(stage) {
  const last = stage.attempts[stage.attempts.length - 1];
  return !last ? 1 : last.evaluation ? last.attempt + 1 : last.attempt;
}

function stepNodes(v) {
  const steps = [];
  const orch = !v ? { tone: "running", detail: "Planning…" }
    : v.status === "rejected" ? { tone: "failed", detail: "Rejected" } : { tone: "passed", detail: "Planned" };
  steps.push({ name: "Orchestrator", ...orch });
  for (const s of v ? v.stages : state.config.agents.filter((a) => a.persona !== "evaluator").map((a) => ({ persona: a.persona, status: "pending", attempts: [] }))) {
    const t = tone(s.status);
    const detail = {
      waiting: "Waiting", passed: s.latest_score != null ? `Passed · ${s.latest_score.toFixed(2)}` : "Done",
      human: "Accepted by human", review: "Needs review", failed: STATUS_TEXT[s.status],
      running: `Running · attempt ${currentAttempt(s)}`, retry: `Retry · attempt ${currentAttempt(s)}`,
    }[t];
    steps.push({ name: agent(s.persona).name, tone: t, detail });
  }
  const final = !v ? "waiting" : v.status === "complete" ? "passed" : ["failed", "aborted"].includes(v.status) ? "failed" : "waiting";
  steps.push({ name: "Final Report", tone: final, detail: { passed: "Ready", failed: "Stopped", waiting: "Waiting" }[final] });
  return steps;
}

function renderProgress(v) {
  const steps = stepNodes(v);
  fill($("stepper"), steps.map((s, i) => el("li", { class: `step ${s.tone}` },
    el("span", { class: "node", "aria-hidden": "true" },
      s.tone === "running" ? el("span", { class: "spinner" }) : GLYPH[s.tone] || String(i + 1)),
    el("span", { class: "step-name" }, s.name),
    el("span", { class: "step-state" }, s.detail))));
  const done = steps.filter((s) => ["passed", "human"].includes(s.tone)).length;
  $("progress-fill").style.width = `${Math.round((done / steps.length) * 100)}%`;
  renderActivity(v);
}

function renderActivity(v) {
  const box = $("activity");
  if (!v) return fill(box, el("span", { class: "spinner" }), "Master Orchestrator is validating the requirement and planning the workflow…");
  const active = v.stages.find((s) => ["running", "retrying"].includes(s.status));
  if (v.status === "queued") return fill(box, el("span", { class: "spinner" }), "Workflow planned. Starting the Product Manager…");
  if (active) {
    const name = agent(active.persona).name;
    const what = active.status === "retrying" ? "is revising its output with the evaluator's feedback" : "is working";
    const since = stageStart(v, active);
    return fill(box, el("span", { class: "spinner" }), `${name} ${what} · attempt ${currentAttempt(active)}`,
      since ? [" · ", el("span", { "data-since": since })] : null);
  }
  if (v.status === "running") return fill(box, el("span", { class: "spinner" }), "Evaluator is scoring the handoff…");
  if (v.pending_review) {
    const label = (state.config.handoffs.find((h) => h.id === v.pending_review.handoff) || {}).label;
    return fill(box, badge("awaiting_review"), `Paused at ${label}: a human reviewer needs to decide (see the review panel below).`);
  }
  if (v.final_result) {
    const took = v.started_at && v.finished_at ? ` in ${duration(Date.parse(v.finished_at) - Date.parse(v.started_at))}` : "";
    return fill(box, badge(v.status), `${v.final_result.headline}${took}.`);
  }
  return fill(box, badge(v.status), STATUS_TEXT[v.status] || v.status);
}

function tick() {
  const v = state.view;
  const now = Date.now();
  if (v && v.started_at) {
    const end = v.finished_at ? Date.parse(v.finished_at) : now;
    $("run-clock").textContent = `${v.finished_at ? "Finished in" : "Running for"} ${duration(end - Date.parse(v.started_at))}`;
  } else {
    $("run-clock").textContent = "";
  }
  document.querySelectorAll("[data-since]").forEach((node) => {
    node.textContent = duration(now - Date.parse(node.dataset.since));
  });
}

function renderOrchestrator(v) {
  const checks = (v.assessment && v.assessment.checks) || [];
  fill($("checks"), checks.map((c) => el("li", {},
    el("span", { class: c.passed ? "ok" : "bad", "aria-label": c.passed ? "passed" : "failed" }, c.passed ? "✓" : "✗"),
    el("span", {}, c.detail))));
  const clarifications = (v.assessment && v.assessment.clarifications) || [];
  if (clarifications.length) {
    $("checks").append(el("li", {}, el("span", {}, `Questions for the PM: ${clarifications.join(" · ")}`)));
  }
  const steps = (v.plan && v.plan.steps) || [];
  fill($("plan"), steps.map((s) => el("li", { title: s.responsibility },
    el("b", {}, s.agent), ` (${s.model}): ${s.receives} → `, el("b", {}, s.produces),
    s.gate_label ? el("div", { class: "gate" }, `then Evaluator gate ${s.gate_label}`) : null)));
  const standIn = { simulator: "the local simulator", example: "a curated example" }[v.source];
  $("plan-note").textContent = v.plan
    ? `Gates are scored on ${v.plan.evaluator_model} and re-scored on ${v.plan.evaluator_confirmation_model} `
      + `when borderline. Pass rule: ${v.plan.pass_rule}. Up to ${v.plan.max_attempts_per_handoff} attempts `
      + "per handoff, then a human reviews."
      + (standIn ? ` These are the models live mode routes to; in this run ${standIn} produced every output.` : "")
    : "";
}

function agentCard({ persona, status, log, foot, onView, since }) {
  const a = agent(persona);
  const logList = el("ul", { class: "agent-log" }, log.length
    ? log.map((line) => el("li", {
      class: /: PASS/.test(line) ? "pass" : /: RETRY/.test(line) ? "retry" : /ESCALATE|Blocked/.test(line) ? "escalate" : "",
    }, line))
    : [el("li", { class: "empty" }, "Waiting for upstream stages…")]);
  const card = el("article", {
    class: `agent${["running", "retrying", "awaiting_review"].includes(status) ? " current" : ""}`,
    "aria-label": `${a.name}: ${STATUS_TEXT[status] || status}`,
  },
  el("div", { class: "agent-head" },
    el("div", { class: "avatar", "aria-hidden": "true" }, a.short),
    el("div", { class: "agent-title" }, el("b", {}, a.name), el("span", {}, a.description)),
    badge(status)),
  logList,
  el("div", { class: "agent-foot" },
    el("span", {}, foot, since ? [" · ", el("span", { "data-since": since })] : null),
    onView ? el("button", { class: "linkbtn", type: "button", onclick: onView }, "View output") : null));
  requestAnimationFrame(() => { logList.scrollTop = logList.scrollHeight; });
  return card;
}

function renderAgents(v) {
  const cards = v.stages.map((s) => agentCard({
    persona: s.persona, status: s.status, log: s.log,
    since: ["running", "retrying"].includes(s.status) ? stageStart(v, s) : null,
    foot: s.attempts.length
      ? `${s.attempts.length} attempt${s.attempts.length > 1 ? "s" : ""}`
        + (s.latest_score != null ? ` · score ${s.latest_score.toFixed(2)}` : "")
        + (s.finished_at && s.started_at ? ` · ${duration(Date.parse(s.finished_at) - Date.parse(s.started_at))}` : "")
      : "Not started",
    onView: s.attempts.length ? () => {
      state.tab = s.persona;
      renderOutputs(state.view);
      $("tabs").scrollIntoView({ behavior: "smooth", block: "start" });
    } : null,
  }));
  const ev = v.evaluator;
  const evStatus = ev.evaluations.length === 0 ? "pending"
    : v.status === "complete" ? "completed" : isSettled(v) ? v.status : "running";
  cards.push(agentCard({
    persona: "evaluator", status: evStatus,
    log: ev.evaluations.map((e) => `${e.handoff_label} #${e.attempt}: ${e.verdict.toUpperCase()}`
      + (e.overall_score != null ? ` (${e.overall_score.toFixed(2)})` : " (pre-gate)")
      + (e.judge_escalated ? " · Tier-2 confirmed" : "") + (e.critical_flag ? " · critical" : "")),
    foot: `${ev.judge_calls} judge calls · ${ev.tier2_confirmations} Tier-2 · ${usd(ev.cost_usd)}`,
    onView: ev.evaluations.length ? () => $("scores").scrollIntoView({ behavior: "smooth" }) : null,
  }));
  fill($("agents"), cards);
}

function renderReview(v) {
  const box = $("review");
  const r = v.pending_review;
  if (!r) { box.hidden = true; box.replaceChildren(); box.dataset.for = ""; return; }
  const key = `${r.handoff}-${r.attempts_so_far}`;
  if (!box.hidden && box.dataset.for === key) return;
  box.dataset.for = key;
  box.hidden = false;
  const label = (state.config.handoffs.find((h) => h.id === r.handoff) || {}).label || r.handoff;
  const guidance = el("textarea", { rows: 3, placeholder: "Guidance for the retry (required for Retry)", "aria-label": "Retry guidance" });
  const msg = el("p", { class: "muted", role: "status" });
  const decide = async (action) => {
    if (action === "retry" && !guidance.value.trim()) { msg.textContent = "Guidance is required to retry."; return; }
    msg.textContent = "Sending decision…";
    const res = await api(`/runs/${encodeURIComponent(v.run_id)}/resume`, {
      method: "POST", body: JSON.stringify({ action, guidance: guidance.value.trim() || null }),
    });
    if (res.ok) { box.hidden = true; box.dataset.for = ""; follow(v.run_id); }
    else msg.textContent = `Failed (${res.status}): ${JSON.stringify(res.body && res.body.detail)}`;
  };
  fill(box,
    el("div", { class: "card-head" }, el("h2", {}, `Human review required: ${label}`), badge("awaiting_review")),
    el("p", {}, `The evaluator escalated after ${r.attempts_so_far} attempt(s)`,
      r.overall_score != null ? ` (score ${r.overall_score.toFixed(2)}${r.critical_flag ? ", critical" : ""})` : "",
      ". The pipeline is paused until you decide."),
    el("div", { class: "callout" }, r.evaluator_feedback),
    r.blocking_issues.length ? list(r.blocking_issues) : null,
    guidance,
    el("div", { class: "row" },
      el("button", { class: "btn primary", type: "button", onclick: () => decide("accept_as_is"), disabled: !Object.keys(r.artifact).length }, "Accept as is"),
      el("button", { class: "btn", type: "button", onclick: () => decide("retry") }, "Retry with guidance"),
      el("button", { class: "btn danger", type: "button", onclick: () => decide("abort") }, "Abort run")),
    msg,
  );
}

function renderFinal(v) {
  const box = $("final");
  const f = v.final_result;
  if (!f) { box.hidden = true; return; }
  box.hidden = false;
  const t = v.totals;
  const exec = f.test_execution;
  const sourceNote = {
    simulator: "Simulated run: agents filled templates and a rule-based judge scored them; no model was called.",
    example: "Curated example: the agents replayed hand-authored outputs; token and cost figures are estimates.",
    claude: "Live run: tokens and cost are as reported by the Claude API.",
  }[v.source];
  const reportNote = el("span", { class: "muted", role: "status" });
  const download = el("button", { class: "btn primary", type: "button",
    onclick: () => downloadReport(v.run_id, download, reportNote) }, "Download SDLC Execution Report (PDF)");
  fill(box,
    el("div", { class: "final-head" }, el("h2", {}, "Final Result"), badge(v.status)),
    el("p", { class: `headline ${f.tone}` }, el("b", {}, f.headline)),
    v.error ? el("div", { class: "callout" }, `Error: ${v.error}`) : null,
    el("div", { class: "stats" },
      [[`${f.gates_passed}/${f.gates_total}`, "gates passed by evaluator"], [t.retries, "retries"],
        [t.escalations, "human escalations"], [t.llm_calls, "LLM calls"],
        [`${compact(t.input_tokens)} / ${compact(t.output_tokens)}`, "tokens in / out"],
        [usd(t.cost_usd), "total cost"]].map(([b, s]) => el("div", { class: "stat" }, el("b", {}, b), el("span", {}, s)))),
    list(f.highlights),
    exec ? el("p", { class: "muted" }, exec.tests_executed
      ? `Generated tests executed: ${exec.tests_passed} passed, ${exec.tests_failed} failed.`
      : `Generated code syntax-checked (${exec.files_checked} files); tests not executed: ${exec.skipped_reason}.`) : null,
    sourceNote ? el("p", { class: "muted" }, sourceNote) : null,
    el("div", { class: "row" }, download, reportNote),
  );
}

async function downloadReport(runId, button, note) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Preparing report…";
  note.textContent = "";
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/report.pdf`, { headers: authHeaders() });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const url = URL.createObjectURL(await res.blob());
    const link = el("a", { href: url, download: `${runId}-sdlc-execution-report.pdf` });
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) {
    note.textContent = `Could not generate the report (${err.message}).`;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function renderMetrics(v) {
  const evals = v.evaluator.evaluations;
  const firstAttempts = evals.filter((e) => e.attempt === 1);
  const firstPass = firstAttempts.length ? firstAttempts.filter((e) => e.verdict === "pass").length / firstAttempts.length : null;
  const retryRate = evals.length ? evals.filter((e) => e.verdict === "retry").length / evals.length : null;
  const timed = v.stages.filter((s) => s.started_at && s.finished_at);
  const stageMs = timed.length ? timed.reduce((sum, s) => sum + Date.parse(s.finished_at) - Date.parse(s.started_at), 0) / timed.length : null;
  const t = v.totals;
  const live = v.source === "claude";
  const noModel = "no model calls in this run";
  const tiles = [
    [pct(firstPass), "first-pass success", "gates passed on attempt 1"],
    [pct(retryRate), "retry rate", "evaluations that asked for a retry"],
    [stageMs === null ? "—" : duration(stageMs), "average stage time", "wall-clock per finished stage"],
    [live && t.llm_calls ? `${Math.round(t.llm_latency_ms / t.llm_calls)} ms` : "—", "average model latency", live ? "per LLM call" : noModel],
    [live && t.input_tokens ? pct(t.cache_read_tokens / t.input_tokens) : "—", "cache hit rate", live ? "input tokens read from cache" : noModel],
    [compact(t.input_tokens + t.output_tokens), "total tokens", v.source === "claude" ? "as billed" : "estimated (≈4 chars/token)"],
  ];
  fill($("metrics"), tiles.map(([b, s, small]) => el("div", { class: "stat" }, el("b", {}, b), el("span", {}, s), el("small", {}, small))));

  const rows = v.stages.map((s) => ({
    name: agent(s.persona).name, cls: "",
    tokens: s.attempts.reduce((sum, a) => sum + a.input_tokens + a.output_tokens, 0),
  }));
  rows.push({ name: agent("evaluator").name, cls: "evaluator", tokens: v.evaluator.input_tokens + v.evaluator.output_tokens });
  const max = Math.max(...rows.map((r) => r.tokens), 0);
  fill($("token-bars"), max === 0
    ? el("p", { class: "empty" }, "No agent has run yet.")
    : rows.map((r) => el("div", { class: "token-row" },
      el("span", {}, r.name),
      el("span", { class: "track", role: "img", "aria-label": `${r.tokens} tokens` },
        el("i", { class: r.cls, style: { width: `${Math.max(r.tokens ? 2 : 0, (r.tokens / max) * 100)}%` } })),
      el("span", { class: "count" }, r.tokens.toLocaleString()))));
}

function renderDecisions(v) {
  const head = el("thead", {}, el("tr", {}, ["Time", "Gate", "Attempt", "Decision", "Decided by", "Reason"].map((h) => el("th", {}, h))));
  const rows = v.decisions.map((d) => el("tr", {},
    el("td", {}, time(d.ts)), el("td", {}, d.handoff_label), el("td", {}, d.attempt),
    el("td", {}, badge(d.decision.toLowerCase(), d.decision.replaceAll("_", " "))),
    el("td", {}, d.decided_by), el("td", {}, d.reason)));
  fill($("decisions"), head, el("tbody", {}, rows.length ? rows
    : [el("tr", {}, el("td", { colspan: 6, class: "empty" }, "No decisions yet: the first gate runs after the Product Manager."))]));
}

// ------------------------------------------------------------------ outputs
function renderOutputs(v) {
  const stages = v.stages;
  if (!stages.some((s) => s.persona === state.tab)) {
    const active = stages.filter((s) => s.attempts.length);
    state.tab = (active[active.length - 1] || stages[0]).persona;
  }
  const tabs = stages.map((s) => el("button", {
    class: "tab", role: "tab", type: "button", tabindex: s.persona === state.tab ? 0 : -1,
    "aria-selected": String(s.persona === state.tab),
    onclick: () => { state.tab = s.persona; renderOutputs(state.view); },
    onkeydown: (e) => {
      if (!["ArrowRight", "ArrowLeft"].includes(e.key)) return;
      const i = stages.indexOf(s) + (e.key === "ArrowRight" ? 1 : -1);
      state.tab = stages[(i + stages.length) % stages.length].persona;
      renderOutputs(state.view);
      $("tabs").querySelector('[aria-selected="true"]').focus();
    },
  }, `${agent(s.persona).name}${s.attempts.length > 1 ? ` (${s.attempts.length})` : ""}`));
  fill($("tabs"), tabs);

  const stage = stages.find((s) => s.persona === state.tab);
  if (!stage.attempts.length) {
    $("attempts").replaceChildren();
    $("output").className = "output single";
    fill($("output"), el("p", { class: "empty" }, `${agent(stage.persona).name} has not produced output yet.`));
    return;
  }
  const latest = stage.attempts[stage.attempts.length - 1].attempt;
  const chosen = state.attempt[stage.persona] || latest;
  fill($("attempts"), stage.attempts.map((a) => el("button", {
    class: "attempt-btn", type: "button", "aria-pressed": String(a.attempt === chosen),
    onclick: () => { state.attempt[stage.persona] = a.attempt; renderOutputs(state.view); },
  }, `Attempt ${a.attempt}${a.evaluation ? ` · ${a.evaluation.verdict.toUpperCase()}` : ""}`)));
  const attempt = stage.attempts.find((a) => a.attempt === chosen) || stage.attempts[stage.attempts.length - 1];

  const meta = el("p", { class: "muted" }, `${attempt.model} (${attempt.tier}, ${attempt.route_reason}) · `
    + `${attempt.input_tokens.toLocaleString()} in / ${attempt.output_tokens.toLocaleString()} out tokens · ${usd(attempt.cost_usd)}`);
  const body = attempt.output ? renderArtifact(stage.persona, attempt.output)
    : el("p", {}, `No valid output: ${attempt.parse_error || "pending"}`);
  const gated = Boolean(stage.handoff);
  $("output").className = gated ? "output" : "output single";
  fill($("output"),
    gated ? null : el("p", { class: "muted" },
      "QA output is not gated; its acceptance-criteria coverage is reported in the final result."),
    el("div", { class: "artifact" }, meta, body),
    gated ? (attempt.evaluation ? evalBox(attempt.evaluation) : el("div", { class: "evalbox muted" }, "Not evaluated yet.")) : null,
  );
}

function evalBox(e) {
  const labels = Object.fromEntries(state.config.dimensions.map((d) => [d.id, d.label]));
  return el("div", { class: "evalbox" },
    el("div", { class: "card-head" }, el("b", {}, `Evaluator · ${e.handoff_label}`), badge(e.verdict)),
    el("p", { class: "muted" }, e.judge === "deterministic" ? "Rejected by the deterministic pre-gate (no LLM call)."
      : `${e.judge === "tier2" ? "Tier-2" : "Tier-1"} judge (${e.model_used})`
        + (e.judge_escalated ? `; Tier-1 scored ${e.tier1_overall_score}, re-scored on Tier 2` : "")),
    e.dimension_scores ? el("div", { class: "dims" }, Object.entries(e.dimension_scores).map(([k, s]) =>
      el("div", { class: "dim" }, el("span", {}, labels[k] || k),
        el("span", { class: "bar", role: "img", "aria-label": `${s} of 5` }, el("i", { class: `s${s}`, style: { width: `${s * 20}%` } })),
        el("b", {}, s)))) : null,
    e.overall_score != null ? el("p", {}, el("b", {}, `Overall ${e.overall_score.toFixed(2)}`),
      e.critical_flag ? " · critical flag (a dimension scored 1)" : "") : null,
    el("h4", {}, "Feedback"), el("p", {}, e.feedback),
    e.blocking_issues.length ? [el("h4", {}, "Blocking issues"), list(e.blocking_issues)] : null,
    e.findings.length ? [el("h4", {}, "Pre-gate notes"), list(e.findings)] : null,
  );
}

function codeFile(f, index) {
  const key = `${state.tab}:${f.path}`;
  const open = state.openFiles.has(key) || (state.openFiles.size === 0 && index === 1);
  const details = el("details", { open },
    el("summary", {}, f.path), el("pre", { class: "code" }, el("code", {}, f.content || "(empty)")));
  details.addEventListener("toggle", () => {
    if (details.open) state.openFiles.add(key); else state.openFiles.delete(key);
  });
  if (open) state.openFiles.add(key);
  return details;
}

function renderArtifact(persona, o) {
  const H = (t) => el("h4", {}, t);
  switch (persona) {
    case "pm": return el("div", {},
      H("Objective"), el("p", {}, o.objective),
      H("Business goals"), el("ol", { start: 0 }, o.business_goals.map((g) => el("li", {}, g))),
      H("Success metrics"), list(o.success_metrics),
      H("Constraints"), el("div", { class: "callout" }, list(o.constraints)),
      H("Out of scope"), o.out_of_scope.length ? list(o.out_of_scope) : el("p", { class: "muted" }, "None stated."),
      H("Stakeholders"), el("p", {}, o.stakeholders.map((s) => el("span", { class: "pill" }, s))));
    case "ba": {
      const trace = Object.fromEntries((o.traceability || []).map((t) => [t.story_id, t.business_goal_indices]));
      return el("div", {},
        H(`User stories (${o.user_stories.length})`),
        o.user_stories.map((s) => el("div", { class: "story" },
          el("p", {}, el("span", { class: "id" }, `${s.id} `), `As a ${s.as_a}, I want ${s.i_want}, so that ${s.so_that}. `,
            trace[s.id] ? el("span", { class: "pill" }, `goals ${trace[s.id].join(", ")}`) : null),
          el("ul", {}, s.acceptance_criteria.map((ac) => el("li", {}, el("code", {}, ac.id), " ", ac.description))))),
        H("Non-functional requirements"), list(o.non_functional_requirements),
        H("Open questions"), list(o.open_questions));
    }
    case "architect": return el("div", {},
      H("Integration approach"), el("div", { class: "callout" }, o.integration_approach),
      H("Components"), el("table", { class: "table" }, el("tbody", {}, o.components.map((c) =>
        el("tr", {}, el("td", {}, el("b", {}, c.name)), el("td", {}, c.responsibility))))),
      H("Data flow"), el("p", {}, o.data_flow),
      H("Risks and mitigations"), el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Risk"), el("th", {}, "Mitigation"))),
        el("tbody", {}, o.risks.map((r) => el("tr", {}, el("td", {}, r.description), el("td", {}, r.mitigation))))),
      H("Architecture decision records"), o.adrs.map((a) => el("div", { class: "story" },
        el("p", {}, el("b", {}, a.title), `: ${a.decision}`),
        el("p", { class: "muted" }, `Alternatives: ${a.alternatives_considered.join("; ")}`),
        el("p", {}, a.rationale))));
    case "dev": return el("div", {},
      H("Summary"), el("p", {}, o.summary),
      H("Implemented stories"), el("p", {}, o.implemented_story_ids.map((s) => el("span", { class: "pill" }, s))),
      H(`Code (${o.files.length} files)`), o.files.map(codeFile),
      H("Stubbed stories"), list(o.stubbed_stories.map((s) => `${s.story_id}: ${s.plan}`)),
      H("Deviations"), list(o.deviations));
    case "qa": return el("div", {},
      H(`Test cases (${o.test_cases.length})`), el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, ["ID", "Story", "Criteria", "Type", "Steps", "Expected result"].map((h) => el("th", {}, h)))),
        el("tbody", {}, o.test_cases.map((t) => el("tr", {}, el("td", {}, t.id), el("td", {}, t.story_id),
          el("td", {}, t.acceptance_criterion_ids.join(", ")), el("td", {}, t.type),
          el("td", {}, el("ol", {}, t.steps.map((step) => el("li", {}, step)))), el("td", {}, t.expected_result)))))),
      H("Coverage"), el("p", {}, o.coverage_summary),
      H("Untestable criteria"), el("p", {}, (o.untestable_criteria || []).map((s) => el("span", { class: "pill" }, s))),
      H("Gaps"), list(o.gaps),
      o.execution ? [H("Measured execution (attached by code, not by the model)"), el("div", { class: "callout" },
        o.execution.tests_executed ? `${o.execution.tests_passed} passed, ${o.execution.tests_failed} failed`
          : `${o.execution.files_checked} files syntax-checked`
            + `${o.execution.syntax_errors.length ? `; errors: ${o.execution.syntax_errors.join("; ")}` : ", no errors"}`
            + `; tests not run: ${o.execution.skipped_reason}`)] : null);
    default: return el("pre", { class: "code" }, JSON.stringify(o, null, 2));
  }
}

function renderScores(v) {
  const dims = state.config.dimensions;
  const head = el("thead", {}, el("tr", {},
    ["Gate", "Attempt", "Judge", ...dims.map((d) => d.label), "Overall", "Verdict"].map((h) => el("th", {}, h))));
  const rows = v.evaluator.evaluations.map((e) => el("tr", {},
    el("td", {}, e.handoff_label), el("td", {}, e.attempt),
    el("td", {}, e.judge + (e.judge_escalated ? " (confirmed)" : "")),
    dims.map((d) => el("td", {}, e.dimension_scores ? e.dimension_scores[d.id] : "—")),
    el("td", {}, e.overall_score != null ? e.overall_score.toFixed(2) : "—"),
    el("td", {}, badge(e.verdict))));
  fill($("scores"), head, el("tbody", {}, rows.length ? rows
    : [el("tr", {}, el("td", { colspan: dims.length + 5, class: "empty" }, "No evaluations yet."))]));
}

function renderTimeline(v) {
  fill($("timeline"), v.timeline.map((t) => el("li", { class: t.tone },
    el("time", { datetime: t.ts }, time(t.ts)), el("span", { class: "actor" }, t.actor),
    el("div", {}, t.message))));
}

// ------------------------------------------------------------------ product guide
function renderGuide(config) {
  const demo = config.mode === "offline";
  setBadge("guide-mode", null, demo ? "Demo mode" : "Live mode", demo ? "retry" : "passed");
  const decision = (a) => a.gate ? `Evaluator gate ${a.gate}: pass, retry or escalate`
    : a.persona === "evaluator" ? "Decides pass / retry / escalate for every handoff"
      : "Not gated; its coverage is reported in the Final Result";
  fill($("guide-agents"), config.agents.map((a) => el("article", { class: "agent-doc" },
    el("header", {}, el("span", { class: "avatar" }, a.short), el("b", {}, a.name)),
    el("dl", {},
      el("dt", {}, "Purpose"), el("dd", {}, a.description),
      el("dt", {}, "Input"), el("dd", {}, a.receives),
      el("dt", {}, "Output"), el("dd", {}, a.produces),
      el("dt", {}, "Decision point"), el("dd", {}, decision(a))))));
  fill($("guide-dims"), config.dimensions.map((d) => el("li", {}, d.label)));
  $("guide-pass-rule").textContent = config.pass_rule;
}

// ------------------------------------------------------------------ history
async function loadHistory() {
  const res = await api("/api/runs");
  const table = $("history");
  if (!res.ok) {
    fill(table, el("tr", {}, el("td", { class: "empty" }, `Could not load runs (${res.status}).`)));
    return;
  }
  fill(table,
    el("thead", {}, el("tr", {}, ["Requirement", "Status", "Started", "Run ID"].map((h) => el("th", {}, h)))),
    el("tbody", {}, res.body.length ? res.body.map((r) => el("tr", {
      class: "clickable", tabindex: 0, onclick: () => openRun(r.run_id),
      onkeydown: (e) => { if (e.key === "Enter") openRun(r.run_id); },
    }, el("td", {}, r.title), el("td", {}, badge(r.status)), el("td", {}, new Date(r.created).toLocaleString()),
      el("td", {}, el("code", {}, r.run_id))))
      : [el("tr", {}, el("td", { colspan: 4, class: "empty" }, "No runs yet. Start one from New Workflow."))]));
}

boot();
