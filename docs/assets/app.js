/* Break Investigator — replays recorded runs from docs/data. No framework, no build step. */
(() => {
  "use strict";

  const CAUSES = ["TIMING", "MISSED_CA", "STALE_PRICE", "FX", "FAILED_SETTLEMENT", "BOOKING_ERROR"];
  const LABEL = {
    TIMING: "Timing", MISSED_CA: "Missed corporate action", STALE_PRICE: "Stale price", FX: "FX",
    FAILED_SETTLEMENT: "Failed settlement", BOOKING_ERROR: "Booking error", UNEXPLAINED: "Unexplained",
  };
  const SHORT = {
    TIMING: "Timing", MISSED_CA: "Missed CA", STALE_PRICE: "Stale price", FX: "FX",
    FAILED_SETTLEMENT: "Failed settle", BOOKING_ERROR: "Booking error", UNEXPLAINED: "Unexplained",
  };
  const TYPE = { QTY: "Quantity", MV: "Market value", CASH: "Cash" };
  const OWNERS = ["Reconciliations (monitor only)", "Corporate Actions", "Pricing & Valuations", "Market Data - FX",
    "Settlements", "Trade Support", "Reconciliations"];
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const nf2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const S = {
    index: null, bench: null, runs: new Map(), sel: null, gen: 0, speed: 1, autoscroll: true,
    filter: { type: "ALL", cause: "ALL", q: "" }, reviews: store.get("bb.reviews", {}), started: false,
  };

  // ---------------------------------------------------------------- formatting
  function money(v, ccy = "USD") { return `${ccy} ${nf2.format(v)}`; }
  function compact(v) {
    const a = Math.abs(v), s = v < 0 ? "−" : "";
    if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(2)}M`;
    if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(1)}k`;
    return `${s}$${a.toFixed(0)}`;
  }
  function qty(v) { return nf0.format(v); }
  function cell(v) {
    if (v === null || v === undefined) return '<span class="null">null</span>';
    if (typeof v === "number") return Number.isInteger(v) ? nf0.format(v) : (Math.abs(v) < 10 ? String(+v.toFixed(8)) : nf2.format(v));
    return esc(v);
  }
  function hl(sql) {
    return esc(sql).replace(/(&#39;(?:[^&]|&(?!#39;))*?&#39;)|(\b\d+(?:\.\d+)?\b)|(\b(?:SELECT|FROM|WHERE|AND|OR|LEFT|RIGHT|INNER|JOIN|ON|AS|UNION|ALL|ORDER|BY|LIMIT|IS|NULL|NOT|IN|DESC|ASC|CASE|WHEN|THEN|ELSE|END|WITH|GROUP|HAVING|COUNT|SUM|DISTINCT|ABS)\b)/g,
      (m, str, num, kw) => str ? `<span class="str">${str}</span>` : num ? `<span class="num">${num}</span>` : `<span class="kw">${kw}</span>`);
  }
  function vname(v) {
    return String(v).replace(/^(aged_)?t\+(\d)/, (m, a, d) => `${a || ""}T+${d}`).replace(/\+secondary_/, " + secondary ").replace(/_/g, " ");
  }
  function pill(cause, text) {
    return `<span class="pill" style="--c:var(--c-${cause})">${esc(text ?? SHORT[cause] ?? cause)}</span>`;
  }

  // ---------------------------------------------------------------- theme
  function initTheme() {
    const saved = store.get("bb.theme", null);
    if (saved) document.documentElement.dataset.theme = saved;
    $("#theme").addEventListener("click", () => {
      const dark = document.documentElement.dataset.theme
        ? document.documentElement.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      const next = dark ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      store.set("bb.theme", next);
    });
    if (!saved && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      document.documentElement.dataset.theme = "dark";
    }
  }

  // ---------------------------------------------------------------- tooltip
  const tip = $("#tip");
  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    const left = Math.min(window.innerWidth - r.width - 10, Math.max(10, x + 14));
    const top = y + r.height + 24 > window.innerHeight ? y - r.height - 14 : y + 16;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  }
  function hideTip() { tip.hidden = true; }

  // ---------------------------------------------------------------- hero
  function countUp(el, to, dec = 0, suffix = "") {
    if (reduceMotion) { el.textContent = to.toFixed(dec) + suffix; return; }
    const t0 = performance.now(), dur = 1300;
    const tick = (t) => {
      const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = (to * e).toFixed(dec) + suffix;
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  function renderStats() {
    const b = S.bench;
    const vals = [[b.n, 0, ""], [b.accuracy * 100, 1, "%"], [b.misclassified, 0, ""], [b.citation_rate * 100, 0, "%"]];
    $$("#stats .stat b").forEach((el, i) => countUp(el, vals[i][0], vals[i][1], vals[i][2]));
    $$("#stats .stat span")[2].textContent = `wrong calls (${b.abstained} sent to a human)`;
  }
  function renderLegend() {
    $("#legend").innerHTML = CAUSES.map((c) => `<span><i style="--c:var(--c-${c})"></i>${LABEL[c]}</span>`).join("")
      + '<span><i class="dash"></i>Sent to analyst</span>';
  }
  function renderMosaic() {
    const m = $("#mosaic");
    m.innerHTML = S.index.breaks.map((b) => {
      const abst = b.pred === "UNEXPLAINED";
      return `<button class="tile${abst ? " abstain" : ""}" type="button" data-id="${b.id}" style="--c:var(--c-${b.pred})" aria-label="${b.id} ${LABEL[b.pred]}"></button>`;
    }).join("");
    m.addEventListener("mousemove", (e) => {
      const t = e.target.closest(".tile");
      if (!t) return hideTip();
      const b = S.index.breaks.find((x) => x.id === t.dataset.id);
      const amt = b.type === "QTY" ? `${qty(b.diff)} sh (${compact(b.usd)})` : compact(b.usd);
      const match = b.pred === b.truth ? '<span style="color:var(--ok)">match</span>' : '<span style="color:var(--warn)">sent to analyst</span>';
      showTip(`<b>${b.id}</b> · ${esc(b.account)} · ${esc(b.ticker ?? b.ccy + " cash")}<br>${TYPE[b.type]} break, ${amt}<br>Called: <b>${LABEL[b.pred]}</b> ${Math.round(b.conf * 100)}%<br>Seeded: ${LABEL[b.truth]} · ${match}`, e.clientX, e.clientY);
    });
    m.addEventListener("mouseleave", hideTip);
    m.addEventListener("click", (e) => {
      const t = e.target.closest(".tile");
      if (!t) return;
      select(t.dataset.id, true);
      $("#investigate").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
    });
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((en) => en.isIntersecting)) return;
      io.disconnect();
      $$(".tile", m).forEach((t, i) => setTimeout(() => t.classList.add("on"), reduceMotion ? 0 : 200 + i * 7));
    }, { threshold: 0.2 });
    io.observe(m);
  }
  function markTile(id) {
    $$(".tile.sel").forEach((t) => t.classList.remove("sel"));
    const t = $(`.tile[data-id="${id}"]`);
    if (t) t.classList.add("sel");
  }

  // ---------------------------------------------------------------- queue
  function renderCauseSelect() {
    const counts = {};
    S.index.breaks.forEach((b) => { counts[b.pred] = (counts[b.pred] || 0) + 1; });
    $("#causeSel").innerHTML = `<option value="ALL">All root causes</option>`
      + CAUSES.map((c) => `<option value="${c}">${LABEL[c]} (${counts[c] || 0})</option>`).join("")
      + `<option value="UNEXPLAINED">Sent to analyst (${counts.UNEXPLAINED || 0})</option>`;
  }
  function filtered() {
    const { type, cause, q } = S.filter;
    const needle = q.trim().toLowerCase();
    return S.index.breaks.filter((b) => (type === "ALL" || b.type === type)
      && (cause === "ALL" || b.pred === cause)
      && (!needle || [b.id, b.ticker, b.account, b.name].some((v) => v && String(v).toLowerCase().includes(needle))));
  }
  function renderQueue() {
    const list = filtered();
    $("#queue").innerHTML = list.map((b) => {
      const amt = b.type === "QTY" ? `${b.diff > 0 ? "+" : ""}${qty(b.diff)} sh` : compact(b.usd);
      const decided = S.reviews[b.id];
      return `<li><button class="q-item" data-id="${b.id}" aria-current="${b.id === S.sel}">
        <span class="q-main">${esc(b.ticker ?? `${b.ccy} income cash`)} <span class="muted" style="font-weight:400">· ${esc(b.account)}</span></span>
        <span class="q-amt">${amt}</span>
        <span class="q-meta"><span class="q-id">${b.id}</span>${pill(b.pred)}${decided ? `<span class="tag">${decided.action}</span>` : ""}</span>
      </button></li>`;
    }).join("") || '<li class="muted small" style="padding:16px">Nothing matches.</li>';
    $("#queueCount").textContent = `${list.length} of ${S.index.breaks.length} breaks`;
  }
  function bindQueue() {
    $("#queue").addEventListener("click", (e) => {
      const it = e.target.closest(".q-item");
      if (it) select(it.dataset.id, true);
    });
    $("#queue").addEventListener("keydown", (e) => {
      if (!["ArrowDown", "ArrowUp"].includes(e.key)) return;
      e.preventDefault();
      const items = $$(".q-item");
      const i = items.findIndex((x) => x.dataset.id === S.sel);
      const next = items[Math.max(0, Math.min(items.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next) { next.focus(); select(next.dataset.id, true); }
    });
    $("#q").addEventListener("input", (e) => { S.filter.q = e.target.value; renderQueue(); });
    $("#causeSel").addEventListener("change", (e) => { S.filter.cause = e.target.value; renderQueue(); });
    $("#typeSeg").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      $$("#typeSeg button").forEach((x) => x.setAttribute("aria-selected", x === b));
      S.filter.type = b.dataset.type;
      renderQueue();
    });
  }

  // ---------------------------------------------------------------- flow strip
  function flow(stage, opts = {}) {
    const order = ["load_break", "investigate", "check_citations", "propose_fix", "human_review", "close"];
    const idx = order.indexOf(stage);
    $$("#flow .node").forEach((n) => {
      const i = order.indexOf(n.dataset.node);
      n.classList.remove("done", "active", "wait", "reject");
      if (i < idx) n.classList.add("done");
      else if (i === idx) n.classList.add(opts.wait ? "wait" : opts.reject ? "reject" : opts.done ? "done" : "active");
    });
    $("#close-label").textContent = opts.reject ? "reopen" : "close";
  }

  // ---------------------------------------------------------------- investigation
  async function loadRun(id) {
    if (S.runs.has(id)) return S.runs.get(id);
    const r = await fetch(`data/runs/${id}.json`).then((x) => x.json());
    S.runs.set(id, r);
    return r;
  }
  function renderHead(b, run) {
    const br = run.break_row;
    let ib, cu, df, dsub = "";
    if (b.type === "QTY") {
      ib = `${qty(br.ibor_value)} sh`; cu = `${qty(br.custodian_value)} sh`;
      df = `${br.difference > 0 ? "+" : ""}${qty(br.difference)} sh`;
      dsub = `<small>${compact(br.difference_usd)}</small>`;
    } else if (b.type === "MV") {
      ib = money(br.ibor_value); cu = money(br.custodian_value); df = money(br.difference);
    } else {
      ib = money(br.ibor_value, br.currency); cu = money(br.custodian_value, br.currency); df = money(br.difference, br.currency);
    }
    const name = run.security ? `${run.security.ticker} · ${run.security.name}` : `${br.currency} income cash`;
    $("#workHead").innerHTML = `
      <div class="wh-top"><h3 class="wh-title">${esc(name)}</h3><span class="wh-sub mono">${b.id}</span></div>
      <div class="wh-sub">${esc(br.account_id)} · ${TYPE[b.type]} break · as of ${S.index.as_of}${run.security ? ` · ${esc(run.security.isin)}` : ""}</div>
      <div class="compare">
        <div class="cmp"><span>IBOR</span><b>${ib}</b></div>
        <div class="cmp"><span>Custodian</span><b>${cu}</b></div>
        <div class="cmp diff"><span>Difference</span><b>${df}</b>${dsub}</div>
      </div>`;
  }

  function citedRows(run) {
    const m = new Map();
    (run.finding.evidence || []).forEach((e) => {
      if (!m.has(e.qid)) m.set(e.qid, new Set());
      m.get(e.qid).add(e.row);
    });
    (run.finding.summary.match(/Q\d+:r\d+/g) || []).forEach((c) => {
      const [q, r] = c.split(":r");
      if (!m.has(q)) m.set(q, new Set());
      m.get(q).add(+r);
    });
    return m;
  }

  function tableHTML(q, cited) {
    if (q.error) return `<div class="err">${esc(q.error)}</div>`;
    if (!q.rows.length) return '<div class="trunc">0 rows</div>';
    const head = `<tr><th></th>${q.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>`;
    const body = q.rows.map((r, i) => `<tr data-q="${q.qid}" data-r="${i}" class="${cited && cited.has(i) ? "cited" : ""}" style="animation-delay:${Math.min(i, 10) * 35}ms">
      <td class="ri">r${i}</td>${r.map((v) => `<td class="${typeof v === "number" ? "n" : ""}">${cell(v)}</td>`).join("")}</tr>`).join("");
    return `<div class="tbl-wrap"><table class="rows">${head}${body}</table></div>${q.truncated ? `<div class="trunc">showing ${q.rows.length} of ${q.row_count} rows</div>` : ""}`;
  }

  function stepEl(kind, html) {
    const li = document.createElement("li");
    li.className = `step ${kind}`;
    li.innerHTML = `<span class="dot"></span>${html}`;
    return li;
  }

  async function typeWords(p, text, gen, sp) {
    if (sp() === Infinity) { p.textContent = text; return; }
    const words = text.split(/(\s+)/);
    p.classList.add("caret");
    let out = "";
    for (let i = 0; i < words.length; i += 2) {
      if (gen !== S.gen) return;
      out += words[i] + (words[i + 1] || "");
      p.textContent = out;
      if (i % 4 === 0) { scrollSteps(); await sleep(26 / sp()); }
    }
    p.classList.remove("caret");
  }
  async function typeSQL(pre, sql, gen, sp) {
    if (sp() === Infinity) { pre.innerHTML = hl(sql); return; }
    const step = Math.ceil(22 * sp());
    for (let i = 0; i < sql.length; i += step) {
      if (gen !== S.gen) return;
      pre.textContent = sql.slice(0, i + step);
      pre.scrollTop = pre.scrollHeight;
      await sleep(16);
    }
    pre.innerHTML = hl(sql);
  }
  function scrollSteps() {
    if (!S.autoscroll) return;
    const el = $("#steps");
    el.scrollTop = el.scrollHeight;
  }

  async function replay(id, instant = false) {
    const gen = ++S.gen;
    const b = S.index.breaks.find((x) => x.id === id);
    const run = await loadRun(id);
    if (gen !== S.gen) return;
    const sp = () => (instant || reduceMotion ? Infinity : S.speed);
    S.autoscroll = true;
    renderHead(b, run);
    const steps = $("#steps");
    steps.innerHTML = "";
    $("#finding").className = "finding empty";
    $("#finding").innerHTML = '<p class="muted">Investigating…</p>';
    const pause = (ms) => (sp() === Infinity ? Promise.resolve() : sleep(ms / sp()));
    const cited = citedRows(run);
    const qmap = new Map(run.queries.map((q) => [q.qid, q]));

    flow("load_break");
    await pause(350);
    if (gen !== S.gen) return;
    flow("investigate");

    for (const s of run.steps) {
      if (gen !== S.gen) return;
      if (s.kind === "thought") {
        const li = stepEl("thought", "<p></p>");
        steps.append(li);
        await typeWords($("p", li), s.text, gen, sp);
        await pause(260);
      } else if (s.kind === "observation") {
        const li = stepEl("obs", "<p></p>");
        steps.append(li);
        await typeWords($("p", li), s.text, gen, sp);
        await pause(380);
      } else if (s.kind === "tool") {
        const q = qmap.get(s.qid);
        const li = stepEl("tool", `<div class="qcard">
            <div class="qhead"><span class="qid">${q.qid}</span><span class="qtool">${esc(q.tool)}</span>
              <span class="qpurpose">${esc(q.purpose)}</span>
              <span class="qmeta">${q.row_count} row${q.row_count === 1 ? "" : "s"} · ${q.elapsed_ms} ms</span>
              <button class="qsql-toggle" type="button" aria-expanded="true">SQL</button></div>
            <pre class="sql"></pre><div class="qres"></div></div>`);
        steps.append(li);
        scrollSteps();
        const pre = $("pre", li);
        await typeSQL(pre, q.sql, gen, sp);
        if (gen !== S.gen) return;
        $(".qres", li).innerHTML = tableHTML(q, cited.get(q.qid));
        scrollSteps();
        await pause(420);
        if (sp() === Infinity || q.sql.length > 160) {
          pre.hidden = true;
          $(".qsql-toggle", li).setAttribute("aria-expanded", "false");
        }
      }
      scrollSteps();
    }
    if (gen !== S.gen) return;

    // hypothesis scoreboard
    const hyps = run.finding.hypotheses || [];
    if (hyps.length) {
      const li = stepEl("obs", `<div class="hyps">${hyps.map((h) => `<div class="hyp" style="--c:var(--c-${h.cause})">
          <span>${LABEL[h.cause]}</span><span class="bar"><i data-w="${Math.round(h.fit * 100)}"></i></span><b>${Math.round(h.fit * 100)}%</b></div>`).join("")}</div>`);
      li.classList.add("hyp-step");
      steps.append(li);
      scrollSteps();
      requestAnimationFrame(() => requestAnimationFrame(() => $$("i[data-w]", li).forEach((i) => { i.style.width = `${i.dataset.w}%`; })));
      await pause(900);
    }
    if (gen !== S.gen) return;

    flow("check_citations");
    const cc = run.citation_check;
    const okc = cc.ok;
    steps.append(stepEl("obs", `<div class="cite-check ${okc ? "" : "bad"}">
      <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="${okc ? "M5 12.5 10 17l9-10" : "M12 7v6m0 4h.01"}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
      citation check: ${cc.evidence_items} evidence rows, ${cc.inline_citations} inline cites, ${cc.dangling.length} dangling</div>`));
    scrollSteps();
    await pause(650);
    if (gen !== S.gen) return;

    flow("propose_fix");
    renderFinding(id, run, true);
    await pause(700);
    if (gen !== S.gen) return;
    applyReviewFlow(id);
  }

  function applyReviewFlow(id) {
    const d = S.reviews[id];
    if (!d) return flow("human_review", { wait: true });
    flow("close", d.action === "reject" ? { reject: true } : { done: true });
  }

  function summaryHTML(text) {
    return esc(text).replace(/\[((?:Q\d+:r\d+)(?:,\s*Q\d+:r\d+)*)\]/g, (m, inner) =>
      inner.split(/,\s*/).map((c) => `<button class="cite" type="button" data-cite="${c}">${c.replace(":", "·")}</button>`).join(""));
  }

  function renderFinding(id, run, animate) {
    const f = run.finding;
    const el = $("#finding");
    el.className = "finding";
    const c = f.root_cause;
    const R = 25, C = 2 * Math.PI * R;
    const match = c === run.label.root_cause;
    el.innerHTML = `
      <p class="f-label">Root cause</p>
      <div class="f-cause" style="--c:var(--c-${c})">
        <div class="ring" role="img" aria-label="confidence ${Math.round(f.confidence * 100)} percent">
          <svg width="58" height="58" viewBox="0 0 58 58"><circle class="bg" cx="29" cy="29" r="${R}"/>
          <circle class="fg" cx="29" cy="29" r="${R}" stroke-dasharray="${C}" stroke-dashoffset="${animate && !reduceMotion ? C : C * (1 - f.confidence)}" data-off="${C * (1 - f.confidence)}"/></svg>
          <b>${Math.round(f.confidence * 100)}%</b>
        </div>
        <div><h3>${LABEL[c]}</h3><div class="muted small">${esc(f.owner_team)}</div></div>
      </div>
      <p class="f-summary">${summaryHTML(f.summary)}</p>
      <div class="f-block"><h4>Proposed fix</h4><p>${esc(f.proposed_fix)}</p></div>
      ${(run.needs_attention || []).length ? `<div class="f-block" style="background:var(--warn-soft)"><h4 style="color:var(--warn)">Flagged for attention</h4><p>${run.needs_attention.map(esc).join(", ").replace(/_/g, " ")}</p></div>` : ""}
      <div class="truth">Seeded label: ${pill(run.label.root_cause, LABEL[run.label.root_cause])}
        <span class="tag">${esc(vname(run.label.variant))}</span>
        ${match ? '<span class="ok">match</span>' : '<span class="no">agent abstained</span>'}</div>
      <div class="review" id="review"></div>
      <div class="audit"><h4>Audit trail</h4><ol id="auditList"></ol></div>`;
    if (animate && !reduceMotion) requestAnimationFrame(() => requestAnimationFrame(() => {
      const fg = $(".ring .fg", el);
      if (fg) fg.style.strokeDashoffset = fg.dataset.off;
    }));
    renderReview(id, run);
    renderAudit(id, run);
  }

  function renderReview(id, run) {
    const box = $("#review");
    const d = S.reviews[id];
    const f = run.finding;
    if (d) {
      const reject = d.action === "reject";
      const outcome = reject ? "Reopened for manual investigation"
        : (d.root_cause === "TIMING" ? "Closed, no action needed" : `Assigned to ${d.owner_team}`);
      box.innerHTML = `<div class="status ${reject ? "bad" : "ok"}">
          <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path d="${reject ? "M6 6l12 12M18 6 6 18" : "M5 12.5 10 17l9-10"}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>
          <div>${d.action === "edit" ? "Edited and approved" : reject ? "Rejected" : "Approved"} · ${esc(outcome)}
          <small>${d.action === "edit" ? `Root cause set to ${esc(LABEL[d.root_cause])}. ` : ""}${d.note ? `“${esc(d.note)}” · ` : ""}${new Date(d.at).toLocaleString()}</small></div></div>
        <p class="small" style="margin:10px 0 0"><button class="btn sm" type="button" id="undo">Undo decision</button></p>`;
      box.onclick = null;
      $("#undo").addEventListener("click", () => { delete S.reviews[id]; store.set("bb.reviews", S.reviews); renderReview(id, run); renderAudit(id, run); applyReviewFlow(id); renderQueue(); });
      return;
    }
    box.innerHTML = `<p class="f-label">Your call</p>
      <p class="small muted" style="margin:0">The run is parked at <span class="mono">human_review</span>. Nothing closes until someone decides.</p>
      <div class="review-actions">
        <button class="btn ok" type="button" data-act="approve">Approve</button>
        <button class="btn" type="button" data-act="edit">Edit</button>
        <button class="btn danger" type="button" data-act="reject">Reject</button>
      </div>
      <div id="editSlot"></div>`;
    box.onclick = (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "edit") return openEdit(id, run);
      decide(id, run, { action: act, root_cause: f.root_cause, owner_team: f.owner_team, proposed_fix: f.proposed_fix, note: "" });
    };
  }

  function openEdit(id, run) {
    const f = run.finding;
    const owners = [...new Set([f.owner_team, ...OWNERS])];
    $("#editSlot").innerHTML = `<form class="edit-form" id="editForm">
      <label>Root cause<select name="root_cause">${[...CAUSES, "UNEXPLAINED"].map((c) => `<option value="${c}" ${c === f.root_cause ? "selected" : ""}>${LABEL[c]}</option>`).join("")}</select></label>
      <label>Owner team<select name="owner_team">${owners.map((o) => `<option ${o === f.owner_team ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>
      <label>Fix<textarea name="proposed_fix" rows="3">${esc(f.proposed_fix)}</textarea></label>
      <label>Note<input name="note" placeholder="Why you changed it"></label>
      <div class="review-actions" style="grid-template-columns:1fr 1fr"><button class="btn primary" type="submit">Save and approve</button><button class="btn" type="button" id="cancelEdit">Cancel</button></div>
    </form>`;
    $("#editForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      decide(id, run, { action: "edit", root_cause: fd.get("root_cause"), owner_team: fd.get("owner_team"), proposed_fix: fd.get("proposed_fix"), note: fd.get("note") });
    });
    $("#cancelEdit").addEventListener("click", () => renderReview(id, run));
  }

  function decide(id, run, d) {
    S.reviews[id] = { ...d, at: new Date().toISOString() };
    store.set("bb.reviews", S.reviews);
    renderReview(id, run);
    renderAudit(id, run);
    applyReviewFlow(id);
    renderQueue();
  }

  function renderAudit(id, run) {
    const t0 = run.audit.length ? new Date(run.audit[0].ts).getTime() : 0;
    const rows = run.audit.map((a) => {
      const dt = ((new Date(a.ts).getTime() - t0) / 1000).toFixed(3);
      let what = `${a.node} · ${a.event}`;
      if (a.node === "tool" && a.payload) what = `query ${a.payload.qid} ${a.payload.tool} (${a.payload.rows} row${a.payload.rows === 1 ? "" : "s"})`;
      if (a.node === "investigate" && a.payload) what = `finding ${a.payload.root_cause} ${Math.round(a.payload.confidence * 100)}%`;
      return `<li><span>+${dt}s</span><span>${esc(what)}</span></li>`;
    });
    const d = S.reviews[id];
    if (d) {
      rows.push(`<li class="you"><span>you</span><span>human_review · ${d.action}${d.action === "edit" ? ` → ${d.root_cause}` : ""}</span></li>`);
      rows.push(`<li class="you"><span>you</span><span>${d.action === "reject" ? "reopen · rejected_by_analyst" : `close · ${d.root_cause === "TIMING" ? "CLOSED_NO_ACTION" : "ASSIGNED:" + esc(d.owner_team)}`}</span></li>`);
    } else {
      rows.push('<li><span>…</span><span>human_review · waiting</span></li>');
    }
    $("#auditList").innerHTML = rows.join("");
  }

  function bindCitations() {
    const find = (c) => {
      const [q, r] = c.split(":r");
      return $(`#steps tr[data-q="${q}"][data-r="${r}"]`);
    };
    const reveal = (tr) => {
      const card = tr.closest(".qcard");
      tr.classList.remove("flash");
      void tr.offsetWidth;
      tr.classList.add("flash");
      return card;
    };
    $("#finding").addEventListener("mouseover", (e) => {
      const c = e.target.closest(".cite");
      if (!c) return;
      const tr = find(c.dataset.cite);
      if (tr) reveal(tr);
    });
    $("#finding").addEventListener("click", (e) => {
      const c = e.target.closest(".cite");
      if (!c) return;
      const tr = find(c.dataset.cite);
      if (!tr) return;
      const card = reveal(tr);
      const steps = $("#steps");
      S.autoscroll = false;
      const top = card.getBoundingClientRect().top - steps.getBoundingClientRect().top + steps.scrollTop - 12;
      steps.scrollTo({ top, behavior: reduceMotion ? "auto" : "smooth" });
      const wrap = tr.closest(".tbl-wrap");
      if (wrap) wrap.scrollTop = tr.offsetTop - 30;
      if (window.innerWidth < 1180) $(".work-pane").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    });
    $("#steps").addEventListener("click", (e) => {
      const t = e.target.closest(".qsql-toggle");
      if (!t) return;
      const pre = $("pre.sql", t.closest(".qcard"));
      pre.hidden = !pre.hidden;
      if (!pre.hidden && !pre.querySelector("span")) pre.innerHTML = hl(pre.textContent);
      t.setAttribute("aria-expanded", String(!pre.hidden));
    });
    ["wheel", "touchmove"].forEach((ev) => $("#steps").addEventListener(ev, () => { S.autoscroll = false; }, { passive: true }));
  }

  function select(id, play = true) {
    S.sel = id;
    $$(".q-item").forEach((x) => x.setAttribute("aria-current", String(x.dataset.id === id)));
    markTile(id);
    const it = $(`.q-item[data-id="${id}"]`);
    if (it) {
      const q = $("#queue");
      const r = it.getBoundingClientRect(), qr = q.getBoundingClientRect();
      if (r.top < qr.top || r.bottom > qr.bottom) q.scrollTop += r.top - qr.top - 60;
    }
    replay(id, !play);
  }

  function bindControls() {
    $("#replay").addEventListener("click", () => S.sel && replay(S.sel));
    $("#skip").addEventListener("click", () => S.sel && replay(S.sel, true));
    $("#speed").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      $$("#speed button").forEach((x) => x.setAttribute("aria-selected", x === b));
      S.speed = +b.dataset.speed;
    });
  }

  // ---------------------------------------------------------------- benchmark
  function renderBench() {
    const m = S.bench;
    const per = m.per_class;
    const cm = m.confusion;
    const cols = [...CAUSES, "UNEXPLAINED"];
    const head = { ...SHORT, UNEXPLAINED: "Abstain" };
    const maxv = Math.max(...CAUSES.map((c) => per[c].support));
    const pct = (x) => `${(x * 100).toFixed(1)}%`;
    const misses = m.by_variant.filter((v) => v.correct < v.n);
    $("#bench").innerHTML = `
      <div class="card">
        <p class="f-label">Matched the seeded root cause</p>
        <div class="big">${pct(m.accuracy)}</div>
        <p class="muted" style="margin:10px 0 0">${m.correct} of ${m.n} breaks. <b style="color:var(--text)">${m.misclassified} wrong calls.</b> ${m.abstained} came back <i>unexplained</i> and went to an analyst instead of getting a guess.</p>
        <div class="kpis">
          <div class="kpi"><b>${m.citation_ok}/${m.n}</b><span>findings with valid row citations</span></div>
          <div class="kpi"><b>${m.avg_queries}</b><span>read-only queries per break</span></div>
          <div class="kpi"><b>${pct(m.accuracy_when_answered)}</b><span>accuracy when it answered</span></div>
        </div>
        <div class="cls">${CAUSES.map((c) => `<div class="cls-row" style="--c:var(--c-${c})">
            <span>${LABEL[c]} <span class="muted">· ${per[c].support}</span></span>
            <span class="bar"><i data-w="${per[c].recall * 100}"></i></span><b>${Math.round(per[c].recall * 100)}%</b></div>`).join("")}</div>
        <p class="note">Recall by root cause. Precision is 100% across the board: every time it named a cause, it was the right one.</p>
      </div>
      <div class="card">
        <p class="f-label">Confusion matrix</p>
        <p class="small muted" style="margin:0">Rows are the seeded label, columns are what the agent called.</p>
        <div class="cm" id="cm">
          <span></span>${cols.map((c) => `<span class="hd">${head[c]}</span>`).join("")}
          ${CAUSES.map((t) => `<span class="rh">${SHORT[t]}</span>${cols.map((p) => {
            const v = (cm[t] || {})[p] || 0;
            const diag = t === p, abst = p === "UNEXPLAINED";
            const a = v ? (diag ? 35 + 65 * v / maxv : abst ? 30 : 75) : 0;
            const col = diag ? `var(--c-${t})` : abst ? "var(--c-UNEXPLAINED)" : "var(--bad)";
            const bg = v ? `color-mix(in srgb, ${col} ${a}%, transparent)` : "";
            return `<span class="cell ${v ? (abst ? "abst" : "hit") : ""}" data-t="${t}" data-p="${p}" data-v="${v}" style="${bg ? `background:${bg}` : ""}">${v || "·"}</span>`;
          }).join("")}`).join("")}
        </div>
        <p class="f-label" style="margin-top:22px">By scenario</p>
        <div class="variants">${CAUSES.map((c) => `<div class="vgroup" style="--c:var(--c-${c})"><h5><i></i>${LABEL[c]}</h5>
            ${m.by_variant.filter((v) => v.cause === c).map((v) => `<div class="var ${v.correct < v.n ? "miss" : ""}">
              <span>${esc(vname(v.variant))}</span><b>${v.correct}/${v.n}</b></div>`).join("")}</div>`).join("")}</div>
        <p class="note">The ${m.abstained} abstentions are the scenarios a fixed playbook can't see: ${misses.map((v) => esc(vname(v.variant))).join(", ")}. These numbers are from the deterministic investigator, which runs without an API key. The Claude investigator plugs into the same graph and the same benchmark (<code>--mode llm</code>) and can write its own SQL to go after exactly these cases.</p>
      </div>`;
    const cmEl = $("#cm");
    cmEl.addEventListener("mousemove", (e) => {
      const c = e.target.closest(".cell");
      if (!c) return hideTip();
      showTip(`Seeded <b>${LABEL[c.dataset.t]}</b><br>called <b>${LABEL[c.dataset.p]}</b>: ${c.dataset.v}`, e.clientX, e.clientY);
    });
    cmEl.addEventListener("mouseleave", hideTip);
    const io = new IntersectionObserver((en) => {
      if (!en.some((x) => x.isIntersecting)) return;
      io.disconnect();
      $$("#bench i[data-w]").forEach((i, k) => setTimeout(() => { i.style.width = `${i.dataset.w}%`; }, k * 90));
    }, { threshold: 0.25 });
    io.observe($("#bench"));
  }

  function renderSchema(tables, refusals) {
    $("#tables").innerHTML = tables.map((t) => `<div class="tbl ${t.agent_access ? "" : "locked"}">
      <b><span>${esc(t.name)}</span><span>${t.agent_access ? nf0.format(t.rows) : "no access"}</span></b>
      <span>${t.agent_access ? `${t.columns.length} columns` : "answer key · not on the allow-list · not granted to the agent role"}</span></div>`).join("");
    $("#refusals").innerHTML = refusals.map((r) => `<div class="ref"><code>${esc(r.sql)}</code><span>${esc(r.error)}</span></div>`).join("");
  }

  // ---------------------------------------------------------------- boot
  function reveal() {
    const els = $$(".reveal");
    if (reduceMotion || !("IntersectionObserver" in window)) { els.forEach((e) => e.classList.add("in")); return; }
    const io = new IntersectionObserver((en) => en.forEach((x) => { if (x.isIntersecting) { x.target.classList.add("in"); io.unobserve(x.target); } }), { threshold: 0.08 });
    els.forEach((e) => io.observe(e));
  }

  function pickDefault() {
    const pref = ["partial", "wrong_ratio", "fat_finger", "restamped_prior_close", "dividend_not_booked"];
    for (const v of pref) {
      const b = S.index.breaks.find((x) => x.variant === v && x.pred === x.truth);
      if (b) return b.id;
    }
    return S.index.breaks[0].id;
  }

  async function boot() {
    initTheme();
    reveal();
    try {
      const [index, bench, schema, guard] = await Promise.all(
        ["data/index.json", "data/benchmark.json", "data/schema.json", "data/guard_examples.json"].map((u) => fetch(u).then((r) => {
          if (!r.ok) throw new Error(`${u}: ${r.status}`);
          return r.json();
        })));
      S.index = index; S.bench = bench;
      renderStats();
      renderLegend();
      renderMosaic();
      renderCauseSelect();
      renderQueue();
      bindQueue();
      bindControls();
      bindCitations();
      renderBench();
      renderSchema(schema, guard);
      S.sel = pickDefault();
      renderQueue();
      markTile(S.sel);
      const io = new IntersectionObserver((en) => {
        if (!en.some((x) => x.isIntersecting) || S.started) return;
        S.started = true;
        io.disconnect();
        select(S.sel, true);
      }, { threshold: 0.25 });
      io.observe($(".app"));
    } catch (err) {
      console.error(err);
      $("#steps").innerHTML = `<li class="err">Couldn't load the recorded runs (${esc(err.message)}).</li>`;
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
