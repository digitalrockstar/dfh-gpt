import { html, raw, out, inr, num, pct, dmy } from "./ui.js";
import { api, acts, mount, toast, S } from "./core.js";
import { spark } from "./ui.js";

const st = { tab: "overview", q: "", seg: "", consent: "", sort: "spend", page: 1, ostatus: "open", opage: 1, oq: "", aged: 0, ifilter: "unanswered", ipage: 1, icur: null, qstatus: "" };
let root = null, opts = null, nl = [], ql = [];
const $ = s => document.querySelector(s);
const fdt = t => { const d = new Date(String(t).replace(" ", "T")); return isNaN(d) ? "" : d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); };
const fd = t => t ? dmy(String(t).slice(0, 10)) : "";
const pill = (t, c = "") => html`<span class="pill ${c}">${t}</span>`;
const empty = t => html`<p class="muted small" style="padding:14px 0">${t}</p>`;
const kpi = (l, v, s) => html`<div class="kpi glass"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
const seg = (id, act, cur, items) => html`<div class="seg" id="${id}">${items.map(([v, l]) => html`<button data-act="${act}" data-v="${v}" class="${String(cur) === String(v) ? "on" : ""}">${l}</button>`)}</div>`;
const flip = (id, v) => document.querySelectorAll(`#${id} button`).forEach(b => b.classList.toggle("on", b.dataset.v === String(v)));
const LBL = { new: "New", confirmed: "Confirmed", pay_pending: "Awaiting payment", paid: "Paid", packed: "Packed", dispatched: "Dispatched", delivered: "Delivered", cancelled: "Cancelled", draft: "Draft", sent: "Sent", accepted: "Accepted", lost: "Lost", expired: "Expired", scheduled: "Scheduled", queued: "Queued (simulated)" };
const OK = { delivered: "active", accepted: "active", sent: "t", queued: "t" }, BAD = { cancelled: 1, lost: 1, expired: 1 };
const sp = s => pill(LBL[s] || s, OK[s] || (BAD[s] ? "warn" : "t"));
const ACT = { confirm: "Confirm order", send_link: "Send payment link", mark_paid: "Payment received (simulated)", pack: "Mark packed", dispatch: "Dispatch from store", deliver: "Mark delivered", cancel: "Cancel order" };
const closeAll = () => document.querySelectorAll(".scrim,.drawer,.modal").forEach(n => n.remove());
const drawer = inner => { closeAll(); document.body.insertAdjacentHTML("beforeend", out(html`<div class="scrim" data-act="c-close"></div><div class="drawer glass wide">${inner}</div>`)); };
const modal = inner => { closeAll(); document.body.insertAdjacentHTML("beforeend", out(html`<div class="scrim" data-act="c-close"></div><div class="modal glass">${inner}</div>`)); };
const debounce = (f, ms = 250) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; };
const TABS = o => [["overview", "Overview", 1], ["customers", "Customers", 1], ["orders", "WhatsApp orders", 1], ["inbox", "Inbox", 1], ["campaigns", "Campaigns", o.can_campaign], ["quotes", "Corporate quotes", o.can_quotes]].filter(t => t[2]);

export async function render(el) {
  root = el; opts = await api("/crm/options");
  const tabs = TABS(opts); if (!tabs.find(t => t[0] === st.tab)) st.tab = "overview";
  mount(el, html`<div class="head"><div><div class="eyebrow">Module 8</div><h1>Customers and WhatsApp</h1><p class="muted">${opts.notice}${opts.store_scope ? " You see your own store's customers and orders." : ""}</p></div>
    <div class="row">${seg("ctabs", "c-tab", st.tab, tabs.map(t => [t[0], t[1]]))}</div></div><div id="cbody"><div class="card glass"><div class="sk" style="height:120px"></div></div></div>`);
  load();
}
function load() {
  const f = { overview, customers, orders, inbox, campaigns, quotes }[st.tab];
  f().catch(e => { const b = $("#cbody"); if (b) b.innerHTML = out(html`<div class="card glass"><p class="muted">${e.message}</p></div>`); });
}

// ---------- overview ----------
async function overview() {
  const s = await api("/crm/summary"), w = s.wa, k = [
    kpi("Customers", num(s.customers), `${pct(s.consent_pct, 0)} opted in to WhatsApp, ${num(s.new30)} new in 30 days`),
    kpi("Repeat customers", pct(s.repeat_pct, 0), `${num(s.active90)} bought in the last 90 days`),
    kpi("Loyalty liability", inr(s.liability), `${num(s.points)} points outstanding. ${opts.assumptions.point_value}`),
    kpi("WhatsApp orders, 30 days", num(w.orders), `${inr(w.value)}, ${num(w.open)} open, ${pct(w.paid_pct, 0)} reach payment`),
    kpi("Unpaid over 24 hours", num(w.unpaid_aged), `${inr(w.unpaid_aged_value)} waiting on a payment link`),
    kpi("Messages waiting", num(s.inbox.unanswered), `${num(s.inbox.past_window)} past the 24 hour window`)];
  if (s.quotes) k.push(kpi("Quote pipeline", inr(s.quotes.open_value), `${num(s.quotes.open)} open. Win rate ${s.quotes.win_rate == null ? "n/a" : pct(s.quotes.win_rate, 0)} in 60 days`));
  mount($("#cbody"), html`<div class="kpis">${k}</div><div class="cols eq"><div class="card solid"><div class="ct"><h2>Segments</h2></div><table class="tbl"><thead><tr><th>Segment</th><th class="r">Customers</th><th class="r">Avg orders</th><th class="r">Spend</th><th class="r">Opted in</th></tr></thead><tbody>
    ${s.segments.map(r => html`<tr class="click" data-act="c-segjump" data-v="${r.segment}"><td><b>${r.segment}</b></td><td class="num r">${num(r.n)}</td><td class="num r">${r.avg_orders.toFixed(1)}</td><td class="num r">${inr(r.spend)}</td><td class="num r">${pct(r.cons / r.n, 0)}</td></tr>`)}</tbody></table>
    <p class="small muted" style="margin-top:8px">Rules, in order: New is first seen in the last 30 days. One-time has a single order. Lapsing last bought over 75 days ago. Loyal has 6 or more orders and bought in the last 45 days. Everyone else is Regular. Customer purchases are synthetic and not linked to bills.</p></div>
    <div class="card glass"><div class="ct"><h2>${s.campaigns ? "Recent campaign opt-outs" : "WhatsApp orders by stage"}</h2></div>${s.campaigns ? html`${s.campaigns.map(c => html`<div class="row" style="justify-content:space-between;padding:7px 0"><span>${c.name}<div class="small muted">${num(c.sent)} sent</div></span><span class="num">${pct(c.optout_pct, 1)} ${c.optout_pct >= 0.02 ? pill("High", "warn") : ""}</span></div>`)}<p class="small muted" style="margin-top:8px">Opt-outs above 2 percent get raised in the Control Tower.</p>` : html`<p class="muted small">Open the WhatsApp orders tab to work the queue.</p>`}</div></div>`);
}
acts["c-segjump"] = a => { st.seg = a.dataset.v; st.page = 1; st.tab = "customers"; flip("ctabs", "customers"); load(); };

// ---------- customers ----------
async function customers() {
  const p = new URLSearchParams({ q: st.q, segment: st.seg, consent: st.consent, sort: st.sort, page: st.page, size: 25 });
  const r = await api("/crm/customers?" + p);
  mount($("#cbody"), html`<div class="card solid"><div class="ct"><h2>Customers</h2><span class="small muted">${num(r.total)} found</span></div>
    <div class="row" style="flex-wrap:wrap;gap:10px;margin-bottom:12px"><input class="input" id="cq" placeholder="Name, phone or code" value="${st.q}" style="min-width:220px">
    ${seg("csegs", "c-seg", st.seg, [["", "All"], ...["New", "Regular", "Loyal", "Lapsing", "One-time"].map(x => [x, x])])}${seg("ccons", "c-cons", st.consent, [["", "Any consent"], ["1", "Opted in"], ["0", "Not opted in"]])}
    <select class="input" id="csort"><option value="spend">Highest spend</option><option value="recent">Latest order</option><option value="orders">Most orders</option><option value="points">Most points</option><option value="new">Newest</option></select></div>
    ${r.items.length ? html`<div class="tblwrap"><table class="tbl"><thead><tr><th>Customer</th><th>Segment</th><th class="r">Orders</th><th class="r">Spend</th><th>Last order</th><th class="r">Points</th><th>WhatsApp</th></tr></thead><tbody>
    ${r.items.map(c => html`<tr class="click" data-act="c-cust" data-id="${c.id}"><td><b>${c.name || "No name"}</b><div class="small muted">${c.phone}, ${c.city}</div></td><td>${pill(c.segment, "t")}</td><td class="num r">${c.orders_n}</td><td class="num r">${inr(c.spend_paise)}</td><td>${fd(c.last_order_on)}</td><td class="num r">${num(c.loyalty_points)}</td><td>${c.consent_wa ? pill("Opted in", "active") : pill("No consent")}</td></tr>`)}</tbody></table></div>
    <div class="row" style="justify-content:space-between;margin-top:10px"><span class="small muted">Page ${r.page} of ${Math.max(1, Math.ceil(r.total / r.size))}</span><span><button class="btn sm" data-act="c-page" data-v="-1" ${r.page <= 1 ? "disabled" : ""}>Previous</button> <button class="btn sm" data-act="c-page" data-v="1" ${r.page * r.size >= r.total ? "disabled" : ""}>Next</button></span></div>` : empty("No customers match.")}</div>`);
  $("#csort").value = st.sort; $("#csort").onchange = e => { st.sort = e.target.value; st.page = 1; customers(); };
  $("#cq").oninput = debounce(e => { st.q = e.target.value.trim(); st.page = 1; const el = $("#cq"); customers().then(() => { const n = $("#cq"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }); });
}
acts["c-seg"] = a => { st.seg = a.dataset.v; st.page = 1; customers(); }; acts["c-cons"] = a => { st.consent = a.dataset.v; st.page = 1; customers(); };
acts["c-page"] = a => { st.page = Math.max(1, st.page + +a.dataset.v); customers(); };
acts["c-close"] = closeAll;
acts["c-tab"] = a => { st.tab = a.dataset.v; flip("ctabs", st.tab); load(); };

async function openCust(id) {
  const d = await api("/crm/customers/" + id), c = d.customer, mv = d.months.map(m => m.v);
  drawer(html`<div class="row" style="justify-content:space-between"><div><div class="eyebrow">${c.code}</div><h1>${c.name || "No name on file"}</h1><p class="muted small">${c.phone}, ${c.city}. Customer since ${fd(c.created_at)}</p></div><button class="icon" data-act="c-close">✕</button></div>
    <div class="kpis" style="margin-top:12px">${kpi("Segment", c.segment, `${c.orders_n} orders`)}${kpi("Lifetime spend", inr(c.spend_paise), c.recency_days == null ? "" : `Last order ${c.recency_days} days ago`)}${kpi("Loyalty points", num(c.loyalty_points), `Worth ${inr(c.points_value)}`)}</div>
    <div class="row" style="gap:10px;margin:6px 0 14px">${c.consent_wa ? pill("Opted in to WhatsApp", "active") : pill("Not opted in")}<button class="btn sm" data-act="c-consent" data-id="${c.id}" data-on="${c.consent_wa ? 1 : 0}">${c.consent_wa ? "Record opt-out" : "Record opt-in"}</button>${d.can_adjust ? html`<button class="btn sm" data-act="c-adjust" data-id="${c.id}">Adjust points</button>` : ""}</div>
    <div class="cols eq"><div class="card solid"><div class="ct"><h2>Spend by month</h2></div>${mv.length > 1 ? html`${raw(spark(mv, 240, 44))}` : empty("One month of history.")}${d.categories.map(x => html`<div class="row" style="justify-content:space-between;padding:4px 0"><span>${x.c}</span><span class="num small muted">${inr(x.v)}</span></div>`)}</div>
    <div class="card solid"><div class="ct"><h2>Loyalty</h2></div>${d.loyalty.length ? html`<table class="tbl"><tbody>${d.loyalty.slice(0, 6).map(x => html`<tr><td class="small">${fd(x.ts)}</td><td class="small">${x.kind}${x.note ? html`<div class="muted">${x.note}</div>` : ""}</td><td class="num r">${x.points > 0 ? "+" : ""}${x.points}</td></tr>`)}</tbody></table>` : empty("No points yet.")}</div></div>
    <div class="card solid"><div class="ct"><h2>Recent purchases</h2></div><table class="tbl"><tbody>${d.purchases.slice(0, 8).map(p => html`<tr><td>${fd(p.business_date)}</td><td>${p.store.replace("Dry Fruit House ", "")}</td><td class="muted small">${p.source === "pos" ? `POS bill · ${p.invoice_no}` : `${p.items} items, ${p.top_category || ""}` }</td><td class="num r">${inr(p.net_paise)}</td></tr>`)}</tbody></table></div>
    <div class="card solid"><div class="ct"><h2>WhatsApp orders</h2></div>${d.orders.length ? html`<table class="tbl"><tbody>${d.orders.map(o => html`<tr class="click" data-act="c-order" data-id="${o.id}"><td>${o.order_no}</td><td>${sp(o.status)}</td><td class="num r">${inr(o.total_paise)}</td></tr>`)}</tbody></table>` : empty("No WhatsApp orders.")}</div>
    ${d.messages.length ? html`<div class="card solid"><div class="ct"><h2>Latest messages</h2><button class="btn sm" data-act="c-thread" data-id="${c.id}">Open chat</button></div>${d.messages.slice(0, 4).map(m => html`<div class="small" style="padding:4px 0"><span class="muted">${fdt(m.ts)} ${m.direction === "in" ? "Customer" : "DFH"}:</span> ${m.body}</div>`)}</div>` : ""}`);
}
acts["c-cust"] = a => openCust(a.dataset.id).catch(e => toast(e.message, true));
acts["c-consent"] = async a => {
  const on = a.dataset.on === "1", src = on ? "request" : "counter";
  try { await api(`/crm/customers/${a.dataset.id}/consent`, { method: "POST", body: { action: on ? "revoke" : "grant", source: src } }); toast(on ? "Opt-out recorded" : "Opt-in recorded"); openCust(a.dataset.id); if (st.tab === "customers") customers(); } catch (e) { toast(e.message, true); }
};
acts["c-adjust"] = a => modal(html`<h1 style="font-size:22px">Adjust loyalty points</h1><p class="small muted" style="margin:6px 0 12px">Up to ${opts.adj_limit} points for marketing. Larger changes need the CEO or admin. Use a negative number to remove points.</p>
  <label class="small muted">Points</label><input class="input" id="ad-p" type="number" style="width:100%"><label class="small muted" style="display:block;margin-top:10px">Reason</label><input class="input" id="ad-r" style="width:100%">
  <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn" data-act="c-close">Cancel</button><button class="btn pri" data-act="c-adjsave" data-id="${a.dataset.id}">Save</button></div>`);
acts["c-adjsave"] = async a => {
  try { await api(`/crm/customers/${a.dataset.id}/loyalty`, { method: "POST", body: { points: +$("#ad-p").value, reason: $("#ad-r").value } }); toast("Points adjusted"); openCust(a.dataset.id); if (st.tab === "customers") customers(); } catch (e) { toast(e.message, true); }
};

// ---------- orders ----------
const OSEG = [["open", "Open"], ["new", "New"], ["confirmed", "Confirmed"], ["pay_pending", "Awaiting payment"], ["paid", "Paid"], ["packed", "Packed"], ["dispatched", "Dispatched"], ["delivered", "Delivered"], ["cancelled", "Cancelled"]];
async function orders() {
  const p = new URLSearchParams({ status: st.ostatus, q: st.oq, aged: st.aged, page: st.opage, size: 20 });
  const r = await api("/crm/orders?" + p), open = ["new", "confirmed", "pay_pending", "paid", "packed"].reduce((n, k) => n + (r.counts[k] || 0), 0);
  mount($("#cbody"), html`<div class="card solid"><div class="ct"><h2>WhatsApp orders</h2><button class="btn pri" data-act="c-neworder">New order</button></div>
    <div class="row" style="flex-wrap:wrap;gap:10px;margin-bottom:12px">${seg("ostat", "c-ost", st.ostatus, OSEG.map(([v, l]) => [v, v === "open" ? `${l} (${open})` : r.counts[v] ? `${l} (${num(r.counts[v])})` : l]))}</div>
    <div class="row" style="gap:10px;margin-bottom:12px"><input class="input" id="oq" placeholder="Order number, name or phone" value="${st.oq}" style="min-width:240px"><button class="chip ${st.aged ? "on" : ""}" data-act="c-aged">Unpaid over 24 hours</button></div>
    ${r.items.length ? html`<div class="tblwrap"><table class="tbl"><thead><tr><th>Order</th><th>Customer</th><th>Store</th><th>Status</th><th class="r">Total</th><th class="r">Age</th></tr></thead><tbody>
    ${r.items.map(o => html`<tr class="click" data-act="c-order" data-id="${o.id}"><td><b>${o.order_no}</b><div class="small muted">${o.source}</div></td><td>${o.name || "No name"}<div class="small muted">${o.phone}</div></td><td class="small">${o.store.replace("Dry Fruit House ", "")}</td>
    <td>${sp(o.status)} ${o.stuck ? pill("Stuck", "warn") : ""}</td><td class="num r">${inr(o.total_paise)}</td><td class="num r">${o.age_h < 48 ? o.age_h + " h" : Math.floor(o.age_h / 24) + " d"}</td></tr>`)}</tbody></table></div>
    <div class="row" style="justify-content:space-between;margin-top:10px"><span class="small muted">${num(r.total)} orders, page ${r.page} of ${Math.max(1, Math.ceil(r.total / r.size))}</span><span><button class="btn sm" data-act="c-opage" data-v="-1" ${r.page <= 1 ? "disabled" : ""}>Previous</button> <button class="btn sm" data-act="c-opage" data-v="1" ${r.page * r.size >= r.total ? "disabled" : ""}>Next</button></span></div>` : empty("No orders in this view.")}</div>`);
  $("#oq").oninput = debounce(e => { st.oq = e.target.value.trim(); st.opage = 1; orders().then(() => { const n = $("#oq"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }); });
}
acts["c-ost"] = a => { st.ostatus = a.dataset.v; st.opage = 1; orders(); }; acts["c-aged"] = () => { st.aged = st.aged ? 0 : 1; st.opage = 1; orders(); }; acts["c-opage"] = a => { st.opage = Math.max(1, st.opage + +a.dataset.v); orders(); };

async function openOrder(id) {
  const d = await api("/crm/orders/" + id), o = d.order, c = d.customer;
  drawer(html`<div class="row" style="justify-content:space-between"><div><div class="eyebrow">${o.order_no}</div><h1>${inr(o.total_paise)} ${sp(o.status)}</h1><p class="muted small">${c.name || "No name"}, ${c.phone}. Fulfilled from ${o.store.replace("Dry Fruit House ", "")}. Created ${fdt(o.created_at)} through ${o.source}.</p></div><button class="icon" data-act="c-close">✕</button></div>
    <div class="card solid"><div class="ct"><h2>Items</h2></div><table class="tbl"><thead><tr><th>Item</th><th class="r">Qty</th><th class="r">Price</th><th class="r">At store</th></tr></thead><tbody>${d.lines.map(l => html`<tr><td>${l.name}</td><td class="num r">${l.qty}</td><td class="num r">${inr(l.price_paise * l.qty)}</td><td class="num r">${["delivered", "dispatched", "cancelled"].includes(o.status) ? "" : html`<span class="${l.available < l.qty ? "pill warn" : ""}">${l.available}</span>`}</td></tr>`)}
    <tr><td colspan="3" class="small muted">Delivery ${o.delivery_paise ? inr(o.delivery_paise) : "free"}. ${opts.assumptions.delivery}</td><td></td></tr></tbody></table>${o.note ? html`<p class="small" style="margin-top:8px">${o.note}</p>` : ""}${o.cancel_reason ? html`<p class="small muted">Cancelled: ${o.cancel_reason}</p>` : ""}</div>
    ${d.actions.length ? html`<div class="card glass"><div class="ct"><h2>Next step</h2></div><div class="row" style="flex-wrap:wrap;gap:10px">${d.actions.filter(a => a !== "cancel").map(a => html`<button class="btn pri" data-act="c-oact" data-id="${o.id}" data-a="${a}">${a === "send_link" && o.status === "pay_pending" ? "Resend payment link" : ACT[a]}</button>`)}</div>
    ${d.actions.includes("cancel") ? html`<div class="row" style="gap:10px;margin-top:12px"><input class="input" id="co-note" placeholder="Reason, needed to cancel" style="flex:1"><button class="btn" data-act="c-oact" data-id="${o.id}" data-a="cancel">Cancel order</button></div>` : ""}
    <p class="small muted" style="margin-top:8px">Dispatch takes stock from ${o.store.replace("Dry Fruit House ", "")} using earliest expiry first. Payment is simulated until a gateway is connected.</p></div>` : ""}
    <div class="card solid"><div class="ct"><h2>Messages</h2></div>${d.messages.length ? d.messages.map(m => html`<div class="small" style="padding:5px 0"><span class="muted">${fdt(m.ts)} ${m.direction === "in" ? "Customer" : "DFH"}:</span> ${m.body}</div>`) : empty("No messages logged.")}</div>`);
}
acts["c-order"] = a => openOrder(a.dataset.id).catch(e => toast(e.message, true));
acts["c-oact"] = async a => {
  const body = { action: a.dataset.a }; if (a.dataset.a === "cancel") body.note = ($("#co-note")?.value || "").trim();
  try { const r = await api(`/crm/orders/${a.dataset.id}/action`, { method: "POST", body }); toast(r.refund_due ? "Cancelled. A refund is due" : "Done"); openOrder(a.dataset.id); if (st.tab === "orders") orders(); } catch (e) { toast(e.message, true); }
};
acts["c-neworder"] = () => {
  nl = [];
  modal(html`<h1 style="font-size:22px">New WhatsApp order</h1><p class="small muted" style="margin:6px 0 12px">Packed items only. Stock is checked at the chosen store.</p>
    <div class="row" style="gap:10px;align-items:flex-start"><div style="flex:1"><label class="small muted">Phone</label><input class="input" id="no-ph" placeholder="+91-98xxxxxxxx" style="width:100%"></div><div style="flex:1"><label class="small muted">Name (new customers)</label><input class="input" id="no-nm" style="width:100%"></div></div>
    <div class="row" style="gap:10px;margin-top:10px"><div style="flex:2"><label class="small muted">Store</label><select class="input" id="no-st" style="width:100%" ${opts.store_scope ? "disabled" : ""}>${opts.stores.map(s => html`<option value="${s.id}">${s.name.replace("Dry Fruit House ", "")}, ${s.city}</option>`)}</select></div>
    <div style="flex:1"><label class="small muted">Source</label><select class="input" id="no-src" style="width:100%"><option>whatsapp</option><option>call</option><option>website</option></select></div></div>
    <label class="small muted" style="display:block;margin-top:10px">Add item</label><input class="input" id="no-q" placeholder="Search by name or code" style="width:100%" autocomplete="off"><div id="no-res" class="small" style="margin-top:6px"></div><div id="no-lines"></div>
    <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn" data-act="c-close">Cancel</button><button class="btn pri" data-act="c-nosave">Create order</button></div>`);
  $("#no-q").oninput = debounce(async e => { const v = e.target.value.trim(); if (v.length < 2) return $("#no-res").innerHTML = "";
    const r = await api(`/skus?q=${encodeURIComponent(v)}&size=8`); $("#no-res").innerHTML = out(html`${r.items.filter(s => s.base_uom === "ea" && s.status === "active").map(s => html`<button class="chip" style="margin:0 6px 6px 0" data-act="c-noadd" data-id="${s.id}" data-n="${s.name}">${s.name}</button>`)}`); });
};
const drawLines = () => { $("#no-lines").innerHTML = out(html`${nl.map(l => html`<div class="row" style="justify-content:space-between;padding:6px 0"><span>${l.name}</span><span><input class="input" id="nl-${l.id}" type="number" min="1" max="99" value="${l.qty}" style="width:70px"> <button class="btn sm" data-act="c-norm" data-id="${l.id}">Remove</button></span></div>`)}`); };
acts["c-noadd"] = a => { const id = +a.dataset.id; if (!nl.find(l => l.id === id)) nl.push({ id, name: a.dataset.n, qty: 1 }); drawLines(); };
acts["c-norm"] = a => { nl = nl.filter(l => l.id !== +a.dataset.id); drawLines(); };
acts["c-nosave"] = async () => {
  try {
    const r = await api("/crm/orders", { method: "POST", body: { phone: $("#no-ph").value, name: $("#no-nm").value || null, source: $("#no-src").value, fulfil_location_id: +$("#no-st").value, lines: nl.map(l => ({ sku_id: l.id, qty: +$(`#nl-${l.id}`).value })) } });
    closeAll(); toast(`Created ${r.order_no} for ${inr(r.total_paise)}`); st.ostatus = "new"; st.tab = "orders"; flip("ctabs", "orders"); load();
  } catch (e) { toast(e.message, true); }
};

// ---------- inbox ----------
async function inbox() {
  const r = await api(`/crm/inbox?filter=${st.ifilter}&page=${st.ipage}&size=25`);
  mount($("#cbody"), html`<div class="cols eq"><div class="card solid"><div class="ct"><h2>Conversations</h2>${seg("ifil", "c-ifil", st.ifilter, [["unanswered", `Waiting (${r.counts.unanswered})`], ["window", "Window open"], ["all", "All"]])}</div>
    <p class="small muted" style="margin-bottom:8px">${r.counts.past_window} waiting past the 24 hour window. Those can only be answered with an approved template.</p>
    ${r.items.length ? html`<div class="tblwrap" style="max-height:520px"><table class="tbl"><tbody>${r.items.map(x => html`<tr class="click" data-act="c-thread" data-id="${x.customer_id}"><td><b>${x.name || "No name"}</b><div class="small muted">${x.body}</div></td>
    <td class="r small">${x.waiting ? pill(x.hours >= 24 ? "Window closed" : "Waiting " + Math.round(x.hours) + " h", x.hours >= 24 ? "warn" : "t") : pill("Replied", "active")}<div class="muted">${fdt(x.last_ts)}</div></td></tr>`)}</tbody></table></div>
    <div class="row" style="justify-content:space-between;margin-top:10px"><span class="small muted">${num(r.total)} conversations</span><span><button class="btn sm" data-act="c-ipage" data-v="-1" ${r.page <= 1 ? "disabled" : ""}>Previous</button> <button class="btn sm" data-act="c-ipage" data-v="1" ${r.page * r.size >= r.total ? "disabled" : ""}>Next</button></span></div>` : empty("Nothing waiting.")}</div>
    <div class="card glass" id="cthread"><p class="muted small" style="padding:14px 0">Pick a conversation to read it and reply.</p></div></div>`);
  if (st.icur) openThread(st.icur, true);
}
acts["c-ifil"] = a => { st.ifilter = a.dataset.v; st.ipage = 1; inbox(); }; acts["c-ipage"] = a => { st.ipage = Math.max(1, st.ipage + +a.dataset.v); inbox(); };
async function openThread(id, quiet) {
  st.icur = id; const d = await api("/crm/inbox/" + id), c = d.customer;
  const el = $("#cthread"); if (!el) { st.tab = "inbox"; flip("ctabs", "inbox"); closeAll(); await inbox(); return; }
  el.innerHTML = out(html`<div class="ct"><h2>${c.name || "No name"}</h2><span class="small muted">${c.phone}, ${c.city}</span></div>
    <div style="max-height:340px;overflow:auto;padding:4px 0">${d.messages.map(m => html`<div style="display:flex;justify-content:${m.direction === "in" ? "flex-start" : "flex-end"};margin:6px 0"><div class="small" style="max-width:80%;padding:8px 12px;border-radius:14px;background:${m.direction === "in" ? "color-mix(in srgb,var(--text) 8%,transparent)" : "color-mix(in srgb,var(--primary) 18%,transparent)"}">${m.body}<div class="muted" style="font-size:10.5px;margin-top:3px">${fdt(m.ts)}${m.template ? ", template" : ""}</div></div></div>`)}</div>
    <div style="margin-top:10px">${d.window_open ? html`<textarea class="input" id="rp-body" style="min-height:70px;font-family:inherit;font-size:13px" placeholder="Write a reply"></textarea><div class="row" style="justify-content:flex-end;margin-top:8px"><button class="btn pri" data-act="c-reply" data-id="${id}">Send reply</button></div>`
      : html`<p class="small muted" style="margin-bottom:8px">The 24 hour window is closed. Send an approved template.</p><div class="row" style="gap:10px"><select class="input" id="rp-tpl" style="flex:1">${d.templates.filter(t => t.category === "utility" || c.consent_wa).map(t => html`<option value="${t.code}">${t.name} (${t.category})</option>`)}</select><button class="btn pri" data-act="c-reply" data-id="${id}" data-t="1">Send template</button></div>`}</div>`);
  const box = el.querySelector("div[style*=overflow]"); if (box) box.scrollTop = box.scrollHeight;
}
acts["c-thread"] = a => { closeAll(); openThread(a.dataset.id).catch(e => toast(e.message, true)); };
acts["c-reply"] = async a => {
  const body = a.dataset.t ? { template: $("#rp-tpl").value } : { body: $("#rp-body").value };
  try { await api(`/crm/inbox/${a.dataset.id}/reply`, { method: "POST", body }); toast("Reply logged as sent"); openThread(a.dataset.id); } catch (e) { toast(e.message, true); }
};

// ---------- campaigns ----------
async function campaigns() {
  const r = await api("/crm/campaigns");
  mount($("#cbody"), html`<div class="card solid"><div class="ct"><h2>Campaigns</h2><button class="btn pri" data-act="c-newcamp">New campaign</button></div>
    <p class="small muted" style="margin-bottom:10px">Sending is simulated: the WhatsApp Business API is not connected, so nothing leaves the system. Only opted-in customers are counted, nobody gets more than one marketing message in ${opts.freq_cap_days} days, and sends go out between 9 am and 9 pm. Audiences above ${num(opts.campaign_approval)} and sends to everyone need the CEO or admin.</p>
    <div class="tblwrap"><table class="tbl"><thead><tr><th>Campaign</th><th>Status</th><th class="r">Opted in</th><th class="r">Held by cap</th><th class="r">Sent</th><th class="r">Delivered</th><th class="r">Replied</th><th class="r">Opt-outs</th><th></th></tr></thead><tbody>
    ${r.items.map(c => html`<tr><td><b>${c.name}</b><div class="small muted">${c.segment}${c.scheduled_at ? ", " + fdt(c.scheduled_at) : ""}</div></td><td>${sp(c.status)}</td><td class="num r">${num(c.audience)}</td><td class="num r">${c.status === "draft" || c.status === "scheduled" ? num(c.suppressed) : ""}</td>
    <td class="num r">${c.sent ? num(c.sent) : c.status === "draft" || c.status === "scheduled" ? html`<span class="muted">${num(c.eligible)} eligible</span>` : ""}</td><td class="num r">${c.delivery_pct == null ? "" : pct(c.delivery_pct, 0)}</td><td class="num r">${c.reply_pct == null ? "" : pct(c.reply_pct, 1)}</td>
    <td class="num r">${c.optout_pct == null ? "" : html`${pct(c.optout_pct, 1)} ${c.optout_pct >= 0.02 ? pill("High", "warn") : ""}`}</td>
    <td class="r">${c.status === "draft" || c.status === "scheduled" ? html`<button class="btn sm pri" data-act="c-camp" data-id="${c.id}" data-a="send">Send now</button> <button class="btn sm" data-act="c-camp" data-id="${c.id}" data-a="cancel">Cancel</button>` : ""}</td></tr>`)}</tbody></table></div>
    ${r.median_optout != null ? html`<p class="small muted" style="margin-top:8px">Median opt-out rate across sent campaigns is ${pct(r.median_optout, 1)}.</p>` : ""}</div>`);
  window.__camp = r;
}
acts["c-camp"] = async a => {
  try { const r = await api(`/crm/campaigns/${a.dataset.id}/action`, { method: "POST", body: { action: a.dataset.a } }); toast(a.dataset.a === "send" ? `Queued for ${num(r.sent)} customers (simulated). ${num(r.suppressed)} held by the frequency cap` : "Cancelled"); campaigns(); } catch (e) { toast(e.message, true); }
};
acts["c-newcamp"] = () => { const r = window.__camp; modal(html`<h1 style="font-size:22px">New campaign</h1><label class="small muted" style="display:block;margin-top:10px">Name</label><input class="input" id="nc-n" style="width:100%">
  <div class="row" style="gap:10px;margin-top:10px"><div style="flex:1"><label class="small muted">Segment</label><select class="input" id="nc-s" style="width:100%">${r.segments.map(s => html`<option>${s}</option>`)}</select></div><div style="flex:1"><label class="small muted">Template</label><select class="input" id="nc-t" style="width:100%">${r.templates.map(t => html`<option value="${t.code}">${t.name}</option>`)}</select></div></div>
  <label class="small muted" style="display:block;margin-top:10px">Schedule (optional, 9 am to 9 pm)</label><input class="input" id="nc-a" type="datetime-local" style="width:100%"><p class="small muted" style="margin-top:8px">Leave the time empty to save a draft you can send now.</p>
  <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn" data-act="c-close">Cancel</button><button class="btn pri" data-act="c-ncsave">Save</button></div>`); };
acts["c-ncsave"] = async () => {
  try { const r = await api("/crm/campaigns", { method: "POST", body: { name: $("#nc-n").value, segment: $("#nc-s").value, template: $("#nc-t").value, scheduled_at: $("#nc-a").value || null } }); closeAll(); toast(`Saved. ${num(r.eligible)} eligible, ${num(r.suppressed)} held by the frequency cap`); campaigns(); } catch (e) { toast(e.message, true); }
};

// ---------- quotes ----------
async function quotes() {
  const r = await api("/crm/quotes" + (st.qstatus ? "?status=" + st.qstatus : ""));
  mount($("#cbody"), html`<div class="card solid"><div class="ct"><h2>Corporate gifting quotes</h2><button class="btn pri" data-act="c-newquote">New quote</button></div>
    <div class="row" style="margin-bottom:12px">${seg("qst", "c-qst", st.qstatus, [["", "All"], ...["draft", "sent", "accepted", "lost", "expired"].map(s => [s, `${LBL[s]}${r.counts[s] ? " (" + r.counts[s] + ")" : ""}`])])}</div>
    ${r.items.length ? html`<div class="tblwrap"><table class="tbl"><thead><tr><th>Quote</th><th>Company</th><th>Status</th><th class="r">Boxes</th><th class="r">Value</th><th class="r">Valid until</th></tr></thead><tbody>${r.items.map(q => html`<tr class="click" data-act="c-quote" data-id="${q.id}"><td><b>${q.quote_no}</b></td><td>${q.company}<div class="small muted">${q.contact}${q.city ? ", " + q.city : ""}</div></td><td>${sp(q.shown)} ${q.expiring ? pill(q.days_left + " d left", "warn") : ""}</td><td class="num r">${num(q.boxes)}</td><td class="num r">${inr(q.total_paise)}</td><td class="r">${fd(q.valid_to)}</td></tr>`)}</tbody></table></div>` : empty("No quotes in this view.")}</div>`);
}
acts["c-qst"] = a => { st.qstatus = a.dataset.v; quotes(); };
async function openQuote(id) {
  const d = await api("/crm/quotes/" + id), q = d.quote;
  drawer(html`<div class="row" style="justify-content:space-between"><div><div class="eyebrow">${q.quote_no}</div><h1>${q.company} ${sp(q.shown)}</h1><p class="muted small">${q.contact}${q.phone ? ", " + q.phone : ""}. Valid until ${fd(q.valid_to)}${q.shown === "sent" ? ` (${q.days_left} days left)` : ""}.</p></div><button class="icon" data-act="c-close">✕</button></div>
    <div class="card solid"><div class="ct"><h2>Gift boxes</h2><span class="num">${inr(q.total_paise)}</span></div><table class="tbl"><thead><tr><th>Box</th><th class="r">Qty</th><th class="r">Discount</th><th class="r">Line total</th><th class="r">Can build now</th></tr></thead><tbody>
    ${d.lines.map(l => html`<tr><td>${l.name}</td><td class="num r">${num(l.qty)}</td><td class="num r">${l.discount_pct}%</td><td class="num r">${inr(l.line_total)}</td><td class="num r">${num(l.can_build)} ${l.short ? pill("Short", "warn") : ""}${l.short && l.limiter ? html`<div class="small muted">${l.limiter}</div>` : ""}</td></tr>`)}</tbody></table>
    <p class="small muted" style="margin-top:8px">Can build counts component stock in warehouses, not expired. ${opts.assumptions.discounts}.</p>${q.lost_reason ? html`<p class="small">Lost: ${q.lost_reason}</p>` : ""}</div>
    ${q.shown === "draft" || q.shown === "sent" ? html`<div class="card glass"><div class="row" style="gap:10px;flex-wrap:wrap">${q.shown === "draft" ? html`<button class="btn pri" data-act="c-qact" data-id="${q.id}" data-a="send">Mark as sent</button>` : html`<button class="btn pri" data-act="c-qact" data-id="${q.id}" data-a="accept">Customer accepted</button>`}</div>
    ${q.shown === "sent" ? html`<div class="row" style="gap:10px;margin-top:12px"><input class="input" id="ql-note" placeholder="Reason it was lost" style="flex:1"><button class="btn" data-act="c-qact" data-id="${q.id}" data-a="lost">Mark lost</button></div><p class="small muted" style="margin-top:8px">Accepting creates a corporate order in the bulk order table for a warehouse to fulfil.</p>` : ""}</div>` : ""}`);
}
acts["c-quote"] = a => openQuote(a.dataset.id).catch(e => toast(e.message, true));
acts["c-qact"] = async a => {
  try { await api(`/crm/quotes/${a.dataset.id}/action`, { method: "POST", body: { action: a.dataset.a, note: ($("#ql-note")?.value || "").trim() } }); toast(a.dataset.a === "accept" ? "Accepted. Corporate order created" : "Updated"); openQuote(a.dataset.id); quotes(); } catch (e) { toast(e.message, true); }
};
acts["c-newquote"] = () => {
  ql = [];
  modal(html`<h1 style="font-size:22px">New corporate quote</h1><div class="row" style="gap:10px;margin-top:10px;align-items:flex-start"><div style="flex:2"><label class="small muted">Company</label><input class="input" id="nq-co" style="width:100%"></div><div style="flex:1"><label class="small muted">Contact</label><input class="input" id="nq-ct" style="width:100%"></div></div>
    <div class="row" style="gap:10px;margin-top:10px"><div style="flex:1"><label class="small muted">Phone</label><input class="input" id="nq-ph" style="width:100%"></div><div style="flex:1"><label class="small muted">City</label><input class="input" id="nq-ci" style="width:100%"></div><div style="flex:1"><label class="small muted">Valid for (days)</label><input class="input" id="nq-v" type="number" value="14" style="width:100%"></div></div>
    <label class="small muted" style="display:block;margin-top:10px">Add gift box</label><input class="input" id="nq-q" placeholder="Search gift boxes" style="width:100%" autocomplete="off"><div id="nq-res" class="small" style="margin-top:6px"></div><div id="nq-lines"></div>
    <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn" data-act="c-close">Cancel</button><button class="btn pri" data-act="c-nqsave">Save draft</button></div>`);
  $("#nq-q").oninput = debounce(async e => { const v = e.target.value.trim(); if (v.length < 2) return $("#nq-res").innerHTML = "";
    const r = await api(`/skus?q=${encodeURIComponent(v)}&ptype=kit&size=8`); $("#nq-res").innerHTML = out(html`${r.items.map(s => html`<button class="chip" style="margin:0 6px 6px 0" data-act="c-nqadd" data-id="${s.id}" data-n="${s.name}">${s.name}</button>`)}`); });
};
const drawQ = () => { $("#nq-lines").innerHTML = out(html`${ql.map(l => html`<div class="row" style="justify-content:space-between;padding:6px 0"><span>${l.name}</span><span><input class="input" id="ql-${l.id}" type="number" min="10" max="5000" value="${l.qty}" style="width:90px"> <button class="btn sm" data-act="c-nqrm" data-id="${l.id}">Remove</button></span></div>`)}`); };
acts["c-nqadd"] = a => { const id = +a.dataset.id; if (!ql.find(l => l.id === id)) ql.push({ id, name: a.dataset.n, qty: 100 }); drawQ(); };
acts["c-nqrm"] = a => { ql = ql.filter(l => l.id !== +a.dataset.id); drawQ(); };
acts["c-nqsave"] = async () => {
  try { const r = await api("/crm/quotes", { method: "POST", body: { company: $("#nq-co").value, contact: $("#nq-ct").value, phone: $("#nq-ph").value || null, city: $("#nq-ci").value || null, valid_days: +$("#nq-v").value, lines: ql.map(l => ({ sku_id: l.id, qty: +$(`#ql-${l.id}`).value })) } });
    closeAll(); toast(`Saved ${r.quote_no} for ${inr(r.total_paise)}`); quotes(); } catch (e) { toast(e.message, true); }
};
