"""Masters: entities, locations, vendors and price history, customers, HR, users, calendars, demo findings."""
import hashlib, math, random
from datetime import date, datetime, timedelta
from . import names as N
from .catalog import build_catalog

NOW = datetime(2026, 10, 31, 9, 0)
# code, name, state, country, region, stores, lat, lng, serving warehouse city
CITIES = [
 ("BLR","Bengaluru","Karnataka","India","South",70,12.97,77.59,"BLR"),("BOM","Mumbai","Maharashtra","India","West",45,19.07,72.88,"BOM"),
 ("DEL","Delhi NCR","Delhi","India","North",55,28.61,77.21,"DEL"),("HYD","Hyderabad","Telangana","India","South",40,17.38,78.48,"HYD"),
 ("MAA","Chennai","Tamil Nadu","India","South",38,13.08,80.27,"MAA"),("PNQ","Pune","Maharashtra","India","West",30,18.52,73.86,"PNQ"),
 ("CCU","Kolkata","West Bengal","India","East",25,22.57,88.36,"CCU"),("AMD","Ahmedabad","Gujarat","India","West",20,23.02,72.57,"AMD"),
 ("JAI","Jaipur","Rajasthan","India","North",14,26.91,75.79,"JAI"),("COK","Kochi","Kerala","India","South",16,9.93,76.26,"COK"),
 ("LKO","Lucknow","Uttar Pradesh","India","North",12,26.85,80.95,"LKO"),("IXC","Chandigarh","Chandigarh","India","North",10,30.73,76.78,"IXC"),
 ("IDR","Indore","Madhya Pradesh","India","West",10,22.72,75.86,"NAG"),("CJB","Coimbatore","Tamil Nadu","India","South",8,11.0,76.96,"MAA"),
 ("MYQ","Mysuru","Karnataka","India","South",8,12.30,76.65,"BLR"),("NAG","Nagpur","Maharashtra","India","West",8,21.15,79.09,"NAG"),
 ("STV","Surat","Gujarat","India","West",10,21.17,72.83,"AMD"),("VTZ","Visakhapatnam","Andhra Pradesh","India","South",8,17.69,83.22,"VTZ"),
 ("BBI","Bhubaneswar","Odisha","India","East",6,20.30,85.82,"CCU"),("GAU","Guwahati","Assam","India","East",5,26.14,91.74,"CCU"),
 ("DXB","Dubai","UAE","UAE","GCC",22,25.20,55.27,"DXB"),("AUH","Abu Dhabi","UAE","UAE","GCC",8,24.45,54.38,"DXB"),
 ("SHJ","Sharjah","UAE","UAE","GCC",6,25.35,55.40,"DXB"),("DOH","Doha","Qatar","Qatar","GCC",8,25.29,51.53,"DOH"),
 ("RUH","Riyadh","Saudi Arabia","Saudi Arabia","GCC",8,24.71,46.68,"RUH"),("MCT","Muscat","Oman","Oman","GCC",5,23.59,58.41,"DXB"),
 ("KWI","Kuwait City","Kuwait","Kuwait","GCC",5,29.38,47.99,"DOH")]
WH = [("BLR",2),("BOM",2),("DEL",1),("HYD",1),("MAA",1),("PNQ",1),("CCU",1),("AMD",1),("JAI",1),("COK",1),("LKO",1),
      ("NAG",1),("IXC",1),("VTZ",1),("DXB",2),("DOH",1),("RUH",1)]
PORTS = {"BOM","MAA","COK","CCU","DXB"}
ABBR = {"Karnataka":"KA","Maharashtra":"MH","Delhi":"DL","Telangana":"TS","Tamil Nadu":"TN","West Bengal":"WB","Gujarat":"GJ",
        "Rajasthan":"RJ","Kerala":"KL","Uttar Pradesh":"UP","Chandigarh":"CH","Madhya Pradesh":"MP","Andhra Pradesh":"AP",
        "Odisha":"OD","Assam":"AS","UAE":"AE","Qatar":"QA","Saudi Arabia":"SA","Oman":"OM","Kuwait":"KW"}
CCY = {"India":"INR","UAE":"AED","Qatar":"QAR","Saudi Arabia":"SAR","Oman":"OMR","Kuwait":"KWD"}
FX0 = {"USD":85.5,"AED":23.3,"QAR":23.5,"SAR":22.8,"OMR":222.0,"KWD":279.0,"INR":1.0}
MONTHS = [date(2026, m, 1) for m in range(4, 11)]
# import vendor countries: count, categories
IMPORT = {"USA":(6,"NDF,BER,EXC"),"AUS":(3,"NDF"),"VNM":(4,"NDF,EXC"),"AFG":(3,"NDF,BER,EXC"),"IRN":(3,"NDF,DAT,EXC,SPC"),
          "TUR":(4,"NDF,BER,EXC"),"CHL":(2,"NDF,BER,SED"),"PER":(1,"NDF,SED,EXC"),"UZB":(2,"NDF,BER,SED"),"SAU":(2,"DAT,EXC"),
          "JOR":(2,"DAT,EXC"),"ARE":(3,"DAT,EXC"),"TUN":(1,"DAT"),"EGY":(1,"DAT"),"CHN":(1,"BER,SED,SPC")}
DOMESTIC = [("Mumbai Vashi",5,"NDF,DAT,BER,SED,EXC,CHO,SPC",True),("Delhi Khari Baoli",5,"NDF,DAT,BER,SED,EXC,SPC",True),
            ("Kollam",3,"NDF,SPC",False),("Srinagar",3,"NDF,SPC",False),("Gujarat",2,"NDF,SED,SPC,CHO",False),
            ("Karnataka",2,"NDF,SPC,CHO",False),("Tamil Nadu",2,"NDF,SED",False)]
CAT_PRE = {"NDF":"NDF","DAT":"DAT","EXC":"EXC","BER":"BER","SED":"SED","CHO":"CHO","SPC":"SPC"}


def pw(p):
    return hashlib.pbkdf2_hmac("sha256", p.encode(), b"dfh-demo-salt", 20000).hex()


def build_masters(rng, seed=42):
    T = {}
    cats, products, skus, bom, tax = build_catalog(rng)
    T["category"] = cats; T["product"] = products; T["kit_bom"] = bom
    T["sku"] = [{k: v for k, v in s.items() if k not in ("cat", "origins", "raw_cost", "price_per")} for s in skus]
    T["tax_rate"] = tax
    T["tax_assumption"] = [
        dict(assumption_key="gst_rates", value="Flat 5% dry fruits, nuts, seeds, dates, spices; 18% chocolates and gift boxes",
             note="ASSUMED for the demo. Rates are configurable per HSN and must be confirmed with the company's CA."),
        dict(assumption_key="gsp_einvoice", value="Not integrated", note="GSP, e-invoice and e-way bill API integration is on hold. Data is prepared for it."),
        dict(assumption_key="return_policy_days", value="10", note="Returns accepted within 10 days, only for defective or damaged items."),
        dict(assumption_key="price_basis", value="Prices are tax inclusive MRP", note="Net sales in history are tax inclusive and in INR equivalent."),
        dict(assumption_key="synthetic_data", value="true", note="All volumes, prices, costs and vendors are synthetic.")]
    T["price_list"] = [dict(code="IN-STD", name="India standard", scope_type="country", scope_value="India"),
                       dict(code="GCC-STD", name="GCC standard", scope_type="region", scope_value="GCC")]
    T["role"] = [dict(code=c, name=n) for c, n in [("ceo", "CEO"), ("finance", "Finance"), ("procurement", "Procurement"),
                 ("warehouse", "Warehouse"), ("store_manager", "Store manager"), ("cashier", "Cashier"), ("hr", "HR"),
                 ("marketing", "Marketing"), ("admin", "Admin")]]
    T["wastage_reason"] = [dict(code=c, name=n, is_controllable=ctl, cap_pct=cap) for c, n, ctl, cap in [
        ("EXPIRY", "Expired", True, None), ("RANCID", "Rancid or moisture", True, None), ("PEST", "Pest damage", True, None),
        ("DAMAGE", "Damaged in handling", True, None), ("SPILL", "Spillage", True, None), ("WEIGHT", "Weighing variance", True, 0.5),
        ("SAMPLE", "Tasting samples", True, 0.3), ("THEFT", "Suspected theft", True, None), ("QREJECT", "Quality reject", False, None),
        ("CUSTRET", "Customer return, defective or damaged", False, None)]]
    T["festival_calendar"] = [dict(name=n, start_date=s, end_date=e, peak_date=p, ramp_days=r, uplift=u, regions=g, assumed=True) for n, s, e, p, r, u, g in [
        ("Eid al-Adha", date(2026,5,24), date(2026,5,30), date(2026,5,27), 5, .35, "ALL"),
        ("Raksha Bandhan", date(2026,8,22), date(2026,8,28), date(2026,8,28), 6, .5, "IN"),
        ("Janmashtami", date(2026,9,1), date(2026,9,4), date(2026,9,4), 3, .15, "IN"),
        ("Ganesh Chaturthi", date(2026,9,10), date(2026,9,20), date(2026,9,14), 5, .25, "IN"),
        ("Navratri", date(2026,10,11), date(2026,10,19), date(2026,10,15), 4, .30, "IN"),
        ("Dussehra", date(2026,10,18), date(2026,10,21), date(2026,10,20), 3, .20, "IN"),
        ("Diwali run-up", date(2026,10,20), date(2026,11,10), date(2026,11,8), 12, 1.0, "IN")]]

    # entities and locations
    ents, ent_id = [], {}
    for c in CITIES:
        key = c[2] if c[3] == "India" else c[3]
        if key not in ent_id:
            ent_id[key] = len(ents) + 1
            ents.append(dict(id=ent_id[key], code="DFH-" + ABBR[key], name=f"Dry Fruit House {key} (demo)", country=c[3],
                             currency=CCY[c[3]], state=key if c[3] == "India" else None,
                             gstin=f"DEMO-GSTIN-{ABBR[key]}" if c[3] == "India" else None, is_demo=True))
    T["legal_entity"] = ents
    city = {c[0]: c for c in CITIES}
    locs, lid, wh_by_city = [], 0, {}
    for cc, n in WH:
        c = city[cc]
        for k in range(n):
            lid += 1
            wh_by_city.setdefault(cc, []).append(lid)
            locs.append(dict(id=lid, code=f"DFH-WH-{cc}-{k + 1}", name=f"{c[1]} Warehouse {k + 1}", type="warehouse", city=c[1],
                             state=c[2], country=c[3], region=c[4], entity_id=ent_id[c[2] if c[3] == "India" else c[3]],
                             is_port=(cc in PORTS and k == 0), active=True, lat=c[6], lng=c[7], last_sync_at=NOW - timedelta(minutes=rng.randint(3, 60)),
                             area_sqft=rng.randint(8000, 30000), opened_on=date(rng.randint(2014, 2023), rng.randint(1, 12), 1)))
    stores, bins, devices, series = [], [], [], []
    for w in [l for l in locs]:
        for z in "ABCD":
            for r in range(1, 6):
                for b in range(1, 4):
                    bins.append(dict(warehouse_id=w["id"], zone=z, rack=f"R{r}", bin_code=f"{w['code']}-{z}{r}-{b}"))
    new_store_idx = set(rng.sample(range(500), 25))
    si = 0
    for c in CITIES:
        loc_names = N.LOCALITIES.get(c[0], [])
        for k in range(c[5]):
            lid += 1
            tier = rng.choices(["A", "B", "C"], [.2, .5, .3])[0]
            fmt = {"A": "Flagship", "B": "High Street", "C": "Mall Kiosk"}[tier]
            loc_nm = loc_names[k % len(loc_names)] if loc_names else f"{c[1]} Area {k + 1}"
            if loc_names and k >= len(loc_names):
                loc_nm += f" {k // len(loc_names) + 1}"
            whs = wh_by_city[c[8]]
            opened = (date(2026, 5, 1) + timedelta(days=rng.randint(10, 150))) if si in new_store_idx else date(rng.randint(2012, 2025), rng.randint(1, 12), 1)
            stores.append(dict(id=lid, code=f"DFH-{c[0]}-{k + 1:03d}", name=f"Dry Fruit House {loc_nm}", type="store", city=c[1], state=c[2],
                               country=c[3], region=c[4], entity_id=ent_id[c[2] if c[3] == "India" else c[3]], serving_wh_id=whs[k % len(whs)],
                               format=fmt, tier=tier, area_sqft={"A": rng.randint(900, 1500), "B": rng.randint(450, 800), "C": rng.randint(120, 250)}[tier],
                               opened_on=opened, is_port=False, active=True, lat=c[6] + rng.uniform(-.1, .1), lng=c[7] + rng.uniform(-.1, .1),
                               last_sync_at=NOW - timedelta(minutes=rng.randint(5, 180))))
            si += 1
    # flagship HQ-area store is the first Bengaluru store
    stores[0]["name"] = "Dry Fruit House HSR Layout"; stores[0]["tier"] = "A"; stores[0]["format"] = "Flagship"
    stores[0]["opened_on"] = date(2018, 1, 1)
    for s in stores:
        if s["opened_on"] > date(2026, 4, 30):
            s["_new"] = True
    lid += 1
    locs += stores
    locs.append(dict(id=lid, code="DFH-TRANSIT", name="In transit", type="transit", city="N/A", state="N/A", country="India", region="N/A",
                     entity_id=ent_id["Karnataka"], is_port=False, active=True))
    lid += 1
    hq_id = lid
    locs.append(dict(id=hq_id, code="DFH-HQ", name="Head Office, HSR Layout", type="office", city="Bengaluru", state="Karnataka",
                     country="India", region="South", entity_id=ent_id["Karnataka"], is_port=False, active=True, lat=12.91, lng=77.64))
    # planted: three stores not syncing for ~2 days
    stale = []
    for cc in ("MAA", "DEL", "AMD"):
        s = rng.choice([x for x in stores if x["code"].split("-")[1] == cc])
        s["last_sync_at"] = NOW - timedelta(days=2, hours=rng.randint(0, 6)); stale.append(s["id"])
    T["location"] = [{k: v for k, v in l.items() if not k.startswith("_")} for l in locs]
    T["location_bin"] = bins
    # devices
    did = 0
    for s in stores:
        for n in range(2 if rng.random() < .25 else 1):
            did += 1
            devices.append(dict(id=did, store_id=s["id"], code=f"{s['code']}-D{n + 1}", kind="pos-tablet", last_seen_at=s["last_sync_at"],
                                app_version="0.1.0", active=True))
            series.append(dict(device_id=did, fy="2026-27", prefix=f"{s['code'].replace('DFH-', '')}-D{n + 1}/26-27/", next_no=1, reserved_to=1000))
    T["device"], T["invoice_series"] = devices, series
    # fx
    T["fx_rate"] = [dict(currency=cur, month=m, rate_to_inr=round(r0 * (1 + .003 * i + rng.uniform(-.006, .006)) if cur != "INR" else 1.0, 4), assumed=True)
                    for cur, r0 in FX0.items() for i, m in enumerate(MONTHS)]
    fx = {(r["currency"], r["month"]): r["rate_to_inr"] for r in T["fx_rate"]}

    # vendors
    vendors, vid = [], 0
    pairs = [(a, b) for a in N.VENDOR_PRE for b in N.VENDOR_SUF]; rng.shuffle(pairs)
    for cty, (n, cats_) in IMPORT.items():
        for _ in range(n):
            vid += 1; a, b = pairs.pop()
            vendors.append(dict(id=vid, code=f"V{vid:03d}", name=f"{a} {b} ({cty})", type="import", country=cty, city="N/A",
                                currency="AED" if cty == "ARE" else "USD", categories=cats_, payment_terms_days=rng.choice([30, 45, 60]),
                                gstin=None, active=True, is_trader=False))
    for place, n, cats_, trader in DOMESTIC:
        for _ in range(n):
            vid += 1; a, b = pairs.pop()
            vendors.append(dict(id=vid, code=f"V{vid:03d}", name=f"{a} {b} ({place})", type="domestic", country="IND", city=place, currency="INR",
                                categories=cats_, payment_terms_days=rng.choice([7, 15, 30]), gstin=f"DEMO-VGSTIN-{vid:03d}", active=True, is_trader=trader))
    T["vendor"] = vendors

    # vendor price book: monthly history Apr to Oct
    prod_by_id = {p["id"]: p for p in products}
    cost_rows = []
    purchasable = [s for s in skus if s["procurement_type"] in ("bulk", "finished")]
    month_idx = {}
    for s in purchasable:
        cat = s["cat"]
        elig = [v for v in vendors if cat in v["categories"].split(",") and (v["is_trader"] or v["country"] in s["origins"].split(","))]
        imp = [v for v in elig if v["type"] == "import"]; dom = [v for v in elig if v["type"] == "domestic"]
        k = rng.choice([2, 3, 3, 4, 5])
        chosen = rng.sample(imp, min(len(imp), max(1, k - 1))) if imp else []
        chosen += rng.sample(dom, min(len(dom), k - len(chosen)))
        if len(chosen) < 2:
            chosen += [v for v in elig if v not in chosen][:2 - len(chosen)]
        walk = [1.0]
        for _ in MONTHS[1:]:
            walk.append(walk[-1] * (1 + rng.gauss(.003, .025)))
        month_idx[s["id"]] = walk
        for v in chosen:
            vf = rng.uniform(1.04, 1.12) if v["is_trader"] else (rng.uniform(.90, 1.02) if v["type"] == "import" else rng.uniform(.95, 1.05))
            lead = rng.randint(35, 60) if v["type"] == "import" else (rng.randint(2, 7) if v["is_trader"] else rng.randint(5, 15))
            moq = (rng.randint(1000, 5000) * 1000 if s["procurement_type"] == "bulk" else rng.randint(500, 3000)) if v["type"] == "import" else \
                  (rng.randint(50, 200) * 1000 if s["procurement_type"] == "bulk" else rng.randint(50, 300)) if v["is_trader"] else \
                  (rng.randint(100, 500) * 1000 if s["procurement_type"] == "bulk" else rng.randint(100, 500))
            inc = rng.choice(["FOB", "CIF"]) if v["type"] == "import" else "EXW"
            for mi, m in enumerate(MONTHS):
                inr = s["raw_cost"] * vf * walk[mi] * (1 + rng.uniform(-.015, .015))
                if v["currency"] == "INR":
                    minor = int(round(inr * 100))
                else:
                    minor = int(round(inr / fx[(v["currency"], m)] * 100))
                nxt = MONTHS[mi + 1] - timedelta(days=1) if mi + 1 < len(MONTHS) else None
                cost_rows.append(dict(vendor_id=v["id"], sku_id=s["id"], currency=v["currency"], price_minor=minor, price_per=s["price_per"],
                                      incoterm=inc, moq_qty=moq, lead_time_days=lead, effective_from=m, effective_to=nxt))
    # planted F2: one USA vendor creeps +6/12/18% Aug to Oct on 15 SKUs while others stay flat
    usa = [v for v in vendors if v["country"] == "USA"]
    cnt = {v["id"]: len({r["sku_id"] for r in cost_rows if r["vendor_id"] == v["id"]}) for v in usa}
    creep_v = max(cnt, key=cnt.get)
    creep_skus = sorted({r["sku_id"] for r in cost_rows if r["vendor_id"] == creep_v})[:15]
    for r in cost_rows:
        if r["vendor_id"] == creep_v and r["sku_id"] in creep_skus and r["effective_from"] >= date(2026, 8, 1):
            r["price_minor"] = int(r["price_minor"] * {8: 1.06, 9: 1.12, 10: 1.18}[r["effective_from"].month])
    T["vendor_sku_cost"] = cost_rows

    # customers
    weights = [c[5] for c in CITIES]
    store_by_city = {}
    for s in stores:
        store_by_city.setdefault(s["code"].split("-")[1], []).append(s)
    cust = []
    for i in range(1, 25001):
        c = rng.choices(CITIES, weights)[0]; fs = rng.choice(store_by_city[c[0]])
        wa = rng.random() < .65
        cust.append(dict(id=i, code=f"C{i:06d}", phone=f"+00-{i:08d}", name=(f"{rng.choice(N.FIRST)} {rng.choice(N.LAST)}" if rng.random() < .7 else None),
                         city=c[1], created_at=datetime(2026, 5, 1) + timedelta(days=rng.randint(0, 183)), first_store_id=fs["id"], consent_wa=wa,
                         consent_at=(NOW - timedelta(days=rng.randint(1, 150))) if wa else None, loyalty_points=rng.randint(0, 800),
                         segment=rng.choice(["New", "Regular", "Loyal", "Lapsing", None])))
    T["customer"] = cust

    # HR
    depts = ["Executive", "Finance", "Procurement", "Supply Chain", "Retail Operations", "IT and Analytics", "Marketing", "HR", "Admin"]
    T["department"] = [dict(id=i + 1, name=n) for i, n in enumerate(depts)]
    desg = [("CEO", 9), ("Head of Department", 7), ("Regional Manager", 6), ("Manager", 5), ("Executive", 3), ("Store Manager", 5),
            ("Sales Associate", 2), ("Cashier", 2), ("Warehouse Manager", 5), ("Warehouse Associate", 2)]
    T["designation"] = [dict(id=i + 1, title=t, level=l) for i, (t, l) in enumerate(desg)]
    D = {n: i + 1 for i, n in enumerate(depts)}; G = {t: i + 1 for i, (t, _) in enumerate(desg)}
    emps = []

    def emp(dept, des, loc, mgr, email=False):
        i = len(emps) + 1
        f, l = rng.choice(N.FIRST), rng.choice(N.LAST)
        emps.append(dict(id=i, emp_code=f"E{i:05d}", name=f"{f} {l}", department_id=D[dept], designation_id=G[des], location_id=loc, manager_id=mgr,
                         join_date=date(rng.randint(2015, 2026), rng.randint(1, 12), rng.randint(1, 28)), birth_date=date(rng.randint(1970, 2003), rng.randint(1, 12), rng.randint(1, 28)),
                         status="active", email=f"{f}.{l}.{i}@dfh-demo.invalid".lower() if email else None))
        return i
    ceo = emp("Executive", "CEO", hq_id, None, True)
    heads = {d: emp(d, "Head of Department", hq_id, ceo, True) for d in depts[1:]}
    for d in depts[1:]:
        for _ in range(rng.randint(3, 8)):
            emp(d, rng.choice(["Manager", "Executive"]), hq_id, heads[d], True)
    regional = {r: emp("Retail Operations", "Regional Manager", hq_id, heads["Retail Operations"], True) for r in ("South", "West", "North", "East", "GCC")}
    mgr_of = {}
    for w in [l for l in locs if l["type"] == "warehouse"]:
        m = emp("Supply Chain", "Warehouse Manager", w["id"], heads["Supply Chain"])
        for _ in range(rng.randint(8, 14)):
            emp("Supply Chain", "Warehouse Associate", w["id"], m)
    for s in stores:
        m = emp("Retail Operations", "Store Manager", s["id"], regional[s["region"]]); mgr_of[s["id"]] = m
        for j in range({"A": 5, "B": 3, "C": 2}[s["tier"]]):
            emp("Retail Operations", "Cashier" if j == 0 else "Sales Associate", s["id"], m)
    T["employee"] = emps
    T["shift"] = [dict(code="MORNING", start_time="09:00", end_time="17:00"), dict(code="EVENING", start_time="13:00", end_time="21:30"), dict(code="GENERAL", start_time="09:30", end_time="18:30")]
    T["leave_type"] = [dict(code="CL", name="Casual leave", annual_quota=12), dict(code="SL", name="Sick leave", annual_quota=8), dict(code="EL", name="Earned leave", annual_quota=15), dict(code="WFH", name="Work from home", annual_quota=24)]
    # users
    emp_of = lambda dept, des: next(e["id"] for e in emps if e["department_id"] == D[dept] and e["designation_id"] == G[des])
    wh1 = next(l for l in locs if l["type"] == "warehouse"); st1 = stores[0]
    T["app_user"] = [dict(username=u, name=n, role_id=r, scope_type=sc, scope_id=sid_, password_hash=pw("demo1234"), active=True, employee_id=None) for u, n, r, sc, sid_ in [
        ("ceo", "CEO (demo)", 1, "all", None), ("finance", "Finance (demo)", 2, "all", None), ("procurement", "Procurement (demo)", 3, "all", None),
        ("warehouse", "Warehouse (demo)", 4, "warehouse", wh1["id"]), ("storemgr", "Store manager (demo)", 5, "store", st1["id"]),
        ("cashier", "Cashier (demo)", 6, "store", st1["id"]), ("hr", "HR (demo)", 7, "all", None), ("marketing", "Marketing (demo)", 8, "all", None), ("admin", "Admin (demo)", 9, "all", None)]]

    # Platform demo configuration and operational queue.
    T["approval_rule"] = [
        dict(doc_type="purchase_order", min_amount_paise=5_000_000, max_amount_paise=25_000_000, approver_role_id=2),
        dict(doc_type="purchase_order", min_amount_paise=25_000_001, max_amount_paise=None, approver_role_id=1),
        dict(doc_type="stock_adjustment", min_amount_paise=100_000, max_amount_paise=500_000, approver_role_id=4),
        dict(doc_type="campaign", min_amount_paise=500_000, max_amount_paise=None, approver_role_id=1),
        dict(doc_type="loyalty_adjustment", min_amount_paise=50_000, max_amount_paise=None, approver_role_id=2),
    ]
    T["approval_request"] = [
        dict(doc_type="purchase_order", doc_id="PO-DFH-2026-1187", requested_by=3, status="pending", approver_id=None,
             amount_paise=8_450_000, created_at=NOW - timedelta(hours=19), decided_at=None, note="Seasonal replenishment order."),
        dict(doc_type="stock_adjustment", doc_id="ADJ-WH-HYD-047", requested_by=5, status="pending", approver_id=None,
             amount_paise=245_000, created_at=NOW - timedelta(hours=7), decided_at=None, note="Cycle-count variance at store."),
        dict(doc_type="campaign", doc_id="CMP-DIWALI-2026", requested_by=8, status="pending", approver_id=None,
             amount_paise=650_000, created_at=NOW - timedelta(hours=3), decided_at=None, note="Diwali customer campaign budget."),
    ]
    T["import_job"] = [
        dict(kind="sku_master", filename="sku_master_oct_2026.csv", status="completed", rows_ok=1248, rows_err=0,
             created_at=NOW - timedelta(days=2), report=dict(message="All rows validated and applied.", synthetic=True)),
        dict(kind="fx_rates", filename="monthly_fx_rates_oct_2026.csv", status="completed", rows_ok=42, rows_err=0,
             created_at=NOW - timedelta(days=1), report=dict(message="Rates loaded for active reporting currencies.", synthetic=True)),
        dict(kind="store_stock", filename="store_stock_adjustment_hyd.csv", status="failed", rows_ok=461, rows_err=7,
             created_at=NOW - timedelta(hours=5), report=dict(message="Seven rows rejected: unknown SKU or negative quantity.", synthetic=True)),
    ]

    # planted findings
    pune = [s["id"] for s in stores if s["code"].split("-")[1] == "PNQ"]
    hot = [s["id"] for s in skus if any(k in s["name"] for k in ("Almonds California Regular", "Almonds California Premium", "Cashews Whole W320", "Cashews Whole W240"))]
    hyd_wh = next(l["id"] for l in locs if l["type"] == "warehouse" and l["code"].split("-")[2] == "HYD")
    mum = [s for s in stores if s["code"].split("-")[1] == "BOM" and s["tier"] == "B"]
    pine = [s["id"] for s in skus if s["name"].startswith("Dried Pineapple")]
    kiosks = [s["id"] for s in stores if s["tier"] == "C" and s["code"].split("-")[1] in ("DEL", "HYD", "MAA")][:30]
    diwali_kits = [s["id"] for s in skus if s["cat"] == "GFT" and "Diwali" in s["name"]][:3]
    T["demo_finding"] = [dict(code=c, module=m, title=t, description=d, params=p) for c, m, t, d, p in [
        ("F1", "inventory", "Warehouse with high shrink", "Hyderabad warehouse shrink well above peers (seeded in the inventory module).", dict(warehouse_id=hyd_wh)),
        ("F2", "procurement", "Vendor price creep", "One USA vendor rose 6, 12 and 18 percent over Aug to Oct on 15 SKUs while other vendors stayed flat.", dict(vendor_id=creep_v, sku_ids=creep_skus)),
        ("F3", "demand", "Chronic stockouts in Pune", "Pune stores run out of top almond and cashew SKUs on about 40 percent of days from mid August.", dict(store_ids=pune, sku_ids=hot, start="2026-08-15")),
        ("F4", "pos", "Weekend cash shortage", "One Mumbai high street store shows repeated weekend cash shortage in day close.", dict(store_id=mum[0]["id"] if mum else None)),
        ("F5", "platform", "Stores not syncing", "Three stores last synced about two days ago.", dict(store_ids=stale)),
        ("F6", "wastage", "Dried pineapple expiry loss", "Dried pineapple expires unsold in small kiosks.", dict(sku_ids=pine, store_ids=kiosks)),
        ("F7", "inventory", "Gift box component shortage", "A component of the Diwali gift boxes runs short ahead of the festival.", dict(kit_sku_ids=diwali_kits)),
        ("F8", "finance", "Input credit mismatch", "September input credit in the books differs from the portal statement for Karnataka.", dict(period="2026-09", entity_id=ent_id["Karnataka"]))]]
    return T, dict(creep_vendor=creep_v, stale=stale)
