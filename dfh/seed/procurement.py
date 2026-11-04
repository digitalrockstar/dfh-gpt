"""Module 3 seed: six months of purchase orders, shipments, landed cost, GRNs, vendor invoices and payments.
Planted: a Chennai container held in customs, one Turkish vendor shipping late, one domestic vendor short-shipping while billing in full,
one Australian lot rejected for moisture, and vendor 3 (F2) invoicing 6 percent above PO price. Module 4 will own batches and stock."""
import json, random
from datetime import datetime, date, timedelta
from sqlalchemy import text, insert
from ..schema import metadata
from ..procurement_costs import DEFAULTS, Rates, FLOW, components, line_minor, weight_kg, match

NOW = datetime(2026, 10, 31, 9, 0)
TODAY = NOW.date()
MONTHS = [date(2026, m, 1) for m in range(5, 11)]
REJECT = ["Moisture above limit", "Rancid smell", "Foreign matter", "Damaged packs", "Wrong grade"]


def run(engine, seed=42, outdir="data"):
    rng = random.Random(seed + 7); T = metadata.tables
    with engine.begin() as c:
        for k, (v, n) in DEFAULTS.items():
            c.execute(insert(T["tax_assumption"]), dict(assumption_key=k, value=v, note=n))
        q = lambda s: [dict(r._mapping) for r in c.execute(text(s))]
        vend = {v["id"]: v for v in q("select id, code, type, country, currency, payment_terms_days from vendor")}
        sk = {s["id"]: s for s in q("select s.id, s.base_uom, s.pack_g, s.shelf_life_days, c.code cat from sku s join product p on p.id=s.product_id join category c on c.id=p.category_id")}
        costs = q("select vendor_id, sku_id, price_minor, incoterm, moq_qty, lead_time_days, effective_from from vendor_sku_cost order by effective_from")
        fx = {(r["currency"], str(r["month"])[:7]): r["rate_to_inr"] for r in q("select currency, month, rate_to_inr from fx_rate")}
        whs = [w for w in q("select id, code, is_port, country from location where type='warehouse'") if w["country"] == "India"]
        users = {r["username"]: r["id"] for r in q("select username, id from app_user")}
        params = c.execute(text("select params from demo_finding where code='F2'")).scalar()
        f2 = (json.loads(params) if isinstance(params, (str, bytes, bytearray)) else params)["vendor_id"]
        bid = c.execute(text("select coalesce(max(id), 0) from batch")).scalar()
    R = Rates()
    byvs, vskus = {}, {}
    for r in costs:
        byvs.setdefault((r["vendor_id"], r["sku_id"]), {})[str(r["effective_from"])[:7]] = r
        vskus.setdefault(r["vendor_id"], set()).add(r["sku_id"])
    def rate(cur, ym):
        if cur == "INR": return 1.0
        return fx.get((cur, ym)) or fx[(cur, "2026-10")]
    imports = sorted(v for v in vend if vend[v]["type"] == "import"); dom = sorted(v for v in vend if vend[v]["type"] != "import")
    late_v = next(v for v in imports if vend[v]["country"] == "TUR" and v != f2); short_v = dom[2]; rej_v = next(v for v in imports if vend[v]["country"] == "AUS")
    ports = [w for w in whs if w["is_port"]]
    pos = []
    for m0 in MONTHS:
        extra = [v for v, ms in ((f2, (7, 8)), (late_v, (6, 7, 8)), (rej_v, (7, 8, 9))) if m0.month in ms]
        for v in sorted(vend) + extra:
            vt = vend[v]; p = 1.0 if v in (f2, late_v, short_v, rej_v) else (0.9 if vt["type"] != "import" else 0.55)
            if rng.random() > p: continue
            od = m0 + timedelta(days=rng.randint(0, 27))
            if od > date(2026, 10, 29): od = date(2026, 10, 29) - timedelta(days=rng.randint(0, 6))
            ym = f"{od.year}-{od.month:02d}"; pool = sorted(vskus[v]); rng.shuffle(pool)
            lines = []
            for s in pool[:rng.randint(2, 4 if vt["type"] == "import" else 5)]:
                rows = byvs[(v, s)]; row = rows.get(ym) or rows[max(rows)]
                uom = sk[s]["base_uom"]; mq = row["moq_qty"]; qty = mq * rng.uniform(1.0, 1.6)
                qty = max(mq, int(round(qty / 1000.0) * 1000)) if uom == "g" else max(mq, int(round(qty / 10.0) * 10))
                lines.append(dict(sku_id=s, qty=int(qty), price=row["price_minor"], inc=row["incoterm"], lead=row["lead_time_days"], uom=uom))
            if not lines: continue
            imp = vt["type"] == "import"; cur = vt["currency"]; f = rate(cur, ym)
            total = sum(line_minor(l["qty"], l["price"], l["uom"]) for l in lines)
            pos.append(dict(v=v, od=od, imp=imp, cur=cur, fx=f, lines=lines, total=total, dest=rng.choice(ports if imp else whs), lead=max(l["lead"] for l in lines)))
    pos.sort(key=lambda p: (p["od"], p["v"]))
    # shipments and timeline
    for i, p in enumerate(pos, start=1):
        p["id"] = i; p["no"] = f"PO-{p['od']:%y%m}-{i:04d}"; age = (TODAY - p["od"]).days; vt = vend[p["v"]]
        p["exp"] = p["od"] + timedelta(days=p["lead"]); inr = p["total"] * p["fx"]
        p["appr"] = users["ceo"] if inr > 250_000_000 else users["procurement"]; p["ship"] = None; p["grn"] = None; p["inv"] = None
        if age <= 1 and rng.random() < .5: p["status"] = "draft"; p["appr"] = None; continue
        if age <= 3: p["status"] = "approved"; continue
        if age > 20 and rng.random() < .015: p["status"] = "cancelled"; continue
        air = p["imp"] and rng.random() < .04
        if p["imp"]:
            mode = "air" if air else "sea"; booked = p["od"] + timedelta(days=rng.randint(2, 5)); etd = p["od"] + timedelta(days=rng.randint(4, 6) if air else rng.randint(7, 16))
            if air: p["exp"] = etd + timedelta(days=5)
            eta = p["exp"]
            if p["v"] == late_v and p["od"] >= date(2026, 7, 1): d = rng.randint(9, 16) if rng.random() < .75 else rng.randint(0, 2)
            else: d = rng.choice([0, 0, 1, 2]) if rng.random() < .88 else rng.randint(3, 6) if rng.random() < .7 else rng.randint(7, 10)
            ata = eta + timedelta(days=d); cleared = ata + timedelta(days=rng.randint(2, 4)); deliv = cleared + timedelta(days=rng.randint(1, 3))
            ev = [("booked", booked), ("loaded", etd), ("in_transit", etd + timedelta(days=1)), ("at_port", ata), ("customs", ata + timedelta(days=1)), ("cleared", cleared), ("delivered", deliv)]
        else:
            mode = "road"; eta = p["exp"]; ld = max(p["od"] + timedelta(days=1), eta - timedelta(days=rng.randint(1, 3))); etd = ld
            ata = eta + timedelta(days=rng.choice([0, 0, 0, 1, 2])); cleared = None; deliv = ata
            ev = [("booked", p["od"] + timedelta(days=1)), ("loaded", ld), ("in_transit", ld), ("delivered", deliv)]
        p["ship"] = dict(mode=mode, etd=etd, eta=eta, ev=ev, port=p["dest"]["id"] if p["imp"] else None, air=air)
    # planted customs hold: an October arrival at Chennai that never cleared
    cand = [p for p in pos if p["ship"] and p["imp"] and p["ship"]["mode"] == "sea" and date(2026, 10, 12) <= p["ship"]["ev"][3][1] <= date(2026, 10, 20) and p["v"] != late_v]
    hold = cand[len(cand) // 2]; hold["dest"] = next(w for w in ports if w["code"].endswith("MAA-1")); hold["ship"]["port"] = hold["dest"]["id"]; hold["ship"]["ev"] = hold["ship"]["ev"][:5]; hold["hold"] = True
    ship_n = grn_n = inv_n = pay_n = 0
    rows = {k: [] for k in ("purchase_order", "purchase_order_line", "shipment", "shipment_event", "landed_cost", "batch", "grn", "grn_line", "vendor_invoice", "vendor_payment", "debit_note")}
    sid = gid = ivid = 0
    for p in pos:
        vt = vend[p["v"]]; ym = f"{p['od'].year}-{p['od'].month:02d}"; sh = p["ship"]
        evs = [(s, datetime.combine(d, datetime.min.time()) + timedelta(hours=10 + k)) for k, (s, d) in enumerate(sh["ev"])] if sh else []
        evs = [e for e in evs if e[1] <= NOW]
        for l in p["lines"]: l["val"] = line_minor(l["qty"], l["price"], l["uom"]); l["kg"] = weight_kg(l["qty"], l["uom"], sk[l["sku_id"]]["pack_g"])
        if sh:
            last = evs[-1][0]; p["status"] = "received" if False else ("confirmed" if last in ("booked", "loaded") else "in_transit")
        for l in p["lines"]:
            rows["purchase_order_line"].append(dict(po_id=p["id"], sku_id=l["sku_id"], qty=l["qty"], price_minor=l["price"]))
        done = sh and evs and evs[-1][0] == "delivered"
        if done:
            sid += 1; sh["id"] = sid
        if sh and evs:
            s_id = len(rows["shipment"]) + 1; sh["id"] = s_id; last = evs[-1][0]
            ata = next((d for s, d in sh["ev"] if s == "at_port" or (s == "delivered" and not p["imp"])), None)
            ata = ata if ata and datetime.combine(ata, datetime.min.time()) <= NOW else None
            cn = f"MSKU{rng.randint(1000000, 9999999)}" if sh["mode"] == "sea" else None
            bl = f"{'AWB' if sh['mode'] == 'air' else 'LR' if sh['mode'] == 'road' else 'BL'}{rng.randint(10**7, 10**8 - 1)}"
            rows["shipment"].append(dict(id=s_id, ship_no=f"SH-{p['od']:%y%m}-{s_id:04d}", po_id=p["id"], mode=sh["mode"], container_no=cn, port_id=sh["port"], status=last, etd=sh["etd"], eta=sh["eta"], ata=ata, bl_no=bl))
            for s, ts in evs:
                rows["shipment_event"].append(dict(shipment_id=s_id, status=s, ts=ts, note="Held for customs examination. Query raised with the clearing agent." if p.get("hold") and s == "customs" else None))
            # landed cost actuals once cleared, or delivered for road
            if last in ("cleared", "delivered"):
                gv = p["total"] * p["fx"]; kg = sum(l["kg"] for l in p["lines"]); tot = {}
                for l in p["lines"]:
                    for k, v in components(l["val"] * p["fx"], l["kg"], l["inc"], sk[l["sku_id"]]["cat"], vt["country"], sh["mode"], not p["imp"], R).items(): tot[k] = tot.get(k, 0) + v
                for k, v in tot.items():
                    cur = p["cur"] if k == "freight" else "INR"
                    rows["landed_cost"].append(dict(shipment_id=s_id, component=k, amount_paise=int(v * rng.uniform(.94, 1.08)), currency=cur, fx_rate=p["fx"] if cur != "INR" else 1.0))
            # GRN two days after delivery
            if last == "delivered" and (TODAY - sh["ev"][-1][1]).days >= 2:
                gid += 1; wh = p["dest"]; gno = f"GRN-{wh['code'][7:].replace('-', '')}-{gid:05d}"; rec = datetime.combine(sh["ev"][-1][1], datetime.min.time()) + timedelta(hours=14)
                rows["grn"].append(dict(id=gid, grn_no=gno, po_id=p["id"], shipment_id=s_id, location_id=wh["id"], received_at=rec, status="posted", received_by=users["warehouse"] if wh["id"] == 1 else None))
                gl = []; allv = sum(l["val"] for l in p["lines"]) or 1; landed = {k: 0 for k in ()}
                ext = {}
                for r_ in rows["landed_cost"]:
                    if r_["shipment_id"] == s_id: ext[r_["component"]] = r_["amount_paise"]
                tv = sum(l["val"] * p["fx"] for l in p["lines"]) or 1; tk = sum(l["kg"] for l in p["lines"]) or 1
                short = p["v"] == short_v and p["od"] >= date(2026, 9, 1); rej_lot = p["v"] == rej_v and date(2026, 7, 15) <= p["od"] <= date(2026, 10, 10)
                part = (not short) and (TODAY - p["od"]).days < 25 and rng.random() < .07
                allfull = True
                for l in p["lines"]:
                    uom = l["uom"]; recv = l["qty"]
                    if short: recv = int(l["qty"] * rng.uniform(.88, .92))
                    elif part: recv = int(l["qty"] * rng.uniform(.55, .7))
                    elif rng.random() < .06: recv = int(l["qty"] * rng.uniform(.97, .995))
                    recv = int(round(recv / 100.0) * 100) if uom == "g" else int(recv)
                    rj = 0
                    if rej_lot: rj = recv * 0.07
                    elif rng.random() < .07: rj = recv * rng.uniform(.01, .04)
                    rj = int(round(rj / 100.0) * 100) if uom == "g" else int(rj)
                    acc = recv - rj; allfull = allfull and recv >= l["qty"] * .95
                    extra = sum(v * (l["kg"] / tk if k in ("freight", "port", "inland") else l["val"] * p["fx"] / tv) for k, v in ext.items())
                    unit = int(round((l["val"] * p["fx"] + extra) / l["qty"]))
                    bat = None
                    if acc > 0 and (TODAY - sh["ev"][-1][1]).days <= 60:
                        bid += 1; bat = bid; shelf = sk[l["sku_id"]]["shelf_life_days"] or 270; mfg = rec.date() - timedelta(days=rng.randint(20, 60)); exp = mfg + timedelta(days=shelf)
                        if exp < rec.date() + timedelta(days=30): exp = rec.date() + timedelta(days=int(shelf * .8))
                        rows["batch"].append(dict(id=bid, batch_no=f"{gno}-{l['sku_id']}", sku_id=l["sku_id"], vendor_id=p["v"], received_on=rec.date(), mfg_date=mfg, expiry_date=exp, mrp_paise=None, unit_cost_paise=unit, source_doc_type="grn", source_doc_id=gno))
                    reason = (REJECT[0] if rej_lot else rng.choice(REJECT)) if rj else None
                    rows["grn_line"].append(dict(grn_id=gid, sku_id=l["sku_id"], batch_id=bat, qty_received=recv, qty_accepted=acc, qty_rejected=rj, reject_reason=reason, unit_cost_paise=unit))
                    l["recv"], l["acc"], l["rj"] = recv, acc, rj
                    if rj:
                        amt = line_minor(rj, l["price"], uom) * p["fx"]
                        rows["debit_note"].append(dict(vendor_id=p["v"], grn_id=gid, amount_paise=int(amt), reason=f"Rejected on receipt: {reason}", created_at=rec + timedelta(hours=3)))
                p["status"] = "received" if allfull else "partial"; p["grn"] = gid; p["gdate"] = rec.date()
            elif last == "delivered": p["status"] = "in_transit"
        elif sh is None and p["status"] not in ("draft", "approved", "cancelled"): p["status"] = "confirmed"
        if sh and not evs: p["status"] = "confirmed"
        # invoice
        inv_date = None
        if p["grn"] and not p["imp"]: inv_date = p["gdate"]
        elif p["imp"] and sh and evs and any(s == "loaded" for s, _ in evs): inv_date = sh["etd"]
        if inv_date and p["status"] not in ("cancelled",):
            ivid += 1; ordered = p["total"]
            accepted = sum(line_minor(l.get("acc", 0), l["price"], l["uom"]) for l in p["lines"]) if p["grn"] else 0
            received = sum(line_minor(l.get("recv", l["qty"]), l["price"], l["uom"]) for l in p["lines"]) if p["grn"] else ordered
            amt = received
            if p["v"] == short_v and p["od"] >= date(2026, 9, 1): amt = ordered
            elif p["v"] == f2 and p["od"] >= date(2026, 7, 20) and rng.random() < .8: amt = int(ordered * 1.06)
            elif p["grn"] and rng.random() < .02: amt = ordered
            due = inv_date + timedelta(days=vt["payment_terms_days"])
            debit = int(sum(d["amount_paise"] for d in rows["debit_note"] if d["grn_id"] == p["grn"]) / p["fx"]) if p["grn"] else 0
            mt = match(amt, ordered, accepted, debit, R.tol); st = "pending"; note = None
            if mt["state"] == "matched":
                if due <= TODAY - timedelta(days=3): st = "paid"
                elif (TODAY - inv_date).days > 25: st = "released"
            elif mt["state"] == "mismatch" and p["v"] != f2 and (TODAY - inv_date).days > 75:
                st = "released"; note = "Released after the vendor agreed a credit note."
                # settled mismatches are paid late
            if st == "mismatch": st = "pending"
            rows["vendor_invoice"].append(dict(_state=mt["state"], id=ivid, vendor_id=p["v"], po_id=p["id"], inv_no=f"{vt['code']}/{inv_date:%y}/{ivid:04d}", inv_date=inv_date, amount_minor=int(amt), currency=p["cur"], status=st, due_date=due, note=note))
            p["inv"] = (ivid, st, mt["state"], inv_date)
            if st == "released" and due <= TODAY - timedelta(days=3): rows["vendor_invoice"][-1]["status"] = st = "paid"
            if st == "paid":
                pay_n += 1; pd = min(due - timedelta(days=rng.randint(0, 5)), TODAY)
                rows["vendor_payment"].append(dict(vendor_id=p["v"], invoice_id=ivid, paid_on=pd, amount_minor=int(amt), mode="bank" if not p["imp"] else "tt", fx_rate=rate(p["cur"], f"{pd.year}-{pd.month:02d}")))
    held = [r for r in rows["vendor_invoice"] if r["status"] == "pending" and r["_state"] == "mismatch" and r["inv_date"] < TODAY - timedelta(days=12)][:1]
    for r in held: r["status"] = "held"; r["note"] = "Held until the vendor confirms the quantity shipped."
    for p in pos:
        rows["purchase_order"].append(dict(id=p["id"], po_no=p["no"], vendor_id=p["v"], dest_location_id=p["dest"]["id"], status=p["status"], currency=p["cur"], fx_rate=p["fx"], order_date=p["od"], expected_date=p["exp"], approved_by=p["appr"], total_minor=p["total"], notes=None))
    for r in rows["vendor_invoice"]: r.pop("_state", None)
    with engine.begin() as c:
        for name in ("purchase_order", "purchase_order_line", "shipment", "shipment_event", "landed_cost", "batch", "grn", "grn_line", "vendor_invoice", "vendor_payment", "debit_note"):
            r_ = rows[name]
            for i in range(0, len(r_), 2000): c.execute(insert(T[name]), r_[i:i + 2000])
    st = {}
    for p in pos: st[p["status"]] = st.get(p["status"], 0) + 1
    return dict(pos=len(pos), by_status=st, shipments=len(rows["shipment"]), grns=len(rows["grn"]), invoices=len(rows["vendor_invoice"]), payments=len(rows["vendor_payment"]), debit_notes=len(rows["debit_note"]), batches=len(rows["batch"]), hold=hold["no"], late_vendor=late_v, short_vendor=short_v, reject_vendor=rej_v)
