import { html, raw, out, inr, rs, pct, num, esc } from "./ui.js";
import { api, acts, mount, toast, S } from "./core.js";

const ALL_TABS = [["overview", "Overview"], ["stock", "Stock"], ["reconcile", "Reconciliation"], ["expiry", "Expiry"], ["transfers", "Transfers"], ["indents", "Indents"], ["repack", "Repack"], ["counts", "Cycle counts"], ["kits", "Gift boxes"]];
const STORE_TABS = ["stock", "transfers", "indents"];
const st = { tab: "overview", loc: null, locName: "", q: "", flag: "", sort: "value", page: 1, tstatus: "", istatus: "", days: 60, rwh: null };
let root, whs = [], timer, draft = { lines: [] };
const isW = () => ["ceo", "admin", "warehouse"].includes(S.user.role);
const q2 = o => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== "" && v != null)).toString();
const qty = (n, uom) => uom === "g" ? (n >= 1000 ? num(+(n / 1000).toFixed(2)) + " kg" : n + " g") : num(n) + (uom === "ea" ? "" : " " + uom);
const day = s => s ? new Date(String(s).slice(0, 10) + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "";
const dl = d => d == null ? "" : d < 0 ? html`<span class="pill delisted">expired ${-d} d ago</span>` : d <= 15 ? html`<span class="pill delisted">${d} d</span>` : d <= 30 ? html`<span class="pill warn">${d} d</span>` : html`<span class="muted">${d} d</span>`;
const SP = { low: "warn", out: "delisted", near: "warn", ok: "active", draft: "t", in_transit: "warn", received: "active", cancelled: "delisted", open: "t", allocated: "warn", dispatched: "warn", planned: "t", submitted: "warn", closed: "active", done: "active" };
const pill = s => html`<span class="pill ${SP[s] || "t"}">${String(s).replace("_", " ")}</span>`;
const empty = (m, n = 6) => html`<tr><td colspan="${n}" class="muted" style="padding:28px;text-align:center">${m}</td></tr>`;

function layer(kind, h) {
  document.querySelectorAll(".scrim,.drawer,.modal").forEach(n => n.remove());
  document.body.insertAdjacentHTML("beforeend", `<div class="scrim" data-act="inv-close"></div>` + (kind === "drawer" ? `<aside class="drawer glass wide">${out(h)}</aside>` : `<div class="modal glass">${out(h)}</div>`));
}
const close = () => document.querySelectorAll(".scrim,.drawer,.modal").forEach(n => n.remove());
acts["inv-close"] = close;
const topbar = (eyebrow, title, sub) => html`<div class="row" style="justify-content:space-between;align-items:flex-start"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p class="muted small" style="margin-top:6px">${sub || ""}</p></div><button class="icon" data-act="inv-close" aria-label="Close">✕</button></div>`;
const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

export async function render(el) {
  root = el; const tabs = ALL_TABS.filter(([k]) => S.user.role !== "store_manager" || STORE_TABS.includes(k));
  if (!tabs.some(([k]) => k === st.tab)) st.tab = tabs[0][0];
  whs = await api("/inv/locations?type=warehouse"); const own = S.user.scope_type === "store" ? S.user.scope_id : S.user.scope_type === "warehouse" ? S.user.scope_id : null;
  if (!st.loc) { st.loc = own || (whs[0] && whs[0].id); st.locName = (whs.find(w => w.id === st.loc) || {}).name || ""; }
  mount(el, html`<div class="head"><div><div class="eyebrow">Module 4</div><h1>Inventory and warehouse</h1><p class="muted">One ledger for every kilo. Batches, expiry first picking, transfers, repacking and counts. Synthetic demo data.</p></div></div>
    <div class="tabs" id="invtabs">${tabs.map(([k, l]) => html`<button data-act="inv-tab" data-v="${k}" class="${st.tab === k ? "on" : ""}">${l}</button>`)}</div><div id="invbody"></div>`);
  draw();
}
acts["inv-tab"] = a => { st.tab = a.dataset.v; st.page = 1; document.querySelectorAll("#invtabs button").forEach(b => b.classList.toggle("on", b.dataset.v === st.tab)); draw(); };
const body = () => root.querySelector("#invbody");
const draw = guard(async () => { mount(body(), html`<div class="sk" style="height:140px"></div>`); await ({ overview, stock, reconcile, expiry, transfers, indents, repack, counts, kits })[st.tab](); });

// ---------------- overview
async function overview() {
  const o = await api("/inv/overview"), t = o.totals;
  mount(body(), html`<div class="kpis"><div class="kpi glass"><div class="l">Stock at cost, warehouses</div><div class="v">${inr(t.value)}</div><div class="s">${o.warehouses.length} warehouse${o.warehouses.length > 1 ? "s" : ""} in view</div></div>
    <div class="kpi glass"><div class="l">Expiring within 30 days</div><div class="v">${inr(t.near30)}</div><div class="s">${pct(t.value ? t.near30 / t.value : 0)} of warehouse stock</div></div>
    <div class="kpi glass"><div class="l">In transit</div><div class="v">${inr(t.in_transit)}</div><div class="s">Left the source, not yet received</div></div>
    <div class="kpi glass"><div class="l">Shrink, last 90 days</div><div class="v">${inr(t.shrink90)}</div><div class="s">Typical warehouse loses ${pct(o.median_shrink_rate, 2)} of receipts</div></div></div>
    <div class="cols"><div class="card solid"><div class="ct"><h2>Warehouses</h2><span class="small muted">Click a row to open its stock</span></div><div class="tblwrap" style="max-height:520px"><table class="tbl"><thead><tr><th>Warehouse</th><th class="r">Stock at cost</th><th class="r">Near expiry</th><th class="r">Shrink rate</th><th class="r">Unexplained</th><th class="r">Repack loss</th><th class="r">Under min</th><th class="r">Open indents</th></tr></thead><tbody>
    ${o.warehouses.map(w => html`<tr class="click" data-act="inv-wh" data-id="${w.id}"><td><b>${w.name.replace("Dry Fruit House ", "")}</b><div class="small muted">${w.code}</div></td><td class="r">${inr(w.value)}</td><td class="r">${inr(w.near30)}</td>
    <td class="r">${w.shrink_rate > 3 * o.median_shrink_rate && w.shrink_rate > 0.005 ? raw(`<b style="color:var(--bad)">${pct(w.shrink_rate, 2)}</b>`) : pct(w.shrink_rate, 2)}</td><td class="r">${w.shrink90 ? pct(w.unexplained90 / w.shrink90, 0) : "n/a"}</td><td class="r">${w.repack_loss == null ? "n/a" : pct(w.repack_loss, 1)}</td>
    <td class="r">${w.low_skus}</td><td class="r">${w.open_indents}</td></tr>`)}</tbody></table></div></div>
    <div class="card glass"><div class="ct"><h2>Exceptions</h2><span class="small muted">${o.exceptions.length} open</span></div>${o.exceptions.map(e => html`<div class="exc ${e.ref ? "click" : ""}" ${e.type === "shrink" || e.type === "cover" ? raw(`data-act="inv-wh" data-id="${e.ref}"`) : e.type === "kit" ? raw('data-act="inv-goto" data-v="kits"') : e.type === "expiry" ? raw('data-act="inv-goto" data-v="expiry"') : ""}><span class="dot ${e.severity}"></span><div><b>${e.title}</b><p>${e.detail}</p></div></div>`)}${o.exceptions.length ? "" : html`<p class="muted">Nothing needs attention.</p>`}</div></div>`);
}
acts["inv-wh"] = guard(async a => { st.loc = +a.dataset.id; st.locName = (whs.find(w => w.id === st.loc) || {}).name || ""; st.tab = "stock"; st.flag = ""; st.page = 1; document.querySelectorAll("#invtabs button").forEach(b => b.classList.toggle("on", b.dataset.v === "stock")); draw(); });
acts["inv-goto"] = a => { st.tab = a.dataset.v; document.querySelectorAll("#invtabs button").forEach(b => b.classList.toggle("on", b.dataset.v === st.tab)); draw(); };

// ---------------- stock
async function stock() {
  const r = await api("/inv/stock?" + q2({ location_id: st.loc, q: st.q, flag: st.flag, sort: st.sort, page: st.page, size: 50 })), t = r.totals, isStore = r.location.type === "store";
  mount(body(), html`<div class="row" style="margin-bottom:12px">${S.user.role === "store_manager" ? html`<b>${r.location.name}</b>` : html`<select class="input" id="loc">${whs.map(w => html`<option value="${w.id}" ${w.id === st.loc ? "selected" : ""}>${w.name.replace("Dry Fruit House ", "")}</option>`)}${isStore ? html`<option value="${r.location.id}" selected>${r.location.name}</option>` : ""}</select>
    ${S.user.role !== "warehouse" ? html`<input class="input" id="storeq" placeholder="Or find a store" list="stores" style="width:200px"><datalist id="stores"></datalist>` : ""}`}
    <input class="input" id="sq" placeholder="Search SKU or code" value="${st.q}" style="width:230px"><div class="seg" id="flags">${[["", "All"], ["low", "Under min"], ["near", "Near expiry"], ["out", "Out"]].map(([k, l]) => html`<button data-act="inv-flag" data-v="${k}" class="${st.flag === k ? "on" : ""}">${l}</button>`)}</div>
    <select class="input" id="sort">${[["value", "Value"], ["qty", "Quantity"], ["expiry", "Earliest expiry"], ["name", "Name"]].map(([k, l]) => html`<option value="${k}" ${st.sort === k ? "selected" : ""}>${l}</option>`)}</select></div>
    <div class="kpis"><div class="kpi glass"><div class="l">Stock at cost</div><div class="v">${inr(t.value)}</div><div class="s">${num(t.skus)} SKUs in stock</div></div><div class="kpi glass"><div class="l">Under minimum</div><div class="v">${t.low}</div><div class="s">${isStore ? "Against this store's min level" : "Against the sum of served stores' min levels"}</div></div>
    <div class="kpi glass"><div class="l">Out of stock</div><div class="v">${t.out}</div><div class="s">Listed but nothing on hand</div></div><div class="kpi glass"><div class="l">Expiring within 30 days</div><div class="v">${t.near}</div><div class="s">SKUs with a batch close to expiry</div></div></div>
    <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>SKU</th><th class="r">On hand</th><th class="r">Min / max</th><th>Status</th><th class="r">Batches</th><th>Earliest expiry</th><th class="r">Value</th></tr></thead><tbody>
    ${r.items.map(i => html`<tr class="click" data-act="inv-sku" data-id="${i.id}"><td><b>${i.name}</b><div class="small muted">${i.code} · ${i.cat_name}</div></td><td class="r">${qty(i.qty, i.base_uom)}</td><td class="r muted">${i.min_qty == null ? "n/a" : qty(i.min_qty, i.base_uom) + " / " + qty(i.max_qty, i.base_uom)}</td><td>${pill(i.state)}</td><td class="r">${i.batches}</td><td>${i.first_exp ? html`${day(i.first_exp)} ${dl(i.days_left)}` : ""}</td><td class="r">${inr(i.value)}</td></tr>`)}${r.items.length ? "" : empty("No SKUs match.", 7)}</tbody></table></div></div>
    <div class="row" style="justify-content:space-between;margin-top:12px"><span class="muted small">${num(r.total)} SKUs. Page ${r.page}</span><div class="row"><button class="btn" data-act="inv-pg" data-v="-1" ${r.page <= 1 ? "disabled" : ""}>Previous</button><button class="btn" data-act="inv-pg" data-v="1" ${r.page * r.size >= r.total ? "disabled" : ""}>Next</button></div></div>`);
  const $ = id => body().querySelector(id);
  if ($("#loc")) $("#loc").onchange = e => { st.loc = +e.target.value; st.page = 1; draw(); };
  $("#sq").oninput = e => { st.q = e.target.value; clearTimeout(timer); timer = setTimeout(() => { st.page = 1; draw(); }, 300); }; $("#sort").onchange = e => { st.sort = e.target.value; draw(); };
  if ($("#storeq")) $("#storeq").oninput = async e => { const l = await api("/inv/locations?type=store&q=" + encodeURIComponent(e.target.value)); $("#stores").innerHTML = l.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join(""); const m = l.find(s => s.code === e.target.value); if (m) { st.loc = m.id; draw(); } };
}
acts["inv-flag"] = a => { st.flag = a.dataset.v; st.page = 1; draw(); };
acts["inv-pg"] = a => { st.page += +a.dataset.v; draw(); };
acts["inv-sku"] = guard(async a => {
  const s = await api("/inv/sku/" + a.dataset.id), led = await api(`/inv/ledger?sku_id=${s.id}&size=15` + (st.loc ? `&location_id=${st.loc}` : ""));
  layer("drawer", html`${topbar(s.cat_name, s.name, s.code + " · shelf life " + s.shelf_life_days + " days")}
    <h2 style="font-size:15px;margin:16px 0 6px">Batches, earliest expiry ships first</h2><div class="tblwrap"><table class="tbl"><thead><tr><th>Batch</th><th>Expiry</th><th class="r">On hand</th><th class="r">Locations</th><th class="r">Cost</th></tr></thead><tbody>${s.batches.map(b => html`<tr><td>${b.batch_no}<div class="small muted">received ${day(b.received_on)}</div></td><td>${day(b.expiry_date)} ${dl(b.days_left)}</td><td class="r">${qty(b.qty, s.base_uom)}</td><td class="r">${b.locs}</td><td class="r">${rs(b.unit_cost_paise / 100)}</td></tr>`)}</tbody></table></div>
    <h2 style="font-size:15px;margin:18px 0 6px">Where it sits</h2><div class="tblwrap" style="max-height:240px"><table class="tbl"><thead><tr><th>Location</th><th class="r">On hand</th><th class="r">Value</th></tr></thead><tbody>${s.locations.map(l => html`<tr><td>${l.name.replace("Dry Fruit House ", "")}<div class="small muted">${l.code}</div></td><td class="r">${qty(l.qty, s.base_uom)}</td><td class="r">${inr(l.value)}</td></tr>`)}</tbody></table></div>
    <h2 style="font-size:15px;margin:18px 0 6px">Latest ledger entries${st.loc ? " at the selected location" : ""}</h2>${ledTable(led.items)}`);
});
const ledTable = items => html`<div class="tblwrap" style="max-height:300px"><table class="tbl"><thead><tr><th>When</th><th>Movement</th><th class="r">Qty</th><th>Batch</th><th>Document</th></tr></thead><tbody>${items.map(l => html`<tr><td>${day(l.ts)}</td><td>${l.movement_type.replace("_", " ")}</td><td class="r">${l.qty > 0 ? "+" : ""}${qty(l.qty, l.base_uom)}</td><td>${l.batch_no || ""}</td><td class="small muted">${l.doc_id}</td></tr>`)}${items.length ? "" : empty("No entries here.", 5)}</tbody></table></div>`;


// ---------------- reconciliation
async function reconcile() {
  const r = await api("/inv/reconciliation?" + q2({ location_id: st.loc }));
  const labels = { quantity_mismatch: "Ledger / balance mismatch", negative_balance: "Negative balance", missing_batch: "Missing batch", batch_sku_mismatch: "Batch belongs to another SKU" };
  mount(body(), html`<div class="kpis">
    <div class="kpi glass"><div class="l">Checked positions</div><div class="v">${num(r.checked)}</div><div class="s">SKU × location × batch combinations</div></div>
    <div class="kpi glass"><div class="l">Quantity mismatches</div><div class="v">${num(r.mismatch_count)}</div><div class="s">Balance less ledger quantity</div></div>
    <div class="kpi glass"><div class="l">Absolute variance</div><div class="v">${num(r.absolute_variance)}</div><div class="s">In SKU base units, not value</div></div>
    <div class="kpi glass"><div class="l">Integrity exceptions</div><div class="v">${num(r.issue_count)}</div><div class="s">Includes negative balances and invalid batch links</div></div>
  </div>
  <div class="card glass" style="margin-bottom:12px;padding:14px"><b>Read-only check</b><p class="small muted" style="margin-top:4px">The ledger is the movement history; stock balance is its live projection. This screen compares them and never posts corrective entries.</p></div>
  <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Exception</th><th>Location</th><th>SKU / batch</th><th class="r">Ledger</th><th class="r">Balance</th><th class="r">Variance</th></tr></thead><tbody>
  ${r.items.map(i => html`<tr><td>${labels[i.kind] || i.kind}</td><td>${i.location_code}</td><td><b>${i.sku_name}</b><div class="small muted">${i.sku_code} · ${i.batch_no || "No batch"}</div></td><td class="r">${qty(i.ledger_qty, i.base_uom)}</td><td class="r">${qty(i.balance_qty, i.base_uom)}</td><td class="r">${i.variance_qty ? raw(`<b style="color:var(--bad)">${esc(qty(i.variance_qty, i.base_uom))}</b>`) : "—"}</td></tr>`)}${r.items.length ? "" : empty("No reconciliation exceptions found.", 6)}</tbody></table></div></div>
  <p class="small muted" style="margin-top:8px">Showing up to ${r.limit} exceptions. Counts and variance totals cover the full permitted scope.</p>`);
}

// ---------------- expiry
async function expiry() {
  const r = await api("/inv/expiry?" + q2({ days: st.days })), b = r.buckets;
  mount(body(), html`<div class="row" style="margin-bottom:12px"><div class="seg">${[[30, "30 days"], [60, "60 days"], [90, "90 days"]].map(([d, l]) => html`<button data-act="inv-days" data-v="${d}" class="${st.days === d ? "on" : ""}">${l}</button>`)}</div></div>
    <div class="kpis">${[["Already expired", b.expired, "Stop selling, write off"], ["Within 15 days", b.d15, "Markdown now"], ["16 to 30 days", b.d30, "Push in mixes"], ["31 to 60 days", b.d60, "Watch"]].map(([l, v, s]) => html`<div class="kpi glass"><div class="l">${l}</div><div class="v">${inr(v)}</div><div class="s">${s}, at cost</div></div>`)}</div>
    <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>SKU and batch</th><th>Expiry</th><th class="r">Quantity</th><th class="r">Locations</th><th class="r">Value</th><th>Suggested action</th></tr></thead><tbody>
    ${r.items.map(i => html`<tr><td><b>${i.name}</b><div class="small muted">${i.batch_no} · ${i.cat_name}${i.kiosks ? " · " + i.kiosks + " kiosks" : ""}</div></td><td>${day(i.expiry_date)} ${dl(i.days_left)}</td><td class="r">${qty(i.qty, i.base_uom)}</td><td class="r">${i.locs}</td><td class="r">${inr(i.value)}</td><td class="small">${i.action}</td></tr>`)}${r.items.length ? "" : empty("Nothing expires in this window.")}</tbody></table></div></div>`);
}
acts["inv-days"] = a => { st.days = +a.dataset.v; draw(); };

// ---------------- transfers
async function transfers() {
  const r = await api("/inv/transfers?" + q2({ status: st.tstatus, page: st.page, size: 30 }));
  mount(body(), html`<div class="row" style="margin-bottom:12px;justify-content:space-between"><div class="seg">${[["", "All"], ["draft", "Draft"], ["in_transit", "In transit"], ["received", "Received"]].map(([k, l]) => html`<button data-act="inv-tf" data-v="${k}" class="${st.tstatus === k ? "on" : ""}">${l}</button>`)}</div>${isW() ? html`<button class="btn pri" data-act="inv-newtr">New transfer</button>` : ""}</div>
    <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Transfer</th><th>From</th><th>To</th><th class="r">Lines</th><th class="r">Value</th><th>Status</th><th>Dispatched</th></tr></thead><tbody>
    ${r.items.map(t => html`<tr class="click" data-act="inv-tr" data-id="${t.id}"><td><b>${t.transfer_no}</b>${t.indent_id ? html`<div class="small muted">from indent</div>` : ""}</td><td>${t.from_code}</td><td>${t.to_code}</td><td class="r">${t.lines}</td><td class="r">${inr(t.value || 0)}</td><td>${pill(t.status)}${t.short_lines ? html` <span class="pill warn">${t.short_lines} short</span>` : ""}</td><td>${day(t.dispatched_at)}</td></tr>`)}${r.items.length ? "" : empty("No transfers.", 7)}</tbody></table></div></div>`);
}
acts["inv-tf"] = a => { st.tstatus = a.dataset.v; draw(); };
acts["inv-tr"] = guard(async a => openTransfer(+a.dataset.id));
async function openTransfer(id) {
  const t = await api("/inv/transfers/" + id), mine = isW() && (S.user.scope_type !== "warehouse" || S.user.scope_id === t.from_location_id), recv = t.status === "in_transit" && (isW() || (S.user.role === "store_manager" && S.user.scope_id === t.to_location_id));
  layer("drawer", html`${topbar("Transfer", t.transfer_no, `${t.from_name} to ${t.to_name}`)}<div class="row" style="margin:10px 0">${pill(t.status)}${t.vehicle_no ? html`<span class="muted small">Vehicle ${t.vehicle_no}</span>` : ""}${t.eway_ref ? html`<span class="muted small">E-way ${t.eway_ref}</span>` : ""}</div>
    <div class="tblwrap"><table class="tbl"><thead><tr><th>SKU</th><th>Batch</th><th class="r">Sent</th><th class="r">Received</th></tr></thead><tbody>${t.lines.map(l => html`<tr><td>${l.name}</td><td>${l.batch_no || html`<span class="muted">picked at dispatch</span>`}${l.expiry_date ? html`<div class="small muted">exp ${day(l.expiry_date)}</div>` : ""}</td><td class="r">${qty(l.qty_sent, l.base_uom)}</td>
    <td class="r">${recv ? html`<input class="input rq" data-line="${l.id}" data-uom="${l.base_uom}" type="number" min="0" step="${l.base_uom === "g" ? 100 : 1}" max="${l.qty_sent}" value="${l.qty_sent}" style="width:90px;text-align:right">` : l.qty_received == null ? "" : qty(l.qty_received, l.base_uom)}</td></tr>`)}</tbody></table></div>
    <div class="row" style="margin-top:14px">${t.status === "draft" && mine ? html`<input class="input" id="veh" placeholder="Vehicle number" value="${t.vehicle_no || ""}"><input class="input" id="ew" placeholder="E-way reference (inter-state, above ₹50,000)"><button class="btn pri" data-act="inv-dispatch" data-id="${id}">Dispatch</button><button class="btn" data-act="inv-trcancel" data-id="${id}">Cancel draft</button>` : ""}
    ${recv ? html`<button class="btn pri" data-act="inv-recv" data-id="${id}">Confirm receipt</button><span class="small muted">Lower a quantity to record a short delivery.</span>` : ""}</div>
    ${t.ledger.length ? html`<h2 style="font-size:15px;margin:18px 0 6px">Ledger entries</h2><div class="tblwrap" style="max-height:220px"><table class="tbl"><tbody>${t.ledger.map(l => html`<tr><td>${l.movement_type.replace("_", " ")}</td><td>${l.sku_name}</td><td>${l.loc_code}</td><td class="r">${l.qty > 0 ? "+" : ""}${l.qty}</td></tr>`)}</tbody></table></div>` : ""}`);
}
acts["inv-dispatch"] = guard(async a => { await api(`/inv/transfers/${a.dataset.id}/dispatch`, { method: "POST", body: { vehicle_no: document.querySelector("#veh").value, eway_ref: document.querySelector("#ew").value } }); toast("Dispatched. Earliest expiry batches were picked."); await openTransfer(+a.dataset.id); draw(); });
acts["inv-trcancel"] = guard(async a => { await api(`/inv/transfers/${a.dataset.id}/cancel`, { method: "POST" }); toast("Draft cancelled"); close(); draw(); });
acts["inv-recv"] = guard(async a => { const lines = [...document.querySelectorAll(".rq")].map(i => ({ line_id: +i.dataset.line, qty_received: +i.value })); await api(`/inv/transfers/${a.dataset.id}/receive`, { method: "POST", body: { lines } }); toast("Receipt recorded"); await openTransfer(+a.dataset.id); draw(); });

// line builder shared by transfers and indents
function lineBuilder() {
  return html`<label class="small muted">Add SKU</label><div class="row"><input class="input" id="skq" list="skl" placeholder="SKU code or name" style="flex:1"><datalist id="skl"></datalist><input class="input" id="skn" type="number" min="0" step="any" placeholder="Qty" style="width:90px"><button class="btn" data-act="inv-addline">Add</button></div>
    <p class="small muted" style="margin:4px 0 8px">Loose SKUs are entered in kg. Packed SKUs in units.</p><div id="lines"></div>`;
}
function bindBuilder() {
  const i = document.querySelector("#skq"); i.oninput = async () => { const r = await api("/skus?" + q2({ q: i.value, size: 8, status: "active" })); document.querySelector("#skl").innerHTML = r.items.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join(""); }; drawLines();
}
const drawLines = () => { const el = document.querySelector("#lines"); if (el) mount(el, html`${draft.lines.map((l, k) => html`<div class="row" style="justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line)"><span>${l.name}</span><span>${l.uom === "g" ? l.qty / 1000 + " kg" : l.qty}<button class="icon" data-act="inv-rmline" data-v="${k}" aria-label="Remove">✕</button></span></div>`)}`); };
acts["inv-addline"] = guard(async () => {
  const code = document.querySelector("#skq").value.trim(), n = +document.querySelector("#skn").value; if (!code || !(n > 0)) throw new Error("Pick a SKU and a quantity");
  const r = await api("/skus?" + q2({ q: code, size: 5 })), s = r.items.find(x => x.code === code) || (r.items.length === 1 ? r.items[0] : null); if (!s) throw new Error("Choose a SKU from the list");
  const q = s.base_uom === "g" ? Math.round(n * 1000) : Math.round(n); draft.lines = draft.lines.filter(l => l.sku_id !== s.id).concat([{ sku_id: s.id, name: s.name, qty: q, uom: s.base_uom }]);
  document.querySelector("#skq").value = ""; document.querySelector("#skn").value = ""; drawLines();
});
acts["inv-rmline"] = a => { draft.lines.splice(+a.dataset.v, 1); drawLines(); };
const payLines = () => draft.lines.map(l => ({ sku_id: l.sku_id, qty: l.qty }));
async function destList() { const l = await api("/inv/locations?type=store&q=" + encodeURIComponent(document.querySelector("#dq").value)); document.querySelector("#dl").innerHTML = l.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join(""); return l; }

acts["inv-newtr"] = guard(async () => {
  draft = { lines: [] }; const from = S.user.scope_type === "warehouse" ? whs.filter(w => w.id === S.user.scope_id) : whs;
  layer("modal", html`${topbar("Transfer", "New transfer", "Stock leaves the source when you dispatch, not when you save.")}<label class="small muted">From warehouse</label><select class="input" id="tf" style="width:100%">${from.map(w => html`<option value="${w.id}">${w.name.replace("Dry Fruit House ", "")}</option>`)}</select>
    <label class="small muted">To warehouse or store</label><input class="input" id="dq" list="dl" placeholder="Code, for example DFH-WH-BLR-2" style="width:100%"><datalist id="dl">${whs.map(w => raw(`<option value="${esc(w.code)}">${esc(w.name)}</option>`))}</datalist>
    ${lineBuilder()}<div class="row" style="margin-top:14px"><button class="btn pri" data-act="inv-savetr">Save draft</button></div>`);
  bindBuilder(); const d = document.querySelector("#dq"), base = d.nextElementSibling.innerHTML; d.oninput = async () => { const l = await api("/inv/locations?type=store&q=" + encodeURIComponent(d.value)); document.querySelector("#dl").innerHTML = base + l.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join(""); };
});
acts["inv-savetr"] = guard(async () => {
  const code = document.querySelector("#dq").value.trim(), l = [...whs, ...await api("/inv/locations?type=store&q=" + encodeURIComponent(code))].find(x => x.code === code); if (!l) throw new Error("Choose a destination from the list");
  const t = await api("/inv/transfers", { method: "POST", body: { from_id: +document.querySelector("#tf").value, to_id: l.id, lines: payLines() } }); toast("Draft saved"); await openTransfer(t.id); draw();
});

// ---------------- indents
async function indents() {
  const r = await api("/inv/indents?" + q2({ status: st.istatus, page: st.page, size: 30 }));
  mount(body(), html`<div class="row" style="margin-bottom:12px;justify-content:space-between"><div class="seg">${[["", "All"], ["open", "Open"], ["allocated", "Allocated"], ["dispatched", "Dispatched"], ["received", "Received"]].map(([k, l]) => html`<button data-act="inv-if" data-v="${k}" class="${st.istatus === k ? "on" : ""}">${l}</button>`)}</div><button class="btn pri" data-act="inv-newind">New indent</button></div>
    <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Indent</th><th>Store</th><th>Warehouse</th><th class="r">Lines</th><th class="r">Allocated</th><th>Needed by</th><th>Status</th></tr></thead><tbody>
    ${r.items.map(i => html`<tr class="click" data-act="inv-ind" data-id="${i.id}"><td><b>${i.indent_no}</b></td><td>${i.store_name.replace("Dry Fruit House ", "")}<div class="small muted">${i.store_code}</div></td><td>${i.wh_code}</td><td class="r">${i.lines}</td><td class="r">${i.req ? pct(i.alloc / i.req, 0) : ""}</td><td>${day(i.needed_by)}</td><td>${pill(i.status)}</td></tr>`)}${r.items.length ? "" : empty("No indents.", 7)}</tbody></table></div></div>`);
}
acts["inv-if"] = a => { st.istatus = a.dataset.v; draw(); };
acts["inv-ind"] = guard(async a => openIndent(+a.dataset.id));
async function openIndent(id) {
  const i = await api("/inv/indents/" + id), w = isW() && (S.user.scope_type !== "warehouse" || S.user.scope_id === i.warehouse_id), own = S.user.role === "store_manager" && S.user.scope_id === i.store_id;
  layer("drawer", html`${topbar("Indent", i.indent_no, `${i.store_name} from ${i.wh_name}, needed by ${day(i.needed_by)}`)}<div class="row" style="margin:10px 0">${pill(i.status)}${i.transfer ? html`<button class="btn" data-act="inv-tr" data-id="${i.transfer.id}">${i.transfer.transfer_no}</button>` : ""}</div>
    <div class="tblwrap"><table class="tbl"><thead><tr><th>SKU</th><th class="r">Requested</th><th class="r">Allocated</th><th>Picked from</th></tr></thead><tbody>${i.lines.map(l => html`<tr><td>${l.name}</td><td class="r">${qty(l.qty_req, l.base_uom)}</td><td class="r">${l.qty_alloc < l.qty_req && i.status !== "open" ? raw(`<b style="color:var(--warn)">${esc(qty(l.qty_alloc, l.base_uom))}</b>`) : qty(l.qty_alloc, l.base_uom)}</td><td class="small muted">${l.picks || ""}</td></tr>`)}</tbody></table></div>
    <div class="row" style="margin-top:14px">${w && ["open", "allocated"].includes(i.status) ? html`<button class="btn ${i.status === "open" ? "pri" : ""}" data-act="inv-alloc" data-id="${id}">${i.status === "open" ? "Allocate stock" : "Re-allocate"}</button>` : ""}
    ${w && i.status === "allocated" ? html`<input class="input" id="veh" placeholder="Vehicle number"><input class="input" id="ew" placeholder="E-way reference if needed"><button class="btn pri" data-act="inv-idisp" data-id="${id}">Dispatch</button>` : ""}${(w || own) && ["open", "allocated"].includes(i.status) ? html`<button class="btn" data-act="inv-icancel" data-id="${id}">Cancel indent</button>` : ""}</div>`);
}
acts["inv-alloc"] = guard(async a => { const r = await api(`/inv/indents/${a.dataset.id}/allocate`, { method: "POST" }); const short = r.lines.filter(l => l.qty_alloc < l.qty_req).length; toast(short ? `Allocated, ${short} line${short > 1 ? "s" : ""} short` : "Allocated in full, earliest expiry first"); await openIndent(+a.dataset.id); draw(); });
acts["inv-idisp"] = guard(async a => { await api(`/inv/indents/${a.dataset.id}/dispatch`, { method: "POST", body: { vehicle_no: document.querySelector("#veh").value, eway_ref: document.querySelector("#ew").value } }); toast("Dispatched, transfer created"); await openIndent(+a.dataset.id); draw(); });
acts["inv-icancel"] = guard(async a => { await api(`/inv/indents/${a.dataset.id}/cancel`, { method: "POST" }); toast("Indent cancelled"); close(); draw(); });
acts["inv-newind"] = guard(async () => {
  draft = { lines: [] }; const sm = S.user.role === "store_manager";
  layer("modal", html`${topbar("Indent", "New indent", "Only SKUs in the store's assortment can be requested.")}${sm ? "" : html`<label class="small muted">Store</label><input class="input" id="dq" list="dl" placeholder="Store code" style="width:100%"><datalist id="dl"></datalist>`}
    <label class="small muted">Needed by</label><input class="input" id="nb" type="date" style="width:100%">${lineBuilder()}<div class="row" style="margin-top:14px"><button class="btn pri" data-act="inv-saveind">Raise indent</button></div>`);
  bindBuilder(); const d = document.querySelector("#dq"); if (d) d.oninput = destList;
});
acts["inv-saveind"] = guard(async () => {
  const body = { lines: payLines() }, nb = document.querySelector("#nb").value; if (nb) body.needed_by = nb;
  if (S.user.role !== "store_manager") { const code = document.querySelector("#dq").value.trim(), l = (await api("/inv/locations?type=store&q=" + encodeURIComponent(code))).find(x => x.code === code); if (!l) throw new Error("Choose a store from the list"); body.store_id = l.id; }
  const i = await api("/inv/indents", { method: "POST", body }); toast("Indent raised"); await openIndent(i.id); draw();
});

// ---------------- repack
async function repack() {
  const wh = S.user.scope_type === "warehouse" ? S.user.scope_id : (st.rwh || (whs[0] || {}).id); st.rwh = wh;
  const [opts, jobs] = await Promise.all([api("/inv/repack/options?warehouse_id=" + wh), api("/inv/repack?warehouse_id=" + wh)]);
  mount(body(), html`<div class="row" style="margin-bottom:12px">${S.user.scope_type === "warehouse" ? html`<b>${(whs.find(w => w.id === wh) || {}).name}</b>` : html`<select class="input" id="rwh">${whs.map(w => html`<option value="${w.id}" ${w.id === wh ? "selected" : ""}>${w.name.replace("Dry Fruit House ", "")}</option>`)}</select>`}</div>
    <div class="cols eq">${isW() ? html`<div class="card glass"><div class="ct"><h2>New repack job</h2><span class="small muted">Loose stock into sealed packs. Loss above 2% is flagged, above 5% is blocked.</span></div>
    <label class="small muted">Bulk SKU</label><select class="input" id="rb" style="width:100%">${opts.map((o, k) => html`<option value="${k}">${o.name}</option>`)}</select><label class="small muted">Batch, earliest expiry first</label><select class="input" id="rbt" style="width:100%"></select>
    <label class="small muted">Input in kg</label><input class="input" id="rin" type="number" step="0.1" min="0.1" style="width:100%"><div id="rout"></div><p class="small" id="ryield" style="margin:10px 0"></p><button class="btn pri" data-act="inv-dorepack">Record repack</button></div>` : ""}
    <div class="card solid"><div class="ct"><h2>Recent jobs</h2></div><div class="tblwrap" style="max-height:480px"><table class="tbl"><thead><tr><th>Job</th><th>Input</th><th>Output</th><th class="r">Loss</th></tr></thead><tbody>${jobs.items.map(j => html`<tr><td><b>${j.job_no}</b><div class="small muted">${j.bulk_name} · ${day(j.started_at)}</div></td><td>${qty(j.input_g, "g")}</td><td class="small">${j.outputs}</td><td class="r">${j.loss_pct > 0.02 ? raw(`<b style="color:var(--bad)">${(j.loss_pct * 100).toFixed(1)}%</b>`) : (j.loss_pct * 100).toFixed(1) + "%"}</td></tr>`)}${jobs.items.length ? "" : empty("No jobs yet.", 4)}</tbody></table></div></div></div>`);
  if (body().querySelector("#rwh")) body().querySelector("#rwh").onchange = e => { st.rwh = +e.target.value; draw(); };
  if (!isW()) return;
  const sel = () => opts[+body().querySelector("#rb").value], upd = () => {
    const o = sel(); if (!o) return; body().querySelector("#rbt").innerHTML = o.batches.map(b => `<option value="${b.id}">${esc(b.batch_no)} · ${esc(qty(b.qty, "g"))} · exp ${day(b.expiry_date)}</option>`).join("");
    mount(body().querySelector("#rout"), html`${o.packs.map(p => html`<div class="row" style="margin-top:6px"><span style="flex:1">${p.name}</span><input class="input ru" data-sku="${p.id}" data-g="${p.pack_g}" type="number" min="0" placeholder="units" style="width:100px"></div>`)}`); calc();
  }, calc = () => { const inp = Math.round((+body().querySelector("#rin").value || 0) * 1000), packed = [...body().querySelectorAll(".ru")].reduce((a, i) => a + (+i.value || 0) * +i.dataset.g, 0), p = body().querySelector("#ryield"); if (!inp) return p.textContent = ""; const l = (inp - packed) / inp * 100;
    p.innerHTML = packed > inp ? '<b style="color:var(--bad)">Packs add up to more than the input.</b>' : `Packed ${esc(qty(packed, "g"))}, loss ${(inp - packed)} g (${l.toFixed(1)}%). ` + (l > 5 ? '<b style="color:var(--bad)">Above the 5% limit.</b>' : l > 2 ? '<b style="color:var(--warn)">Above 2%, will be flagged.</b>' : "Within normal yield."); };
  body().querySelector("#rb").onchange = upd; body().querySelector("#rin").oninput = calc; body().querySelector("#rout").oninput = calc; body().querySelector("#rb").dataset.opts = "1"; window.__repackOpts = opts; if (opts.length) upd(); else body().querySelector("#rout").innerHTML = '<p class="muted small">No loose stock available to repack here.</p>';
}
acts["inv-dorepack"] = guard(async () => {
  const o = window.__repackOpts[+body().querySelector("#rb").value], outputs = [...body().querySelectorAll(".ru")].filter(i => +i.value > 0).map(i => ({ sku_id: +i.dataset.sku, units: +i.value }));
  const r = await api("/inv/repack", { method: "POST", body: { warehouse_id: st.rwh, bulk_sku_id: o.id, batch_id: +body().querySelector("#rbt").value, input_g: Math.round(+body().querySelector("#rin").value * 1000), outputs } });
  toast(`${r.job_no} recorded. Loss ${r.loss_pct}%${r.warn ? ", flagged" : ""}`, r.warn); draw();
});

// ---------------- counts
async function counts() {
  const r = await api("/inv/counts" + (S.user.scope_type === "warehouse" ? "?location_id=" + S.user.scope_id : ""));
  mount(body(), html`<div class="row" style="margin-bottom:12px;justify-content:space-between"><span class="muted small">Class A counts the top 20% of stock lines by value, B the next 30%, C the rest.</span>${isW() ? html`<button class="btn pri" data-act="inv-newcount">New count</button>` : ""}</div>
    <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Plan</th><th>Location</th><th>Class</th><th class="r">Lines</th><th class="r">With variance</th><th>Planned</th><th>Status</th></tr></thead><tbody>${r.map(p => html`<tr class="click" data-act="inv-count" data-id="${p.id}"><td><b>${p.plan_no}</b></td><td>${p.loc_name.replace("Dry Fruit House ", "")}</td><td>${p.abc_class}</td><td class="r">${p.lines}</td><td class="r">${p.with_variance || ""}</td><td>${day(p.planned_on)}</td><td>${pill(p.status)}</td></tr>`)}${r.length ? "" : empty("No counts.", 7)}</tbody></table></div></div>`);
}
acts["inv-count"] = guard(async a => openCount(+a.dataset.id));
async function openCount(id) {
  const p = await api("/inv/counts/" + id), w = isW(), plan = p.status === "planned";
  layer("drawer", html`${topbar("Cycle count · class " + p.abc_class, p.plan_no, p.loc_name)}<div class="row" style="margin:10px 0">${pill(p.status)}</div>
    <div class="tblwrap"><table class="tbl"><thead><tr><th>SKU and batch</th><th class="r">System</th><th class="r">Counted</th><th class="r">Variance</th><th class="r">Value</th></tr></thead><tbody>${p.lines.map(l => html`<tr><td>${l.name}<div class="small muted">${l.batch_no}</div></td><td class="r">${qty(l.system_qty, l.base_uom)}</td>
    <td class="r">${plan && w ? html`<input class="input cq" data-line="${l.id}" type="number" min="0" step="${l.base_uom === "g" ? 100 : 1}" value="${l.system_qty}" style="width:100px;text-align:right">` : l.counted_qty == null ? "" : qty(l.counted_qty, l.base_uom)}</td>
    <td class="r">${l.variance_qty ? raw(`<b style="color:var(--bad)">${l.variance_qty > 0 ? "+" : ""}${esc(qty(l.variance_qty, l.base_uom))}</b>`) : l.variance_qty === 0 ? "0" : ""}</td><td class="r">${l.var_value ? inr(l.var_value) : ""}</td></tr>`)}</tbody></table></div>
    <div class="row" style="margin-top:14px">${plan && w ? html`<button class="btn pri" data-act="inv-csub" data-id="${id}">Submit counts</button><span class="small muted">Quantities start at the system figure. Change only what you found different.</span>` : ""}${p.status === "submitted" && w ? html`<button class="btn pri" data-act="inv-capp" data-id="${id}">Approve and post adjustments</button><span class="small muted">${p.approve_note}</span>` : ""}</div>`);
}
acts["inv-csub"] = guard(async a => { const lines = [...document.querySelectorAll(".cq")].map(i => ({ line_id: +i.dataset.line, counted_qty: +i.value })); await api(`/inv/counts/${a.dataset.id}/submit`, { method: "POST", body: { lines } }); toast("Counts submitted"); await openCount(+a.dataset.id); draw(); });
acts["inv-capp"] = guard(async a => { await api(`/inv/counts/${a.dataset.id}/approve`, { method: "POST" }); toast("Approved, adjustments posted to the ledger"); await openCount(+a.dataset.id); draw(); });
acts["inv-newcount"] = guard(async () => {
  const from = S.user.scope_type === "warehouse" ? whs.filter(w => w.id === S.user.scope_id) : whs;
  layer("modal", html`${topbar("Cycle count", "New count", "A snapshot of system quantities is taken now and refreshed when you submit.")}<label class="small muted">Warehouse</label><select class="input" id="cl" style="width:100%">${from.map(w => html`<option value="${w.id}">${w.name.replace("Dry Fruit House ", "")}</option>`)}</select>
    <label class="small muted">Class</label><select class="input" id="cc" style="width:100%"><option>A</option><option>B</option><option>C</option></select><label class="small muted">Lines</label><input class="input" id="cs" type="number" min="1" max="50" value="15" style="width:100%"><div class="row" style="margin-top:14px"><button class="btn pri" data-act="inv-mkcount">Create count</button></div>`);
});
acts["inv-mkcount"] = guard(async () => { const p = await api("/inv/counts", { method: "POST", body: { location_id: +document.querySelector("#cl").value, abc_class: document.querySelector("#cc").value, size: +document.querySelector("#cs").value } }); toast("Count created"); await openCount(p.id); draw(); });

// ---------------- gift boxes
async function kits() {
  const k = await api("/inv/kits");
  mount(body(), html`<p class="muted" style="margin-bottom:12px">${k.assumption}. Buildable is limited by the scarcest component in warehouse stock. ${k.short} of ${k.total} boxes cannot be fully built.</p>
    <div class="solid"><div class="tblwrap"><table class="tbl"><thead><tr><th>Gift box</th><th class="r">Needed</th><th class="r">On hand</th><th class="r">Still to supply</th><th class="r">Buildable</th><th>Limiting component</th><th></th></tr></thead><tbody>
    ${k.items.map(x => html`<tr><td><b>${x.name}</b></td><td class="r">${num(x.need)}</td><td class="r">${num(x.on_hand)}</td><td class="r">${num(x.gap)}</td><td class="r">${num(x.buildable)}</td><td>${x.limiter.name}<div class="small muted">${num(x.limiter.on_hand)} in warehouses, ${x.limiter.per_kit} per box</div></td><td>${x.short ? html`<span class="pill delisted">short by ${num(x.shortfall)}</span>` : html`<span class="pill active">covered</span>`}</td></tr>`)}</tbody></table></div></div>`);
}
