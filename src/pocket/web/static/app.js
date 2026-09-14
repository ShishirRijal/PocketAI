// Pocket dashboard. No build step, no dependencies: fetch + SVG.
// Every chart/tile/table is driven by the same filter state, which lives in the URL.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const SVGNS = "http://www.w3.org/2000/svg";

const EXP = { JPY: 0, KRW: 0, ISK: 0, BHD: 3, KWD: 3, OMR: 3, TND: 3 };
const DEFAULTS = {
  period: "this_month", start: "", end: "", direction: "expense", q: "", category: "",
  merchant: "", tag: "", min: "", max: "", currency: "", low: "", deleted: "",
};

const state = {
  tab: "overview",
  f: { ...DEFAULTS },
  sort: "occurred_at",
  order: "desc",
  page: 1,
  me: null,
  facets: null,
  summary: null,
  tables: {},
  showTable: new Set(),
};

// ------------------------------------------------------------------ utils

function el(tag, attrs = {}, ...children) {
  const node = tag.startsWith("svg:") ? document.createElementNS(SVGNS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.setAttribute("class", v);
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function money(minor, cur, opts = {}) {
  const e = EXP[cur] ?? 2;
  const v = minor / 10 ** e;
  try {
    const digits = opts.compact
      ? { notation: "compact", minimumFractionDigits: 0, maximumFractionDigits: 1 }
      : { minimumFractionDigits: opts.whole ? 0 : e, maximumFractionDigits: opts.whole ? 0 : e };
    return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, ...digits }).format(v);
  } catch {
    return `${v.toFixed(e)} ${cur}`;
  }
}
const base = () => state.me?.base_currency || "EUR";
const m = (minor, o) => money(minor, base(), o);

function fmtDate(iso, withTime = false) {
  const d = new Date(iso);
  const opts = { month: "short", day: "numeric", timeZone: state.me?.timezone };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return new Intl.DateTimeFormat(undefined, opts).format(d);
}

// round tick step (1/2/2.5/5 × 10^n) so labels read €500, €1K, €1.5K… never €1.3K
function niceScale(v, target = 4) {
  if (v <= 0) return { max: 1, step: 0.25 };
  const raw = v / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  return { max: step * Math.ceil(v / step), step };
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2400);
}

// ------------------------------------------------------------------ api

function params(extra = {}) {
  const f = state.f;
  const p = new URLSearchParams();
  if (f.period === "custom") {
    if (f.start) p.set("start", f.start);
    if (f.end) p.set("end", f.end);
  } else p.set("period", f.period);
  p.set("direction", f.direction);
  for (const k of ["q", "category", "merchant", "tag", "currency"]) if (f[k]) p.set(k, f[k]);
  if (f.min) p.set("min_amount", f.min);
  if (f.max) p.set("max_amount", f.max);
  if (f.low) p.set("low_confidence", "true");
  if (f.deleted) p.set("deleted", "true");
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p;
}

async function api(path, { method = "GET", query, body } = {}) {
  const url = `/api/v1${path}${query ? `?${query}` : ""}`;
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    location.href = "/app/login";
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

// ------------------------------------------------------------------ url state

function readUrl() {
  const p = new URLSearchParams(location.search);
  for (const k of Object.keys(DEFAULTS)) if (p.has(k)) state.f[k] = p.get(k);
  if (p.has("tab")) state.tab = p.get("tab");
  if (p.has("sort")) state.sort = p.get("sort");
  if (p.has("order")) state.order = p.get("order");
}

function writeUrl() {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(state.f)) if (v && v !== DEFAULTS[k]) p.set(k, v);
  if (state.tab !== "overview") p.set("tab", state.tab);
  if (state.sort !== "occurred_at") p.set("sort", state.sort);
  if (state.order !== "desc") p.set("order", state.order);
  const q = p.toString();
  history.replaceState(null, "", q ? `/app?${q}` : "/app");
}

// ------------------------------------------------------------------ tooltip

const tip = $("#tooltip");
function showTip(evt, rows) {
  tip.replaceChildren(...rows);
  tip.hidden = false;
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
const hideTip = () => (tip.hidden = true);
const tipValue = (value, label) => [el("div", { class: "v", text: value }), el("div", { class: "k", text: label })];

// ------------------------------------------------------------------ charts

function columnChart(host, points, { label, sub, tooltip, onClick, avg, height = 220, series = null, legend = null, fmtTick = null, fmtValue = null } = {}) {
  const fv = fmtValue || ((v) => m(v));
  const ft = fmtTick || ((v) => m(v, { compact: true }));
  host.replaceChildren();
  if (!points.length || points.every((p) => (series ? series.every((s) => !p[s.key]) : !p.value))) {
    host.append(el("div", { class: "empty", text: "Nothing in this range" }));
    return;
  }
  const keys = series || [{ key: "value", cls: "" }];
  if (legend) {
    host.append(
      el("div", { class: "legend" }, legend.map((l) => el("span", {}, el("span", { class: "key", style: `background:var(${l.color})` }), l.label))),
    );
  }
  const W = Math.max(280, host.clientWidth);
  const M = { t: 10, r: 8, b: 26, l: 52 };
  const iw = W - M.l - M.r;
  const ih = height - M.t - M.b;
  const { max, step } = niceScale(Math.max(...points.flatMap((p) => keys.map((s) => p[s.key] || 0))));
  const band = iw / points.length;
  const group = keys.length;
  const bw = Math.max(2, Math.min(24, (band * 0.72) / group));
  const svg = el("svg:svg", { viewBox: `0 0 ${W} ${height}`, role: "img", "aria-label": host.dataset.label || "chart" });

  for (let v = 0; v <= max + step / 2; v += step) {
    const y = M.t + ih - (v / max) * ih;
    svg.append(el("svg:line", { class: v === 0 ? "baseline" : "gridline", x1: M.l, x2: W - M.r, y1: y, y2: y }));
    svg.append(el("svg:text", { class: "tick", x: M.l - 8, y: y + 4, "text-anchor": "end", text: ft(v) }));
  }
  if (avg) {
    const y = M.t + ih - (avg / max) * ih;
    svg.append(el("svg:line", { class: "avg-line", x1: M.l, x2: W - M.r, y1: y, y2: y }));
    svg.append(el("svg:text", { class: "avg-label", x: W - M.r, y: y - 4, "text-anchor": "end", text: `avg ${m(avg, { whole: true })}` }));
  }
  const every = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(iw / 64))));
  points.forEach((p, i) => {
    const x0 = M.l + i * band;
    const bars = [];
    keys.forEach((s, j) => {
      const v = p[s.key] || 0;
      const h = (v / max) * ih;
      const x = x0 + (band - bw * group - (group - 1) * 2) / 2 + j * (bw + 2);
      const y = M.t + ih - h;
      const r = Math.min(4, bw / 2, h);
      // rounded data end, square at the baseline
      const d = h <= 0 ? "" : `M${x},${M.t + ih}V${y + r}Q${x},${y} ${x + r},${y}H${x + bw - r}Q${x + bw},${y} ${x + bw},${y + r}V${M.t + ih}Z`;
      const bar = el("svg:path", { class: `bar ${s.cls || ""}`, d });
      bars.push(bar);
      svg.append(bar);
    });
    const hit = el("svg:rect", {
      class: "hit", x: x0, y: M.t, width: band, height: ih, tabindex: 0,
      "aria-label": `${label(p)}: ${fv(p.value ?? 0)}`,
    });
    const enter = (e) => {
      bars.forEach((b) => b.classList.add("hover"));
      const pt = e.clientX ? e : { clientX: hit.getBoundingClientRect().x + band / 2, clientY: hit.getBoundingClientRect().y };
      showTip(pt, tooltip ? tooltip(p) : tipValue(fv(p.value), `${label(p)}${sub ? ` · ${sub(p)}` : ""}`));
    };
    const leave = () => { bars.forEach((b) => b.classList.remove("hover")); hideTip(); };
    hit.addEventListener("pointermove", enter);
    hit.addEventListener("focus", enter);
    hit.addEventListener("pointerleave", leave);
    hit.addEventListener("blur", leave);
    if (onClick) {
      hit.addEventListener("click", () => onClick(p));
      hit.addEventListener("keydown", (e) => e.key === "Enter" && onClick(p));
    }
    svg.append(hit);
    if (i % every === 0) {
      svg.append(el("svg:text", { class: "tick", x: x0 + band / 2, y: height - 8, "text-anchor": "middle", text: p.short || label(p) }));
    }
  });
  host.append(svg);
}

function hbars(host, items, { onClick, active, max: maxItems = 8, total } = {}) {
  host.replaceChildren();
  if (!items.length) {
    host.append(el("div", { class: "empty", text: "Nothing in this range" }));
    return;
  }
  let rows = items;
  if (items.length > maxItems) {
    const rest = items.slice(maxItems - 1);
    rows = [...items.slice(0, maxItems - 1), { name: `Other (${rest.length})`, total_minor: rest.reduce((a, b) => a + b.total_minor, 0), count: rest.reduce((a, b) => a + b.count, 0), other: true }];
  }
  const max = Math.max(...rows.map((r) => r.total_minor));
  const sum = total ?? items.reduce((a, b) => a + b.total_minor, 0);
  host.append(
    el("div", { class: "hbars" }, rows.map((r) => {
      const share = sum ? Math.round((100 * r.total_minor) / sum) : 0;
      return el("button", {
        class: `hbar${active && active === String(r.id ?? r.name) ? " active" : ""}`,
        type: "button",
        disabled: r.other || !onClick,
        onclick: () => onClick && !r.other && onClick(r),
        onpointermove: (e) => showTip(e, tipValue(m(r.total_minor), `${r.name} · ${r.count} txn · ${share}%`)),
        onpointerleave: hideTip,
      },
      el("span", { class: "name", text: r.name, title: r.name }),
      el("span", { class: "track" },
        el("span", { class: "fill", style: `width:${Math.max(1, (70 * r.total_minor) / max)}%` }),
        el("span", { class: "amt", text: m(r.total_minor) }),
        el("span", { class: "share", text: `${share}%` }),
      ));
    })),
  );
}

function dataTable(host, cols, rows) {
  host.replaceChildren(
    el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, cols.map((c) => el("th", { class: c.num ? "num" : "", text: c.label })))),
      el("tbody", {}, rows.map((r) => el("tr", {}, cols.map((c) => el("td", { class: c.num ? "num" : "", text: c.get(r) }))))),
    ),
  );
}

function withTable(key, host, draw, cols, rows) {
  state.tables[key] = { cols, rows };
  if (state.showTable.has(key)) dataTable(host, cols, rows);
  else draw();
}

// ------------------------------------------------------------------ overview

function deltaSpan(cur, prev, upIsGood) {
  if (!prev) return el("span", { text: "no data for the previous period" });
  const pct = Math.round(((cur - prev) / prev) * 100);
  if (pct === 0) return el("span", { text: "same as previous period" });
  const good = pct > 0 === upIsGood;
  return el("span", {},
    el("span", { class: good ? "good" : "bad", text: `${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}%` }),
    ` vs previous (${m(prev, { whole: true })})`);
}

function tile(label, value, delta, hero = false) {
  return el("div", { class: `tile${hero ? " hero" : ""}` },
    el("div", { class: "label", text: label }),
    el("div", { class: "value", text: value, title: value }),
    delta ? el("div", { class: "delta" }, delta) : null);
}

function renderTiles(s) {
  const dir = s.direction;
  const heroLabel = dir === "income" ? "Income" : dir === "all" ? "Total moved" : "Spent";
  const upIsGood = dir === "income";
  $("#tiles").replaceChildren(
    tile(`${heroLabel} · ${s.range.label}`, m(s.total_minor), deltaSpan(s.total_minor, s.previous.total_minor, upIsGood), true),
    tile("Income", m(s.income_minor), deltaSpan(s.income_minor, s.previous.income_minor, true)),
    tile("Net", m(s.net_minor), el("span", { text: s.net_minor >= 0 ? "saved this period" : "more out than in" })),
    tile("Transactions", s.count.toLocaleString(), el("span", { text: `avg ${m(s.avg_per_txn_minor)} each` })),
    tile("Per day", m(s.avg_per_day_minor), el("span", { text: `over ${s.range.days} day${s.range.days === 1 ? "" : "s"}` })),
  );
}

function renderOverview() {
  const s = state.summary;
  if (!s) return;
  renderTiles(s);
  const cur = base();

  const daily = s.series.granularity === "day";
  $("#series-title").textContent = `${s.direction === "income" ? "Income" : "Spending"} per ${daily ? "day" : "week"}`;
  const pts = s.series.points.map((p) => ({
    value: p.total_minor,
    date: p.date,
    short: new Intl.DateTimeFormat(undefined, daily ? { day: "numeric", month: "short" } : { day: "numeric", month: "short" }).format(new Date(`${p.date}T12:00`)),
  }));
  const nonZero = pts.filter((p) => p.value);
  const avg = nonZero.length > 2 ? Math.round(pts.reduce((a, b) => a + b.value, 0) / pts.length) : null;
  withTable("series", $("#chart-series"),
    () => columnChart($("#chart-series"), pts, {
      label: (p) => new Intl.DateTimeFormat(undefined, { weekday: daily ? "short" : undefined, day: "numeric", month: "short", year: "numeric" }).format(new Date(`${p.date}T12:00`)) + (daily ? "" : " (week)"),
      avg,
      onClick: daily ? (p) => { setFilters({ period: "custom", start: p.date, end: p.date }); switchTab("transactions"); } : null,
    }),
    [{ label: daily ? "Day" : "Week of", get: (r) => r.date }, { label: "Amount", num: true, get: (r) => m(r.value) }], pts);

  withTable("category", $("#chart-category"),
    () => hbars($("#chart-category"), s.by_category, {
      active: state.f.category,
      total: s.total_minor,
      onClick: (r) => setFilters({ category: state.f.category === String(r.id) ? "" : String(r.id ?? "") }),
    }),
    [{ label: "Category", get: (r) => r.name }, { label: "Count", num: true, get: (r) => r.count }, { label: "Amount", num: true, get: (r) => m(r.total_minor) }], s.by_category);

  withTable("merchant", $("#chart-merchant"),
    () => hbars($("#chart-merchant"), s.by_merchant, {
      active: state.f.merchant,
      onClick: (r) => setFilters({ merchant: state.f.merchant === r.name ? "" : r.name }),
    }),
    [{ label: "Merchant", get: (r) => r.name }, { label: "Count", num: true, get: (r) => r.count }, { label: "Amount", num: true, get: (r) => m(r.total_minor) }], s.by_merchant);

  const wd = s.by_weekday.map((d) => ({ value: d.total_minor, day: d.day }));
  withTable("weekday", $("#chart-weekday"),
    () => columnChart($("#chart-weekday"), wd, { label: (p) => p.day, height: 180 }),
    [{ label: "Day", get: (r) => r.day }, { label: "Amount", num: true, get: (r) => m(r.value) }], wd);

  const months = s.months.map((x) => ({
    ...x,
    short: new Intl.DateTimeFormat(undefined, { month: "short" }).format(new Date(`${x.month}-15T12:00`)),
  }));
  const monthLabel = (p) => new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(new Date(`${p.month}-15T12:00`));
  withTable("months", $("#chart-months"),
    () => columnChart($("#chart-months"), months, {
      label: monthLabel,
      series: [{ key: "expense", cls: "" }, { key: "income", cls: "s2" }],
      legend: [{ label: "Spending", color: "--series-1" }, { label: "Income", color: "--series-2" }],
      tooltip: (p) => [
        el("div", { class: "k", text: monthLabel(p) }),
        el("div", { class: "row" }, el("span", { class: "line-key", style: "background:var(--series-1)" }), el("span", { class: "v", text: m(p.expense) }), el("span", { class: "k", text: "spent" })),
        el("div", { class: "row" }, el("span", { class: "line-key", style: "background:var(--series-2)" }), el("span", { class: "v", text: m(p.income) }), el("span", { class: "k", text: "income" })),
      ],
    }),
    [{ label: "Month", get: monthLabel }, { label: "Spending", num: true, get: (r) => m(r.expense) }, { label: "Income", num: true, get: (r) => m(r.income) }, { label: "Net", num: true, get: (r) => m(r.income - r.expense) }], months);

  const tags = $("#chart-tags");
  tags.replaceChildren(
    ...(s.by_tag.length
      ? s.by_tag.map((t) => el("button", { type: "button", onclick: () => setFilters({ tag: state.f.tag === t.name ? "" : t.name }) },
          `#${t.name}`, el("small", { text: m(t.total_minor, { whole: true }) })))
      : [el("div", { class: "empty", text: "No tags yet" })]),
  );

  const largest = $("#chart-largest");
  largest.replaceChildren(
    ...(s.largest.length
      ? s.largest.map((t) => el("li", { onclick: () => openDrawer(t.id), tabindex: 0, onkeydown: (e) => e.key === "Enter" && openDrawer(t.id) },
          el("div", {}, el("div", { text: t.merchant || t.category || "—" }), el("div", { class: "sub", text: `${t.category || "Uncategorized"} · ${fmtDate(t.occurred_at)}` })),
          el("div", { class: "amt", text: money(t.amount_minor, t.currency) })))
      : [el("div", { class: "empty", text: "Nothing yet" })]),
  );
}

// ------------------------------------------------------------------ transactions

async function loadTransactions() {
  const data = await api("/transactions", { query: params({ sort: state.sort, order: state.order, page: state.page, page_size: 50 }) });
  $("#txn-summary").textContent = `${data.total.toLocaleString()} transaction${data.total === 1 ? "" : "s"} · ${m(data.total_minor)} · ${data.range.label}`;
  $("#export-csv").href = `/api/v1/export.csv?${params()}`;
  $("#pg-info").textContent = `Page ${data.page} of ${data.pages}`;
  $("#pg-prev").disabled = data.page <= 1;
  $("#pg-next").disabled = data.page >= data.pages;
  $$("#txn-table th[data-sort]").forEach((th) => {
    th.setAttribute("aria-sort", th.dataset.sort === state.sort ? (state.order === "asc" ? "ascending" : "descending") : "none");
  });
  const body = $("#txn-table tbody");
  if (!data.items.length) {
    body.replaceChildren(el("tr", {}, el("td", { colspan: 7, class: "empty", text: "No transactions match these filters" })));
    return;
  }
  body.replaceChildren(...data.items.map((t) => {
    const amt = money(t.amount_minor, t.currency);
    const conv = t.currency !== base() ? el("span", { class: "orig", text: `≈ ${m(t.amount_base_minor)}` }) : null;
    const lo = t.confidence != null && t.confidence < 0.85;
    return el("tr", { class: t.deleted ? "deleted" : "", tabindex: 0, onclick: () => openDrawer(t.id), onkeydown: (e) => e.key === "Enter" && openDrawer(t.id) },
      el("td", { text: fmtDate(t.occurred_at, true) }),
      el("td", { class: `num ${t.direction === "income" ? "income" : ""}` }, `${t.direction === "income" ? "+" : ""}${amt}`, conv),
      el("td", {}, el("span", { class: "cat-pill", text: t.category || "Uncategorized" })),
      el("td", { text: t.merchant || "" }),
      el("td", {}, t.tags.map((g) => el("span", { class: "tag", text: `#${g.name}` }))),
      el("td", { class: "note", text: t.note || "", title: t.note || "" }),
      el("td", { class: "num" }, t.confidence == null ? "" : el("span", { class: `conf ${lo ? "lo" : ""}`, text: `${Math.round(t.confidence * 100)}%` })),
    );
  }));
}

// ------------------------------------------------------------------ drawer

let drawerId = null;
function toLocalInput(iso) {
  const d = new Date(iso);
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: state.me.timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(d);
  const g = (t) => parts.find((p) => p.type === t)?.value;
  return `${g("year")}-${g("month")}-${g("day")}T${g("hour") === "24" ? "00" : g("hour")}:${g("minute")}`;
}

async function openDrawer(id) {
  drawerId = id;
  const t = await api(`/transactions/${id}`);
  const form = $("#d-form");
  const e = EXP[t.currency] ?? 2;
  form.amount.value = (t.amount_minor / 10 ** e).toFixed(e);
  form.currency.value = t.currency;
  form.direction.value = t.direction;
  form.occurred_at.value = toLocalInput(t.occurred_at);
  form.category_id.replaceChildren(
    el("option", { value: "", text: "Uncategorized" }),
    ...state.facets.categories.map((c) => el("option", { value: c.id, text: c.name, selected: c.id === t.category_id })),
  );
  form.merchant.value = t.merchant || "";
  form.tags.value = t.tags.map((g) => g.name).join(", ");
  form.note.value = t.note || "";
  $("#d-title").textContent = `${money(t.amount_minor, t.currency)} · ${t.category || "Uncategorized"}`;
  $("#d-delete").hidden = t.deleted;
  $("#d-restore").hidden = !t.deleted;

  const meta = $("#d-meta");
  meta.replaceChildren(
    el("h3", { text: "Original message" }),
    t.raw_text ? el("blockquote", { text: t.raw_text }) : el("p", { class: "muted", text: "Added outside chat" }),
    el("p", { class: "muted", text: [
      t.channel && `via ${t.channel}`,
      t.confidence != null && `parser confidence ${Math.round(t.confidence * 100)}%`,
      t.fx_rate && `fx ${Number(t.fx_rate).toPrecision(6)} → ${base()} (${m(t.amount_base_minor)})`,
      `logged ${fmtDate(t.created_at, true)}`,
    ].filter(Boolean).join(" · ") }),
    t.versions.length ? el("h3", { text: "History" }) : null,
    t.versions.length ? el("ul", {}, t.versions.map((v) => el("li", {},
      `${fmtDate(v.at, true)} · ${v.by}${v.reason ? ` (${v.reason})` : ""}: `,
      el("code", { text: Object.entries(v.diff).map(([k, [a, b]]) => `${k}: ${JSON.stringify(a)} → ${JSON.stringify(b)}`).join("; ") })))) : null,
    t.llm_calls.length ? el("h3", { text: "Pipeline" }) : null,
    t.llm_calls.length ? el("ul", {}, t.llm_calls.map((c) => el("li", {},
      `${c.purpose} · `, el("code", { text: c.model }), ` · ${c.success ? "ok" : "failed"} · ${c.latency_ms} ms`))) : null,
  );
  $("#drawer").hidden = false;
  $("#scrim").hidden = false;
  form.amount.focus();
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#scrim").hidden = true;
  drawerId = null;
}

async function saveDrawer(evt) {
  evt.preventDefault();
  const form = evt.target;
  const body = {
    amount: Number(form.amount.value),
    currency: form.currency.value.trim().toUpperCase(),
    direction: form.direction.value,
    occurred_at: form.occurred_at.value,
    category_id: form.category_id.value ? Number(form.category_id.value) : null,
    merchant: form.merchant.value,
    note: form.note.value,
    tags: form.tags.value.split(",").map((x) => x.trim()).filter(Boolean),
  };
  try {
    await api(`/transactions/${drawerId}`, { method: "PATCH", body });
    toast("Saved");
    closeDrawer();
    refresh();
  } catch (e) {
    toast(`Couldn't save: ${e.message}`);
  }
}

// ------------------------------------------------------------------ plans

async function loadPlans() {
  const p = await api("/plans");
  const bh = $("#budgets");
  if (!p.budgets.length) bh.replaceChildren(el("div", { class: "empty", text: "No budgets yet. Set one below or say “budget cafes 80”." }));
  else bh.replaceChildren(...p.budgets.map((b) => {
    const frac = b.spent_minor / b.limit_minor;
    const cls = frac >= 1 ? "over" : frac >= 0.8 ? "warn" : "";
    const state = frac >= 1 ? "⛔ over" : frac >= 0.8 ? "⚠ close" : "✓ on track";
    return el("div", { class: "meter-row" },
      el("div", {}, el("div", { text: b.category }), el("div", { class: "muted", style: "font-size:12px", text: `per ${b.period}` })),
      el("div", { class: `meter ${cls}`, role: "meter", "aria-valuemin": 0, "aria-valuemax": b.limit_minor, "aria-valuenow": b.spent_minor, "aria-label": b.category },
        el("span", { style: `width:${Math.min(100, frac * 100).toFixed(1)}%` })),
      el("div", { class: "amt", text: `${m(b.spent_minor)} / ${m(b.limit_minor, { whole: true })}` }),
      el("div", { class: "state" }, `${state} · ${Math.round(frac * 100)}% `,
        el("button", { class: "link", type: "button", text: "remove", onclick: async () => { await api(`/budgets/${b.id}`, { method: "DELETE" }); loadPlans(); } })),
    );
  }));
  const form = $("#budget-form");
  const sel = form.category_id;
  const keep = sel.value;
  sel.replaceChildren(...state.facets.categories.map((c) => el("option", { value: c.id, text: c.name })));
  if (keep) sel.value = keep;
  $("#budget-note").textContent = p.budgets.length ? `${p.budgets.filter((b) => b.spent_minor >= 0.8 * b.limit_minor).length} need attention` : "";

  const lh = $("#lending");
  if (!p.lending.length) lh.replaceChildren(el("div", { class: "empty", text: "All square 🤝" }));
  else lh.replaceChildren(...p.lending.map((l) => el("div", { class: "lend-row" },
    el("span", { text: l.person.charAt(0).toUpperCase() + l.person.slice(1) }),
    el("span", {}, el("span", { class: "muted", text: l.owed_to_me_minor > 0 ? "owes you " : "you owe " }), el("span", { class: "amt", text: m(Math.abs(l.owed_to_me_minor)) })),
  )));

  $("#fixed-note").textContent = p.recurring.length ? `≈ ${m(p.monthly_fixed_minor)} per month in fixed costs` : "";
  const rb = $("#recurring tbody");
  if (!p.recurring.length) rb.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "empty", text: "Nothing recurring yet" })));
  else rb.replaceChildren(...p.recurring.map((r) => el("tr", {},
    el("td", {}, el("div", { text: r.merchant || r.category || "—" }), el("div", { class: "muted", style: "font-size:12px", text: r.schedule })),
    el("td", {}, el("span", { class: "cat-pill", text: r.category || "—" })),
    el("td", { class: "num", text: money(r.amount_minor, r.currency) }),
    el("td", { text: fmtDate(`${r.next_run}T12:00:00`) }),
    el("td", {}, el("button", { class: "link", type: "button", text: "stop", onclick: async () => {
      if (!confirm(`Stop “${r.description}”?`)) return;
      await api(`/recurring/${r.id}`, { method: "DELETE" });
      toast("Stopped");
      loadPlans();
    } })),
  )));
}

// ------------------------------------------------------------------ system

async function loadSystem() {
  const s = await api("/system");
  const failRate = s.calls_30d ? Math.round((100 * s.failed_30d) / s.calls_30d) : 0;
  $("#sys-tiles").replaceChildren(
    tile("LLM spend · 30 days", `$${s.cost_30d_usd.toFixed(4)}`, el("span", { text: `daily cap $${s.daily_cap_usd.toFixed(2)}` }), true),
    tile("LLM calls", s.calls_30d.toLocaleString(), el("span", { text: "across all stages" })),
    tile("Failed attempts", `${failRate}%`, el("span", { text: `${s.failed_30d} fell through to a fallback` })),
    tile("Messages", Object.values(s.outcomes).reduce((a, b) => a + b, 0).toLocaleString(), el("span", { text: "last 30 days" })),
    tile("Cost / message", s.calls_30d ? `$${(s.cost_30d_usd / Math.max(1, Object.values(s.outcomes).reduce((a, b) => a + b, 0))).toFixed(5)}` : "—", el("span", { text: "average" })),
  );
  const byDay = {};
  for (const u of s.usage) byDay[u.day] = (byDay[u.day] || 0) + u.cost_usd;
  const days = [];
  for (let i = 29; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10);
    days.push({ date: d, value: byDay[d] || 0, short: d.slice(5) });
  }
  const host = $("#chart-cost");
  const usd = (v) => `$${v.toFixed(4)}`;
  // dollars, not the user's currency
  columnChart(host, days, {
    label: (p) => p.date,
    fmtValue: usd,
    fmtTick: (v) => `$${v < 0.01 ? v.toFixed(3) : v.toFixed(2)}`,
  });

  $("#sys-chains").replaceChildren(...Object.entries(s.chains).map(([p, models]) =>
    el("div", { class: "chain" }, el("div", { class: "p", text: p }), models.map((mm) => el("code", { text: mm })))));

  const outcomes = Object.entries(s.outcomes).map(([name, count]) => ({ name, total_minor: count, count }));
  const oh = $("#chart-outcomes");
  oh.replaceChildren();
  if (!outcomes.length) oh.append(el("div", { class: "empty", text: "No messages yet" }));
  else {
    const max = Math.max(...outcomes.map((o) => o.count));
    oh.append(el("div", { class: "hbars" }, outcomes.sort((a, b) => b.count - a.count).map((o) =>
      el("div", { class: "hbar" },
        el("span", { class: `name ${["llm_unavailable", "cost_cap"].includes(o.name) ? "outcome-bad" : ""}`, text: o.name === "null" ? "pending" : o.name }),
        el("span", { class: "track" }, el("span", { class: "fill", style: `width:${(70 * o.count) / max}%` }), el("span", { class: "amt", text: String(o.count) }))))));
  }
  $("#sys-messages tbody").replaceChildren(...s.recent_messages.map((r) => el("tr", {},
    el("td", { text: r.id }), el("td", { text: fmtDate(r.at, true) }), el("td", { text: r.channel }),
    el("td", { class: "note", text: r.text || "[media]", title: r.text || "" }), el("td", { text: r.outcome || "…" }))));
}

// ------------------------------------------------------------------ filters UI

function syncControls() {
  const f = state.f;
  $("#f-period").value = f.period;
  $("#custom-range").hidden = f.period !== "custom";
  $("#f-start").value = f.start;
  $("#f-end").value = f.end;
  $$("#f-direction button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.value === f.direction)));
  $("#f-q").value = f.q;
  $("#f-category").value = f.category;
  $("#f-merchant").value = f.merchant;
  $("#f-tag").value = f.tag;
  $("#f-min").value = f.min;
  $("#f-max").value = f.max;
  $("#f-currency").value = f.currency;
  $("#f-low").checked = !!f.low;
  $("#f-deleted").checked = !!f.deleted;
  renderChips();
}

function renderChips() {
  const f = state.f;
  const chips = [];
  const add = (label, reset) => chips.push(el("span", { class: "chip" }, label, el("button", { type: "button", "aria-label": `Remove ${label}`, text: "✕", onclick: () => setFilters(reset) })));
  if (f.category) add(`Category: ${state.facets?.categories.find((c) => String(c.id) === f.category)?.name || f.category}`, { category: "" });
  if (f.merchant) add(`Merchant: ${f.merchant}`, { merchant: "" });
  if (f.tag) add(`#${f.tag}`, { tag: "" });
  if (f.q) add(`“${f.q}”`, { q: "" });
  if (f.min) add(`≥ ${f.min}`, { min: "" });
  if (f.max) add(`≤ ${f.max}`, { max: "" });
  if (f.currency) add(f.currency, { currency: "" });
  if (f.low) add("Low confidence", { low: "" });
  if (f.deleted) add("Deleted", { deleted: "" });
  $("#active-chips").replaceChildren(...chips);
}

function setFilters(patch) {
  Object.assign(state.f, patch);
  state.page = 1;
  syncControls();
  writeUrl();
  refresh();
}

function fillFacets() {
  const fc = state.facets;
  $("#f-category").replaceChildren(el("option", { value: "", text: "All categories" }), ...fc.categories.map((c) => el("option", { value: c.id, text: c.name })));
  $("#f-merchant").replaceChildren(el("option", { value: "", text: "All merchants" }), ...fc.merchants.map((x) => el("option", { value: x.name, text: `${x.name} (${x.count})` })));
  $("#f-tag").replaceChildren(el("option", { value: "", text: "All tags" }), ...fc.tags.map((t) => el("option", { value: t.name, text: `#${t.name}` })));
  $("#f-currency").replaceChildren(el("option", { value: "", text: "Any" }), ...fc.currencies.map((c) => el("option", { value: c, text: c })));
}

function bindFilters() {
  $("#f-period").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v === "custom") {
      const today = new Date().toISOString().slice(0, 10);
      setFilters({ period: v, start: state.f.start || `${today.slice(0, 8)}01`, end: state.f.end || today });
    } else setFilters({ period: v, start: "", end: "" });
  });
  $("#f-start").addEventListener("change", (e) => setFilters({ start: e.target.value }));
  $("#f-end").addEventListener("change", (e) => setFilters({ end: e.target.value }));
  $$("#f-direction button").forEach((b) => b.addEventListener("click", () => setFilters({ direction: b.dataset.value })));
  let qTimer;
  $("#f-q").addEventListener("input", (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => setFilters({ q: e.target.value.trim() }), 300);
  });
  for (const [id, key] of [["#f-category", "category"], ["#f-merchant", "merchant"], ["#f-tag", "tag"], ["#f-currency", "currency"]]) {
    $(id).addEventListener("change", (e) => setFilters({ [key]: e.target.value }));
  }
  $("#f-min").addEventListener("change", (e) => setFilters({ min: e.target.value }));
  $("#f-max").addEventListener("change", (e) => setFilters({ max: e.target.value }));
  $("#f-low").addEventListener("change", (e) => setFilters({ low: e.target.checked ? "1" : "" }));
  $("#f-deleted").addEventListener("change", (e) => setFilters({ deleted: e.target.checked ? "1" : "" }));
  $("#f-more").addEventListener("click", (e) => {
    const box = $("#more-filters");
    box.hidden = !box.hidden;
    e.target.setAttribute("aria-expanded", String(!box.hidden));
  });
  $("#f-clear").addEventListener("click", () => setFilters({ ...DEFAULTS, period: state.f.period, start: state.f.start, end: state.f.end }));
}

// ------------------------------------------------------------------ quick log

async function quickSend(text) {
  const box = $("#quick-reply");
  box.hidden = false;
  box.replaceChildren(el("div", { class: "you", text: `you: ${text}` }), el("div", { class: "muted", text: "…" }));
  try {
    const res = await api("/say", { method: "POST", body: { text } });
    const nodes = [el("div", { class: "you", text: `you: ${text}` })];
    for (const r of res.replies) {
      nodes.push(el("div", { text: r.text }));
      if (r.options?.length) {
        nodes.push(el("div", { class: "opts" }, r.options.map((o) =>
          el("button", { class: "btn small", type: "button", text: o.label, onclick: () => quickSend(o.value) }))));
      }
    }
    nodes.push(el("button", { class: "icon-btn close", type: "button", "aria-label": "Dismiss", text: "✕", onclick: () => (box.hidden = true) }));
    box.replaceChildren(...nodes);
    state.facets = await api("/facets");
    fillFacets();
    syncControls();
    refresh();
  } catch (e) {
    box.replaceChildren(el("div", { text: `Couldn't send: ${e.message}` }));
  }
}

// ------------------------------------------------------------------ tabs & refresh

function switchTab(tab) {
  state.tab = tab;
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  $$(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${tab}`));
  $(".filters").hidden = tab === "system" || tab === "plans";
  writeUrl();
  refresh();
}

let refreshSeq = 0;
async function refresh() {
  const seq = ++refreshSeq;
  document.body.classList.add("loading"); // previous render stays, dimmed
  try {
    if (state.tab === "overview") {
      const s = await api("/summary", { query: params() });
      if (seq !== refreshSeq) return;
      state.summary = s;
      renderOverview();
    } else if (state.tab === "transactions") {
      await loadTransactions();
    } else if (state.tab === "plans") {
      await loadPlans();
    } else {
      await loadSystem();
    }
  } catch (e) {
    if (e.message !== "unauthorized") toast(`Couldn't load: ${e.message}`);
  } finally {
    if (seq === refreshSeq) document.body.classList.remove("loading");
  }
}

function bindChrome() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("#quick").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#quick-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    quickSend(text);
  });
  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("pocket.theme", root.dataset.theme); } catch {}
    renderOverview();
  });
  $$(".table-toggle").forEach((btn) => btn.addEventListener("click", () => {
    const key = btn.closest("[data-chart]").dataset.chart;
    if (state.showTable.has(key)) state.showTable.delete(key); else state.showTable.add(key);
    btn.textContent = state.showTable.has(key) ? "Chart" : "Table";
    renderOverview();
  }));
  $$("#txn-table th[data-sort] button").forEach((b) => b.addEventListener("click", () => {
    const key = b.parentElement.dataset.sort;
    if (state.sort === key) state.order = state.order === "desc" ? "asc" : "desc";
    else { state.sort = key; state.order = key === "merchant" || key === "category" ? "asc" : "desc"; }
    state.page = 1;
    writeUrl();
    refresh();
  }));
  $("#pg-prev").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); refresh(); });
  $("#pg-next").addEventListener("click", () => { state.page += 1; refresh(); });
  $("#d-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawerId) closeDrawer(); });
  $("#d-form").addEventListener("submit", saveDrawer);
  $("#budget-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/budgets", { method: "POST", body: { category_id: Number(f.category_id.value), amount: Number(f.amount.value), period: f.period.value } });
      f.amount.value = "";
      toast("Budget saved");
      loadPlans();
    } catch (err) {
      toast(`Couldn't save: ${err.message}`);
    }
  });
  $("#d-delete").addEventListener("click", async () => {
    await api(`/transactions/${drawerId}`, { method: "DELETE" });
    toast("Deleted");
    closeDrawer();
    refresh();
  });
  $("#d-restore").addEventListener("click", async () => {
    await api(`/transactions/${drawerId}/restore`, { method: "POST" });
    toast("Restored");
    closeDrawer();
    refresh();
  });
  let rTimer;
  addEventListener("resize", () => {
    clearTimeout(rTimer);
    rTimer = setTimeout(() => state.tab === "overview" && renderOverview(), 150);
  });
}

async function main() {
  readUrl();
  bindChrome();
  bindFilters();
  [state.me, state.facets] = await Promise.all([api("/me"), api("/facets")]);
  fillFacets();
  syncControls();
  switchTab(state.tab);
}

main();
