/* GemstoneAI Console: the intake station, the Verify review queue and account settings.
   Talks to the enterprise API on the same origin. Every value from the API is written
   as text (never as HTML), and only http(s) links are rendered. */
(() => {
  "use strict";

  const API = new URL("../", window.location.href).pathname.replace(/\/$/, "");
  const MAX_PHOTOS = 5;
  const MAX_BYTES = 8 * 1024 * 1024;
  const PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp", "image/heic"];
  const DECISIONS = { list: "List", review: "Review", lab: "Lab", decline: "Decline" };
  const ACTIONS = { allow: "Allow", request_lab_report: "Request lab report", manual_review: "Manual review", hold: "Hold" };
  const EVENTS = {
    "intake.item.completed": "Intake decision ready",
    "verify.check.completed": "Verify check ready",
    "verify.check.flagged": "Verify check flagged",
    "batch.completed": "Batch finished",
  };

  let KEY = null;
  let ACCOUNT = null;
  const files = { intake: [], verify: [] };
  let verifyMode = "open";

  // ── helpers ────────────────────────────────────────────────────────────────

  const $ = (id) => document.getElementById(id);

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  function store(kind, key, value) {
    try {
      const s = kind === "session" ? sessionStorage : localStorage;
      if (value === undefined) return s.getItem(key);
      if (value === null) s.removeItem(key); else s.setItem(key, value);
    } catch (_) { /* storage blocked: the console still works for this page view */ }
    return null;
  }

  class ApiError extends Error {
    constructor(status, body) {
      const err = (body && body.error) || {};
      super(err.message || `Request failed (${status})`);
      this.status = status;
      this.code = err.code;
      this.details = err.details;
    }
  }

  async function api(path, { method = "GET", body, idempotent = false, raw = false } = {}) {
    const headers = { Authorization: `Bearer ${KEY}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotent) headers["Idempotency-Key"] = newKey();
    const res = await fetch(API + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (raw && res.ok) return res;
    if (res.status === 204) return null;
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, data);
    return data;
  }

  function newKey() {
    if (window.crypto && crypto.randomUUID) return "console-" + crypto.randomUUID();
    return "console-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }

  function money(value, currency) {
    if (value === null || value === undefined) return "";
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD", maximumFractionDigits: 0 }).format(value);
    } catch (_) {
      return `${Math.round(value).toLocaleString()} ${currency || ""}`.trim();
    }
  }

  const pct = (x) => (x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`);
  const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");

  function safeUrl(u) {
    try {
      const url = new URL(u);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function showError(el, err) {
    el.textContent = err ? (err.message || String(err)) : "";
  }

  async function download(path, filename) {
    const res = await api(path, { raw: true });
    const url = URL.createObjectURL(await res.blob());
    const a = h("a", { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ── sign in ────────────────────────────────────────────────────────────────

  async function signIn(key) {
    KEY = key;
    ACCOUNT = await api("/account");
    store("session", "gsk", key);
    $("signin").hidden = true;
    $("who").hidden = false;
    $("who-name").textContent = ACCOUNT.name;
    $("who-sandbox").hidden = ACCOUNT.environment !== "sandbox";
    buildTabs();
  }

  function signOut() {
    store("session", "gsk", null);
    window.location.reload();
  }

  function buildTabs() {
    const tabs = [];
    if (ACCOUNT.scopes.includes("intake")) tabs.push(["intake", "Intake station"]);
    if (ACCOUNT.scopes.includes("verify")) tabs.push(["verify", "Verify queue"]);
    tabs.push(["settings", "Settings"]);
    const nav = $("tabs");
    nav.replaceChildren(...tabs.map(([id, label]) =>
      h("button", { type: "button", role: "tab", "data-view": id, onclick: () => openView(id) }, label)));
    nav.hidden = false;
    const wanted = (window.location.hash || "").slice(1);
    openView(tabs.some(([id]) => id === wanted) ? wanted : tabs[0][0]);
  }

  function openView(id) {
    for (const btn of $("tabs").querySelectorAll("button")) btn.setAttribute("aria-selected", String(btn.dataset.view === id));
    for (const view of ["intake", "verify", "settings"]) $(`view-${view}`).hidden = view !== id;
    history.replaceState(null, "", `#${id}`);
    if (id === "intake") { loadIntakeQueue(); loadIntakeStats(); }
    if (id === "verify") { loadVerifyQueue(); loadVerifyStats(); }
    if (id === "settings") loadSettings();
  }

  // ── photos ─────────────────────────────────────────────────────────────────

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  async function pickFiles(kind, input) {
    const list = Array.from(input.files || []);
    const thumbs = $(`${kind}-thumbs`);
    files[kind] = [];
    thumbs.replaceChildren();
    for (const file of list.slice(0, MAX_PHOTOS)) {
      if (!PHOTO_TYPES.includes(file.type)) throw new Error(`${file.name}: use JPEG, PNG, WebP or HEIC photos.`);
      if (file.size > MAX_BYTES) throw new Error(`${file.name} is larger than 8 MB.`);
      const dataUrl = await readFile(file);
      files[kind].push({ data: dataUrl.split(",")[1], media_type: file.type });
      thumbs.append(h("img", { src: dataUrl, alt: file.name }));
    }
  }

  function photosFor(kind) {
    const urls = $(`${kind}-urls`).value.split(/\s+/).map((u) => u.trim()).filter(Boolean)
      .map((url) => ({ url }));
    const photos = [...files[kind], ...urls];
    if (!photos.length) throw new Error("Add at least one photo or photo link.");
    if (photos.length > MAX_PHOTOS) throw new Error("Use at most 5 photos.");
    for (const p of urls) if (!p.url.startsWith("https://")) throw new Error("Photo links must start with https://");
    return photos;
  }

  // ── shared result pieces ───────────────────────────────────────────────────

  function identificationBox(scan) {
    const ident = scan.identification || {};
    const alts = (ident.alternatives || []).filter((a) => a.probability >= 0.05);
    return h("div", { class: "box" },
      h("div", { class: "label", text: ident.status === "identified" ? `Identified · ${pct(ident.confidence)} confidence` : "Not identified" }),
      h("div", { text: ident.summary || "The scan could not identify the item." }),
      alts.length ? h("div", { class: "chips" }, h("span", { class: "small muted", text: "Could also be" }),
        alts.map((a) => h("span", { class: "chip", text: `${a.label} ${pct(a.probability)}` }))) : null,
      (ident.open_questions || []).length ? h("div", { class: "small muted mt8" },
        `Still open: ${ident.open_questions.join(" ")}`) : null,
      (ident.next_evidence || []).length ? h("div", { class: "small muted" },
        `Would settle it: ${ident.next_evidence.join("; ")}`) : null);
  }

  function priceBox(scan) {
    const price = scan.price_context || {};
    if (price.status !== "provided") {
      return h("div", { class: "box" }, h("div", { class: "label", text: "No price" }),
        h("div", { class: "small", text: price.withheld_reason || "The scan gave no price." }));
    }
    return h("div", { class: "box" },
      h("div", { class: "label", text: `Price · ${price.basis || "range"}` }),
      h("div", { class: "price", text: `${money(price.low, price.currency)} – ${money(price.high, price.currency)}` }),
      price.explanation ? h("div", { class: "small muted", text: price.explanation }) : null);
  }

  function comparablesTable(scan) {
    const comps = scan.comparables || {};
    const items = (comps.items || []).slice(0, 8);
    if (!items.length) return h("p", { class: "small muted", text: "No comparables were found." });
    return h("div", {},
      h("div", { class: "label", text: `Comparables · ${comps.total} found: ${comps.realized || 0} sold, ${comps.asking || 0} asking` }),
      h("table", { class: "comps" }, h("tbody", {}, items.map((c) => {
        const url = safeUrl(c.url);
        const title = url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: c.title || c.source }) : (c.title || c.source);
        return h("tr", {},
          h("td", {}, h("span", { class: `tag tag-${c.price_type}`, text: c.price_type === "realized" ? "Sold" : "Asking" })),
          h("td", {}, title, h("div", { class: "small muted", text: [c.observed_on, ...(c.differences || [])].filter(Boolean).join(" · ") })),
          h("td", { text: money(c.price, c.currency) }));
      }))));
  }

  function metaLine(record) {
    const scan = record.scan || {};
    return h("div", { class: "meta" }, record.id, h("br"), `${scan.scan_id || ""} · ${scan.model_version || ""}`,
      scan.synthetic_data ? h("div", { text: "Sandbox example" }) : null);
  }

  function finalCall(kind, record, options, onDone) {
    const wrap = h("div", { class: "final" });
    const review = record.review;
    const note = h("input", { type: "text", maxlength: 2000, placeholder: "Note (optional)" });
    const reviewer = h("input", { type: "text", maxlength: 128, placeholder: "Your name or staff id", value: store("local", "reviewer") || "" });
    const error = h("p", { class: "error", role: "alert" });
    let chosen = null;
    const buttons = options.map(([value, label]) => h("button", {
      type: "button", class: review && review.outcome === value ? "chosen" : null,
      onclick: (e) => {
        chosen = value;
        for (const b of e.currentTarget.parentElement.children) b.classList.remove("chosen");
        e.currentTarget.classList.add("chosen");
      },
    }, label));
    const save = h("button", {
      type: "button", class: "btn btn-primary",
      onclick: async () => {
        if (!chosen) { error.textContent = "Choose an option first."; return; }
        store("local", "reviewer", reviewer.value.trim());
        save.disabled = true;
        try {
          const updated = await api(`/${kind}/${record.id}/review`, {
            method: "POST", body: { outcome: chosen, note: note.value.trim() || null, reviewer: reviewer.value.trim() || null },
          });
          onDone(updated);
        } catch (err) {
          showError(error, err);
          save.disabled = false;
        }
      },
    }, "Record");
    wrap.append(
      h("div", { class: "label", text: kind === "intake/items" ? "Final decision" : "Your verdict" }),
      review ? h("div", { class: "recorded" },
        `Recorded: ${options.find(([v]) => v === review.outcome)?.[1] || review.outcome}`,
        review.agrees ? " (agrees)" : " (differs)",
        review.reviewer ? ` · ${review.reviewer}` : "", ` · ${when(review.reviewed_at)}`,
        review.note ? h("div", { class: "small muted", text: review.note }) : null,
        (review.previous || []).length ? h("div", { class: "small muted", text: `${review.previous.length} earlier call${review.previous.length > 1 ? "s" : ""} kept` }) : null) : null,
      h("div", { class: "final-buttons" }, buttons),
      h("div", { class: "row2" }, h("label", { class: "field" }, h("span", { text: "Note" }), note),
        h("label", { class: "field" }, h("span", { text: "Reviewer" }), reviewer)),
      h("div", { class: "form-foot" }, save, error));
    return wrap;
  }

  function processingCard(path, record, render) {
    const btn = h("button", {
      type: "button", class: "btn btn-quiet",
      onclick: async () => { btn.disabled = true; render(await api(`/${path}/${record.id}`)); },
    }, "Check again");
    return h("div", { class: "card" }, h("div", { class: "result-head" },
      h("div", { class: "decision-big" }, "Scan running", h("small", { text: "The result will appear here when the scan finishes." })),
      metaLine(record)), btn);
  }

  // ── Intake station ─────────────────────────────────────────────────────────

  function renderIntake(record) {
    const target = $("intake-detail");
    if (record.status === "processing") {
      target.replaceChildren(processingCard("intake/items", record, renderIntake));
      return;
    }
    const routing = record.routing;
    const title = routing.prefill && routing.prefill.title;
    target.replaceChildren(h("div", { class: "card" },
      h("div", { class: "result-head" },
        h("div", {},
          h("span", { class: `badge d-${routing.decision}`, text: "Routed by your rules" }),
          h("div", { class: "decision-big mt8" }, DECISIONS[routing.decision],
            h("small", { text: [record.reference && `Reference ${record.reference}`, when(record.created_at)].filter(Boolean).join(" · ") }))),
        metaLine(record)),
      h("ul", { class: "reasons" }, routing.reasons.map((r) => h("li", {}, h("code", { text: r.code }), r.message))),
      routing.consignor_description ? h("p", { class: "small muted" }, `Consignor says: ${routing.consignor_description}`) : null,
      h("div", { class: "grid2" }, identificationBox(record.scan), priceBox(record.scan)),
      title ? h("div", { class: "box mb16" },
        h("div", { class: "label", text: "Suggested listing title" }), h("div", { text: title.value }),
        h("div", { class: "small muted", text: routing.prefill.note })) : null,
      comparablesTable(record.scan),
      finalCall("intake/items", record, Object.entries(DECISIONS), (updated) => {
        renderIntake(updated); loadIntakeQueue(); loadIntakeStats();
      })));
  }

  async function submitIntake(e) {
    e.preventDefault();
    const error = $("intake-error");
    const button = $("intake-submit");
    showError(error, null);
    try {
      const body = { photos: photosFor("intake") };
      const desc = $("intake-desc").value.trim();
      const ref = $("intake-ref").value.trim();
      const details = {};
      if ($("intake-type").value) details.item_type = $("intake-type").value;
      if ($("intake-metal").value) details.metal = { type: $("intake-metal").value };
      if (desc) body.consignor_description = desc;
      if (ref) body.reference = ref;
      if (Object.keys(details).length) body.details = details;
      button.disabled = true;
      button.textContent = "Scanning…";
      const record = await api("/intake/items", { method: "POST", body, idempotent: true });
      renderIntake(record);
      $("intake-form").reset();
      $("intake-thumbs").replaceChildren();
      files.intake = [];
      loadIntakeQueue();
      loadIntakeStats();
    } catch (err) {
      showError(error, err);
    } finally {
      button.disabled = false;
      button.textContent = "Scan and route";
    }
  }

  function intakeQueryString() {
    const filter = $("intake-filter").value;
    if (filter === "__awaiting") return "reviewed=false";
    return filter ? `decision=${encodeURIComponent(filter)}` : "";
  }

  async function loadIntakeQueue() {
    const list = $("intake-queue");
    try {
      const page = await api(`/intake/items?limit=30&${intakeQueryString()}`);
      if (!page.data.length) { list.replaceChildren(h("li", { class: "empty", text: "Nothing here yet." })); return; }
      list.replaceChildren(...page.data.map((row) => h("li", {}, h("button", {
        type: "button",
        onclick: async (e) => {
          for (const b of list.querySelectorAll("button")) b.classList.remove("on");
          e.currentTarget.classList.add("on");
          renderIntake(await api(`/intake/items/${row.id}`));
        },
      },
      h("span", { class: `badge d-${row.decision || "processing"}`, text: row.decision ? DECISIONS[row.decision] : "Running" }),
      h("span", { class: "q-title", text: row.reference || row.identified_as || row.id }),
      h("span", { class: "q-sub", text: [when(row.created_at), row.review_outcome ? `final: ${DECISIONS[row.review_outcome]}` : "awaiting final call"].join(" · ") })))));
    } catch (err) {
      list.replaceChildren(h("li", { class: "empty", text: err.message }));
    }
  }

  function statTiles(target, tiles) {
    target.replaceChildren(...tiles.map(([label, value, key]) =>
      h("div", { class: key ? "stat key" : "stat" }, h("b", { text: value }), h("span", { text: label }))));
  }

  async function loadIntakeStats() {
    try {
      const s = await api("/intake/stats");
      statTiles($("intake-stats"), [
        ["Items, last 30 days", s.items], ["List", s.by_decision.list], ["Review", s.by_decision.review],
        ["Lab", s.by_decision.lab], ["Decline", s.by_decision.decline], ["Final calls recorded", s.reviewed],
        ["List items corrected (target < 2%)", pct(s.list_correction_rate), true],
      ]);
    } catch (_) { $("intake-stats").replaceChildren(); }
  }

  // ── Verify queue ───────────────────────────────────────────────────────────

  function scoreClass(score) {
    return score >= 70 ? "s-high" : score >= 40 ? "s-mid" : "s-low";
  }

  function evidenceLine(check) {
    const ev = check.evidence || {};
    if (ev.matches && ev.matches.length) return `Seen before: ${ev.matches.map((m) => m.reference || m.record_id).join(", ")}`;
    if (ev.prior_flagged !== undefined && check.fired) return `${ev.prior_flagged} earlier flagged checks`;
    return null;
  }

  function renderVerify(record) {
    const target = $("verify-detail");
    if (record.status === "processing") {
      target.replaceChildren(processingCard("verify/checks", record, renderVerify));
      return;
    }
    const risk = record.risk;
    const subject = risk.subject;
    target.replaceChildren(h("div", { class: "card" },
      h("div", { class: "result-head" },
        h("div", {},
          h("span", { class: `score ${scoreClass(risk.risk_score)}`, text: risk.risk_score }), " ",
          h("span", { class: `badge a-${risk.action}`, text: ACTIONS[risk.action] }),
          h("div", { class: "decision-big mt8" }, `${subject.type[0].toUpperCase()}${subject.type.slice(1)} ${subject.external_id}`,
            h("small", { text: [subject.seller_id && `Seller ${subject.seller_id}`, subject.stated_value && `Stated ${money(subject.stated_value, subject.currency)}`, when(record.created_at)].filter(Boolean).join(" · ") }))),
        metaLine(record)),
      h("ul", { class: "checks-list" }, risk.checks.map((c) => h("li", { class: c.fired ? "fired" : (c.ran ? "" : "notrun") },
        h("span", { class: "dot" }),
        h("div", {}, h("div", { text: c.message }), evidenceLine(c) ? h("div", { class: "evidence", text: evidenceLine(c) }) : null),
        h("span", { class: "w", text: c.fired ? `+${c.weight}` : (c.ran ? "0" : "not run") })))),
      h("div", { class: "grid2" }, identificationBox(record.scan), priceBox(record.scan)),
      finalCall("verify/checks", record, [["confirmed", "Confirmed problem"], ["cleared", "Cleared"]], (updated) => {
        renderVerify(updated); loadVerifyQueue(); loadVerifyStats();
      }),
      h("p", { class: "disclaimer", text: risk.note })));
  }

  function verifyQueryString() {
    if (verifyMode === "reviewed") return "reviewed=true&action=&order=newest";
    if (verifyMode === "all") return "reviewed=any&action=&order=newest";
    return "";
  }

  async function loadVerifyQueue() {
    const body = $("verify-queue");
    try {
      const page = await api(`/verify/checks?limit=50&${verifyQueryString()}`);
      if (!page.data.length) {
        body.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "empty", text: verifyMode === "open" ? "No open flags. Nice." : "Nothing here yet." })));
        return;
      }
      body.replaceChildren(...page.data.map((row) => h("tr", {
        onclick: async (e) => {
          for (const tr of body.querySelectorAll("tr")) tr.classList.remove("on");
          e.currentTarget.classList.add("on");
          renderVerify(await api(`/verify/checks/${row.id}`));
        },
      },
      h("td", {}, row.risk_score === null ? "…" : h("span", { class: `score ${scoreClass(row.risk_score)}`, text: row.risk_score })),
      h("td", {}, h("div", { text: row.reference || row.id }), h("div", { class: "small muted", text: [row.subject_type, row.seller_id].filter(Boolean).join(" · ") })),
      h("td", { text: row.stated_value ? money(row.stated_value, row.stated_currency) : "–" }),
      h("td", {}, h("div", { class: "chips" }, (row.fired_checks || []).map((c) => h("span", { class: "chip fired", text: c.replace(/_/g, " ") })))),
      h("td", {}, row.action ? h("span", { class: `badge a-${row.action}`, text: ACTIONS[row.action] }) : "Running",
        row.review_outcome ? h("div", { class: "small muted", text: row.review_outcome }) : null))));
    } catch (err) {
      body.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "empty", text: err.message })));
    }
  }

  async function loadVerifyStats() {
    try {
      const s = await api("/verify/stats");
      const value = Object.entries(s.confirmed_stated_value).map(([cur, v]) => money(v, cur)).join(" + ") || money(0, "USD");
      statTiles($("verify-stats"), [
        ["Checks, last 30 days", s.checks], ["Flagged", s.flagged], ["Flags reviewed", s.flagged_reviewed],
        ["Flags confirmed", pct(s.flag_precision)], ["Top 5% confirmed", pct(s.top_flags.precision), true],
        ["Stated value caught", value],
      ]);
    } catch (_) { $("verify-stats").replaceChildren(); }
  }

  async function submitVerify(e) {
    e.preventDefault();
    const error = $("verify-error");
    const button = $("verify-submit");
    showError(error, null);
    try {
      const subject = { type: $("verify-type").value, external_id: $("verify-ext").value.trim() };
      if ($("verify-seller").value.trim()) subject.seller_id = $("verify-seller").value.trim();
      if ($("verify-value").value) subject.stated_value = Number($("verify-value").value);
      button.disabled = true;
      button.textContent = "Checking…";
      const record = await api("/verify/checks", { method: "POST", body: { photos: photosFor("verify"), subject }, idempotent: true });
      renderVerify(record);
      $("verify-form").reset();
      $("verify-thumbs").replaceChildren();
      files.verify = [];
      loadVerifyQueue();
      loadVerifyStats();
    } catch (err) {
      showError(error, err);
    } finally {
      button.disabled = false;
      button.textContent = "Run check";
    }
  }

  // ── Settings ───────────────────────────────────────────────────────────────

  const RULE_LABELS = {
    list_max_value: "List directly up to (high end, $)",
    min_confidence_to_list: "Minimum confidence to list (0–1)",
    lab_min_value: "Send to lab from (low end, $)",
    decline_below_value: "Decline below (high end, $)",
    lab_spread_ratio: "Lab when a report is suggested and high ≥ this × low",
    price_low_ratio: "Flag a stated value below this × the scan's low end",
    price_high_ratio: "Flag a stated value above this × the scan's high end",
    review_at: "Manual review from risk score",
    hold_at: "Hold from risk score",
    seller_lookback_days: "Seller history window (days)",
    seller_flag_count: "Earlier flags that make a seller pattern",
  };

  async function loadRules(form, fieldsId, path) {
    const values = await api(path);
    $(fieldsId).replaceChildren(...Object.entries(values).map(([name, value]) =>
      h("label", { class: "field" }, h("span", { text: RULE_LABELS[name] || name }),
        h("input", { type: "number", step: "any", name, value: value === null ? "" : value }))));
    form.hidden = false;
    form.onsubmit = async (e) => {
      e.preventDefault();
      const note = form.querySelector(".note");
      note.textContent = "";
      note.style.color = "";
      const body = {};
      for (const input of form.querySelectorAll("input")) body[input.name] = input.value === "" ? null : Number(input.value);
      try {
        await api(path, { method: "PUT", body });
        note.textContent = "Saved. New results use these.";
      } catch (err) {
        note.style.color = "var(--bad)";
        note.textContent = err.details && err.details.length ? err.details.map((d) => d.msg).join(" ") : err.message;
      }
    };
  }

  async function loadWebhook() {
    const eventsBox = $("webhook-events");
    let config = null;
    try { config = await api("/webhooks"); } catch (err) { if (err.status !== 404) throw err; }
    $("webhook-url").value = config ? config.url : "";
    eventsBox.replaceChildren(...Object.entries(EVENTS).map(([name, label]) =>
      h("label", {}, h("input", { type: "checkbox", value: name, checked: !config || config.events.includes(name) }), ` ${label}`)));
    $("webhook-test").hidden = !config || !config.active;
    loadDeliveries();
  }

  async function loadDeliveries() {
    const list = $("webhook-deliveries");
    try {
      const page = await api("/webhooks/deliveries?limit=10");
      list.replaceChildren(...(page.data.length ? page.data.map((d) => h("li", {},
        h("span", { text: `${EVENTS[d.event_type] || d.event_type} · ${when(d.created_at)}` }),
        h("span", { class: d.status === "failed" ? "error" : "muted", text: d.status === "delivered" ? `delivered (${d.last_status_code})` : `${d.status}${d.last_error ? `: ${d.last_error}` : ""}` })))
        : [h("li", { class: "muted", text: "No deliveries yet." })]));
    } catch (err) {
      list.replaceChildren(h("li", { class: "error", text: err.message }));
    }
  }

  async function saveWebhook(e) {
    e.preventDefault();
    const note = $("webhook-note");
    note.textContent = "";
    note.style.color = "";
    const events = Array.from($("webhook-events").querySelectorAll("input:checked")).map((i) => i.value);
    try {
      const config = await api("/webhooks", { method: "PUT", body: { url: $("webhook-url").value.trim(), events } });
      const secretBox = $("webhook-secret");
      if (config.secret) {
        secretBox.replaceChildren(h("strong", { text: "Signing secret (shown once): " }), config.secret);
        secretBox.hidden = false;
      }
      note.textContent = "Saved.";
      $("webhook-test").hidden = false;
    } catch (err) {
      note.style.color = "var(--bad)";
      note.textContent = err.message;
    }
  }

  async function testWebhook() {
    const note = $("webhook-note");
    note.style.color = "";
    try {
      const d = await api("/webhooks/test", { method: "POST" });
      note.textContent = d.status === "delivered" ? "Test event delivered." : `Not delivered: ${d.last_error || d.status}`;
      if (d.status !== "delivered") note.style.color = "var(--bad)";
    } catch (err) {
      note.style.color = "var(--bad)";
      note.textContent = err.message;
    }
    loadDeliveries();
  }

  function loadSettings() {
    if (ACCOUNT.scopes.includes("intake")) loadRules($("rules-intake"), "rules-intake-fields", "/intake/rules");
    if (ACCOUNT.scopes.includes("verify")) loadRules($("rules-verify"), "rules-verify-fields", "/verify/settings");
    loadWebhook().catch((err) => { $("webhook-note").textContent = err.message; });
  }

  // ── wiring ─────────────────────────────────────────────────────────────────

  $("signin-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const error = $("signin-error");
    showError(error, null);
    try {
      await signIn($("signin-key").value.trim());
    } catch (err) {
      KEY = null;
      showError(error, err.status === 401 ? new Error("That key was not recognised.") : err);
    }
  });
  $("sign-out").addEventListener("click", signOut);
  $("intake-form").addEventListener("submit", submitIntake);
  $("verify-form").addEventListener("submit", submitVerify);
  $("intake-filter").addEventListener("change", loadIntakeQueue);
  $("webhook-form").addEventListener("submit", saveWebhook);
  $("webhook-test").addEventListener("click", testWebhook);
  for (const kind of ["intake", "verify"]) {
    $(`${kind}-files`).addEventListener("change", (e) =>
      pickFiles(kind, e.target).catch((err) => showError($(`${kind}-error`), err)));
  }
  for (const btn of document.querySelectorAll(".seg button")) {
    btn.addEventListener("click", () => {
      verifyMode = btn.dataset.mode;
      for (const b of document.querySelectorAll(".seg button")) b.classList.toggle("on", b === btn);
      loadVerifyQueue();
    });
  }
  $("intake-export").addEventListener("click", (e) => {
    e.preventDefault();
    download(`/intake/items?format=csv&${intakeQueryString()}`, "intake-queue.csv").catch((err) => showError($("intake-error"), err));
  });
  $("verify-export").addEventListener("click", (e) => {
    e.preventDefault();
    download(`/verify/checks?format=csv&${verifyQueryString()}`, "verify-queue.csv").catch((err) => showError($("verify-error"), err));
  });

  const saved = store("session", "gsk");
  if (saved) signIn(saved).catch(() => { KEY = null; store("session", "gsk", null); });
})();
