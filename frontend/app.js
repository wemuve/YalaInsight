/* YalaInsight single-page client. No build step; talks to /api/v1 with a bearer token. */
"use strict";

const S = { token: null, me: null, charts: [], maps: [], plants: null,
  config: { map_tile_url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", map_tile_attribution: "© OpenStreetMap contributors", show_demo_login: false } };
const $ = (sel, root = document) => root.querySelector(sel);
const view = () => $("#view");

// ------------------------------------------------------------------ utilities
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (v, d = 1) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
const money = (v, ccy) => (v === null || v === undefined ? "—" : `${ccy ? ccy + " " : ""}${num(v, Math.abs(v) < 1000 ? 2 : 0)}`);
function energy(kwh) {
  if (kwh === null || kwh === undefined) return "—";
  const a = Math.abs(kwh);
  if (a >= 1e6) return `${num(kwh / 1e6, 2)} GWh`;
  if (a >= 1e3) return `${num(kwh / 1e3, 1)} MWh`;
  return `${num(kwh, 0)} kWh`;
}
const date = (s) => (s ? new Date(s).toISOString().slice(0, 10) : "—");
const dt = (s) => (s ? new Date(s).toISOString().replace("T", " ").slice(0, 16) + "Z" : "—");
const chip = (basis, title = "") => `<span class="chip ${esc(basis)}" title="${esc(title)}">${esc(basis)}</span>`;
const sev = (s) => `<span class="sev ${esc(s)}">${esc(s)}</span>`;
const status = (s) => `<span class="status">${esc(String(s).replace(/_/g, " "))}</span>`;
const trace = (text, formula) => `<span class="trace" title="${esc(formula)}">${text}</span>`;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.token) headers.Authorization = `Bearer ${S.token}`;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const r = await fetch(`/api/v1${path}`, { ...opts, headers });
  if (r.status === 401 && S.token) { logout(); throw new Error("Session expired — please sign in again"); }
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    let d = body && body.detail !== undefined ? body.detail : body;
    if (Array.isArray(d)) d = d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
    if (d && typeof d === "object" && d.message) {
      d = d.message + (d.errors || []).slice(0, 5).map((e) => ` · row ${e.row} [${e.field}] ${e.message}`).join("");
    }
    const err = new Error(typeof d === "string" ? d : JSON.stringify(d));
    err.status = r.status;
    throw err;
  }
  return body;
}

async function download(path, filename) {
  const r = await fetch(`/api/v1${path}`, { headers: { Authorization: `Bearer ${S.token}` } });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail; } catch (_) { /* not json */ }
    toast(`Download failed: ${msg}`);
    return;
  }
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

function periodQS(extra = {}) {
  const p = new URLSearchParams(extra);
  const s = $("#p-start").value, e = $("#p-end").value;
  if (s) p.set("start", `${s}T00:00:00Z`);
  if (e) {
    const d = new Date(`${e}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + 1); // inclusive end date -> exclusive timestamp
    p.set("end", d.toISOString());
  }
  return p.toString();
}
function periodBody() {
  const q = new URLSearchParams(periodQS());
  const o = {};
  if (q.get("start")) o.start = q.get("start");
  if (q.get("end")) o.end = q.get("end");
  return o;
}

function cleanup() {
  S.charts.forEach((c) => c.destroy());
  S.charts = [];
  S.maps.forEach((m) => m.remove());
  S.maps = [];
}
function chart(canvas, cfg) {
  const ink = css("--muted"), line = css("--line");
  cfg.options = cfg.options || {};
  cfg.options.maintainAspectRatio = false;
  cfg.options.plugins = { legend: { labels: { color: ink, boxWidth: 12 } }, ...(cfg.options.plugins || {}) };
  cfg.options.scales = cfg.options.scales || {};
  Object.values(cfg.options.scales).forEach((s) => {
    s.ticks = { color: ink, ...(s.ticks || {}) };
    s.grid = { color: line, ...(s.grid || {}) };
  });
  const c = new Chart(canvas, cfg);
  S.charts.push(c);
  return c;
}
const COLORS = { actual: "#0f6b5c", expected: "#9aa7a5", accent: "#e8a33d", bad: "#c0392b", info: "#2f6db5", purple: "#7d5fb2" };

function setCrumbs(parts) {
  $("#crumbs").innerHTML = parts.map((p) => (p.href ? `<a href="${p.href}">${esc(p.label)}</a>` : esc(p.label))).join(" / ");
}
function can(perm) {
  const ps = S.me?.permissions || [];
  return ps.includes("*") || ps.includes(perm);
}
const hasModule = (m) => (S.me?.modules || []).includes(m);

function formData(form) {
  const o = {};
  new FormData(form).forEach((v, k) => {
    const el = form.elements[k];
    if (v === "" || v === null) return;
    if (el && el.type === "number") o[k] = Number(v);
    else if (el && el.type === "checkbox") o[k] = el.checked;
    else o[k] = v;
  });
  form.querySelectorAll("input[type=checkbox]").forEach((c) => { o[c.name] = c.checked; });
  return o;
}

function openDrawer(html) {
  const d = $("#drawer");
  d.querySelector(".drawer-body").innerHTML = `<div class="row spread"><span></span><button class="btn ghost" data-close>✕ Close</button></div>${html}`;
  d.classList.remove("hidden");
  return d.querySelector(".drawer-body");
}
function closeDrawer() { $("#drawer").classList.add("hidden"); }

async function plantsList(force = false) {
  if (!S.plants || force) S.plants = await api("/plants");
  return S.plants;
}

// ------------------------------------------------------------------ auth
async function login(e) {
  e.preventDefault();
  const f = formData(e.target);
  $("#login-error").textContent = "";
  try {
    const r = await api("/auth/login", { method: "POST", json: { email: f.email, password: f.password, otp: f.otp || null } });
    S.token = r.access_token;
    try { sessionStorage.setItem("yala_token", S.token); } catch (_) { /* storage unavailable */ }
    $("#otp-row").classList.add("hidden");
    e.target.elements.otp.value = "";
    await boot();
  } catch (err) {
    if (err.message === "mfa_required") {
      $("#otp-row").classList.remove("hidden");
      e.target.elements.otp.focus();
      $("#login-error").textContent = "Enter the 6-digit code from your authenticator app.";
    } else {
      $("#login-error").textContent = err.message;
    }
  }
}
async function signup(e) {
  e.preventDefault();
  const f = formData(e.target);
  $("#signup-error").textContent = "";
  f.default_currency = (f.default_currency || "USD").toUpperCase();
  if (f.country_code) f.country_code = f.country_code.toUpperCase();
  try {
    const r = await api("/auth/signup", { method: "POST", json: f });
    S.token = r.access_token;
    try { sessionStorage.setItem("yala_token", S.token); } catch (_) { /* storage unavailable */ }
    e.target.reset();
    showLoginForm(true);
    location.hash = "#/dashboard";
    await boot();
    toast("Welcome! Your organisation is ready — register your first plant to begin.", 6000);
  } catch (err) {
    $("#signup-error").textContent = err.message;
  }
}
function showLoginForm(login) {
  $("#login-form").classList.toggle("hidden", !login);
  $("#signup-form").classList.toggle("hidden", login);
}
function logout() {
  S.token = null; S.me = null; S.plants = null;
  try { sessionStorage.removeItem("yala_token"); } catch (_) { /* storage unavailable */ }
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
}
async function boot() {
  try { S.me = await api("/auth/me"); } catch (_) { logout(); return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  const u = S.me.user;
  $("#whoami").innerHTML = `<a href="#/account" style="color:inherit">${esc(u ? u.full_name : "API key")}</a><br><span class="muted">${esc(S.me.organization.name)} · ${esc(u ? u.role : "")}</span>`;
  if (u && u.must_change_password && location.hash !== "#/account") location.hash = "#/account";
  else route();
}

// ------------------------------------------------------------------ router
const routes = [
  [/^#\/dashboard$/, viewDashboard],
  [/^#\/plants$/, viewPlants],
  [/^#\/plants\/([\w-]+)(?:\/(\w+))?$/, viewPlant],
  [/^#\/equipment\/([\w-]+)$/, viewEquipment],
  [/^#\/alerts$/, viewAlerts],
  [/^#\/work-orders$/, viewWorkOrders],
  [/^#\/simulator$/, viewSimulator],
  [/^#\/carbon$/, viewCarbon],
  [/^#\/reports$/, viewReports],
  [/^#\/settings(?:\/(\w+))?$/, viewSettings],
  [/^#\/account$/, viewAccount],
];
async function route() {
  if (!S.token) return;
  let h = location.hash || "#/dashboard";
  if (S.me?.user?.must_change_password && h !== "#/account") { location.hash = "#/account"; return; }
  cleanup();
  closeDrawer();
  document.querySelector(".sidebar").classList.remove("open");
  const key = h.split("/")[1].split("?")[0];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === key));
  for (const [re, fn] of routes) {
    const m = h.match(re);
    if (m) {
      view().innerHTML = `<div class="loading">Loading…</div>`;
      try { await fn(...m.slice(1)); } catch (err) {
        view().innerHTML = `<div class="panel"><h2>Could not load this view</h2><p class="error">${esc(err.message)}</p>
          ${err.status === 402 ? `<p class="muted">This module is not part of your organisation's subscription.</p>` : ""}</div>`;
      }
      return;
    }
  }
  location.hash = "#/dashboard";
}

// ------------------------------------------------------------------ dashboard
async function viewDashboard() {
  setCrumbs([{ label: "Portfolio dashboard" }]);
  const [d, plants] = await Promise.all([api(`/dashboard/summary?${periodQS()}`), plantsList()]);
  const p = d.portfolio, ccy = p.loss_currency;
  if (!p.plant_count) {
    view().innerHTML = `<div class="panel"><h1>Welcome to YalaInsight</h1>
      <p>Your organisation <b>${esc(S.me.organization.name)}</b> has no plants yet. Three steps to your first analysis:</p>
      <ol>
        <li><b>Register a plant</b> with its capacity, location and module parameters.</li>
        <li><b>Add an energy contract</b> (PPA, feed-in tariff or self-consumption price) so losses can be valued.</li>
        <li><b>Import data</b>: weather/irradiance CSV first, then metered energy. The expected-generation model runs automatically.</li>
      </ol>
      <div class="row">${can("plants:write") ? `<a class="btn primary" href="#/plants">Register your first plant</a>` : `<span class="muted">Ask an administrator to register a plant.</span>`}
        ${can("users:manage") ? `<a class="btn" href="#/settings/users">Invite colleagues</a>` : ""}</div>
      <p class="note">CSV formats are described on each plant's “Data & contracts” tab. Sample files are in the repository's <code>sample_data/</code> folder.</p></div>`;
    return;
  }
  const fx = Object.entries(p.fx_rates_used || {}).map(([c, r]) => `${c}: 1 ${r.base} = ${r.rate} ${r.quote} (${r.rate_date}, ${r.source})`).join("; ");
  view().innerHTML = `
    <div class="row spread"><div><h1>Portfolio overview</h1>
      <div class="muted small">${date(d.start)} → ${date(new Date(new Date(d.end) - 1))} · figures traceable via the basis chips and plant drill-down</div></div></div>
    <div class="kpis">
      ${kpi("Installed capacity", `${num(p.dc_capacity_kwp / 1000, 1)} MWp`, `${p.plant_count} plants · ${num(p.ac_capacity_kw / 1000, 1)} MWac`)}
      ${kpi("Actual generation", energy(p.actual_kwh), `expected ${energy(p.expected_kwh)} · variance ${p.variance_pct === null ? "—" : num(p.variance_pct, 1) + "%"}`, "measured")}
      ${kpi("Performance ratio", pct(p.performance_ratio), "capacity-weighted, uncorrected", "measured")}
      ${kpi("Availability", pct(p.availability), "time-based, capacity-weighted", "measured")}
      ${kpi("Est. gross lost energy value", money(p.gross_lost_value, ccy), `lost revenue ${money(p.lost_revenue, ccy)} · ${energy(p.shortfall_kwh)} shortfall`, "modelled")}
      ${kpi("Addressable by maintenance", money(p.addressable_value, ccy), "downtime, inverter, soiling, unexplained", "modelled")}
      ${kpi("Active anomalies", `${p.active_alerts}`, `${p.high_alerts} high / critical`, "", "#/alerts")}
      ${kpi("Overdue maintenance", `${p.overdue_work_orders}`, `${p.open_work_orders} open work orders`, "", "#/work-orders")}
      ${kpi("Est. avoided emissions", `${num(p.carbon_tco2e_estimated, 0)} tCO₂e`, "estimate — not verified, not credits", "modelled", "#/carbon")}
    </div>
    ${p.mixed_currencies ? `<div class="warnbox">Plants use different currencies (${p.mixed_currencies.join(", ")}) and no exchange rate was found — monetary totals are shown per plant only.</div>` : ""}
    ${fx ? `<div class="note">Converted to ${esc(ccy)} with dated rates: ${esc(fx)}</div>` : ""}
    <div class="grid g2">
      <div class="panel"><h2>Portfolio map</h2><div id="map" class="map"></div></div>
      <div class="panel"><h2>Expected vs actual by plant</h2><div class="chart"><canvas id="c-plants"></canvas></div></div>
    </div>
    <div class="panel"><h2>Plants</h2><div class="table-wrap"><table>
      <thead><tr><th>Plant</th><th class="num">MWp</th><th class="num">Actual</th><th class="num">Expected</th><th class="num">Variance</th>
        <th class="num">PR</th><th class="num">Availability</th><th class="num">Est. lost value</th><th class="num">Addressable</th><th class="num">Alerts</th><th class="num">Overdue WO</th></tr></thead>
      <tbody>${d.plants.map((r) => `<tr class="click" data-href="#/plants/${r.plant_id}">
        <td><span class="dot ${r.health}"></span>${esc(r.name)} <span class="muted small">${esc(r.country_code || "")}</span></td>
        <td class="num">${num(r.dc_capacity_kwp / 1000, 1)}</td><td class="num">${energy(r.performance.actual_kwh)}</td>
        <td class="num">${energy(r.performance.expected_kwh)}</td><td class="num">${r.performance.variance_pct === null ? "—" : num(r.performance.variance_pct, 1) + "%"}</td>
        <td class="num">${pct(r.performance.performance_ratio)}</td><td class="num">${pct(r.performance.availability)}</td>
        <td class="num">${r.loss ? money(r.loss.gross_value, r.loss.currency) : "—"}</td>
        <td class="num">${r.loss ? money(r.loss.addressable_value, r.loss.currency) : "—"}</td>
        <td class="num">${r.alerts.active}${r.alerts.high_or_critical ? ` <span class="sev high">${r.alerts.high_or_critical}</span>` : ""}</td>
        <td class="num">${r.work_orders.overdue}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="grid g2">
      <div class="panel"><h2>Highest-priority anomalies</h2>${alertTable(d.top_alerts, true)}
        <p class="note">Ranked by severity, estimated financial impact and urgency. Alert impacts can overlap; use the financial analysis for non-double-counted totals.</p></div>
      <div class="panel"><h2>Overdue maintenance</h2>${woTable(d.overdue_work_orders)}</div>
    </div>`;
  bindRowLinks();
  // map
  const byId = Object.fromEntries(d.plants.map((r) => [r.plant_id, r]));
  const m = portfolioMap("map", plants.map((pl) => ({ ...pl, k: byId[pl.id] })));
  S.maps.push(m);
  chart($("#c-plants"), {
    type: "bar",
    data: { labels: d.plants.map((r) => r.name), datasets: [
      { label: "Expected (MWh, modelled)", data: d.plants.map((r) => r.performance.expected_kwh / 1000), backgroundColor: COLORS.expected },
      { label: "Actual (MWh, measured)", data: d.plants.map((r) => r.performance.actual_kwh_compared / 1000), backgroundColor: COLORS.actual }] },
    options: { scales: { x: {}, y: { title: { display: true, text: "MWh" } } } },
  });
}

function kpi(label, value, sub = "", basis = "", href = "") {
  return `<div class="kpi ${href ? "click" : ""}" ${href ? `data-href="${href}"` : ""}><div class="label"><span>${esc(label)}</span>${basis ? chip(basis) : ""}</div>
    <div class="value">${value}</div><div class="sub">${sub}</div></div>`;
}

function portfolioMap(id, plants) {
  const m = L.map(id, { scrollWheelZoom: false });
  L.tileLayer(S.config.map_tile_url, { maxZoom: 18, attribution: S.config.map_tile_attribution }).addTo(m);
  const pts = [];
  const color = { ok: "#1f8a4c", warning: "#c07a12", critical: "#c0392b", no_data: "#888" };
  plants.forEach((p) => {
    if (p.latitude === null) return;
    const k = p.k;
    const c = color[k ? k.health : "no_data"];
    const r = 6 + Math.sqrt(p.dc_capacity_kwp / 1000) * 3;
    const mk = L.circleMarker([p.latitude, p.longitude], { radius: r, color: c, fillColor: c, fillOpacity: 0.6, weight: 2 }).addTo(m);
    mk.bindPopup(`<b><a href="#/plants/${p.id}">${esc(p.name)}</a></b><br>${num(p.dc_capacity_kwp / 1000, 1)} MWp · ${esc(p.status)}<br>
      ${k ? `PR ${pct(k.performance.performance_ratio)} · variance ${k.performance.variance_pct === null ? "—" : num(k.performance.variance_pct, 1) + "%"}<br>
      Alerts ${k.alerts.active} · est. exposure ${k.loss ? money(k.loss.gross_value, k.loss.currency) : "—"}<br>Overdue WO ${k.work_orders.overdue}` : ""}`);
    pts.push([p.latitude, p.longitude]);
    if (p.boundary) L.geoJSON(p.boundary, { style: { color: c, weight: 1, fillOpacity: 0.1 } }).addTo(m);
  });
  if (pts.length) m.fitBounds(pts, { padding: [40, 40], maxZoom: 9 }); else m.setView([5, 20], 3);
  setTimeout(() => m.invalidateSize(), 50);
  return m;
}

function bindRowLinks(root = document) {
  root.querySelectorAll("[data-href]").forEach((el) => el.addEventListener("click", (e) => {
    if (e.target.closest("a,button")) return;
    location.hash = el.dataset.href;
  }));
  root.querySelectorAll("[data-alert]").forEach((el) => el.addEventListener("click", (e) => {
    if (e.target.closest("a,button")) return;
    openAlert(el.dataset.alert);
  }));
  root.querySelectorAll("[data-wo]").forEach((el) => el.addEventListener("click", (e) => {
    if (e.target.closest("a,button")) return;
    openWorkOrder(el.dataset.wo);
  }));
}

function alertTable(alerts, showPlant = false) {
  if (!alerts.length) return `<p class="muted">No alerts.</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>Severity</th><th>Alert</th>${showPlant ? "<th>Plant</th>" : ""}<th>Window</th>
    <th class="num">Est. energy</th><th class="num">Est. impact</th><th>Status</th><th class="num">Priority</th></tr></thead><tbody>
    ${alerts.map((a) => `<tr class="click" data-alert="${a.id}"><td>${sev(a.severity)}</td><td>${esc(a.title)}<div class="muted small">${esc(a.rule_id)} · confidence ${esc(a.confidence)}</div></td>
      ${showPlant ? `<td>${esc(a.plant_name || "")}</td>` : ""}<td class="small" style="white-space:nowrap">${date(a.window_start)}<br>${date(a.window_end)}</td>
      <td class="num">${energy(a.est_energy_loss_kwh)}</td><td class="num">${money(a.est_financial_impact, a.currency)}</td>
      <td>${status(a.status)}</td><td class="num">${num(a.priority_score, 0)}</td></tr>`).join("")}</tbody></table></div>`;
}

function woTable(wos) {
  if (!wos.length) return `<p class="muted">No work orders.</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>Work order</th><th>Plant</th><th>Priority</th><th>Status</th><th>Due</th><th>Assignee</th><th class="num">Est. cost</th><th class="num">Est. recovery / yr</th></tr></thead><tbody>
    ${wos.map((w) => `<tr class="click" data-wo="${w.id}"><td>${esc(w.title)}<div class="muted small">${esc(w.kind)}</div></td><td>${esc(w.plant_name || "")}</td>
      <td>${esc(w.priority)}</td><td>${status(w.status)}</td><td>${w.overdue ? `<b style="color:var(--bad)">${date(w.due_date)}</b>` : date(w.due_date)}</td>
      <td>${esc(w.assignee_name || w.contractor || "—")}</td><td class="num">${money(w.estimated_cost, w.currency)}</td>
      <td class="num">${money(w.est_recovery_value_per_year, w.currency)}</td></tr>`).join("")}</tbody></table></div>`;
}

// ------------------------------------------------------------------ plants
async function viewPlants() {
  setCrumbs([{ label: "Plants" }]);
  const plants = await plantsList(true);
  view().innerHTML = `
    <div class="row spread"><h1>Plants</h1>${can("plants:write") ? `<button class="btn primary" id="add-plant">Register plant</button>` : ""}</div>
    <div class="panel"><div class="table-wrap"><table><thead><tr><th>Plant</th><th>Code</th><th>Location</th><th class="num">DC kWp</th><th class="num">AC kW</th>
      <th>Technology</th><th>Commissioned</th><th>Status</th></tr></thead><tbody>
      ${plants.map((p) => `<tr class="click" data-href="#/plants/${p.id}"><td>${esc(p.name)}</td><td>${esc(p.code)}</td>
        <td>${esc(p.region || "")} ${esc(p.country_code || "")}<div class="muted small">${num(p.latitude, 4)}, ${num(p.longitude, 4)}</div></td>
        <td class="num">${num(p.dc_capacity_kwp)}</td><td class="num">${num(p.ac_capacity_kw)}</td><td>${esc(p.technology)} · ${esc(p.mounting)}</td>
        <td>${date(p.commissioning_date)}</td><td>${status(p.status)}</td></tr>`).join("") || `<tr><td colspan="8" class="muted">No plants yet.</td></tr>`}
      </tbody></table></div></div>
    <div class="panel hidden" id="plant-form-panel"><h2>Register a plant</h2>
      <form id="plant-form" class="form">
        <label>Name <input name="name" required></label><label>Code <input name="code" required pattern="[A-Za-z0-9_\\-]+"></label>
        <label>DC capacity (kWp) <input name="dc_capacity_kwp" type="number" step="any" required></label>
        <label>AC capacity (kW) <input name="ac_capacity_kw" type="number" step="any" required></label>
        <label>Latitude <input name="latitude" type="number" step="any" required></label><label>Longitude <input name="longitude" type="number" step="any" required></label>
        <label>Country (ISO-2) <input name="country_code" maxlength="2" placeholder="KE"></label><label>Region <input name="region"></label>
        <label>Timezone <input name="timezone" value="UTC"></label><label>Commissioning date <input name="commissioning_date" type="date"></label>
        <label>Status <select name="status"><option>operational</option><option>commissioning</option><option>construction</option><option>decommissioned</option></select></label>
        <label>Technology <input name="technology" value="mono-Si"></label><label>Mounting <select name="mounting"><option>fixed</option><option>single-axis</option></select></label>
        <label>Tilt (°) <input name="tilt_deg" type="number" value="15"></label><label>Azimuth (° from equator) <input name="azimuth_deg" type="number" value="0"></label>
        <label>Temp. coefficient (%/°C) <input name="temp_coeff_pct_per_c" type="number" step="0.01" value="-0.37"></label>
        <label>System losses (%) <input name="system_loss_pct" type="number" step="0.1" value="14"></label>
        <label>Degradation (%/yr) <input name="annual_degradation_pct" type="number" step="0.05" value="0.5"></label>
        <label>Expected PR <input name="expected_pr" type="number" step="0.01" value="0.8"></label>
        <label>Currency <input name="currency" value="${esc(S.me.organization.currency)}" maxlength="3"></label>
        <div class="full row"><button class="btn primary">Save plant</button><span class="error" id="plant-err"></span></div>
      </form></div>`;
  bindRowLinks();
  const btn = $("#add-plant");
  if (btn) btn.onclick = () => $("#plant-form-panel").classList.toggle("hidden");
  const f = $("#plant-form");
  if (f) f.onsubmit = async (e) => {
    e.preventDefault();
    const body = formData(f);
    if (body.country_code) body.country_code = body.country_code.toUpperCase();
    try { const p = await api("/plants", { method: "POST", json: body }); S.plants = null; location.hash = `#/plants/${p.id}/data`; }
    catch (err) { $("#plant-err").textContent = err.message; }
  };
}

const PLANT_TABS = [["overview", "Performance"], ["financial", "Financial loss & recovery"], ["anomalies", "Anomalies"], ["maintenance", "Maintenance"],
  ["equipment", "Equipment"], ["carbon", "Carbon"], ["forecast", "Forecast"], ["map", "Site map"], ["data", "Data & contracts"]];

async function viewPlant(id, tab = "overview") {
  const p = await api(`/plants/${id}`);
  setCrumbs([{ label: "Plants", href: "#/plants" }, { label: p.name }]);
  view().innerHTML = `
    <div class="row spread"><div><h1>${esc(p.name)}</h1><div class="muted small">${esc(p.code)} · ${num(p.dc_capacity_kwp)} kWp DC / ${num(p.ac_capacity_kw)} kW AC ·
      ${esc(p.technology)} ${esc(p.mounting)} · ${esc(p.region || "")} ${esc(p.country_code || "")} · commissioned ${date(p.commissioning_date)} · ${esc(p.status)}</div></div></div>
    <nav class="tabs">${PLANT_TABS.map(([k, l]) => `<a href="#/plants/${id}/${k}" class="${k === tab ? "active" : ""}">${l}</a>`).join("")}</nav>
    <div id="tab"></div>`;
  const fn = { overview: tabOverview, financial: tabFinancial, anomalies: tabAnomalies, maintenance: tabMaintenance, equipment: tabEquipment,
    carbon: tabCarbon, forecast: tabForecast, map: tabMap, data: tabData }[tab] || tabOverview;
  try { await fn(p); } catch (err) {
    $("#tab").innerHTML = `<div class="panel"><p class="error">${esc(err.message)}</p>${err.status === 402 ? `<p class="muted">Module not included in your plan.</p>` : ""}</div>`;
  }
}

async function tabOverview(p, gran = "day") {
  const d = await api(`/plants/${p.id}/performance?${periodQS({ granularity: gran })}`);
  const s = d.summary;
  $("#tab").innerHTML = `
    <div class="kpis">
      ${kpi("Actual generation", energy(s.actual_kwh), `${num(s.specific_yield_kwh_per_kwp, 1)} kWh/kWp specific yield`, "measured")}
      ${kpi("Expected generation", energy(s.expected_kwh), d.model ? `${esc(d.model.name)} v${esc(d.model.version)} ±${d.model.uncertainty_pct}%` : "no model run", "modelled")}
      ${kpi("Production variance", s.variance_pct === null ? "—" : `${num(s.variance_pct, 1)}%`, energy(s.variance_kwh), "modelled")}
      ${kpi("Performance ratio", pct(s.performance_ratio), `expected ≈ ${pct(p.expected_pr, 0)}`, "measured")}
      ${kpi("Performance index", pct(s.performance_index), "actual ÷ modelled expected", "modelled")}
      ${kpi("Availability", pct(s.availability), "plant-level, time-based (partial outages show in PI)", "measured")}
      ${kpi("Data coverage", pct(s.data_coverage), `${s.missing_actual_intervals} missing metered · ${s.missing_expected_intervals} no baseline`)}
    </div>
    ${d.flags.map((f) => `<div class="warnbox">${esc(f)}</div>`).join("")}
    <div class="panel"><div class="row spread"><h2>Expected vs actual</h2>
      <div class="row">${["hour", "day", "month", "year"].map((g) => `<button class="btn small ${g === gran ? "primary" : ""}" data-gran="${g}">${g}</button>`).join("")}</div></div>
      <div class="chart tall"><canvas id="c-perf"></canvas></div></div>
    <div class="panel"><h2>${{ hour: "Hourly", day: "Daily", month: "Monthly", year: "Annual" }[gran]} table</h2><div class="table-wrap"><table>
      <thead><tr><th>Period</th><th class="num">Actual kWh</th><th class="num">Expected kWh</th><th class="num">Variance</th><th class="num">PR</th><th class="num">PI</th><th class="num">Availability</th><th class="num">Coverage</th></tr></thead>
      <tbody>${d.series.slice(-400).reverse().map((r) => `<tr><td>${esc(r.period)}</td><td class="num">${num(r.actual_kwh)}</td><td class="num">${num(r.expected_kwh)}</td>
        <td class="num">${r.variance_pct === null ? "—" : num(r.variance_pct, 1) + "%"}</td><td class="num">${pct(r.performance_ratio)}</td><td class="num">${pct(r.performance_index)}</td>
        <td class="num">${pct(r.availability)}</td><td class="num">${pct(r.data_coverage, 0)}</td></tr>`).join("")}</tbody></table></div>
      <div class="note">${Object.entries(d.definitions).map(([k, v]) => `<b>${esc(k.replace(/_/g, " "))}</b>: ${esc(v)}`).join("<br>")}</div></div>`;
  document.querySelectorAll("[data-gran]").forEach((b) => (b.onclick = () => { cleanup(); tabOverview(p, b.dataset.gran); }));
  chart($("#c-perf"), {
    data: { labels: d.series.map((r) => r.period), datasets: [
      { type: "bar", label: "Actual kWh (measured)", data: d.series.map((r) => r.actual_kwh), backgroundColor: COLORS.actual, yAxisID: "y", order: 2 },
      { type: "line", label: "Expected kWh (modelled)", data: d.series.map((r) => r.expected_kwh), borderColor: COLORS.expected, pointRadius: 0, yAxisID: "y", order: 1 },
      { type: "line", label: "Performance ratio", data: d.series.map((r) => r.performance_ratio), borderColor: COLORS.accent, pointRadius: 0, yAxisID: "y2", order: 0 }] },
    options: { interaction: { mode: "index", intersect: false },
      scales: { x: { ticks: { maxTicksLimit: 14 } }, y: { title: { display: true, text: "kWh" } }, y2: { position: "right", min: 0, max: 1.1, grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${Math.round(v * 100)}%` } } } },
  });
}

const CATEGORY_LABEL = { curtailment: "Curtailment", grid_outage: "Grid outage", planned_maintenance: "Planned maintenance", plant_downtime: "Plant downtime",
  inverter_failure: "Inverter failure / underperformance", soiling: "Soiling (estimate)", excess_degradation: "Excess degradation (estimate)", unexplained: "Unexplained" };

async function tabFinancial(p) {
  $("#tab").innerHTML = `
    <div class="panel"><h2>Financial loss & recovery analysis</h2>
      <form id="fin-form" class="form">
        <label>Soiling share of expected (%) <input type="number" name="soiling_pct" step="0.1" min="0" value="0"></label>
        <label>Excess degradation share (%) <input type="number" name="excess_degradation_pct" step="0.1" min="0" value="0"></label>
        <label>Recovery rate (0–1) <input type="number" name="recovery_rate" step="0.05" min="0" max="1" value="0.8"></label>
        <label>Repair / intervention cost <input type="number" name="repair_cost" step="any" min="0" value="0"></label>
        <label>Incremental annual cost <input type="number" name="incremental_annual_cost" step="any" min="0" value="0"></label>
        <label>Days until fix is effective <input type="number" name="days_to_effect" min="0" max="365" value="14"></label>
        <label>Horizon (years) <input type="number" name="horizon_years" min="1" max="30" value="5"></label>
        <label>Discount rate <input type="number" name="discount_rate" step="0.01" min="0" max="1" value="0.08"></label>
        <label>Run-rate window (days) <input type="number" name="run_rate_days" min="1" max="365" value="7"></label>
        <label>Report currency (optional) <input name="report_currency" maxlength="3" placeholder="${esc(p.currency)}"></label>
        <label class="full">Addressable categories (valued for recovery)
          <div class="row">${["plant_downtime", "inverter_failure", "soiling", "excess_degradation", "unexplained"].map((c) =>
            `<label class="row"><input type="checkbox" name="cat_${c}" ${["inverter_failure", "soiling", "excess_degradation"].includes(c) ? "checked" : ""}> ${CATEGORY_LABEL[c]}</label>`).join("")}</div></label>
        <label class="row"><input type="checkbox" name="persistent" checked> Loss expected to persist without intervention</label>
        <label class="row"><input type="checkbox" name="use_alerts_for_inverter_faults" checked> Use unreviewed alerts for attribution</label>
        <div class="full row"><button class="btn primary">Run analysis</button><span class="muted small">Uses the period selected at the top (default: latest 30 days).</span></div>
      </form></div><div id="fin-out"></div>`;
  const f = $("#fin-form");
  f.onsubmit = async (e) => { e.preventDefault(); await runFinancial(p, f); };
  await runFinancial(p, f);
}

async function runFinancial(p, f) {
  const v = formData(f);
  const cats = Object.keys(v).filter((k) => k.startsWith("cat_") && v[k]).map((k) => k.slice(4));
  const body = { ...periodBody(), soiling_pct: v.soiling_pct || 0, excess_degradation_pct: v.excess_degradation_pct || 0,
    use_alerts_for_inverter_faults: v.use_alerts_for_inverter_faults, report_currency: v.report_currency ? v.report_currency.toUpperCase() : null,
    recovery: { categories: cats, recovery_rate: v.recovery_rate ?? 0.8, repair_cost: v.repair_cost || 0, incremental_annual_cost: v.incremental_annual_cost || 0,
      days_to_effect: v.days_to_effect ?? 14, horizon_years: v.horizon_years || 5, discount_rate: v.discount_rate ?? 0.08, persistent: v.persistent, run_rate_days: v.run_rate_days || 7 } };
  const out = $("#fin-out");
  out.innerHTML = `<div class="loading">Calculating…</div>`;
  cleanup();
  let a;
  try { a = await api(`/plants/${p.id}/financial-analysis`, { method: "POST", json: body }); }
  catch (err) { out.innerHTML = `<div class="panel"><p class="error">${esc(err.message)}</p></div>`; return; }
  const ccy = a.tariff.currency, sc = a.scenarios, K = ["low", "base", "high"];
  const conv = (m) => (m.converted ? `<div class="muted small">${money(m.converted.value, m.converted.currency)}</div>` : "");
  const row = (label, fn, basis, formula) => `<tr><td>${trace(esc(label), formula)}</td>${K.map((k) => `<td class="num">${fn(sc[k])}</td>`).join("")}<td>${chip(basis)}</td></tr>`;
  out.innerHTML = `
    <div class="grid g3">
      ${kpi("Energy shortfall (base)", energy(sc.base.energy.shortfall_kwh.value), `range ${energy(sc.low.energy.shortfall_kwh.value)} – ${energy(sc.high.energy.shortfall_kwh.value)}`, "modelled")}
      ${kpi("Gross lost energy value (base)", money(sc.base.gross_lost_energy_value.value, ccy), `lost revenue ${money(sc.base.lost_revenue.value, ccy)}`, "modelled")}
      ${kpi("Net recovery benefit (base)", money(sc.base.recovery.net_benefit.value, ccy), `payback ${sc.base.recovery.payback_years === null ? "n/a" : num(sc.base.recovery.payback_years, 2) + " yrs"} · NPV ${money(sc.base.recovery.npv.value, ccy)}`, "simulated")}
    </div>
    ${a.data_quality.warning ? `<div class="warnbox">${esc(a.data_quality.warning)}</div>` : ""}
    <div class="note">Period ${date(a.period.start)} → ${date(new Date(new Date(a.period.end) - 1))} (${num(a.period.days, 1)} days) · data coverage ${pct(a.data_quality.coverage)} ·
      tariff ${esc(a.tariff.kind)} ${num(a.tariff.price_per_kwh, 4)} ${esc(ccy)}/kWh (${esc(a.tariff.price_date)})${a.tariff.curtailment_compensated ? " · curtailment compensated" : ""} ·
      model ${esc(a.model.name)} v${esc(a.model.version)} ±${a.model.uncertainty_pct}% · engine v${esc(a.engine_version)}. ${esc(a.tariff.note)}</div>
    <div class="panel"><h2>Scenarios</h2><div class="table-wrap"><table>
      <thead><tr><th>Figure (hover for formula)</th><th class="num">Low</th><th class="num">Base</th><th class="num">High</th><th>Basis</th></tr></thead><tbody>
      ${row("Expected generation", (s) => energy(s.energy.expected_kwh.value), "modelled", "Σ modelled expected over compared intervals × baseline multiplier")}
      ${row("Actual generation", (s) => energy(s.energy.actual_kwh.value), "measured", "Σ metered over compared intervals")}
      ${row("Energy shortfall", (s) => energy(s.energy.shortfall_kwh.value), "modelled", "Σ attributed (expected − actual), each kWh attributed once")}
      ${row("Gross lost energy value", (s) => money(s.gross_lost_energy_value.value, ccy) + conv(s.gross_lost_energy_value), "modelled", "lost kWh × energy price")}
      ${row("Lost revenue", (s) => money(s.lost_revenue.value, ccy) + conv(s.lost_revenue), "modelled", "gross value excluding contractually compensated categories")}
      ${row("Addressable loss in period", (s) => energy(s.recovery.addressable_loss_in_period_kwh), "modelled", "Σ lost kWh in the selected addressable categories (historical, sunk)")}
      ${row("Addressable loss still occurring (run-rate window)", (s) => energy(s.recovery.addressable_loss_in_run_rate_window_kwh), "modelled", "addressable kWh in the final run-rate window of the period")}
      ${row("Projected annual addressable loss", (s) => energy(s.recovery.annualised_addressable_loss_kwh), "simulated", "run-rate window loss × 365 ÷ window days (only if loss persists)")}
      ${row("Annual recovered energy", (s) => energy(s.recovery.annual_recovered_kwh), "simulated", "annualised addressable loss × recovery rate")}
      ${row("Annual recovered value", (s) => money(s.recovery.annual_recovered_value.value, ccy), "simulated", "annual recovered kWh × price × price multiplier")}
      ${row("Repair / intervention cost", (s) => money(s.recovery.repair_cost.value, ccy), "simulated", "repair cost × scenario cost multiplier")}
      ${row("Net benefit over horizon", (s) => money(s.recovery.net_benefit.value, ccy) + conv(s.recovery.net_benefit), "simulated", "Σ yearly (value − incremental cost) − repair cost")}
      ${row("NPV", (s) => money(s.recovery.npv.value, ccy), "simulated", "−repair cost + Σ net cash flow ÷ (1 + discount)^year")}
      ${row("ROI", (s) => (s.recovery.roi === null ? "—" : pct(s.recovery.roi, 0)), "simulated", "net benefit ÷ total cost")}
      ${row("Payback", (s) => (s.recovery.payback_years === null ? "not within horizon" : num(s.recovery.payback_years, 2) + " yrs"), "simulated", "time until cumulative net cash flow ≥ 0")}
      ${row("Scenario: recovery rate / days to effect", (s) => `${pct(s.assumptions.recovery_rate, 0)} / ${s.assumptions.days_to_effect} d`, "simulated", "scenario assumptions")}
      </tbody></table></div></div>
    <div class="grid g2">
      <div class="panel"><h2>Loss attribution (base)</h2><div class="chart"><canvas id="c-loss"></canvas></div>
        <div class="table-wrap"><table><thead><tr><th>Cause</th><th class="num">Lost kWh</th><th class="num">Gross value</th><th class="num">Lost revenue</th><th>Addressable</th></tr></thead><tbody>
        ${Object.entries(sc.base.losses).map(([c, v]) => `<tr><td>${esc(CATEGORY_LABEL[c] || c)}</td><td class="num">${num(v.lost_production_kwh.value)}</td>
          <td class="num">${money(v.gross_value.value)}</td><td class="num">${trace(money(v.lost_revenue.value), v.lost_revenue.formula)}</td><td>${v.addressable_by_maintenance ? "yes" : "no"}</td></tr>`).join("")}
        </tbody></table></div></div>
      <div class="panel"><h2>Sensitivity of NPV (±20 %)</h2><div class="chart"><canvas id="c-tornado"></canvas></div>
        <h3 style="margin-top:12px">Cumulative cash flow (base)</h3><div class="chart" style="height:200px"><canvas id="c-cash"></canvas></div></div>
    </div>
    <div class="panel"><h2>Assumptions & traceability</h2><ul class="compact">${a.assumptions.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <p class="small muted">Attribution inputs: ${esc(JSON.stringify(a.attribution_inputs))}</p>
      ${can("workorders:write") ? `<button class="btn" id="fin-wo">Create work order with base-case recovery estimate</button>` : ""}</div>`;
  const losses = Object.entries(sc.base.losses).filter(([, v]) => v.lost_production_kwh.value > 0);
  chart($("#c-loss"), { type: "bar", data: { labels: losses.map(([c]) => CATEGORY_LABEL[c] || c),
    datasets: [{ label: "Lost kWh (base)", data: losses.map(([, v]) => v.lost_production_kwh.value), backgroundColor: losses.map(([, v]) => (v.addressable_by_maintenance ? COLORS.accent : COLORS.expected)) }] },
    options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { title: { display: true, text: "kWh" } }, y: {} } } });
  const t = a.sensitivity;
  chart($("#c-tornado"), { type: "bar", data: { labels: t.map((r) => r.driver.replace(/_/g, " ")),
    datasets: [{ label: `NPV range (${ccy})`, data: t.map((r) => [Math.min(r.npv_at_minus, r.npv_at_plus), Math.max(r.npv_at_minus, r.npv_at_plus)]), backgroundColor: COLORS.info }] },
    options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { title: { display: true, text: `NPV (${ccy})` } }, y: {} } } });
  const cf = sc.base.recovery.cash_flows;
  chart($("#c-cash"), { type: "line", data: { labels: cf.map((r) => `Y${r.year}`), datasets: [{ label: "Cumulative", data: cf.map((r) => r.cumulative), borderColor: COLORS.actual, fill: false }] },
    options: { plugins: { legend: { display: false } }, scales: { x: {}, y: {} } } });
  const wb = $("#fin-wo");
  if (wb) wb.onclick = () => woForm({ plant_id: p.id, title: "Recovery intervention", est_recovery_kwh_per_year: Math.round(sc.base.recovery.annual_recovered_kwh),
    est_recovery_value_per_year: sc.base.recovery.annual_recovered_value.value, estimated_cost: sc.base.recovery.repair_cost.value || null, currency: ccy });
}

async function tabAnomalies(p) {
  const alerts = await api(`/alerts?plant_id=${p.id}`);
  $("#tab").innerHTML = `<div class="panel"><div class="row spread"><h2>Anomalies & diagnostics</h2>
      ${can("alerts:run") ? `<button class="btn primary" id="run-det">Run detection (selected period, default 90 d)</button>` : ""}</div>
      ${alertTable(alerts)}
      <p class="note">Alerts are generated by transparent, versioned statistical rules. An alert is <b>not a confirmed fault</b> until a reviewer records an inspection finding.</p></div>`;
  bindRowLinks($("#tab"));
  const b = $("#run-det");
  if (b) b.onclick = async () => {
    b.disabled = true;
    try { const r = await api(`/plants/${p.id}/anomaly-detection`, { method: "POST", json: periodBody() }); toast(`${r.findings} findings: ${r.created} new, ${r.updated} updated`); tabAnomalies(p); }
    catch (err) { toast(err.message); b.disabled = false; }
  };
}

async function openAlert(id) {
  const a = await api(`/alerts/${id}`);
  const body = openDrawer(`
    <div>${sev(a.severity)} ${status(a.status)} ${chip(a.basis, a.fault_confirmed ? "confirmed by inspection" : "anomaly flag — unconfirmed")}</div>
    <h2>${esc(a.title)}</h2>
    <div class="muted small"><a href="#/plants/${a.plant_id}/anomalies">${esc(a.plant_name)}</a>${a.equipment_id ? ` · <a href="#/equipment/${a.equipment_id}">${esc(a.equipment_name)}</a>` : ""}
      · rule ${esc(a.rule_id)} v${esc(a.rule_version)} · confidence ${esc(a.confidence)} · ${dt(a.window_start)} → ${dt(a.window_end)}</div>
    <div class="kpis">${kpi("Est. energy impact", energy(a.est_energy_loss_kwh), "", "modelled")}${kpi("Est. financial impact", money(a.est_financial_impact, a.currency), "at applicable tariff", "modelled")}
      ${kpi("Priority", num(a.priority_score, 0), `urgency ${num(a.urgency_score, 2)}`)}</div>
    <div><h3>Why this was flagged</h3><p>${esc(a.explanation)}</p></div>
    <div><h3>Evidence</h3><pre class="evidence">${esc(JSON.stringify(a.evidence, null, 2))}</pre></div>
    <div><h3>Recommended inspection / actions</h3><ul class="compact">${a.recommended_actions.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
    ${a.finding_notes ? `<div><h3>Inspection finding</h3><p>${esc(a.finding_notes)}</p><p class="muted small">Reviewed ${dt(a.reviewed_at)}</p></div>` : ""}
    <div><h3>Work orders</h3>${a.work_orders.length ? a.work_orders.map((w) => `<div><a href="#" data-open-wo="${w.id}">${esc(w.title)}</a> ${status(w.status)}</div>`).join("") : `<p class="muted">None</p>`}
      ${can("workorders:write") ? `<button class="btn" id="a-wo">Create work order</button>` : ""}</div>
    ${can("alerts:review") ? `<form id="review" class="form"><label>Review outcome <select name="status">
      ${["acknowledged", "investigating", "confirmed", "false_positive", "resolved"].map((s) => `<option ${s === a.status ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label class="full">Inspection notes (required to confirm / reject) <textarea name="finding_notes">${esc(a.finding_notes || "")}</textarea></label>
      <div class="full row"><button class="btn primary">Save review</button><span class="error" id="rev-err"></span></div></form>` : ""}`);
  body.querySelector("[data-close]").onclick = closeDrawer;
  body.querySelectorAll("[data-open-wo]").forEach((el) => (el.onclick = (e) => { e.preventDefault(); openWorkOrder(el.dataset.openWo); }));
  const wb = body.querySelector("#a-wo");
  if (wb) wb.onclick = () => woForm({ plant_id: a.plant_id, equipment_id: a.equipment_id, alert_id: a.id, title: `Investigate: ${a.title}`.slice(0, 300),
    priority: a.severity === "critical" ? "critical" : a.severity, description: a.recommended_actions.join("\n"), currency: a.currency });
  const rf = body.querySelector("#review");
  if (rf) rf.onsubmit = async (e) => {
    e.preventDefault();
    try { await api(`/alerts/${a.id}/review`, { method: "POST", json: formData(rf) }); toast("Review saved"); openAlert(a.id); }
    catch (err) { body.querySelector("#rev-err").textContent = err.message; }
  };
}

async function woForm(prefill = {}) {
  const [plants, users] = await Promise.all([plantsList(), api("/users/directory")]);
  const eq = prefill.plant_id ? await api(`/plants/${prefill.plant_id}/equipment`) : [];
  const body = openDrawer(`<h2>New work order</h2><form id="wo-form" class="form">
    <label class="full">Title <input name="title" required value="${esc(prefill.title || "")}"></label>
    <label>Plant <select name="plant_id" required>${plants.map((p) => `<option value="${p.id}" ${p.id === prefill.plant_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
    <label>Equipment <select name="equipment_id"><option value="">—</option>${eq.map((e) => `<option value="${e.id}" ${e.id === prefill.equipment_id ? "selected" : ""}>${esc(e.name)} (${esc(e.kind)})</option>`).join("")}</select></label>
    <label>Type <select name="kind">${["corrective", "preventive", "inspection", "commissioning"].map((k) => `<option>${k}</option>`).join("")}</select></label>
    <label>Priority <select name="priority">${["low", "medium", "high", "critical"].map((k) => `<option ${k === (prefill.priority || "medium") ? "selected" : ""}>${k}</option>`).join("")}</select></label>
    <label>Assignee <select name="assignee_id"><option value="">—</option>${users.map((u) => `<option value="${u.id}">${esc(u.full_name)} (${esc(u.role)})</option>`).join("")}</select></label>
    <label>Contractor <input name="contractor"></label><label>Due date <input type="date" name="due_date"></label>
    <label>Estimated cost <input type="number" step="any" name="estimated_cost" value="${prefill.estimated_cost ?? ""}"></label>
    <label>Currency <input name="currency" maxlength="3" value="${esc(prefill.currency || "")}"></label>
    <label>Est. recovery kWh / yr <input type="number" step="any" name="est_recovery_kwh_per_year" value="${prefill.est_recovery_kwh_per_year ?? ""}"></label>
    <label>Est. recovery value / yr <input type="number" step="any" name="est_recovery_value_per_year" value="${prefill.est_recovery_value_per_year ?? ""}"></label>
    <label class="full">Description <textarea name="description">${esc(prefill.description || "")}</textarea></label>
    <input type="hidden" name="alert_id" value="${esc(prefill.alert_id || "")}">
    <div class="full row"><button class="btn primary">Create</button><span class="error" id="wo-err"></span></div></form>`);
  body.querySelector("[data-close]").onclick = closeDrawer;
  const f = body.querySelector("#wo-form");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const d = formData(f);
    if (d.currency) d.currency = d.currency.toUpperCase();
    try { const w = await api("/work-orders", { method: "POST", json: d }); toast("Work order created"); openWorkOrder(w.id); }
    catch (err) { body.querySelector("#wo-err").textContent = err.message; }
  };
}

const NEXT = { open: ["assigned", "in_progress", "on_hold", "cancelled"], assigned: ["in_progress", "on_hold", "cancelled"], in_progress: ["completed", "on_hold", "cancelled"],
  on_hold: ["in_progress", "cancelled"], completed: ["verified", "in_progress"], verified: [], cancelled: ["open"] };

async function openWorkOrder(id) {
  const w = await api(`/work-orders/${id}`);
  const r = w.realised;
  const body = openDrawer(`
    <div>${status(w.status)} <span class="muted small">${esc(w.kind)} · priority ${esc(w.priority)}</span></div>
    <h2>${esc(w.title)}</h2>
    <div class="muted small"><a href="#/plants/${w.plant_id}/maintenance">${esc(w.plant_name)}</a>${w.equipment_id ? ` · <a href="#/equipment/${w.equipment_id}">equipment history</a>` : ""}
      ${w.alert_id ? ` · <a href="#" id="wo-alert">source alert</a>` : ""} · due ${date(w.due_date)}${w.overdue ? ` <b style="color:var(--bad)">overdue</b>` : ""}</div>
    ${w.description ? `<p style="white-space:pre-wrap">${esc(w.description)}</p>` : ""}
    <div class="kpis">${kpi("Estimated cost", money(w.estimated_cost, w.currency))}${kpi("Actual cost", money(w.actual_cost, w.currency), "", w.actual_cost !== null ? "measured" : "")}
      ${kpi("Est. recovery / yr", money(w.est_recovery_value_per_year, w.currency), energy(w.est_recovery_kwh_per_year), "simulated")}
      ${kpi("Realised recovery / yr", r ? money(r.realised_recovery_value_per_year, r.currency) : "—", r ? `${energy(r.realised_recovery_kwh_per_year)} · vs estimate ${r.variance_vs_estimate_pct === null ? "—" : num(r.variance_vs_estimate_pct, 0) + "%"}` : "evaluate after completion", r ? "measured" : "")}</div>
    ${r ? `<div class="note">Post-repair evaluation (${esc(r.scope)}): PI before ${pct(r.before.performance_index)} (${date(r.before.start)}→${date(r.before.end)}) → after ${pct(r.after.performance_index)} (${date(r.after.start)}→${date(r.after.end)}).
      ${r.simple_payback_years !== null ? `Simple payback ${num(r.simple_payback_years, 2)} yrs.` : ""} ${r.caveats.map(esc).join(" ")}</div>` : ""}
    ${can("workorders:execute") ? `<form id="wo-upd" class="form">
      <label>Move to <select name="status"><option value="">(no change)</option>${(NEXT[w.status] || []).map((s) => `<option>${s}</option>`).join("")}</select></label>
      <label>Actual cost <input type="number" step="any" name="actual_cost" value="${w.actual_cost ?? ""}"></label>
      <label>Contractor <input name="contractor" value="${esc(w.contractor || "")}"></label>
      <label class="full">Note / handover record <textarea name="note"></textarea></label>
      <div class="full row"><button class="btn primary">Update</button>
        ${["completed", "verified"].includes(w.status) && can("workorders:write") ? `<button type="button" class="btn" id="wo-eval">Evaluate realised recovery</button>` : ""}
        <span class="error" id="wo-err"></span></div></form>` : ""}
    <div><h3>History</h3>${w.events.map((e) => `<div class="small"><b>${esc(e.kind.replace(/_/g, " "))}</b> · ${dt(e.created_at)}<br>${esc(e.message || "")}
      ${e.data && e.data.from ? `<span class="muted">${esc(e.data.from)} → ${esc(e.data.to)}</span>` : ""}</div>`).join("<hr style='border:0;border-top:1px solid var(--line)'>")}</div>`);
  body.querySelector("[data-close]").onclick = closeDrawer;
  const al = body.querySelector("#wo-alert");
  if (al) al.onclick = (e) => { e.preventDefault(); openAlert(w.alert_id); };
  const f = body.querySelector("#wo-upd");
  if (f) f.onsubmit = async (e) => {
    e.preventDefault();
    const d = formData(f);
    try { await api(`/work-orders/${w.id}`, { method: "PATCH", json: d }); toast("Updated"); openWorkOrder(w.id); }
    catch (err) { body.querySelector("#wo-err").textContent = err.message; }
  };
  const ev = body.querySelector("#wo-eval");
  if (ev) ev.onclick = async () => {
    try { const res = await api(`/work-orders/${w.id}/evaluate-recovery`, { method: "POST" }); toast(res.status === "evaluated" ? "Recovery evaluated" : res.message || res.status); openWorkOrder(w.id); }
    catch (err) { body.querySelector("#wo-err").textContent = err.message; }
  };
}

async function tabMaintenance(p) {
  const [wos, recs] = await Promise.all([api(`/work-orders?plant_id=${p.id}`), api(`/asset-records?plant_id=${p.id}`)]);
  $("#tab").innerHTML = `
    <div class="panel"><div class="row spread"><h2>Work orders</h2>${can("workorders:write") ? `<button class="btn primary" id="new-wo">New work order</button>` : ""}</div>${woTable(wos)}</div>
    <div class="panel"><div class="row spread"><h2>Service history (inspections, commissioning, handovers, corrective actions)</h2></div>
      <div class="table-wrap"><table><thead><tr><th>Date</th><th>Type</th><th>Title</th><th>Contractor / by</th><th>Notes</th></tr></thead><tbody>
      ${recs.map((r) => `<tr><td>${date(r.performed_at)}</td><td>${esc(r.kind.replace(/_/g, " "))}</td><td>${esc(r.title)}</td><td>${esc(r.contractor || r.performed_by || "—")}</td><td class="small">${esc(r.notes || "")}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">No records</td></tr>`}
      </tbody></table></div>
      ${can("workorders:execute") ? `<h3 style="margin-top:12px">Add record</h3><form id="rec-form" class="form">
        <label>Type <select name="kind">${["inspection", "commissioning", "handover", "warranty_claim", "service", "corrective_action", "replacement"].map((k) => `<option>${k}</option>`).join("")}</select></label>
        <label>Title <input name="title" required></label><label>Date <input type="date" name="performed_at" required></label>
        <label>Contractor <input name="contractor"></label><label>Performed by <input name="performed_by"></label>
        <label class="full">Notes <textarea name="notes"></textarea></label><div class="full"><button class="btn">Add record</button> <span class="error" id="rec-err"></span></div></form>` : ""}</div>`;
  bindRowLinks($("#tab"));
  const nb = $("#new-wo");
  if (nb) nb.onclick = () => woForm({ plant_id: p.id, currency: p.currency });
  const rf = $("#rec-form");
  if (rf) rf.onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/asset-records", { method: "POST", json: { ...formData(rf), plant_id: p.id } }); tabMaintenance(p); }
    catch (err) { $("#rec-err").textContent = err.message; }
  };
}

async function tabEquipment(p) {
  const eq = await api(`/plants/${p.id}/equipment`);
  $("#tab").innerHTML = `<div class="panel"><h2>Equipment register</h2><div class="table-wrap"><table>
    <thead><tr><th>Name</th><th>Type</th><th>Manufacturer / model</th><th>Serial</th><th class="num">Rated kW</th><th class="num">DC kWp</th><th>Commissioned</th><th>Warranty</th><th>Status</th></tr></thead><tbody>
    ${eq.map((e) => `<tr class="click" data-href="#/equipment/${e.id}"><td>${esc(e.name)}</td><td>${esc(e.kind)}</td><td>${esc(e.manufacturer || "")} ${esc(e.model || "")}</td>
      <td class="small">${esc(e.serial_number || "")}</td><td class="num">${num(e.rated_power_kw, 0)}</td><td class="num">${num(e.dc_capacity_kwp, 0)}</td><td>${date(e.commissioning_date)}</td>
      <td>${e.warranty_end ? `${date(e.warranty_end)} ${e.warranty_active ? `<span class="chip verified">active</span>` : `<span class="chip">expired</span>`}` : "—"}</td><td>${status(e.status)}</td></tr>`).join("")}
    </tbody></table></div>
    ${can("plants:write") ? `<h3 style="margin-top:12px">Add equipment</h3><form id="eq-form" class="form">
      <label>Type <select name="kind">${["inverter", "string", "combiner", "meter", "weather_station", "transformer", "tracker", "sensor", "module_block"].map((k) => `<option>${k}</option>`).join("")}</select></label>
      <label>Name <input name="name" required></label><label>Manufacturer <input name="manufacturer"></label><label>Model <input name="model"></label>
      <label>Serial number <input name="serial_number"></label><label>Rated kW <input type="number" step="any" name="rated_power_kw"></label>
      <label>DC kWp <input type="number" step="any" name="dc_capacity_kwp"></label><label>Commissioned <input type="date" name="commissioning_date"></label>
      <label>Warranty end <input type="date" name="warranty_end"></label><label>Latitude <input type="number" step="any" name="latitude"></label><label>Longitude <input type="number" step="any" name="longitude"></label>
      <div class="full"><button class="btn">Add</button> <span class="error" id="eq-err"></span></div></form>` : ""}</div>`;
  bindRowLinks($("#tab"));
  const f = $("#eq-form");
  if (f) f.onsubmit = async (e) => {
    e.preventDefault();
    try { await api(`/plants/${p.id}/equipment`, { method: "POST", json: formData(f) }); tabEquipment(p); }
    catch (err) { $("#eq-err").textContent = err.message; }
  };
}

async function viewEquipment(id) {
  const h = await api(`/equipment/${id}/history`);
  setCrumbs([{ label: "Plants", href: "#/plants" }, { label: h.equipment.name }]);
  view().innerHTML = `<h1>${esc(h.equipment.name)}</h1><div class="muted small">Serial ${esc(h.equipment.serial_number || "—")} · warranty until ${date(h.equipment.warranty_end)}</div>
    <div class="panel"><h2>Alerts</h2>${h.alerts.length ? `<table><tbody>${h.alerts.map((a) => `<tr class="click" data-alert="${a.id}"><td>${sev(a.severity)}</td><td>${esc(a.title)}</td><td>${date(a.window_start)}</td><td>${status(a.status)}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">None</p>`}</div>
    <div class="panel"><h2>Work orders</h2>${woTable(h.work_orders)}</div>
    <div class="panel"><h2>Service records</h2>${h.records.length ? `<table><tbody>${h.records.map((r) => `<tr><td>${date(r.performed_at)}</td><td>${esc(r.kind)}</td><td>${esc(r.title)}</td><td>${esc(r.contractor || "")}</td><td class="small">${esc(r.notes || "")}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">None</p>`}</div>`;
  bindRowLinks();
}

async function tabCarbon(p) {
  const c = await api(`/plants/${p.id}/carbon?${periodQS()}`);
  const e = c.estimate;
  $("#tab").innerHTML = `<div class="grid g2"><div class="panel"><h2>Estimated avoided emissions ${chip("modelled", "estimate")}</h2>
      ${e ? `<div class="kpis">${kpi("Avoided (estimated)", `${num(e.estimated_avoided_tco2e, 1)} tCO₂e`, `${date(c.start)} → ${date(new Date(new Date(c.end) - 1))}`)}${kpi("Metered generation", energy(e.generation_kwh), "", "measured")}
        ${kpi("Grid factor", `${e.factor.kg_co2e_per_kwh} kgCO₂e/kWh`, `${esc(e.factor.method)} · ${e.factor.year || ""}`)}</div>
        <p class="small"><b>Factor:</b> ${esc(e.factor.name)}<br><b>Source:</b> ${esc(e.factor.source)}</p>
        ${e.warnings.map((w) => `<div class="warnbox">${esc(w)}</div>`).join("")}<p class="note">${esc(e.methodology)}</p>` : `<div class="warnbox">${esc(c.warning)}</div>`}
      <p class="note">${esc(c.note)}</p></div>
    <div class="panel"><h2>Independently verified reductions ${chip("verified")}</h2>
      ${c.verified_reductions.length ? `<table><thead><tr><th>Period</th><th class="num">tCO₂e</th><th>Standard</th><th>Verifier</th><th>Certificate</th></tr></thead><tbody>
        ${c.verified_reductions.map((v) => `<tr><td>${v.period_start} – ${v.period_end}</td><td class="num">${num(v.tco2e, 1)}</td><td>${esc(v.standard)}</td><td>${esc(v.verifier)}</td><td>${esc(v.certificate_ref || "")}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">No verified reductions recorded.</p>`}
      ${can("carbon:write") ? `<form id="vr-form" class="form"><label>From <input type="date" name="period_start" required></label><label>To <input type="date" name="period_end" required></label>
        <label>tCO₂e <input type="number" step="any" name="tco2e" required></label><label>Standard <input name="standard" required placeholder="e.g. Verra VCS"></label>
        <label>Verifier <input name="verifier" required></label><label>Certificate ref <input name="certificate_ref"></label>
        <div class="full"><button class="btn">Record verified reduction</button> <span class="error" id="vr-err"></span></div></form>` : ""}</div></div>`;
  const f = $("#vr-form");
  if (f) f.onsubmit = async (ev) => {
    ev.preventDefault();
    try { await api("/verified-reductions", { method: "POST", json: { ...formData(f), plant_id: p.id } }); tabCarbon(p); }
    catch (err) { $("#vr-err").textContent = err.message; }
  };
}

async function tabForecast(p) {
  const [f, b] = await Promise.all([api(`/plants/${p.id}/forecast?hours=72`), api(`/plants/${p.id}/forecast/backtest?days=14`)]);
  $("#tab").innerHTML = `<div class="panel"><div class="row spread"><h2>Production forecast — next 72 h from ${dt(f.start)}</h2>${chip("modelled")}</div>
      <div class="kpis">${kpi("Forecast (P50)", energy(f.total_p50_kwh), `${esc(f.model)} v${esc(f.model_version)}`)}${kpi("Confidence", esc(f.confidence), `P10–P90 band ${f.relative_band === null ? "—" : pct(f.relative_band, 0)} of P50`)}
        ${kpi("History coverage", pct(f.data_coverage), `${f.lookback_days}-day lookback`)}</div>
      ${f.warnings.map((w) => `<div class="warnbox">${esc(w)}</div>`).join("")}
      <div class="chart"><canvas id="c-fc"></canvas></div><p class="note">${esc(f.note)}</p></div>
    <div class="panel"><h2>Backtest: forecast vs actual (last ${b.days} days)</h2>
      <div class="kpis">${kpi("MAE (hourly)", b.mae_kwh === null ? "—" : `${num(b.mae_kwh, 0)} kWh`)}${kpi("nRMSE", b.nrmse === null ? "—" : pct(b.nrmse))}${kpi("Bias", b.bias_pct === null ? "—" : `${num(b.bias_pct, 1)}%`)}</div>
      <div class="chart"><canvas id="c-bt"></canvas></div></div>`;
  const pts = f.points;
  chart($("#c-fc"), { type: "line", data: { labels: pts.map((x) => x.ts.slice(5, 13).replace("T", " ")), datasets: [
    { label: "P90", data: pts.map((x) => x.p90_kwh), borderColor: "transparent", backgroundColor: "rgba(15,107,92,.15)", fill: "+1", pointRadius: 0 },
    { label: "P10", data: pts.map((x) => x.p10_kwh), borderColor: "transparent", pointRadius: 0, fill: false },
    { label: "P50 forecast (kWh)", data: pts.map((x) => x.p50_kwh), borderColor: COLORS.actual, pointRadius: 0 }] },
    options: { scales: { x: { ticks: { maxTicksLimit: 12 } }, y: {} } } });
  chart($("#c-bt"), { type: "bar", data: { labels: (b.per_day || []).map((x) => x.date), datasets: [
    { label: "Forecast kWh", data: b.per_day.map((x) => x.forecast_kwh), backgroundColor: COLORS.expected },
    { label: "Actual kWh", data: b.per_day.map((x) => x.actual_kwh), backgroundColor: COLORS.actual }] }, options: { scales: { x: {}, y: {} } } });
}

async function tabMap(p) {
  const g = await api(`/gis/plants/${p.id}/assets`);
  $("#tab").innerHTML = `<div class="panel"><h2>Site boundary & equipment locations</h2><div id="site-map" class="map" style="height:480px"></div>
    <p class="note">Geometry stored in PostGIS (geography, WGS84). Equipment without coordinates is not shown.</p></div>`;
  const m = L.map("site-map");
  L.tileLayer(S.config.map_tile_url, { maxZoom: 19, attribution: S.config.map_tile_attribution }).addTo(m);
  const layer = L.geoJSON(g, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: f.properties.kind === "plant" ? 9 : 6, color: f.properties.kind === "inverter" ? COLORS.accent : COLORS.actual, fillOpacity: 0.7 }),
    style: { color: COLORS.actual, weight: 2, fillOpacity: 0.08 },
    onEachFeature: (f, l) => l.bindPopup(`<b>${esc(f.properties.name)}</b><br>${esc(f.properties.kind)}${f.properties.serial_number ? `<br>${esc(f.properties.serial_number)}` : ""}
      ${f.properties.equipment_id ? `<br><a href="#/equipment/${f.properties.equipment_id}">history</a>` : ""}`),
  }).addTo(m);
  m.fitBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 17 });
  S.maps.push(m);
}

async function tabData(p) {
  const [imports, events, contracts, runs] = await Promise.all([api(`/plants/${p.id}/imports`), api(`/plants/${p.id}/events`), api(`/plants/${p.id}/contracts`), api(`/plants/${p.id}/model-runs`)]);
  $("#tab").innerHTML = `
    <div class="grid g2">
      <div class="panel"><h2>Import CSV</h2>
        ${can("data:import") ? `<form id="imp-form" class="form">
          <label>Dataset <select name="kind"><option value="energy">Energy (kWh per interval)</option><option value="weather">Weather / irradiance</option><option value="events">Operational events</option></select></label>
          <label>Timezone for naive timestamps <input name="timezone" value="${esc(p.timezone)}"></label>
          <label class="full">File <input type="file" name="file" accept=".csv,text/csv" required></label>
          <label class="row"><input type="checkbox" name="dry_run" checked> Validate only (dry run)</label>
          <div class="full"><button class="btn primary">Upload</button></div></form>
          <div id="imp-out"></div>` : `<p class="muted">You do not have permission to import data.</p>`}
        <div class="note">Energy columns: <code>timestamp, energy_kwh</code> (or <code>energy_wh</code>/<code>energy_mwh</code>), optional <code>equipment</code> (serial or name), <code>power_kw</code>, <code>interval_minutes</code>, <code>quality</code>.
          Weather: <code>timestamp, poa_wm2, ghi_wm2, ambient_temp_c, module_temp_c, wind_ms</code>. Events: <code>start, end, category, equipment, description, confirmed</code>.
          Timestamps are interval start, ISO-8601. Duplicates are detected within the file and against stored data. Sample files: <code>sample_data/</code>.</div></div>
      <div class="panel"><h2>Exports & baselines</h2>
        <div class="row">${["energy", "weather", "expected"].map((d) => `<button class="btn" data-export="${d}">${d}.csv</button>`).join("")}</div>
        <h3 style="margin-top:14px">Expected-generation model runs</h3>
        <table><tbody>${runs.slice(0, 5).map((r) => `<tr><td class="small">${dt(r.created_at)}</td><td>${esc(r.model)} v${esc(r.model_version)}</td><td class="num">${num(r.hours_modelled)} h</td><td>±${r.uncertainty_pct}%</td></tr>`).join("") || `<tr><td class="muted">No runs</td></tr>`}</tbody></table>
        ${can("data:import") ? `<div class="row" style="margin-top:8px"><button class="btn" id="rerun">Re-run model</button>
          ${hasModule("integrations") ? `<button class="btn" id="pvgis">Fetch PVGIS long-term baseline</button>` : ""}</div><div id="pvgis-out" class="small"></div>` : ""}</div>
    </div>
    <div class="panel"><h2>Import history</h2><div class="table-wrap"><table><thead><tr><th>When</th><th>File</th><th>Type</th><th class="num">Rows</th><th class="num">Imported</th><th class="num">Duplicates</th><th class="num">Rejected</th><th>SHA-256</th></tr></thead><tbody>
      ${imports.map((b) => `<tr><td class="small">${dt(b.created_at)}</td><td>${esc(b.filename || "")}</td><td>${esc(b.kind)}</td><td class="num">${num(b.rows_total)}</td><td class="num">${num(b.rows_imported)}</td>
        <td class="num">${num(b.rows_duplicate)}</td><td class="num">${num(b.rows_rejected)}</td><td class="small muted">${esc((b.sha256 || "").slice(0, 12))}</td></tr>`).join("") || `<tr><td colspan="8" class="muted">No uploads yet (demo data was generated synthetically).</td></tr>`}</tbody></table></div></div>
    <div class="grid g2">
      <div class="panel"><h2>Energy contracts / tariffs</h2><table><thead><tr><th>Type</th><th>Counterparty</th><th class="num">Price/kWh</th><th>Valid</th><th>Curtailment comp.</th><th class="num">Export cap</th></tr></thead><tbody>
        ${contracts.map((c) => `<tr><td>${esc(c.kind)}</td><td>${esc(c.counterparty || "")}</td><td class="num">${num(c.price_per_kwh, 4)} ${esc(c.currency)}${c.escalation_pct_per_year ? `<div class="muted small">+${c.escalation_pct_per_year}%/yr</div>` : ""}</td>
          <td class="small">${date(c.valid_from)} → ${c.valid_to ? date(c.valid_to) : "open"}</td><td>${c.curtailment_compensated ? "yes" : "no"}</td><td class="num">${c.export_cap_kw ? num(c.export_cap_kw) + " kW" : "—"}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">No contract — financial analysis needs one.</td></tr>`}</tbody></table>
        ${can("plants:write") ? `<form id="ct-form" class="form" style="margin-top:10px"><label>Type <select name="kind"><option value="ppa">PPA</option><option value="feed_in_tariff">Feed-in tariff</option><option value="self_consumption">Self-consumption (avoided retail)</option><option value="merchant">Merchant / market</option></select></label>
          <label>Price per kWh <input type="number" step="any" name="price_per_kwh" required></label><label>Currency <input name="currency" maxlength="3" value="${esc(p.currency)}" required></label>
          <label>Valid from <input type="date" name="valid_from" required></label><label>Valid to <input type="date" name="valid_to"></label>
          <label>Escalation %/yr <input type="number" step="any" name="escalation_pct_per_year" value="0"></label><label>Export cap (kW) <input type="number" step="any" name="export_cap_kw"></label>
          <label>Counterparty <input name="counterparty"></label><label class="row"><input type="checkbox" name="curtailment_compensated"> Curtailment compensated</label>
          <div class="full"><button class="btn">Add contract</button> <span class="error" id="ct-err"></span></div></form>` : ""}</div>
      <div class="panel"><h2>Operational events</h2><div class="table-wrap" style="max-height:300px;overflow:auto"><table><thead><tr><th>Category</th><th>Start</th><th class="num">Hours</th><th>Confirmed</th><th>Description</th></tr></thead><tbody>
        ${events.map((e) => `<tr><td>${esc(e.category)}</td><td class="small">${dt(e.start_ts)}</td><td class="num">${num(e.duration_hours, 1)}</td><td>${e.confirmed ? "yes" : "no"}</td><td class="small">${esc(e.description || "")}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">None</td></tr>`}</tbody></table></div>
        ${can("data:import") ? `<form id="ev-form" class="form" style="margin-top:10px"><label>Category <select name="category">${["outage", "grid_outage", "curtailment", "planned_maintenance", "inverter_fault", "soiling", "other"].map((c) => `<option>${c}</option>`).join("")}</select></label>
          <label>Start (UTC) <input type="datetime-local" name="start_ts" required></label><label>End (UTC) <input type="datetime-local" name="end_ts" required></label>
          <label class="row"><input type="checkbox" name="confirmed"> Confirmed</label><label class="full">Description <input name="description"></label>
          <div class="full"><button class="btn">Record event</button> <span class="error" id="ev-err"></span></div></form>` : ""}</div>
    </div>`;
  document.querySelectorAll("[data-export]").forEach((b) => (b.onclick = () => download(`/plants/${p.id}/exports/${b.dataset.export}.csv?${periodQS()}`, `${p.code}_${b.dataset.export}.csv`)));
  const imp = $("#imp-form");
  if (imp) imp.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData();
    fd.append("file", imp.elements.file.files[0]);
    const qs = new URLSearchParams({ kind: imp.elements.kind.value, dry_run: imp.elements.dry_run.checked, timezone: imp.elements.timezone.value });
    $("#imp-out").innerHTML = `<p class="muted">Uploading…</p>`;
    try {
      const r = await api(`/plants/${p.id}/imports?${qs}`, { method: "POST", body: fd });
      $("#imp-out").innerHTML = `<div class="note">${r.dry_run ? "<b>Dry run</b> — nothing stored. " : "<b>Imported.</b> "}
        Rows ${num(r.total_rows ?? r.rows_total)} · valid ${num(r.valid_rows ?? r.rows_imported)} · rejected ${num(r.rejected_rows ?? r.rows_rejected)} · duplicates ${num((r.duplicates_in_file ?? 0) + (r.duplicates_existing ?? r.rows_duplicate ?? 0))}
        ${r.model_run ? `· expected model re-run over ${num(r.model_run.hours_modelled)} h` : ""}</div>
        ${(r.warnings || []).map((w) => `<div class="warnbox">${esc(w)}</div>`).join("")}
        ${(r.errors || []).length ? `<pre class="evidence">${esc(r.errors.slice(0, 30).map((x) => `row ${x.row} [${x.field}] ${x.message}`).join("\n"))}</pre>` : ""}`;
      if (!r.dry_run) setTimeout(() => tabData(p), 1500);
    } catch (err) { $("#imp-out").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
  };
  const ct = $("#ct-form");
  if (ct) ct.onsubmit = async (e) => {
    e.preventDefault();
    const d = formData(ct);
    d.currency = d.currency.toUpperCase();
    try { await api(`/plants/${p.id}/contracts`, { method: "POST", json: d }); tabData(p); } catch (err) { $("#ct-err").textContent = err.message; }
  };
  const evf = $("#ev-form");
  if (evf) evf.onsubmit = async (e) => {
    e.preventDefault();
    const d = formData(evf);
    d.start_ts += ":00Z"; d.end_ts += ":00Z";
    try { await api(`/plants/${p.id}/events`, { method: "POST", json: d }); tabData(p); } catch (err) { $("#ev-err").textContent = err.message; }
  };
  const rr = $("#rerun");
  if (rr) rr.onclick = async () => { rr.disabled = true; try { const r = await api(`/plants/${p.id}/model-runs`, { method: "POST" }); toast(`Model run: ${r.hours_modelled} h`); tabData(p); } catch (err) { toast(err.message); rr.disabled = false; } };
  const pv = $("#pvgis");
  if (pv) pv.onclick = async () => {
    pv.disabled = true;
    $("#pvgis-out").textContent = "Contacting PVGIS…";
    try { const r = await api(`/plants/${p.id}/baselines/pvgis`, { method: "POST" });
      $("#pvgis-out").innerHTML = `<div class="note">PVGIS (${esc(r.model_version)}): ${energy(r.annual_kwh)}/yr · ${num(r.specific_yield_kwh_per_kwp, 0)} kWh/kWp. ${esc(r.note)} ${esc(r.license_notes)}</div>`; }
    catch (err) { $("#pvgis-out").innerHTML = `<span class="error">${esc(err.message)}</span>`; }
    pv.disabled = false;
  };
}

// ------------------------------------------------------------------ portfolio views
async function viewAlerts() {
  setCrumbs([{ label: "Anomalies & alerts" }]);
  const st = new URLSearchParams(location.hash.split("?")[1] || "");
  const [alerts, stats] = await Promise.all([api(`/alerts?status=${st.get("status") || "active"}`), api("/alerts/stats")]);
  view().innerHTML = `<div class="row spread"><h1>Anomalies & alerts</h1>
      <select id="a-status">${["active", "open", "investigating", "confirmed", "false_positive", "resolved", ""].map((s) => `<option value="${s}" ${s === (st.get("status") ?? "active") ? "selected" : ""}>${s || "all"}</option>`).join("")}</select></div>
    <div class="panel">${alertTable(alerts, true)}</div>
    <div class="panel"><h2>Rule validation (Stage 2 pilot metric)</h2><table><thead><tr><th>Rule</th><th class="num">Alerts</th><th class="num">Confirmed</th><th class="num">False positive</th><th class="num">Unreviewed</th><th class="num">Precision</th></tr></thead><tbody>
      ${Object.entries(stats.by_rule).map(([r, s]) => `<tr><td>${esc(r)}</td><td class="num">${s.total}</td><td class="num">${s.confirmed}</td><td class="num">${s.false_positive}</td><td class="num">${s.open}</td><td class="num">${s.precision === null ? "—" : pct(s.precision, 0)}</td></tr>`).join("")}
      </tbody></table><p class="note">${esc(stats.note)}</p></div>`;
  $("#a-status").onchange = (e) => { location.hash = `#/alerts?status=${e.target.value}`; };
  bindRowLinks();
}
routes.unshift([/^#\/alerts\?.*$/, viewAlerts]);

async function viewWorkOrders() {
  setCrumbs([{ label: "Maintenance" }]);
  const wos = await api("/work-orders");
  const active = wos.filter((w) => !["completed", "verified", "cancelled"].includes(w.status));
  const done = wos.filter((w) => ["completed", "verified"].includes(w.status));
  view().innerHTML = `<div class="row spread"><h1>Maintenance work orders</h1>${can("workorders:write") ? `<button class="btn primary" id="new-wo">New work order</button>` : ""}</div>
    <div class="panel"><h2>Active (${active.length})</h2>${woTable(active)}</div>
    <div class="panel"><h2>Completed — estimated vs realised recovery</h2>${done.length ? `<div class="table-wrap"><table><thead><tr><th>Work order</th><th>Plant</th><th>Completed</th><th class="num">Actual cost</th><th class="num">Est. kWh/yr</th><th class="num">Realised kWh/yr</th><th class="num">Variance</th></tr></thead><tbody>
      ${done.map((w) => `<tr class="click" data-wo="${w.id}"><td>${esc(w.title)}</td><td>${esc(w.plant_name)}</td><td>${date(w.completed_at)}</td><td class="num">${money(w.actual_cost, w.currency)}</td>
        <td class="num">${num(w.est_recovery_kwh_per_year)} ${chip("simulated")}</td><td class="num">${w.realised ? num(w.realised.realised_recovery_kwh_per_year) + " " + chip("measured") : "<span class='muted'>not evaluated</span>"}</td>
        <td class="num">${w.realised && w.realised.variance_vs_estimate_pct !== null ? num(w.realised.variance_vs_estimate_pct, 0) + "%" : "—"}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">None yet.</p>`}</div>`;
  bindRowLinks();
  const nb = $("#new-wo");
  if (nb) nb.onclick = () => woForm({});
}

async function viewSimulator() {
  setCrumbs([{ label: "Recovery simulator" }]);
  view().innerHTML = `<h1>Recovery what-if simulator</h1><p class="muted">Value an intervention against a loss of known annual size. All outputs are <b>simulated</b>. For plant-specific, data-driven analysis use a plant's “Financial loss & recovery” tab.</p>
    <div class="panel"><form id="sim" class="form">
      <label>Annual addressable loss (kWh) <input type="number" name="annual_addressable_loss_kwh" value="250000" step="any"></label>
      <label>Recovery rate (0–1) <input type="number" name="recovery_rate" value="0.8" step="0.05"></label>
      <label>Energy value per kWh <input type="number" name="energy_price" value="0.085" step="any"></label>
      <label>Currency <input name="currency" value="${esc(S.me.organization.currency)}" maxlength="3"></label>
      <label>Repair cost <input type="number" name="repair_cost" value="12000" step="any"></label>
      <label>Incremental annual cost <input type="number" name="incremental_annual_cost" value="0" step="any"></label>
      <label>Days to effect <input type="number" name="days_to_effect" value="21"></label><label>Horizon (years) <input type="number" name="horizon_years" value="5"></label>
      <label>Discount rate <input type="number" name="discount_rate" value="0.08" step="0.01"></label><label>Degradation %/yr <input type="number" name="annual_degradation_pct" value="0.5" step="0.05"></label>
      <label>Price escalation %/yr <input type="number" name="price_escalation_pct" value="0" step="0.1"></label>
      <div class="full"><button class="btn primary">Simulate</button></div></form></div><div id="sim-out"></div>`;
  const f = $("#sim");
  const run = async () => {
    cleanup();
    const d = formData(f);
    d.currency = (d.currency || "USD").toUpperCase();
    try {
      const r = await api("/finance/recovery-simulation", { method: "POST", json: d });
      const x = r.result, c = r.currency;
      $("#sim-out").innerHTML = `<div class="kpis">${kpi("Annual recovered energy", energy(x.annual_recovered_kwh), "", "simulated")}${kpi("Annual recovered value", money(x.annual_recovered_value, c), "", "simulated")}
        ${kpi("Net benefit", money(x.net_benefit, c), `over ${d.horizon_years} years`, "simulated")}${kpi("NPV", money(x.npv, c), `at ${pct(d.discount_rate, 0)}`, "simulated")}
        ${kpi("ROI", x.roi === null ? "—" : pct(x.roi, 0), "", "simulated")}${kpi("Payback", x.payback_years === null ? "not within horizon" : `${num(x.payback_years, 2)} yrs`, "", "simulated")}</div>
        <div class="grid g2"><div class="panel"><h2>Cash flows</h2><div class="chart"><canvas id="c-cf"></canvas></div></div><div class="panel"><h2>NPV sensitivity (±20 %)</h2><div class="chart"><canvas id="c-sens"></canvas></div></div></div>
        <div class="panel"><h2>Formulae</h2><ul class="compact">${Object.entries(r.formulae).map(([k, v]) => `<li><b>${esc(k)}</b> = ${esc(v)}</li>`).join("")}</ul></div>`;
      chart($("#c-cf"), { data: { labels: x.cash_flows.map((y) => `Y${y.year}`), datasets: [
        { type: "bar", label: "Net cash flow", data: x.cash_flows.map((y) => y.net_cash_flow), backgroundColor: COLORS.actual },
        { type: "line", label: "Cumulative (after repair cost)", data: x.cash_flows.map((y) => y.cumulative), borderColor: COLORS.accent }] }, options: { scales: { x: {}, y: {} } } });
      chart($("#c-sens"), { type: "bar", data: { labels: r.sensitivity.map((s) => s.driver.replace(/_/g, " ")), datasets: [{ label: "NPV range", data: r.sensitivity.map((s) => [Math.min(s.npv_at_minus, s.npv_at_plus), Math.max(s.npv_at_minus, s.npv_at_plus)]), backgroundColor: COLORS.info }] },
        options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: {}, y: {} } } });
    } catch (err) { $("#sim-out").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
  };
  f.onsubmit = (e) => { e.preventDefault(); run(); };
  run();
}

async function viewCarbon() {
  setCrumbs([{ label: "Carbon & sustainability" }]);
  const c = await api(`/carbon/portfolio?${periodQS()}`);
  view().innerHTML = `<h1>Carbon & sustainability</h1>
    <div class="kpis">${kpi("Estimated avoided emissions", `${num(c.estimated_avoided_tco2e, 1)} tCO₂e`, `${date(c.start)} → ${date(new Date(new Date(c.end) - 1))}`, "modelled")}
      ${kpi("Independently verified", `${num(c.verified_tco2e, 1)} tCO₂e`, "reported separately", "verified")}</div>
    <div class="warnbox">${esc(c.note)}</div>
    <div class="panel"><table><thead><tr><th>Plant</th><th class="num">Metered generation</th><th class="num">Factor kgCO₂e/kWh</th><th>Factor source</th><th class="num">Estimated tCO₂e</th><th class="num">Verified tCO₂e</th></tr></thead><tbody>
      ${c.plants.map((r) => `<tr class="click" data-href="#/plants/${r.plant_id}/carbon"><td>${esc(r.plant_name)}</td><td class="num">${r.estimate ? energy(r.estimate.generation_kwh) : "—"}</td>
        <td class="num">${r.estimate ? r.estimate.factor.kg_co2e_per_kwh : "—"}${r.estimate && r.estimate.factor.is_sample ? ` <span class="chip simulated" title="illustrative sample factor">sample</span>` : ""}</td>
        <td class="small">${r.estimate ? esc(r.estimate.factor.name) : esc(r.warning || "")}</td><td class="num">${r.estimate ? num(r.estimate.estimated_avoided_tco2e, 1) : "—"}</td>
        <td class="num">${num(r.verified_reductions.reduce((a, v) => a + v.tco2e, 0), 1)}</td></tr>`).join("")}</tbody></table></div>`;
  bindRowLinks();
}

async function viewReports() {
  setCrumbs([{ label: "Reports" }]);
  const plants = await plantsList();
  view().innerHTML = `<h1>Reports</h1><div class="panel"><form id="rep" class="form">
    <label>Plant <select name="plant">${plants.map((p) => `<option value="${p.id}" data-code="${esc(p.code)}">${esc(p.name)}</option>`).join("")}</select></label>
    <label>Report <select name="kind"><option value="performance">Plant performance</option><option value="financial">Financial loss & recovery</option><option value="maintenance">Maintenance & alerts</option><option value="carbon">Carbon (estimated vs verified)</option></select></label>
    <label>Format <select name="fmt"><option value="pdf">PDF</option><option value="csv">CSV</option></select></label>
    <div><button class="btn primary">Download</button></div></form>
    <p class="note">Reports use the period selected at the top (default: latest 30 days of data) and state their data sources, model versions, figure basis and assumptions.</p></div>`;
  $("#rep").onsubmit = (e) => {
    e.preventDefault();
    const f = e.target;
    const code = f.plant.selectedOptions[0].dataset.code;
    download(`/plants/${f.plant.value}/reports/${f.kind.value}.${f.fmt.value}?${periodQS()}`, `${code}_${f.kind.value}.${f.fmt.value}`);
  };
}

// ------------------------------------------------------------------ settings
const SETTINGS_TABS = [["org", "Organisation & modules"], ["users", "Users"], ["keys", "API keys"], ["webhooks", "Webhooks"], ["factors", "Emission factors"],
  ["fx", "Exchange rates"], ["connectors", "Connectors"], ["sources", "Data sources"], ["audit", "Audit log"], ["platform", "Platform"]];

async function viewSettings(tab = "org") {
  setCrumbs([{ label: "Settings & integrations" }]);
  const tabs = SETTINGS_TABS.filter(([k]) => k !== "platform" || S.me.user?.role === "platform_admin");
  view().innerHTML = `<h1>Settings & integrations</h1><nav class="tabs">${tabs.map(([k, l]) => `<a href="#/settings/${k}" class="${k === tab ? "active" : ""}">${l}</a>`).join("")}</nav><div id="tab"></div>`;
  const T = $("#tab");
  const guard = async (fn) => { try { await fn(); } catch (err) { T.innerHTML = `<div class="panel"><p class="error">${esc(err.message)}</p></div>`; } };
  if (tab === "org") await guard(async () => {
    const o = await api("/org");
    T.innerHTML = `<div class="panel"><h2>${esc(o.name)}</h2><p>Plan: <b>${esc(o.plan.name)}</b> · ${o.plant_count} / ${o.plan.max_plants ?? "unlimited"} plants · API ${o.plan.api_requests_per_minute} req/min · currency ${esc(o.default_currency)}</p>
      <table><thead><tr><th>Module</th><th>Status</th></tr></thead><tbody>${o.modules.map((m) => `<tr><td>${esc(m.name)}</td><td>${m.enabled ? `<span class="chip verified">enabled</span>` : `<span class="chip">not in plan</span>`}</td></tr>`).join("")}</tbody></table>
      <p class="note">Modules are licensed per subscription plan with optional per-organisation overrides set by the platform administrator. No payment processing is implemented.</p></div>`;
  });
  if (tab === "users") await guard(async () => {
    const [users, plants] = await Promise.all([api("/users"), plantsList()]);
    const pname = Object.fromEntries(plants.map((p) => [p.id, p.name]));
    const scopeText = (u) => (u.plant_scope === null ? "All plants" : u.plant_scope.map((id) => pname[id] || "?").join(", ") || "No plants");
    T.innerHTML = `<div class="panel"><div class="table-wrap"><table><thead><tr><th>Name</th><th>Role</th><th>Plant access</th><th>Security</th><th>Last sign-in</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr><td>${esc(u.full_name)}<div class="muted small">${esc(u.email)}</div></td>
        <td><select data-role="${u.id}">${["client_admin", "plant_owner", "analyst", "technician", "read_only"].map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}${u.role === "platform_admin" ? "<option selected>platform_admin</option>" : ""}</select></td>
        <td class="small">${esc(scopeText(u))}<br><button class="btn small" data-scope="${u.id}">Change</button></td>
        <td class="small">${u.is_active ? "" : `<span class="chip">inactive</span> `}${u.mfa_enabled ? `<span class="chip verified">2FA</span> ` : `<span class="chip">no 2FA</span> `}${u.locked ? `<span class="chip simulated">locked</span> ` : ""}${u.must_change_password ? `<span class="chip">temp password</span>` : ""}</td>
        <td class="small">${dt(u.last_login_at)}</td>
        <td><div class="row">
          <button class="btn small" data-toggle="${u.id}" data-active="${u.is_active}">${u.is_active ? "Deactivate" : "Activate"}</button>
          ${u.locked ? `<button class="btn small" data-unlock="${u.id}">Unlock</button>` : ""}
          ${u.mfa_enabled ? `<button class="btn small" data-mfa="${u.id}">Reset 2FA</button>` : ""}
          <button class="btn small" data-pw="${u.id}">Reset password</button></div></td></tr>`).join("")}</tbody></table></div>
      <div id="u-edit"></div>
      <h3 style="margin-top:12px">Invite user</h3><form id="u-form" class="form"><label>Name <input name="full_name" required></label><label>Email <input type="email" name="email" required></label>
        <label>Role <select name="role">${["analyst", "technician", "plant_owner", "read_only", "client_admin"].map((r) => `<option>${r}</option>`).join("")}</select></label>
        <label>Temporary password <input type="password" name="password" required minlength="10" autocomplete="new-password"></label>
        <div><button class="btn">Create</button> <span class="error" id="u-err"></span></div></form>
      <p class="note">New users and admin-reset passwords are temporary: the user must choose their own password at first sign-in. Role changes, resets and deactivation sign the user out everywhere.</p></div>`;
    const patch = async (id, body, msg) => { try { await api(`/users/${id}`, { method: "PATCH", json: body }); toast(msg); viewSettings("users"); } catch (err) { toast(err.message); } };
    T.querySelectorAll("[data-role]").forEach((s) => (s.onchange = () => patch(s.dataset.role, { role: s.value }, "Role updated")));
    T.querySelectorAll("[data-toggle]").forEach((b) => (b.onclick = () => patch(b.dataset.toggle, { is_active: b.dataset.active !== "true" }, "Updated")));
    T.querySelectorAll("[data-unlock]").forEach((b) => (b.onclick = () => patch(b.dataset.unlock, { unlock: true }, "Account unlocked")));
    T.querySelectorAll("[data-mfa]").forEach((b) => (b.onclick = () => patch(b.dataset.mfa, { reset_mfa: true }, "Two-factor authentication reset")));
    T.querySelectorAll("[data-pw]").forEach((b) => (b.onclick = () => {
      $("#u-edit").innerHTML = `<form id="pw-form" class="form panel"><label>New temporary password <input type="password" name="password" required minlength="10" autocomplete="new-password"></label>
        <div><button class="btn primary">Set temporary password</button> <button type="button" class="btn ghost" data-cancel>Cancel</button></div></form>`;
      $("#pw-form").onsubmit = (e) => { e.preventDefault(); patch(b.dataset.pw, { password: e.target.password.value }, "Password reset — user must change it at next sign-in"); };
      $("#u-edit [data-cancel]").onclick = () => { $("#u-edit").innerHTML = ""; };
    }));
    T.querySelectorAll("[data-scope]").forEach((b) => (b.onclick = () => {
      const u = users.find((x) => x.id === b.dataset.scope);
      $("#u-edit").innerHTML = `<form id="scope-form" class="form panel"><h3 class="full">Plant access — ${esc(u.full_name)}</h3>
        <label class="row full"><input type="checkbox" name="all" ${u.plant_scope === null ? "checked" : ""}> All plants in the organisation (including future ones)</label>
        ${plants.map((p) => `<label class="row"><input type="checkbox" name="plant" value="${p.id}" ${u.plant_scope && u.plant_scope.includes(p.id) ? "checked" : ""}> ${esc(p.name)}</label>`).join("")}
        <div class="full"><button class="btn primary">Save access</button> <button type="button" class="btn ghost" data-cancel>Cancel</button></div></form>`;
      $("#scope-form").onsubmit = (e) => {
        e.preventDefault();
        const f = e.target;
        const ids = [...f.querySelectorAll("input[name=plant]:checked")].map((c) => c.value);
        patch(u.id, f.elements.all.checked ? { all_plants: true } : { plant_scope: ids }, "Plant access updated");
      };
      $("#u-edit [data-cancel]").onclick = () => { $("#u-edit").innerHTML = ""; };
    }));
    $("#u-form").onsubmit = async (e) => { e.preventDefault(); try { await api("/users", { method: "POST", json: formData(e.target) }); viewSettings("users"); } catch (err) { $("#u-err").textContent = err.message; } };
  });
  if (tab === "keys") await guard(async () => {
    const keys = await api("/api-keys");
    T.innerHTML = `<div class="panel"><p class="muted">API keys authenticate third-party systems via the <code>X-API-Key</code> header. Requires the API module (Enterprise plan). Keys are stored hashed.</p>
      <table><thead><tr><th>Name</th><th>Prefix</th><th>Role</th><th>Created</th><th>Last used</th><th>Status</th><th></th></tr></thead><tbody>
      ${keys.map((k) => `<tr><td>${esc(k.name)}</td><td><code>yala_${esc(k.prefix)}_…</code></td><td>${esc(k.role)}</td><td class="small">${dt(k.created_at)}</td><td class="small">${dt(k.last_used_at)}</td>
        <td>${k.revoked_at ? "revoked" : "active"}</td><td>${k.revoked_at ? "" : `<button class="btn small danger" data-revoke="${k.id}">Revoke</button>`}</td></tr>`).join("")}</tbody></table>
      <form id="k-form" class="form" style="margin-top:10px"><label>Name <input name="name" required></label><label>Role <select name="role">${["read_only", "analyst", "technician", "plant_owner"].map((r) => `<option>${r}</option>`).join("")}</select></label>
        <div><button class="btn">Create key</button></div></form><div id="k-out"></div></div>`;
    T.querySelectorAll("[data-revoke]").forEach((b) => (b.onclick = async () => { if (!confirm("Revoke this key?")) return; await api(`/api-keys/${b.dataset.revoke}`, { method: "DELETE" }); viewSettings("keys"); }));
    $("#k-form").onsubmit = async (e) => { e.preventDefault(); try { const r = await api("/api-keys", { method: "POST", json: formData(e.target) }); $("#k-out").innerHTML = `<div class="warnbox">New key (shown once): <code>${esc(r.key)}</code><br>${esc(r.note)}</div>`; } catch (err) { $("#k-out").innerHTML = `<p class="error">${esc(err.message)}</p>`; } };
  });
  if (tab === "webhooks") await guard(async () => {
    const [hooks, del] = await Promise.all([api("/webhooks"), api("/webhooks/deliveries")]);
    T.innerHTML = `<div class="panel"><p class="muted">Signed (HMAC-SHA256, header <code>X-Yala-Signature</code>) JSON POSTs for <code>alert.created</code>, <code>work_order.created</code>, <code>work_order.completed</code>, <code>import.completed</code>.</p>
      <table><thead><tr><th>URL</th><th>Events</th><th></th></tr></thead><tbody>${hooks.map((h) => `<tr><td>${esc(h.url)}</td><td>${esc(h.events.join(", "))}</td>
        <td><button class="btn small" data-test="${h.id}">Send test</button> <button class="btn small danger" data-del="${h.id}">Delete</button></td></tr>`).join("") || `<tr><td class="muted" colspan="3">None</td></tr>`}</tbody></table>
      <form id="h-form" class="form" style="margin-top:10px"><label class="full">HTTPS URL <input name="url" required placeholder="https://example.com/yala-hook"></label>
        <label>Events (comma separated) <input name="events" value="alert.created,work_order.completed"></label><div><button class="btn">Add webhook</button></div></form><div id="h-out"></div>
      <h3 style="margin-top:12px">Recent deliveries</h3><table><tbody>${del.map((d) => `<tr><td class="small">${dt(d.attempted_at)}</td><td>${esc(d.event)}</td><td>${d.success ? "✓" : "✗"} ${d.status_code ?? ""}</td><td class="small">attempt ${d.attempts}${d.next_attempt_at ? ` · retry ${dt(d.next_attempt_at)}` : ""}</td><td class="small muted">${esc(d.error || "")}</td></tr>`).join("") || `<tr><td class="muted">None</td></tr>`}</tbody></table></div>`;
    T.querySelectorAll("[data-test]").forEach((b) => (b.onclick = async () => { await api(`/webhooks/${b.dataset.test}/test`, { method: "POST" }); toast("Test queued"); setTimeout(() => viewSettings("webhooks"), 1500); }));
    T.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => { await api(`/webhooks/${b.dataset.del}`, { method: "DELETE" }); viewSettings("webhooks"); }));
    $("#h-form").onsubmit = async (e) => { e.preventDefault(); const d = formData(e.target); d.events = d.events.split(",").map((s) => s.trim()).filter(Boolean);
      try { const r = await api("/webhooks", { method: "POST", json: d }); $("#h-out").innerHTML = `<div class="warnbox">Signing secret (shown once): <code>${esc(r.secret)}</code></div>`; } catch (err) { $("#h-out").innerHTML = `<p class="error">${esc(err.message)}</p>`; } };
  });
  if (tab === "factors") await guard(async () => {
    const f = await api("/emission-factors");
    T.innerHTML = `<div class="panel"><table><thead><tr><th>Country</th><th>Name</th><th class="num">kgCO₂e/kWh</th><th>Method</th><th>Year</th><th>Source</th><th></th></tr></thead><tbody>
      ${f.map((x) => `<tr><td>${esc(x.country_code || "")}</td><td>${esc(x.name)}</td><td class="num">${x.kg_co2e_per_kwh}</td><td>${esc(x.method)}</td><td>${x.year || ""}</td><td class="small">${esc(x.source)}</td>
        <td>${x.is_sample ? `<span class="chip simulated">sample</span>` : ""}${x.shared ? `<span class="chip">shared</span>` : ""}</td></tr>`).join("")}</tbody></table>
      ${can("carbon:write") ? `<form id="f-form" class="form" style="margin-top:10px"><label>Name <input name="name" required></label><label>Country <input name="country_code" maxlength="2"></label>
        <label>kgCO₂e/kWh <input type="number" step="any" name="kg_co2e_per_kwh" required></label><label>Method <input name="method" required value="combined margin"></label>
        <label>Year <input type="number" name="year"></label><label class="full">Source (publication, dataset) <input name="source" required></label><label class="full">Source URL <input name="source_url"></label>
        <div><button class="btn">Add factor</button> <span class="error" id="f-err"></span></div></form>` : ""}</div>`;
    const ff = $("#f-form");
    if (ff) ff.onsubmit = async (e) => { e.preventDefault(); const d = formData(ff); if (d.country_code) d.country_code = d.country_code.toUpperCase();
      try { await api("/emission-factors", { method: "POST", json: d }); viewSettings("factors"); } catch (err) { $("#f-err").textContent = err.message; } };
  });
  if (tab === "fx") await guard(async () => {
    const r = await api("/exchange-rates");
    T.innerHTML = `<div class="panel"><p class="muted">Currency conversion always uses the latest explicit rate dated on or before the valuation date; no rate is ever assumed.</p>
      <table><thead><tr><th>Pair</th><th class="num">Rate</th><th>Date</th><th>Source</th></tr></thead><tbody>${r.map((x) => `<tr><td>1 ${esc(x.base)} = ? ${esc(x.quote)}</td><td class="num">${x.rate}</td><td>${x.rate_date}</td><td class="small">${esc(x.source)}</td></tr>`).join("")}</tbody></table>
      ${can("finance:simulate") ? `<form id="fx-form" class="form" style="margin-top:10px"><label>Base <input name="base" maxlength="3" required value="USD"></label><label>Quote <input name="quote" maxlength="3" required></label>
        <label>Rate <input type="number" step="any" name="rate" required></label><label>Date <input type="date" name="rate_date" required></label><label class="full">Source <input name="source" required></label>
        <div><button class="btn">Add rate</button> <span class="error" id="fx-err"></span></div></form>` : ""}</div>`;
    const fx = $("#fx-form");
    if (fx) fx.onsubmit = async (e) => { e.preventDefault(); const d = formData(fx); d.base = d.base.toUpperCase(); d.quote = d.quote.toUpperCase();
      try { await api("/exchange-rates", { method: "POST", json: d }); viewSettings("fx"); } catch (err) { $("#fx-err").textContent = err.message; } };
  });
  if (tab === "connectors") await guard(async () => {
    const c = await api("/integrations/connectors");
    T.innerHTML = `<div class="panel"><table><thead><tr><th>Connector</th><th>Type</th><th>Status</th><th>Licensing</th><th>Coverage</th></tr></thead><tbody>
      ${c.map((x) => `<tr><td><b>${esc(x.provider)}</b><div class="muted small">${esc(x.key)}${x.model_version ? " · " + esc(x.model_version) : ""}</div></td><td>${esc(x.type)}</td><td>${status(x.status)}</td>
        <td class="small">${esc(x.license_notes)}</td><td class="small">${esc(x.coverage_notes)}</td></tr>`).join("")}</tbody></table>
      <p class="note">External resource models provide expected-production baselines only — never proof of actual equipment condition. Site telemetry is the primary source for actual performance.</p></div>`;
  });
  if (tab === "sources") await guard(async () => {
    const s = await api("/data-sources");
    T.innerHTML = `<div class="panel"><table><thead><tr><th>Source</th><th>Kind</th><th>Model version</th><th>Retrieved</th><th>Licence</th></tr></thead><tbody>
      ${s.map((x) => `<tr><td>${esc(x.name)}<div class="muted small">${esc(x.provider || "")}</div></td><td>${esc(x.kind)}</td><td>${esc(x.model_version || "")}</td><td class="small">${dt(x.retrieved_at)}</td><td class="small">${esc(x.license_notes || "")}</td></tr>`).join("")}</tbody></table></div>`;
  });
  if (tab === "audit") await guard(async () => {
    const a = await api("/audit-logs?limit=200");
    T.innerHTML = `<div class="panel"><div class="table-wrap"><table><thead><tr><th>When</th><th>Action</th><th>Entity</th><th>User</th><th>IP</th><th>Details</th></tr></thead><tbody>
      ${a.map((x) => `<tr><td class="small">${dt(x.created_at)}</td><td>${esc(x.action)}</td><td class="small">${esc(x.entity_type || "")} ${esc((x.entity_id || "").slice(0, 8))}</td><td class="small">${esc((x.user_id || "").slice(0, 8))}</td><td class="small">${esc(x.ip || "")}</td><td class="small muted">${esc(JSON.stringify(x.details)).slice(0, 160)}</td></tr>`).join("")}</tbody></table></div></div>`;
  });
  if (tab === "platform") await guard(async () => {
    const [orgs, plans, jobs] = await Promise.all([api("/platform/organizations"), api("/platform/plans"), api("/platform/jobs")]);
    T.innerHTML = `<div class="panel"><h2>Tenants & licences</h2><div class="table-wrap"><table><thead><tr><th>Organisation</th><th>Plan</th><th>Modules</th><th>Active</th></tr></thead><tbody>
      ${orgs.map((o) => `<tr><td>${esc(o.name)}<div class="muted small">${esc(o.slug)}</div></td><td><select data-plan="${o.id}">${plans.map((p) => `<option value="${p.code}" ${p.code === o.plan_code ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></td>
        <td class="small">${esc(o.modules.join(", "))}</td><td><button class="btn small" data-active="${o.id}" data-val="${!o.is_active}">${o.is_active ? "Suspend" : "Reactivate"}</button></td></tr>`).join("")}</tbody></table></div></div>
      <div class="panel"><h2>Onboard a customer organisation</h2><form id="org-form" class="form">
        <label>Organisation name <input name="name" required></label><label>Slug (url-safe) <input name="slug" required pattern="[a-z0-9\\-]{3,80}"></label>
        <label>Plan <select name="plan_code">${plans.map((p) => `<option value="${p.code}">${esc(p.name)}</option>`).join("")}</select></label>
        <label>Currency <input name="default_currency" value="USD" maxlength="3" required></label><label>Country (ISO-2) <input name="country_code" maxlength="2"></label>
        <label>Admin name <input name="admin_name" required></label><label>Admin email <input type="email" name="admin_email" required></label>
        <label>Temporary admin password <input type="password" name="admin_password" required minlength="10" autocomplete="new-password"></label>
        <div class="full"><button class="btn primary">Create organisation</button> <span class="error" id="org-err"></span></div></form></div>
      <div class="panel"><h2>Background jobs</h2><table><thead><tr><th>Job</th><th>Last run</th><th>Status</th><th>Details</th></tr></thead><tbody>
        ${jobs.map((j) => `<tr><td>${esc(j.name)}</td><td class="small">${dt(j.last_finished_at || j.last_started_at)}</td><td>${status(j.last_status || "never")}</td><td class="small muted">${esc(JSON.stringify(j.details))}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">No runs yet (scheduler disabled or just started).</td></tr>`}</tbody></table></div>`;
    T.querySelectorAll("[data-plan]").forEach((s) => (s.onchange = async () => { await api(`/platform/organizations/${s.dataset.plan}/license`, { method: "PATCH", json: { plan_code: s.value } }); viewSettings("platform"); }));
    T.querySelectorAll("[data-active]").forEach((b) => (b.onclick = async () => { await api(`/platform/organizations/${b.dataset.active}/license`, { method: "PATCH", json: { is_active: b.dataset.val === "true" } }); viewSettings("platform"); }));
    $("#org-form").onsubmit = async (e) => {
      e.preventDefault();
      const d = formData(e.target);
      d.default_currency = d.default_currency.toUpperCase();
      if (d.country_code) d.country_code = d.country_code.toUpperCase();
      try { await api("/platform/organizations", { method: "POST", json: d }); toast("Organisation created — share the temporary password securely"); viewSettings("platform"); }
      catch (err) { $("#org-err").textContent = err.message; }
    };
  });
}

// ------------------------------------------------------------------ my account
async function viewAccount() {
  setCrumbs([{ label: "My account" }]);
  S.me = await api("/auth/me");
  const u = S.me.user;
  if (!u) { view().innerHTML = `<div class="panel"><p>API-key sessions have no account settings.</p></div>`; return; }
  view().innerHTML = `<h1>My account</h1><div class="muted small">${esc(u.email)} · ${esc(u.role)} · ${esc(S.me.organization.name)}</div>
    ${u.must_change_password ? `<div class="warnbox">You signed in with a temporary password. Choose your own password to continue.</div>` : ""}
    <div class="grid g2">
      <div class="panel"><h2>Change password</h2><form id="cp-form" class="form">
        <label class="full">Current password <input type="password" name="current_password" required autocomplete="current-password"></label>
        <label class="full">New password (≥ 10 characters, letters and digits) <input type="password" name="new_password" required minlength="10" autocomplete="new-password"></label>
        <label class="full">Repeat new password <input type="password" name="repeat" required minlength="10" autocomplete="new-password"></label>
        <div class="full"><button class="btn primary">Change password</button> <span class="error" id="cp-err"></span></div></form>
        <p class="note">Changing your password signs out your other sessions.</p></div>
      <div class="panel"><h2>Two-factor authentication</h2><div id="mfa-box"></div></div>
    </div>
    <div class="panel"><h2>Sessions</h2><p class="muted">Lost a device or shared a computer? Sign out of every browser and device at once.</p>
      <button class="btn danger" id="logout-all">Sign out everywhere</button></div>`;
  $("#cp-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = formData(e.target);
    if (f.new_password !== f.repeat) { $("#cp-err").textContent = "The two new passwords do not match."; return; }
    try {
      const r = await api("/auth/change-password", { method: "POST", json: { current_password: f.current_password, new_password: f.new_password } });
      S.token = r.access_token;
      try { sessionStorage.setItem("yala_token", S.token); } catch (_) { /* storage unavailable */ }
      toast("Password changed");
      S.me = await api("/auth/me");
      location.hash = "#/dashboard";
    } catch (err) { $("#cp-err").textContent = err.message; }
  };
  $("#logout-all").onclick = async () => { await api("/auth/logout-all", { method: "POST" }); logout(); };
  const box = $("#mfa-box");
  if (u.mfa_enabled) {
    box.innerHTML = `<p><span class="chip verified">enabled</span> Sign-in requires a code from your authenticator app.</p>
      <form id="mfa-off" class="form"><label>Password <input type="password" name="password" required autocomplete="current-password"></label>
      <label>Current code <input name="otp" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required></label>
      <div class="full"><button class="btn danger">Turn off two-factor</button> <span class="error" id="mfa-err"></span></div></form>`;
    $("#mfa-off").onsubmit = async (e) => { e.preventDefault(); try { await api("/auth/mfa/disable", { method: "POST", json: formData(e.target) }); toast("Two-factor turned off"); viewAccount(); } catch (err) { $("#mfa-err").textContent = err.message; } };
  } else {
    box.innerHTML = `<p class="muted">Protect your account with a 6-digit code from an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, Authy…).</p>
      ${u.must_change_password ? `<p class="small muted">Change your password first.</p>` : `<button class="btn primary" id="mfa-start">Set up two-factor</button>`}<div id="mfa-setup"></div>`;
    const st = $("#mfa-start");
    if (st) st.onclick = async () => {
      try {
        const r = await api("/auth/mfa/setup", { method: "POST" });
        $("#mfa-setup").innerHTML = `<ol class="compact small"><li>In your authenticator app choose “Add account” → “Enter a setup key”.</li>
          <li>Account: <code>${esc(u.email)}</code> · Key: <code style="user-select:all">${esc(r.secret.replace(/(.{4})/g, "$1 ").trim())}</code> · Type: time-based</li>
          <li>Enter the 6-digit code the app shows.</li></ol>
          <p class="small muted">Setup link for apps that accept one: <code style="user-select:all;word-break:break-all">${esc(r.otpauth_uri)}</code></p>
          <form id="mfa-on" class="form"><label>Code <input name="otp" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required autocomplete="one-time-code"></label>
          <div><button class="btn primary">Turn on two-factor</button> <span class="error" id="mfa-err"></span></div></form>`;
        $("#mfa-on").onsubmit = async (e) => { e.preventDefault(); try { await api("/auth/mfa/enable", { method: "POST", json: formData(e.target) }); toast("Two-factor authentication is on"); viewAccount(); } catch (err) { $("#mfa-err").textContent = err.message; } };
      } catch (err) { toast(err.message); }
    };
  }
}

// ------------------------------------------------------------------ init
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) closeDrawer();
  if (e.target.id === "drawer") closeDrawer();
  const k = e.target.closest(".kpi[data-href]");
  if (k) location.hash = k.dataset.href;
});
$("#login-form").addEventListener("submit", login);
$("#signup-form").addEventListener("submit", signup);
$("#show-signup").onclick = (e) => { e.preventDefault(); showLoginForm(false); };
$("#show-login").onclick = (e) => { e.preventDefault(); showLoginForm(true); };
$("#logout").onclick = logout;
$("#menu").onclick = () => document.querySelector(".sidebar").classList.toggle("open");
["p-start", "p-end"].forEach((id) => $(`#${id}`).addEventListener("change", route));
$("#p-reset").onclick = () => { $("#p-start").value = ""; $("#p-end").value = ""; route(); };
window.addEventListener("hashchange", route);
(async () => {
  try { S.config = { ...S.config, ...(await api("/auth/config")) }; } catch (_) { /* keep defaults */ }
  if (S.config.allow_signup) $("#signup-link").classList.remove("hidden");
  if (S.config.show_demo_login) {
    $("#demo-hint").classList.remove("hidden");
    $("#login-form").elements.email.value = "admin@demo.yalainsight.local";
  }
  try { S.token = sessionStorage.getItem("yala_token"); } catch (_) { S.token = null; }
  if (S.token) boot(); else logout();
})();
