import { html, raw, out, inr, pct, num, deltaPill, trendChart, spark, dmy } from "./ui.js";
import { api, acts, mount, toast, S } from "./core.js";

const PERIODS = [[7, "7 days"], [30, "30 days"], [90, "90 days"], [184, "6 months"]];
const FORMATS = [["", "All formats"], ["Flagship", "Flagship"], ["High Street", "High street"], ["Mall Kiosk", "Mall kiosk"]];
const STATUS = { open: "Open", assigned: "Assigned", in_progress: "In progress", resolved: "Resolved" };
const st = { tab: "overview", days: 30, region: "", city: "", fmt: "", order: "top", cmp: "wow", lfl: 1, inbox: "active", limit: 10 };
let filters = { regions: [], cities: [], formats: [] }, root = null, owners = [];
const $ = s => document.querySelector(s);
const qs = () => `days=${st.days}&region=${encodeURIComponent(st.region)}&city=${encodeURIComponent(st.city)}&fmt=${encodeURIComponent(st.fmt)}`;
const seg = (id, act, cur, items) => html`<div class="seg" id="${id}">${items.map(([v, l]) => html`<button data-act="${act}" data-v="${v}" class="${String(cur) === String(v) ? "on" : ""}">${l}</button>`)}</div>`;
const flip = (id, v) => document.querySelectorAll(`#${id} button`).forEach(b => b.classList.toggle("on", b.dataset.v === String(v)));
const kpi = (l, v, s) => html`<div class="kpi glass"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
const pill = (t, c) => html`<span class="pill ${c}">${t}</span>`;
const empty = t => html`<p class="muted small" style="padding:14px 0">${t}</p>`;
const pts = x => x == null ? raw('<span class="d fl">n/a</span>') : raw(`<span class="d ${x >= 0 ? "up" : "dn"}">${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)} pts</span>`);
const drawer = h => { closeAll(); document.body.insertAdjacentHTML("beforeend", out(html`<div class="scrim" data-act="ct-close"></div><div class="drawer glass wide"><div class="row" style="justify-content:space-between"><span></span><button class="icon" data-act="ct-close" aria-label="Close">✕</button></div>${h}</div>`)); };
const closeAll = () => document.querySelectorAll(".scrim,.drawer").forEach(n => n.remove());
const viewsKey = () => `dfh_ct_views_${S.user?.role || "x"}`;
const views = () => { try { return JSON.parse(localStorage.getItem(viewsKey()) || "[]"); } catch { return []; } };

export async function render(el) {
  root = el; filters = await api("/ct/filters").catch(() => filters); owners = await api("/ct/owners").catch(() => []);
  const tabs = [["overview", "Overview"], ["inbox", "Exceptions inbox"]].concat(S.user.role === "store_manager" ? [] : [["wh", "Warehouses"]]);
  if (!tabs.find(t => t[0] === st.tab)) st.tab = "overview";
  mount(el, html`<div class="head"><div><div class="eyebrow">Module 1</div><h1>Control Tower</h1><p id="asof" class="muted">Loading</p></div>
    <div class="row">${seg("cttabs", "ct-tab", st.tab, tabs)}</div></div><div id="ctbody"></div>`);
  load();
}

function load() { ({ overview, inbox, wh })[st.tab](); }

// ---------------- overview ----------------
function overview() {
  const b = $("#ctbody"); const vs = views();
  mount(b, html`<div class="row" style="margin-bottom:12px;flex-wrap:wrap">
    ${seg("per", "ct-days", st.days, PERIODS)}
    <select class="input" id="reg"><option value="">All regions</option>${filters.regions.map(r => html`<option ${st.region === r ? "selected" : ""}>${r}</option>`)}</select>
    <select class="input" id="cty"><option value="">All cities</option>${filters.cities.map(r => html`<option ${st.city === r ? "selected" : ""}>${r}</option>`)}</select>
    <select class="input" id="fmt">${FORMATS.map(([v, l]) => html`<option value="${v}" ${st.fmt === v ? "selected" : ""}>${l}</option>`)}</select>
    <select class="input" id="sv"><option value="">Saved views</option>${vs.map((v, i) => html`<option value="${i}">${v.name}</option>`)}</select>
    <button class="btn" data-act="ct-save-view">Save view</button><button class="btn" data-act="ct-snap">Morning summary</button></div>
    <div class="kpis" id="kpis">${[1, 2, 3, 4, 5].map(() => html`<div class="kpi glass"><div class="sk" style="height:70px"></div></div>`)}</div>
    <div class="kpis" id="kpis2"></div>
    <div class="cols"><div class="card glass"><div class="ct"><h2>Net sales by day</h2><span class="small muted">Thick line is the 7-day average. Dashed lines mark festival peaks.</span></div><div class="chart" id="trend"></div></div>
    <div class="card glass"><div class="ct"><h2>Category mix</h2><span class="small muted">Share of net sales</span></div><div class="bars" id="mix"></div></div></div>
    <div class="card glass" style="margin-bottom:16px"><div class="ct"><h2>Period compare</h2><div class="row">${seg("cmpm", "ct-cmp", st.cmp, [["wow", "Week on week"], ["mom", "Month on month"]])}${seg("lfl", "ct-lfl", st.lfl, [[1, "Like for like"], [0, "All stores"]])}</div></div><div id="cmp"></div></div>
    <div class="cols eq"><div class="card solid"><div class="ct"><h2>Store league</h2><div class="row">${seg("ord", "ct-ord", st.order, [["top", "Top"], ["bottom", "Bottom"]])}${seg("lim", "ct-lim", st.limit, [[10, "10"], [25, "25"], [50, "50"]])}</div></div><div id="league"></div></div>
    <div class="card glass"><div class="ct"><h2>Exceptions</h2><span class="small muted" id="excn"></span></div><div id="exc"></div></div></div>`);
  const on = (id, k) => { $(id).onchange = e => { st[k] = e.target.value; refresh(); }; };
  on("#reg", "region"); on("#cty", "city"); on("#fmt", "fmt");
  $("#sv").onchange = e => { const v = vs[+e.target.value]; if (!v) return; Object.assign(st, v.st); overview(); };
  addEventListener("resize", () => window.__ctTrend && window.__ctTrend());
  refresh();
}

async function refresh() {
  if (!root || !$("#kpis")) return;
  try {
    const q = qs(), f = `region=${encodeURIComponent(st.region)}&city=${encodeURIComponent(st.city)}&fmt=${encodeURIComponent(st.fmt)}`;
    const [s, tr, mix, lg, ex, hd] = await Promise.all([api(`/ct/summary?${q}`), api(`/ct/trend?${q}`), api(`/ct/categories?${q}`), api(`/ct/league?${q}&order=${st.order}&limit=${st.limit}`), api(`/ct/exceptions?${f}`), api(`/ct/headline?${q}`)]);
    const c = s.current, dlabel = `vs previous ${s.days} days`;
    $("#asof").textContent = `Data through ${new Date(s.end + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "long", year: "numeric" })}. Synthetic demo data, tax inclusive sales.`;
    const syncN = ex.filter(e => e.type === "sync").length;
    mount($("#kpis"), html`${kpi("Net sales", inr(c.net), deltaPill(s.delta.net, " " + dlabel))}
      ${kpi("Gross margin", pct(c.gm), html`${pts(s.delta.gm)} ex tax, at standard cost`)}
      ${kpi("Net sales per store per day", inr(c.per_store_day), deltaPill(s.delta.per_store_day, " " + dlabel))}
      ${kpi("Stockout events", num(s.stockouts.events), `${num(s.stockouts.days)} store-SKU days lost across ${num(s.stockouts.stores)} stores`)}
      ${kpi("Stores not syncing", syncN, `${num(c.stores)} stores selling in this period`)}`);
    mount($("#kpis2"), html`${kpi("Gross margin after wastage", pct(hd.gm_after_wastage), `Wastage is ${pct(hd.wastage_pct_sales, 2)} of sales, ${inr(hd.wastage_cost)} at cost`)}
      ${kpi("Store stock at cost", inr(hd.store_stock_cost), hd.warehouse_stock_cost ? `Warehouses hold ${inr(hd.warehouse_stock_cost)}` : "Your store only")}
      ${kpi("Stock cover", hd.cover_days == null ? "n/a" : `${hd.cover_days.toFixed(0)} days`, "Store stock over daily cost of goods sold")}`);
    const draw = () => { const t = $("#trend"); if (t) trendChart(t, tr, s.festivals); }; window.__ctTrend = draw; draw();
    mount($("#mix"), html`${mix.map(m => html`<div class="r"><span>${m.name}</span><div class="b"><i style="width:${(m.share / mix[0].share * 100).toFixed(1)}%"></i></div><span class="num r">${pct(m.share)} <span class="muted small">GM ${pct(m.gm, 0)}</span></span></div>`)}`);
    mount($("#league"), html`<div class="tblwrap" style="max-height:420px"><table class="tbl"><thead><tr><th>#</th><th>Store</th><th>City</th><th class="r">Per day</th><th class="r">GM</th><th class="r">Change</th></tr></thead><tbody>
      ${lg.rows.map((r, i) => html`<tr class="click" data-act="ct-store" data-id="${r.id}"><td class="muted">${i + 1}</td><td><b>${r.name.replace("Dry Fruit House ", "")}</b><div class="small muted">${r.code}${r.new ? " · new store" : ""}</div></td><td>${r.city}</td><td class="r">${inr(r.per_day)}</td><td class="r">${pct(r.gm, 0)}</td><td class="r">${r.vs_prev == null ? raw('<span class="muted">n/a</span>') : deltaPill(r.vs_prev)}</td></tr>`)}</tbody></table></div><p class="small muted" style="margin-top:8px">Ranked on net sales per trading day, ${num(lg.total)} stores in view. Select a store to drill down. New stores are left out of the bottom list.</p>`);
    $("#excn").textContent = `${ex.length} open`;
    mount($("#exc"), html`${ex.slice(0, 8).map(e => html`<div class="exc"><span class="dot ${e.severity}"></span><div><b>${e.title}</b><p>${e.detail}</p></div></div>`)}<button class="btn" data-act="ct-tab" data-v="inbox" style="margin-top:10px">Open the inbox</button>`);
    compare();
  } catch (e) { if ($("#asof")) $("#asof").textContent = "Could not load: " + e.message; }
}

async function compare() {
  const el = $("#cmp"); if (!el) return;
  const r = await api(`/ct/compare?mode=${st.cmp}&lfl=${st.lfl}&region=${encodeURIComponent(st.region)}&city=${encodeURIComponent(st.city)}&fmt=${encodeURIComponent(st.fmt)}`).catch(e => ({ err: e.message }));
  if (r.err) return mount(el, empty(r.err));
  mount(el, html`<div class="kpis"><div class="kpi"><div class="l">${dmy(r.start)} to ${dmy(r.end)}</div><div class="v">${inr(r.net)}</div><div class="s">${deltaPill(r.change, ` vs ${dmy(r.prev_start)} to ${dmy(r.prev_end)}`)}</div></div>
    <div class="kpi"><div class="l">Gross margin</div><div class="v">${pct(r.gm)}</div><div class="s">${pts(r.gm == null || r.gm_prev == null ? null : r.gm - r.gm_prev)}</div></div>
    <div class="kpi"><div class="l">Stores in compare</div><div class="v">${r.stores == null ? "All" : num(r.stores)}</div><div class="s">${r.like_for_like ? "Like for like" : "Includes new stores"}</div></div></div>
    <div class="tblwrap"><table class="tbl"><thead><tr><th>Category</th><th class="r">Net sales</th><th class="r">Change</th><th class="r">GM</th></tr></thead><tbody>${r.categories.map(c => html`<tr><td>${c.name}</td><td class="r">${inr(c.net)}</td><td class="r">${deltaPill(c.change)}</td><td class="r">${pct(c.gm, 1)} ${c.gm_prev == null ? "" : pts(c.gm - c.gm_prev)}</td></tr>`)}</tbody></table></div><p class="small muted" style="margin-top:8px">${r.note}</p>`);
}

// ---------------- drill ----------------
acts["ct-store"] = async a => {
  try {
    const d = await api(`/ct/store/${a.dataset.id}?days=${st.days}`), c = d.current, p = d.previous;
    drawer(html`<h1>${d.store.name.replace("Dry Fruit House ", "")}</h1><p class="muted small">${d.store.code} · ${d.store.city} · ${d.store.format}. Last ${d.days} days.${d.store.last_sync_at ? " Last sync " + d.store.last_sync_at.slice(0, 16).replace("T", " ") + "." : ""}</p>
      <div class="kpis">${kpi("Net sales", inr(c.net), deltaPill(p && p.net ? c.net / p.net - 1 : null))}${kpi("Gross margin", pct(c.gm), "ex tax")}${kpi("Per trading day", inr(c.per_store_day), `${c.store_days} days`)}${kpi("Wastage", inr(d.wastage_cost), "at cost, same period")}</div>
      <div class="card solid"><div class="ct"><h2>Daily net sales</h2></div>${raw(spark(d.trend.map(x => x.net), 640, 60))}</div>
      ${d.exceptions.length ? html`<div class="card solid"><div class="ct"><h2>Open exceptions</h2></div>${d.exceptions.map(e => html`<div class="exc"><span class="dot ${e.severity}"></span><div><b>${e.title}</b><p>${e.detail}</p></div></div>`)}</div>` : ""}
      <div class="card solid"><div class="ct"><h2>Top SKUs</h2><span class="small muted">Select one to see its daily sales</span></div><div class="tblwrap"><table class="tbl"><thead><tr><th>SKU</th><th>Category</th><th class="r">Net sales</th></tr></thead><tbody>${d.top_skus.map(k => html`<tr class="click" data-act="ct-sku" data-s="${d.store.id}" data-k="${k.id}"><td>${k.name}</td><td class="muted">${k.cat}</td><td class="r">${inr(k.net)}</td></tr>`)}</tbody></table></div></div>
      <div class="card solid"><div class="ct"><h2>Category mix</h2></div>${d.mix.slice(0, 6).map(m => html`<div class="r" style="display:flex;justify-content:space-between;padding:3px 0"><span>${m.name}</span><span class="num">${pct(m.share)}</span></div>`)}</div>
      <div class="card solid"><div class="ct"><h2>Stockouts</h2></div>${d.stockouts.length ? d.stockouts.map(x => html`<div class="small" style="padding:3px 0">${x.name}, ${dmy(x.start)} to ${dmy(x.end)}</div>`) : empty("No stockouts in this period.")}</div>
      <div class="card solid"><div class="ct"><h2>POS transaction sample</h2><span class="small muted">${d.pos_summary.start} to ${d.pos_summary.end} · separate from chain sales history</span></div>
        <div class="kpis">${kpi("POS bills", num(d.pos_summary.bills), "Non-void transactions")}${kpi("Average bill value", inr(d.pos_summary.avg_bill_paise, false), "POS bills only")}${kpi("SKU lines per bill", d.pos_summary.lines_per_bill.toFixed(1), "Each line is one SKU")}${kpi("Approved refunds", inr(d.pos_summary.refunds_paise, false), `${d.pos_summary.approved_returns} returns`)}</div>
        <p class="small muted">These transaction-level figures are intentionally not added to the aggregate Parquet sales KPIs above. Quantities remain in their SKU base unit (kg for loose goods, units for packed goods).</p></div>
      <div class="card solid"><div class="ct"><h2>Recent POS bills</h2><span class="small muted">Select a bill to inspect lines, tenders and returns</span></div>${d.bills.length ? html`<table class="tbl"><tbody>${d.bills.map(b => html`<tr class="click" data-act="ct-bill" data-id="${b.id}"><td>${b.invoice_no}</td><td>${dmy(b.business_date)}</td><td class="r">${inr(b.net_paise, false)}</td><td>${b.status}</td></tr>`)}</tbody></table>` : empty(d.bills_note)}</div>`);
  } catch (e) { toast(e.message, true); }
};
acts["ct-bill"] = async a => {
  try {
    const d = await api("/ct/bills/" + encodeURIComponent(a.dataset.id)), b = d.bill;
    drawer(html`<div class="eyebrow">POS transaction · ${b.store_code}</div><h1>${b.invoice_no}</h1><p class="muted small">${b.store_name.replace("Dry Fruit House ", "")} · ${dmy(b.business_date)} · ${b.status}${b.cashier ? " · " + b.cashier : ""}</p>
      <div class="kpis">${kpi("Bill total", inr(b.net_paise, false), "Tax inclusive")}${kpi("Approved refunds", inr(d.approved_refunds_paise, false), `${d.returns.length} return requests`)}${kpi("Retained value", inr(d.retained_value_paise, false), "After approved refunds")}</div>
      ${b.void_reason ? html`<p class="small">${pill("Void", "warn")} ${b.void_reason}</p>` : ""}
      <div class="card solid"><div class="ct"><h2>Items</h2><span class="small muted">${d.lines.length} SKU lines</span></div><div class="tblwrap"><table class="tbl"><thead><tr><th>SKU</th><th>Qty</th><th class="r">Rate</th><th class="r">Discount</th><th class="r">Net</th></tr></thead><tbody>${d.lines.map(x => html`<tr><td><b>${x.name}</b><div class="small muted">${x.code}</div></td><td>${x.qty_label}</td><td class="r">${inr(x.unit_price_paise, false)}</td><td class="r">${inr(x.discount_paise, false)}</td><td class="r">${inr(x.net_paise, false)}</td></tr>`)}</tbody></table></div></div>
      <div class="card solid"><div class="ct"><h2>Payment tenders</h2></div>${d.payments.length ? html`<table class="tbl"><tbody>${d.payments.map(x => html`<tr><td>${x.mode}</td><td>${x.status}</td><td class="r">${inr(x.amount_paise, false)}</td></tr>`)}</tbody></table>` : empty("No payment records.")}</div>
      <div class="card solid"><div class="ct"><h2>Returns</h2></div>${d.returns.length ? html`<table class="tbl"><tbody>${d.returns.map(x => html`<tr><td>${x.return_no}<div class="small muted">${x.reason} · ${dmy(x.created_at.slice(0, 10))}</div></td><td>${x.status}</td><td class="r">${inr(x.refund_paise, false)}</td></tr>`)}</tbody></table>` : empty("No returns recorded.")}</div>
      <p class="small muted">${d.source}. This bill is not added to the aggregate Parquet history.</p>`);
  } catch (e) { toast(e.message, true); }
};
acts["ct-sku"] = async a => {
  try {
    const d = await api(`/ct/store/${a.dataset.s}/sku/${a.dataset.k}?days=60`), q = d.sku.base_uom === "g" ? (x => `${(x / 1000).toFixed(1)} kg`) : (x => `${x} units`);
    drawer(html`<h1>${d.sku.name}</h1><p class="muted small">${d.store.name.replace("Dry Fruit House ", "")}, ${d.store.city}. Last ${d.days} days.</p>
      <div class="kpis">${kpi("Units sold", q(d.daily.reduce((s, x) => s + x.qty, 0)), "")}${kpi("Net sales", inr(d.daily.reduce((s, x) => s + x.net, 0)), "")}${kpi("On hand now", d.on_hand ? q(d.on_hand) : "None recorded", "")}</div>
      <div class="card solid"><div class="ct"><h2>Daily sales</h2></div>${raw(spark(d.daily.map(x => x.net), 640, 60))}</div>
      <div class="card solid"><div class="ct"><h2>Stockouts</h2></div>${d.stockouts.length ? d.stockouts.map(x => html`<div class="small" style="padding:3px 0">${dmy(x.start)} to ${dmy(x.end)}</div>`) : empty("None in this period.")}</div>
      <div class="card solid"><div class="ct"><h2>Wastage entries</h2></div>${d.wastage.length ? html`<table class="tbl"><tbody>${d.wastage.map(w => html`<tr><td>${dmy(w.ts.slice(0, 10))}</td><td>${w.reason}</td><td class="r">${inr(w.cost_paise, false)}</td><td>${w.status}</td></tr>`)}</tbody></table>` : empty("No loss recorded for this SKU in this store.")}</div>`);
  } catch (e) { toast(e.message, true); }
};

// ---------------- inbox ----------------
async function inbox() {
  const b = $("#ctbody"); mount(b, html`<div class="card glass"><div class="sk" style="height:120px"></div></div>`);
  try {
    const r = await api(`/ct/inbox?status=${st.inbox}`);
    $("#asof").textContent = `${r.open} open, ${r.unassigned} without an owner, ${r.overdue} overdue. Items appear when a rule fires and clear when it stops firing.`;
    mount(b, html`<div class="row" style="margin-bottom:12px">${seg("ibs", "ct-ibs", st.inbox, [["active", "Active"], ["open", "Unassigned"], ["resolved", "Resolved"], ["all", "All"]])}</div>
      <div class="card solid"><div class="tblwrap"><table class="tbl"><thead><tr><th></th><th>Exception</th><th>Owner</th><th>Due</th><th>Status</th></tr></thead><tbody>
      ${r.rows.map(x => html`<tr><td><span class="dot ${x.severity}" style="display:inline-block"></span></td><td><b>${x.title}</b><div class="small muted">${x.type}${x.store_name ? " · " + x.store_name.replace("Dry Fruit House ", "") : ""}</div></td>
        <td>${r.can_assign ? html`<select class="input" data-ct-owner="${x.id}"><option value="">Unassigned</option>${owners.map(o => html`<option value="${o.id}" ${x.owner_id === o.id ? "selected" : ""}>${o.name}</option>`)}</select>` : (x.owner_name || raw('<span class="muted">Unassigned</span>'))}</td>
        <td>${r.can_assign ? html`<input class="input" type="date" value="${x.due_date || ""}" data-ct-due="${x.id}">` : (x.due_date ? dmy(x.due_date) : "")}${x.overdue ? raw(' <span class="pill warn">Overdue</span>') : ""}</td>
        <td><select class="input" data-ct-status="${x.id}">${Object.entries(STATUS).map(([k, l]) => html`<option value="${k}" ${x.status === k ? "selected" : ""}>${l}</option>`)}</select></td></tr>`)}</tbody></table></div>
      ${r.rows.length ? "" : empty("Nothing here.")}<p class="small muted" style="margin-top:8px">${r.can_assign ? "The CEO and admin assign owners and due dates. An owner moves the item through its status." : "Only the CEO or admin can assign. The owner of an item can change its status."}</p></div>`);
    b.querySelectorAll("[data-ct-owner]").forEach(s => s.onchange = () => patch(s.dataset.ctOwner, s.value ? { owner_id: +s.value } : { unassign: true }));
    b.querySelectorAll("[data-ct-due]").forEach(s => s.onchange = () => s.value && patch(s.dataset.ctDue, { due_date: s.value }));
    b.querySelectorAll("[data-ct-status]").forEach(s => s.onchange = () => patch(s.dataset.ctStatus, { status: s.value }));
  } catch (e) { mount(b, empty("Could not load: " + e.message)); }
}
async function patch(id, body) {
  try { await api(`/ct/inbox/${id}`, { method: "PATCH", body }); toast("Saved"); } catch (e) { toast(e.message, true); }
  inbox();
}

// ---------------- warehouses ----------------
async function wh() {
  const b = $("#ctbody"); mount(b, html`<div class="card glass"><div class="sk" style="height:120px"></div></div>`);
  try {
    const r = await api("/ct/warehouses"); $("#asof").textContent = `Warehouse stock at cost as of ${dmy(r.as_of)}.`;
    mount(b, html`<div class="card solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Warehouse</th><th class="r">Stock at cost</th><th class="r">0 to 30 d</th><th class="r">31 to 60 d</th><th class="r">61 to 90 d</th><th class="r">Over 90 d</th><th class="r">Expiring in 30 d</th><th class="r">In transit</th><th class="r">Loss, 30 d</th></tr></thead><tbody>
      ${r.rows.map(w => html`<tr><td><b>${w.name.replace("Dry Fruit House ", "")}</b><div class="small muted">${w.city} · serves ${w.stores} stores</div></td><td class="r">${inr(w.stock_cost)}</td><td class="r">${inr(w.ageing.d0_30)}</td><td class="r">${inr(w.ageing.d31_60)}</td><td class="r">${inr(w.ageing.d61_90)}</td><td class="r">${inr(w.ageing.d90p)}</td><td class="r">${inr(w.expiring_30d)}</td>
        <td class="r">${w.in_transit}${w.oldest_in_transit_days != null ? html` <span class="small muted">oldest ${w.oldest_in_transit_days} d</span>` : ""}${w.draft ? html` <span class="small muted">${w.draft} draft</span>` : ""}</td><td class="r">${inr(w.loss_30d)} ${w.loss_flag ? pill("Outlier", "warn") : ""}</td></tr>`)}</tbody></table></div><p class="small muted" style="margin-top:8px">${r.note}</p></div>`);
  } catch (e) { mount(b, empty("Could not load: " + e.message)); }
}

// ---------------- actions ----------------
acts["ct-close"] = closeAll;
acts["ct-tab"] = a => { st.tab = a.dataset.v; flip("cttabs", st.tab); load(); };
acts["ct-days"] = a => { st.days = +a.dataset.v; flip("per", st.days); refresh(); };
acts["ct-ord"] = a => { st.order = a.dataset.v; flip("ord", st.order); refresh(); };
acts["ct-lim"] = a => { st.limit = +a.dataset.v; flip("lim", st.limit); refresh(); };
acts["ct-cmp"] = a => { st.cmp = a.dataset.v; flip("cmpm", st.cmp); compare(); };
acts["ct-lfl"] = a => { st.lfl = +a.dataset.v; flip("lfl", st.lfl); compare(); };
acts["ct-ibs"] = a => { st.inbox = a.dataset.v; inbox(); };
acts["ct-save-view"] = () => {
  const name = prompt("Name this view"); if (!name) return;
  const v = views().filter(x => x.name !== name); v.push({ name, st: { days: st.days, region: st.region, city: st.city, fmt: st.fmt, order: st.order, limit: st.limit } });
  localStorage.setItem(viewsKey(), JSON.stringify(v)); overview(); toast("View saved on this browser");
};
acts["ct-snap"] = async () => {
  try {
    const r = await api(`/ct/snapshot?save=${["ceo", "admin"].includes(S.user.role) ? 1 : 0}`);
    drawer(html`<h1>Morning summary</h1><p class="muted small">${r.delivery}</p><pre class="card solid" style="white-space:pre-wrap;font:inherit">${r.text}</pre><button class="btn pri" data-act="ct-copy">Copy text</button>`);
    window.__snap = r.text;
  } catch (e) { toast(e.message, true); }
};
acts["ct-copy"] = () => navigator.clipboard.writeText(window.__snap || "").then(() => toast("Copied"));
acts.noop = () => { };
