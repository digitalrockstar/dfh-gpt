"""Module 8 seed: customer purchases and loyalty, segments by rule, WhatsApp orders and chats, templates, campaigns, corporate quotes.
Own random stream (seed + 8), so no earlier module's numbers move. Sales history is aggregate (Parquet), so customer purchases are generated here
and are not linked to individual bills. Everything is synthetic."""
import math, random
from datetime import datetime, date, timedelta
from sqlalchemy import text

NOW = datetime(2026, 10, 31, 9, 0)
TODAY = NOW.date()
EARN_RS_PER_POINT = 50      # assumed: 1 point per Rs 50 spent
POINT_VALUE_PAISE = 50      # assumed: 1 point redeems for Rs 0.50
FREE_DELIVERY_ABOVE = 99900  # assumed: free above Rs 999, otherwise Rs 49
DELIVERY_FEE = 4900

TEMPLATES = [
    ("order_confirm", "Order confirmation", "utility", "Hi {name}, we have your order {order_no} for Rs {total}. We will send a payment link next."),
    ("payment_link", "Payment link", "utility", "Hi {name}, please pay Rs {total} for order {order_no} using this link: {link}"),
    ("dispatch_update", "Dispatch update", "utility", "Hi {name}, your order {order_no} is on its way. It should reach you within 2 days."),
    ("diwali_gift_promo", "Diwali gift box early access", "marketing", "Hi {name}, Diwali gift boxes are open for early orders at DFH. Reply BOX to see the range. Reply STOP to opt out."),
    ("winback_offer", "We miss you offer", "marketing", "Hi {name}, it has been a while. Here is 10 percent off your next order at DFH. Reply STOP to opt out."),
    ("new_arrival", "New arrivals", "marketing", "Hi {name}, fresh stock of Medjool dates and Californian almonds is in at DFH. Reply STOP to opt out."),
]
COMPANIES = ["Kaveri Software Pvt Ltd", "Northwind Textiles", "Sahyadri Pharma", "Lakeview Hospitals", "Orchid Realty", "Bluepeak Analytics", "Mehta and Sons Jewellers", "Greenfield Schools Trust",
             "Anantha Motors", "Saffron Hospitality", "Vedic Foods Distribution", "Urban Ladder Interiors", "Coastal Shipping Agency", "Pinnacle Insurance Brokers", "Nilgiri Tea Estates", "Sunrise Dental Group",
             "Hindustan Cables", "Rajdhani Logistics", "Maple Education", "Deccan Auto Components", "Ivory Architects", "Tulip Retail Solutions", "Zenith Legal LLP", "Banyan Ventures", "Quest Labs India",
             "Falcon Security Services", "Harbour Fresh Exports", "Lotus Wellness Clinics"]
INQ = ["What is the price of 1 kg almonds?", "Do you have sugar free dates?", "I want to order 5 boxes of the Diwali hamper, can you deliver to my office?", "Where is my order?",
       "Can I get a bulk price for 50 gift boxes?", "The cashews I bought last week tasted stale", "Do you deliver to Whitefield?", "Is the saffron pure? Please share the origin", "Need a bill with GST for my company",
       "Can I pay on delivery?", "Please send the catalogue", "Do you have walnut kernels in 250g?"]


def run(eng, seed=42, data="data"):
    rng = random.Random(seed + 8)
    with eng.begin() as c:
        cust = [dict(r._mapping) for r in c.execute(text("select id, city, created_at, first_store_id, consent_wa, name from customer order by id"))]
        stores = {}
        for r in c.execute(text("select id, city from location where type='store'")): stores.setdefault(r.city, []).append(r.id)
        cats = [r[0] for r in c.execute(text("select name from category order by id"))]
        pur, loy, stat = [], [], {}
        for u in cust:
            d0 = u["created_at"] if isinstance(u["created_at"], datetime) else datetime.fromisoformat(str(u["created_at"]))
            span = max((TODAY - d0.date()).days, 0)
            rate = min(math.exp(rng.gauss(-0.1, 0.75)), 6.0)            # orders per month
            n = 1 + (sum(rng.random() < min(rate * span / 30.0 / 12.0, 0.9) for _ in range(12)) if span > 0 else 0)
            churn = rng.random() < 0.22
            end = span if not churn else max(int(span * rng.uniform(0.1, 0.75)), 0)
            days = sorted({0} | {rng.randint(0, end) for _ in range(n - 1)}) if end > 0 else [0]
            scale = math.exp(rng.gauss(0, 0.45)); pts = 0; spend = 0
            for i, dd in enumerate(days):
                day = d0.date() + timedelta(days=dd)
                if day > TODAY: continue
                net = int(max(15000, rng.lognormvariate(math.log(65000), 0.55) * scale * (1.25 if day.month == 10 else 1.0)))
                store = u["first_store_id"] if rng.random() < 0.88 else rng.choice(stores.get(u["city"], [u["first_store_id"]]))
                pur.append(dict(customer_id=u["id"], store_id=store, business_date=day, net_paise=net, items=rng.randint(1, 7), top_category=rng.choice(cats)))
                spend += net; ep = net // (EARN_RS_PER_POINT * 100); pts += ep
                loy.append(dict(customer_id=u["id"], sale_id=None, points=ep, ts=datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(10, 20)), kind="earn", note=None))
                if i >= 2 and pts > 120 and rng.random() < 0.12:
                    r = rng.randint(50, min(pts, 400)); pts -= r
                    loy.append(dict(customer_id=u["id"], sale_id=None, points=-r, ts=datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(10, 20)), kind="redeem", note="Redeemed at the counter"))
            ds = [p for p in pur[-len(days):] if p["customer_id"] == u["id"]]
            last = max(p["business_date"] for p in ds) if ds else None
            orders = len(ds)
            if (TODAY - d0.date()).days <= 30: seg = "New"
            elif orders == 1: seg = "One-time"
            elif last and (TODAY - last).days > 75: seg = "Lapsing"
            elif orders >= 6 and last and (TODAY - last).days <= 45: seg = "Loyal"
            else: seg = "Regular"
            stat[u["id"]] = dict(i=u["id"], o=orders, s=sum(p["net_paise"] for p in ds), l=last, g=seg)
        c.execute(text("insert into customer_purchase(customer_id, store_id, business_date, net_paise, items, top_category) values (:customer_id,:store_id,:business_date,:net_paise,:items,:top_category)"), pur)
        c.execute(text("insert into loyalty_txn(customer_id, sale_id, points, ts, kind, note) values (:customer_id,:sale_id,:points,:ts,:kind,:note)"), loy)
        pts_by = {}
        for x in loy: pts_by[x["customer_id"]] = pts_by.get(x["customer_id"], 0) + x["points"]
        c.execute(text("update customer set orders_n=:o, spend_paise=:s, last_order_on=:l, segment=:g, loyalty_points=:p where id=:i"),
                  [dict(i=v["i"], o=v["o"], s=v["s"], l=v["l"], g=v["g"], p=pts_by.get(v["i"], 0)) for v in stat.values()])

        # templates
        c.execute(text("insert into wa_template(code, name, category, body, approved) values (:c,:n,:k,:b,true)"), [dict(c=a, n=b, k=k, b=t) for a, b, k, t in TEMPLATES])

        # WhatsApp orders and chats
        skus = [dict(r._mapping) for r in c.execute(text("select id, name, mrp_paise from sku where status='active' and base_uom='ea' and procurement_type in ('repacked','finished') and mrp_paise is not null order by popularity desc limit 160"))]
        pool = [u for u in cust if u["first_store_id"]]
        pool21 = [u for u in cust if u["first_store_id"] == 21]     # the demo store manager's store, so that login has something to work on
        pick = lambda: rng.choice(pool21) if rng.random() < 0.10 else rng.choice(pool)
        names = {u["id"]: u["name"] for u in cust}
        orders, lines, msgs = [], [], []
        N = 640
        for k in range(1, N + 1):
            u = pick()
            age_h = int(abs(rng.gauss(0, 1)) * 24 * 22) if k > 14 else rng.randint(26, 70)
            age_h = min(age_h, 24 * 60)
            ts = NOW - timedelta(hours=age_h)
            nl = rng.choice([1, 1, 2, 2, 3, 4, 5]); picked = rng.sample(skus, nl); sub = 0; ol = []
            for sk in picked:
                q = rng.choice([1, 1, 2, 2, 3, 4, 6]); ol.append((sk["id"], q, sk["mrp_paise"])); sub += q * sk["mrp_paise"]
            fee = 0 if sub >= FREE_DELIVERY_ABOVE else DELIVERY_FEE
            if k <= 14: st = "pay_pending"                                  # planted: unpaid for more than a day
            elif age_h > 24 * 5: st = rng.choices(["delivered", "cancelled"], [92, 8])[0]
            elif age_h > 24 * 2: st = rng.choices(["delivered", "dispatched", "packed", "cancelled"], [60, 20, 10, 10])[0]
            elif age_h > 12: st = rng.choices(["dispatched", "packed", "paid", "pay_pending", "cancelled"], [30, 25, 20, 15, 10])[0]
            else: st = rng.choices(["new", "confirmed", "pay_pending", "paid", "packed"], [28, 22, 22, 18, 10])[0]
            fl = 21 if u["first_store_id"] == 21 else rng.choice(stores.get(u["city"], [u["first_store_id"]]))
            no = f"WA-{ts:%y%m}-{k:05d}"
            link = f"https://pay.dfh-demo.example/p/{no}" if st not in ("new", "confirmed", "cancelled") or (st == "cancelled" and rng.random() < .5) else None
            orders.append(dict(order_no=no, customer_id=u["id"], status=st, source=rng.choices(["whatsapp", "call", "website"], [78, 14, 8])[0], created_at=ts, total_paise=sub + fee, payment_link=link, fulfil_location_id=fl,
                               updated_at=min(ts + timedelta(hours=rng.randint(1, 30)), NOW), paid_at=(ts + timedelta(hours=rng.randint(1, 6)) if st in ("paid", "packed", "dispatched", "delivered") else None),
                               delivered_at=(ts + timedelta(hours=rng.randint(30, 70)) if st == "delivered" else None), city=u["city"], note=None, cancel_reason=(rng.choice(["Customer changed mind", "Item out of stock", "Address outside delivery area", "Duplicate order"]) if st == "cancelled" else None), delivery_paise=fee))
            lines.append(ol)
        c.execute(text("insert into wa_order(order_no, customer_id, status, source, created_at, total_paise, payment_link, fulfil_location_id, updated_at, paid_at, delivered_at, city, note, cancel_reason, delivery_paise) values "
                       "(:order_no,:customer_id,:status,:source,:created_at,:total_paise,:payment_link,:fulfil_location_id,:updated_at,:paid_at,:delivered_at,:city,:note,:cancel_reason,:delivery_paise)"), orders)
        ids = {r.order_no: r.id for r in c.execute(text("select id, order_no from wa_order"))}
        for o, ol in zip(orders, lines):
            oid = ids[o["order_no"]]
            for sid, q, p in ol: c.execute(text("insert into wa_order_line(order_id, sku_id, qty, price_paise) values (:o,:s,:q,:p)"), dict(o=oid, s=sid, q=q, p=p))
            nm = names.get(o["customer_id"]) or "there"
            t0 = o["created_at"]
            msgs.append(dict(customer_id=o["customer_id"], direction="in", ts=t0, kind="text", body="Hi, I would like to place an order for " + ", ".join(f"{q} x item {s}" for s, q, _ in ol[:3]), template=None, status="read", order_id=oid, campaign_id=None))
            if o["status"] != "new":
                msgs.append(dict(customer_id=o["customer_id"], direction="out", ts=t0 + timedelta(minutes=rng.randint(2, 40)), kind="template", body=TEMPLATES[0][3].format(name=nm, order_no=o["order_no"], total=f"{o['total_paise'] / 100:,.0f}"), template="order_confirm", status="read", order_id=oid, campaign_id=None))
            if o["payment_link"]:
                msgs.append(dict(customer_id=o["customer_id"], direction="out", ts=t0 + timedelta(minutes=rng.randint(45, 120)), kind="template", body=TEMPLATES[1][3].format(name=nm, order_no=o["order_no"], total=f"{o['total_paise'] / 100:,.0f}", link=o["payment_link"]), template="payment_link", status="delivered", order_id=oid, campaign_id=None))
            if o["status"] in ("dispatched", "delivered"):
                msgs.append(dict(customer_id=o["customer_id"], direction="out", ts=min(t0 + timedelta(hours=rng.randint(8, 30)), NOW), kind="template", body=TEMPLATES[2][3].format(name=nm, order_no=o["order_no"]), template="dispatch_update", status="read", order_id=oid, campaign_id=None))
        # enquiries without an order; some unanswered (planted: 12 open, 4 past the 24 hour window)
        for k in range(320):
            u = pick()
            if k < 12: ts = NOW - timedelta(hours=(rng.randint(26, 60) if k < 4 else rng.randint(3, 20)))
            else: ts = NOW - timedelta(hours=rng.randint(30, 24 * 40))
            msgs.append(dict(customer_id=u["id"], direction="in", ts=ts, kind="text", body=rng.choice(INQ), template=None, status="read", order_id=None, campaign_id=None))
            if k >= 12: msgs.append(dict(customer_id=u["id"], direction="out", ts=ts + timedelta(minutes=rng.randint(3, 90)), kind="text", body="Thanks for writing to Dry Fruit House. Our team has replied with the details.", template=None, status="read", order_id=None, campaign_id=None))
        c.execute(text("insert into wa_message(customer_id, direction, ts, kind, body, template, status, order_id, campaign_id) values (:customer_id,:direction,:ts,:kind,:body,:template,:status,:order_id,:campaign_id)"), msgs)

        # campaigns. Sent counts follow the real consented audience; the last one set the 7 day frequency cap for part of that audience.
        camps = [("Diwali gift box early access", "diwali_gift_promo", "Loyal", NOW - timedelta(days=3), "sent", .93, .045, .0035),
                 ("We miss you, 10 percent off", "winback_offer", "Lapsing", NOW - timedelta(days=17), "sent", .96, .031, .0042),
                 ("Welcome to DFH", "new_arrival", "New", NOW - timedelta(days=24), "sent", .97, .052, .0021),
                 ("Medjool dates are in", "new_arrival", "Regular", NOW - timedelta(days=38), "sent", .95, .028, .0038),
                 ("Everyone: Diwali sale", "diwali_gift_promo", "All", NOW - timedelta(days=10), "sent", .94, .019, .0412),   # planted: broad blast, high opt-out
                 ("Corporate gifting reminder", "diwali_gift_promo", "Loyal", NOW + timedelta(days=2, hours=1), "scheduled", None, None, None),
                 ("Dhanteras early look", "diwali_gift_promo", "Regular", None, "draft", None, None, None)]
        for nm, tp, seg, at, stt, dl, rp, oo in camps:
            q = "select count(*) from customer where consent_wa=1" + ("" if seg == "All" else " and segment=:s")
            aud = c.execute(text(q), dict(s=seg)).scalar()
            sent = int(aud * rng.uniform(0.85, 1.0)) if stt == "sent" else 0
            c.execute(text("insert into campaign(name, template, segment, scheduled_at, status, sent, delivered, replied, audience, suppressed, optouts, created_by, approved_by, created_at) values (:n,:t,:s,:a,:st,:se,:d,:r,:au,0,:o,:cb,:ab,:ca)"),
                      dict(n=nm, t=tp, s=seg, a=at, st=stt, se=sent, d=int(sent * dl) if dl else 0, r=int(sent * rp) if rp else 0, au=aud, o=int(sent * oo) if oo else 0, cb=8, ab=(1 if stt == "sent" else None), ca=(at - timedelta(days=1) if at else NOW)))
        # frequency cap: the Diwali gift box campaign (3 days ago) reached 90 percent of consented Loyal customers, the broad blast 10 days ago reached everyone consented
        c.execute(text("update customer set last_marketing_at=:t where consent_wa=1 and segment='Loyal' and (id % 10) != 0"), dict(t=NOW - timedelta(days=3)))
        c.execute(text("update customer set last_marketing_at=:t where consent_wa=1 and last_marketing_at is null"), dict(t=NOW - timedelta(days=10)))

        # corporate gifting quotes
        kits = [dict(r._mapping) for r in c.execute(text("select id, name, mrp_paise from sku where procurement_type='kit' and status='active' and mrp_paise is not null order by mrp_paise"))]
        cities = [r[0] for r in c.execute(text("select distinct city from location where type='store' limit 12"))]
        plan = ["accepted"] * 8 + ["sent"] * 7 + ["draft"] * 3 + ["expired"] * 4 + ["lost"] * 4 + ["sent"] * 3   # the last three are planted: valid to within 3 days
        for k, stt in enumerate(plan, 1):
            created = NOW - timedelta(days=rng.randint(2, 40)) if stt != "draft" else NOW - timedelta(days=rng.randint(0, 3))
            planted = k > len(plan) - 3
            if planted: created = NOW - timedelta(days=11); valid = TODAY + timedelta(days=rng.randint(1, 3))
            elif stt == "expired": valid = TODAY - timedelta(days=rng.randint(1, 20))
            else: valid = created.date() + timedelta(days=14)
            if stt == "sent" and not planted and valid < TODAY: valid = TODAY + timedelta(days=rng.randint(4, 12))
            co = COMPANIES[(k - 1) % len(COMPANIES)]; nl = rng.choice([1, 2, 2, 3]); chosen = rng.sample(kits, nl); tot = 0; ql = []
            for kit in chosen:
                q = rng.choice([50, 100, 150, 200, 300, 500, 800]) * (2 if planted else 1)
                disc = 0.12 if q >= 600 else 0.10 if q >= 300 else 0.05 if q >= 100 else 0.0
                price = int(kit["mrp_paise"] * (1 - disc)); ql.append((kit["id"], q, price)); tot += q * price
            c.execute(text("insert into quote(quote_no, company, contact, status, created_at, valid_to, total_paise, owner_id, city, phone, decided_at, lost_reason, note) values (:n,:co,:ct,:st,:ca,:v,:t,8,:ci,:ph,:d,:lr,:no)"),
                      dict(n=f"Q-{created:%y%m}-{k:04d}", co=co, ct=f"{rng.choice(['Anita', 'Rohit', 'Sana', 'Vikram', 'Meera', 'Imran'])} {rng.choice(['Rao', 'Shah', 'Nair', 'Khan', 'Iyer', 'Gupta'])}", st=stt, ca=created, v=valid, t=tot,
                           ci=rng.choice(cities), ph=f"+00-9{rng.randint(10000000, 99999999)}", d=(created + timedelta(days=rng.randint(2, 9)) if stt in ("accepted", "lost") else None),
                           lr=(rng.choice(["Went with a local supplier", "Budget cut", "Wanted a custom box"]) if stt == "lost" else None), no=None))
            qid = c.execute(text("select id from quote where quote_no=:n"), dict(n=f"Q-{created:%y%m}-{k:04d}")).scalar()
            for sid, q, p in ql: c.execute(text("insert into quote_line(quote_id, sku_id, qty, price_paise) values (:q,:s,:n,:p)"), dict(q=qid, s=sid, n=q, p=p))
        info = dict(customers=len(cust), purchases=len(pur), loyalty_txns=len(loy), wa_orders=len(orders), wa_messages=len(msgs), campaigns=len(camps), quotes=len(plan),
                    segments={r[0]: r[1] for r in c.execute(text("select segment, count(*) from customer group by 1"))})
    return info
