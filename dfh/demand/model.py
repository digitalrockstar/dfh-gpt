"""Module 6: demand forecast, backtest, replenishment suggestions.

Method, kept simple so every number can be traced:
  level    trailing 56 day average per store and SKU. Stockout days and new store ramp days are removed, each day is divided by its
           festival factor, and the result is shrunk toward the same SKU in the same store tier (prior worth SHRINK days).
  festival 1 + category sensitivity x calendar intensity. Sensitivity is learned from chain history by regression, one number per category.
  forecast level x 7 x festival factor of the week. Weeks are 7 day buckets that start the day after the data ends.
  buying   packs roll up to their bulk SKU in grams and gift boxes roll up to their components. Order up to
           demand over (lead time + review) + safety stock, against on hand + on order.
Quantities are in the SKU base unit (grams for loose SKUs, units for packed SKUs). Money is paise.
"""
import json, math
from datetime import date, datetime, timedelta
import duckdb, numpy as np, pyarrow as pa
from sqlalchemy import text, insert, delete
from ..schema import metadata

T_END = date(2026, 10, 31)
NOW = datetime(2026, 10, 31, 9, 0)
HORIZON, WINDOW, BT_WEEKS, REVIEW = 8, 56, 4, 21     # REVIEW is the purchase order cycle in days
SHRINK = 14
FIT_FROM = date(2026, 7, 1)        # history starts 1 May and sales ramp over the first two months, so the fit uses July onward
CATS = ["NDF", "DAT", "EXC", "BER", "SED", "GFT", "CHO", "SPC"]
Z_A, Z_B = 1.65, 1.28
COVER_X, COVER_S = 1.9, 14.0     # deterministic seed-only inventory distribution for the legacy demo baseline
METHOD = "festival_level_v1"
D = lambda x: x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _tbl(cols, data):
    return pa.table({c: v for c, v in zip(cols, data)})


class Ctx:
    """Masters and the store x SKU assortment, as aligned numpy arrays."""
    def __init__(self, engine, data_dir):
        with engine.connect() as c:
            q = lambda s: c.execute(text(s)).fetchall()
            st = q("select id, city, region, tier, serving_wh_id, opened_on from location where type='store' order by id")
            sk = q("select s.id, ca.code, s.base_uom, s.procurement_type, s.pack_g, s.bulk_sku_id, s.price_per_kg_paise, s.mrp_paise, s.name, s.status from sku s "
                   "join product p on p.id=s.product_id join category ca on ca.id=p.category_id order by s.id")
            a = q("select store_id, sku_id from store_assortment order by store_id, sku_id")
            self.fest = [(r[0], D(r[1]), r[2], r[3], r[4]) for r in q("select name, peak_date, ramp_days, uplift, regions from festival_calendar")]
            self.bom = q("select kit_sku_id, component_sku_id, qty from kit_bom")
            self.vend = q("select v.vendor_id, v.sku_id, v.currency, v.price_minor, v.price_per, v.moq_qty, v.lead_time_days, v.effective_from, coalesce(f.rate_to_inr, 1.0) "
                          "from vendor_sku_cost v left join fx_rate f on f.currency=v.currency and f.month=v.effective_from order by v.sku_id, v.vendor_id, v.effective_from")
            self.wh = [r[0] for r in q("select id from location where type='warehouse' order by id")]
            fp = {k: c.execute(text("select params from demo_finding where code=:k"), dict(k=k)).scalar() for k in ("F3", "F7")}
        pj = lambda v: json.loads(v) if isinstance(v, str) else v
        self.f3, self.f7 = pj(fp["F3"]), pj(fp["F7"])
        self.data_dir = data_dir
        self.st_id = np.array([r[0] for r in st]); self.st_city = [r[1] for r in st]
        self.st_rg = np.array([1 if r[2] == "GCC" else 0 for r in st]); self.st_tier = [r[3] for r in st]
        self.st_wh = np.array([self.wh.index(r[4]) if r[4] in self.wh else -1 for r in st])
        self.st_open = [D(r[5]) if r[5] else None for r in st]
        self.st_new = np.array([bool(o and o > date(2026, 5, 1)) for o in self.st_open])
        self.sk_id = np.array([r[0] for r in sk]); self.sk_cat = np.array([CATS.index(r[1]) for r in sk])
        self.sk_loose = np.array([r[2] == "g" for r in sk]); self.sk_type = [r[3] for r in sk]
        self.sk_pack = [r[4] for r in sk]; self.sk_bulk = [r[5] for r in sk]; self.sk_name = [r[8] for r in sk]
        self.sk_price = np.array([(r[6] / 1000.0) if r[2] == "g" else float(r[7] or 0) for r in sk])
        self.sx = {int(i): k for k, i in enumerate(self.sk_id)}; self.tx = {int(i): k for k, i in enumerate(self.st_id)}
        self.a_st = np.array([self.tx[r[0]] for r in a]); self.a_sk = np.array([self.sx[r[1]] for r in a]); self.n = len(a)
        self.con = con = duckdb.connect()
        con.register("t_store", _tbl(["id", "rg", "tier", "mature"], [self.st_id, ["GCC" if x else "IN" for x in self.st_rg], self.st_tier,
                                                                   [(o + timedelta(days=60)) if (o and o > date(2026, 5, 1)) else date(1990, 1, 1) for o in self.st_open]]))
        con.register("t_sku", _tbl(["id", "cat", "price"], [self.sk_id, [CATS[i] for i in self.sk_cat], self.sk_price]))
        con.register("t_assort", _tbl(["store_id", "sku_id"], [self.st_id[self.a_st], self.sk_id[self.a_sk]]))
        for t in ("store", "sku"): con.execute(f"create table dim_{t} as select * from t_{t}")
        con.execute("create table assort as select * from t_assort")
        con.execute(f"create view sales as select * from read_parquet('{data_dir}/sales_daily/*/*.parquet', hive_partitioning=true)")
        con.execute(f"create table stockouts as select * from read_parquet('{data_dir}/stockout_events.parquet')")
        con.execute("create table stock_days as select distinct store_id, sku_id, unnest(generate_series(start_date::timestamp, end_date::timestamp, interval 1 day))::date d from stockouts")
        self.min_date = con.execute("select min(date) from sales").fetchone()[0]


def inten(fest, days, rg):
    """Calendar intensity per day. rg 0 is India, 1 is GCC (only festivals marked ALL apply)."""
    ok = ("ALL",) if rg else ("ALL", "IN"); out = np.zeros(len(days))
    for _, peak, ramp, up, regs in fest:
        if regs not in ok: continue
        dl = np.array([(d - peak).days for d in days], float)
        out += up * np.where(dl <= 0, np.exp(-(dl / ramp) ** 2), np.exp(-(dl / (ramp * .5)) ** 2))
    return out


def learn_beta(C, cutoff):
    """Category sensitivity to the festival calendar: daily chain sales (India) on intensity, a trend and weekday effects."""
    days = [FIT_FROM + timedelta(i) for i in range((cutoff - FIT_FROM).days + 1)]; nd = len(days); ix = {d: i for i, d in enumerate(days)}
    Y = np.zeros((len(CATS), nd))
    for d, cat, v in C.con.execute("select s.date, k.cat, sum(s.net_paise) from sales s join dim_sku k on k.id=s.sku_id join dim_store t on t.id=s.store_id where t.rg='IN' and s.date between ? and ? group by 1,2", [FIT_FROM, cutoff]).fetchall():
        Y[CATS.index(cat), ix[d]] = v
    dow = np.array([[1.0 if d.weekday() == k else 0.0 for k in range(6)] for d in days]); dow -= dow.mean(0)
    t = np.arange(nd)
    X = np.column_stack([np.ones(nd), inten(C.fest, days, 0), (t - t.mean()) / nd, dow])
    beta = np.zeros(len(CATS))
    for ci in range(len(CATS)):
        co = np.linalg.lstsq(X, Y[ci], rcond=None)[0]; beta[ci] = max(0.0, co[1] / co[0])
    return beta


def levels(C, cutoff, beta, shrink=SHRINK):
    """Deseasonalised daily level per assortment row, in base units per day."""
    days = [cutoff - timedelta(WINDOW - 1 - i) for i in range(WINDOW)]
    its = [inten(C.fest, days, rg) for rg in (0, 1)]
    rows = [(d, CATS[ci], "GCC" if rg else "IN", float(1 + beta[ci] * its[rg][i])) for rg in (0, 1) for ci in range(len(CATS)) for i, d in enumerate(days)]
    C.con.register("t_dayf", _tbl(["d", "cat", "rg", "f"], list(map(list, zip(*rows)))))
    C.con.execute("create or replace table dayf as select * from t_dayf")
    C.con.execute(f"create or replace table wdays as select d::date d from generate_series('{days[0]}'::timestamp, '{cutoff}'::timestamp, interval 1 day) t(d)")
    C.con.execute("""create or replace table lv as select a.store_id, a.sku_id, count(*) filter (where so.d is null) n_ok,
        coalesce(sum(case when so.d is null then coalesce(s.qty, 0) / f.f end), 0) q_ok
        from assort a join dim_store t on t.id=a.store_id join dim_sku k on k.id=a.sku_id join wdays w on w.d >= t.mature
        join dayf f on f.d=w.d and f.cat=k.cat and f.rg=t.rg
        left join stock_days so on so.store_id=a.store_id and so.sku_id=a.sku_id and so.d=w.d
        left join sales s on s.store_id=a.store_id and s.sku_id=a.sku_id and s.date=w.d group by 1, 2""")
    C.con.execute("create or replace table prior as select t.rg, t.tier, l.sku_id, sum(q_ok) / nullif(sum(n_ok), 0) r from lv l join dim_store t on t.id=l.store_id where n_ok >= 21 group by 1, 2, 3")
    r = C.con.execute(f"""select (case when coalesce(l.n_ok, 0) + {shrink} > 0 then (coalesce(l.q_ok, 0) + {shrink} * coalesce(p.r, 0)) / (coalesce(l.n_ok, 0) + {shrink}) else coalesce(p.r, 0) end) lvl
        from assort a join dim_store t on t.id=a.store_id left join lv l on l.store_id=a.store_id and l.sku_id=a.sku_id
        left join prior p on p.rg=t.rg and p.tier=t.tier and p.sku_id=a.sku_id order by a.store_id, a.sku_id""").fetchnumpy()["lvl"]
    return np.asarray(r, float)


def week_factors(C, beta, start, nweeks):
    """Festival factor per region group, category and week: shape (2, 8, nweeks)."""
    days = [start + timedelta(i) for i in range(7 * nweeks)]; out = np.ones((2, len(CATS), nweeks))
    for rg in (0, 1):
        wk = inten(C.fest, days, rg).reshape(nweeks, 7).mean(1)
        out[rg] = 1 + beta[:, None] * wk[None, :]
    return out


def project(C, level, fw):
    return level[:, None] * 7 * fw[C.st_rg[C.a_st], C.sk_cat[C.a_sk], :]


def _ls1(m, v, s):
    return max(0.0, float((m * v / s ** 2).sum() / (m * m / s ** 2).sum()))


def fit_disp(f, a, bins=16):
    """Error variance of actual about forecast as phi x f + r2 x f squared, fitted on binned cells."""
    f, a = f.ravel(), a.ravel(); ok = f > 0; f, a = f[ok], a[ok]
    if len(f) < 400: return 1.0, 0.1
    ch = np.array_split(np.argsort(f), bins); m = np.array([f[c].mean() for c in ch]); v = np.array([((a[c] - f[c]) ** 2).mean() for c in ch]); s = m + m * m
    co = np.linalg.lstsq(np.column_stack([m, m * m]) / s[:, None], v / s, rcond=None)[0]
    if co[0] < 0: return 0.0, _ls1(m * m, v, s)
    if co[1] < 0: return _ls1(m, v, s), 0.0
    return float(co[0]), float(co[1])


def _grp(keys, X):
    u, inv = np.unique(keys, return_inverse=True); out = np.zeros((len(u), X.shape[1])); np.add.at(out, inv, X); return out


def _wape(a, f): s = a.sum(); return float(np.abs(a - f).sum() / s) if s > 0 else None
def _bias(a, f): s = a.sum(); return float((f.sum() - s) / s) if s > 0 else None
def _rel(a, f): ok = f > 0; return float(math.sqrt(((a[ok] - f[ok]) ** 2).sum() / (f[ok] ** 2).sum())) if ok.any() else None


def backtest(C, shrink=SHRINK):
    """Forecast the last BT_WEEKS weeks from data that ends before them, score against what happened. Compared with a flat average."""
    t0 = T_END - timedelta(7 * BT_WEEKS)
    beta = learn_beta(C, t0); lv = levels(C, t0, beta, shrink); F = project(C, lv, week_factors(C, beta, t0 + timedelta(1), BT_WEEKS))
    Fn = project(C, levels(C, t0, np.zeros(len(CATS)), shrink), np.ones((2, len(CATS), BT_WEEKS)))
    key = C.st_id[C.a_st].astype(np.int64) * 10000 + C.sk_id[C.a_sk]; srt = np.argsort(key); skey = key[srt]
    A = np.zeros((C.n, BT_WEEKS)); M = np.ones((C.n, BT_WEEKS), bool)
    r = C.con.execute("select store_id::bigint * 10000 + sku_id k, (date - ?::date - 1) // 7 w, sum(qty) q from sales where date > ? and date <= ? group by 1, 2", [t0, t0, T_END]).fetchnumpy()
    pos = srt[np.searchsorted(skey, r["k"])]; A[pos, r["w"].astype(int)] = r["q"]
    r = C.con.execute("select distinct store_id::bigint * 10000 + sku_id k, (d - ?::date - 1) // 7 w from stock_days where d > ? and d <= ?", [t0, t0, T_END]).fetchnumpy()
    M[srt[np.searchsorted(skey, r["k"])], r["w"].astype(int)] = False
    M &= ~C.st_new[C.a_st][:, None]
    p = C.sk_price[C.a_sk][:, None]; a, f, fn = np.where(M, A, 0), np.where(M, F, 0), np.where(M, Fn, 0)
    lvl = {}
    wh = np.maximum(C.st_wh[C.a_st], 0); cid = {c: i for i, c in enumerate(sorted(set(C.st_city)))}; city = np.array([cid[c] for c in C.st_city])[C.a_st]
    for name, k in (("store_sku_week", np.arange(C.n)), ("warehouse_sku_week", wh * 100000 + C.sk_id[C.a_sk]), ("city_category_week", city * 10 + C.sk_cat[C.a_sk]), ("chain_category_week", C.sk_cat[C.a_sk])):
        ga, gf, gn = _grp(k, a * p), _grp(k, f * p), _grp(k, fn * p)
        lvl[name] = dict(wape=_wape(ga, gf), wape_flat=_wape(ga, gn), bias=_bias(ga, gf), rel_rmse=_rel(ga, gf), cells=int(len(ga) * BT_WEEKS))
    cats = {}
    for ci, c in enumerate(CATS):
        m = C.sk_cat[C.a_sk] == ci; ga, gf, gn = _grp(C.st_wh[C.a_st][m] * 100000 + C.sk_id[C.a_sk][m], a[m] * p[m]), _grp(C.st_wh[C.a_st][m] * 100000 + C.sk_id[C.a_sk][m], f[m] * p[m]), _grp(C.st_wh[C.a_st][m] * 100000 + C.sk_id[C.a_sk][m], fn[m] * p[m])
        cats[c] = dict(wape=_wape(ga, gf), wape_flat=_wape(ga, gn), bias=_bias(ga, gf), rel_wh=_rel(ga, gf), value=float(ga.sum()))
    disp = {}
    for ci, c in enumerate(CATS):
        for u, lo in (("loose", True), ("packed", False)):
            m = (C.sk_cat[C.a_sk] == ci) & (C.sk_loose[C.a_sk] == lo)
            disp[f"{c}_{u}"] = fit_disp(f[m], a[m]) if m.any() else (1.0, 0.1)
    # coverage of the 80 percent band, fitted on the first two weeks and checked on the last two
    h = BT_WEEKS // 2; cov = []
    for ci in range(len(CATS)):
        for lo in (True, False):
            m = (C.sk_cat[C.a_sk] == ci) & (C.sk_loose[C.a_sk] == lo)
            if not m.any(): continue
            ph, r2 = fit_disp(f[m][:, :h], a[m][:, :h]); ff, aa, pp = f[m][:, h:], a[m][:, h:], p[m]
            sg = np.sqrt(ph * ff + r2 * ff * ff); big = (ff * pp >= 20000) & M[m][:, h:]
            cov.append(((aa >= ff - 1.2816 * sg) & (aa <= ff + 1.2816 * sg))[big])
    cov = np.concatenate(cov)
    cities = np.array(C.st_city)[C.a_st]
    return dict(cutoff=str(t0), weeks=BT_WEEKS, levels=lvl, categories=cats, disp=disp, band=dict(target=0.8, coverage=float(cov.mean()), cells=int(len(cov))),
                beta=[float(x) for x in beta], excluded=dict(new_store_rows=int(C.st_new[C.a_st].sum()), stockout_cells=int((~M & ~C.st_new[C.a_st][:, None]).sum())))


def unit_costs(C):
    """Cheapest current vendor per purchasable SKU. Cost is paise per base unit, before freight and duty (landed cost comes with Module 3)."""
    by = {}
    for v, s, cur, pm, per, moq, lead, eff, fx in C.vend:
        by.setdefault((s, v), []).append((str(eff)[:10], pm * fx / (1000.0 if per == "kg" else 1.0), moq, lead))
    best = {}
    for (s, v), ser in by.items():
        ser.sort(); cost, moq, lead = ser[-1][1], ser[-1][2], ser[-1][3]
        chg = (ser[-1][1] / ser[-4][1] - 1) if len(ser) >= 4 else None
        best.setdefault(s, []).append((cost, v, moq, lead, chg))
    return {s: sorted(l) for s, l in best.items()}


def _edges(C):
    """Rows of (source sku index, target sku index, multiplier, kind). Targets are the SKUs that are bought."""
    e = []
    for k, t in enumerate(C.sk_type):
        if t in ("bulk", "finished"): e.append((k, k, 1.0, "own"))
        elif t == "repacked" and C.sk_bulk[k]: e.append((k, C.sx[C.sk_bulk[k]], float(C.sk_pack[k]), "pack"))
    for kit, comp, q in C.bom:
        c = C.sx[comp]
        if C.sk_type[c] == "finished": e.append((C.sx[kit], c, float(q), "kit"))
        elif C.sk_type[c] == "repacked" and C.sk_bulk[c]: e.append((C.sx[kit], C.sx[C.sk_bulk[c]], float(q) * C.sk_pack[c], "kit"))
    return e


def explode(G, edges, kinds=None):
    out = np.zeros_like(G)
    for s, t, m, kd in edges:
        if kinds is None or kd in kinds: out[:, t, :] += G[:, s, :] * m
    return out


def _cum(R, t):
    n, W = R.shape; C_ = np.concatenate([np.zeros((n, 1)), np.cumsum(R * 7, 1)], 1); tt = np.clip(t, 0, 7 * W); k = np.minimum((tt // 7).astype(int), W - 1); i = np.arange(n)
    return C_[i, k] + (tt - 7 * k) * R[i, k] + np.maximum(t - 7 * W, 0) * R[:, -1]


def _inv(R, y):
    n, W = R.shape; C_ = np.concatenate([np.zeros((n, 1)), np.cumsum(R * 7, 1)], 1); k = (C_[:, 1:] <= y[:, None]).sum(1); i = np.arange(n); kk = np.minimum(k, W - 1)
    rate = R[i, kk]; base = C_[i, np.minimum(k, W)]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(rate > 0, 7 * k + (y - base) / rate, 999.0)
    return np.minimum(t, 999.0)


def replenish(C, Fq, Fb, bt, seed, engine):
    S, NW = len(C.sk_id), len(C.wh)
    whi = C.st_wh[C.a_st]; ok = whi >= 0
    def dense(F):
        G = np.zeros((NW, S, F.shape[1])); np.add.at(G, (whi[ok], C.a_sk[ok]), F[ok]); return G
    Gq, Gb = dense(Fq), dense(Fb); E = _edges(C)
    Dq, Db, Dk = explode(Gq, E), explode(Gb, E), explode(Gq, E, {"kit"})
    cost = unit_costs(C); buy = [k for k in range(S) if C.sk_type[k] in ("bulk", "finished") and int(C.sk_id[k]) in cost]
    rng = np.random.default_rng(seed + 6)
    pw, ps = np.nonzero(Dq[:, buy, :].sum(2) > 0); ps = np.array(buy)[ps] if len(ps) else ps
    n = len(pw); R = Dq[pw, ps, :] / 7.0; Rb = Db[pw, ps, :] / 7.0
    best = [cost[int(C.sk_id[s])][0] for s in ps]
    uc = np.array([b[0] for b in best]); vid = np.array([b[1] for b in best]); moq = np.array([float(b[2]) for b in best]); lead = np.array([b[3] for b in best], float)
    chg = [b[4] for b in best]
    # Deterministic seed baseline only. Runtime demand APIs never read this table;
    # operational availability is supplied by Module 4 balances and Module 3 PO/GRN records.
    base_d = Rb[:, :4].mean(1); cover = np.maximum(2.0, (lead + 14) * COVER_X + rng.normal(0, COVER_S, n))
    oh = base_d * cover; has = rng.random(n) < .45; oo = np.where(has, base_d * lead * rng.uniform(.2, .9, n), 0.0); eta = np.where(has, rng.integers(2, np.maximum(3, lead.astype(int) + 1)), 0)
    with engine.connect() as conn:
        for i, (loc, sku) in enumerate(zip(C.wh[pw], C.sk_id[ps])):
            rows = conn.execute(text("select coalesce(sum(qty),0) from stock_balance where location_id=:l and sku_id=:s"), dict(l=int(loc), s=int(sku)))
            stock_value = float(rows.scalar() or 0)
            oh[i] = stock_value
            pending = conn.execute(text("select coalesce(sum(pl.qty-coalesce(gr.received,0)),0) from purchase_order p join purchase_order_line pl on pl.po_id=p.id left join (select g.po_id,gl.sku_id,sum(gl.qty_received) received from grn g join grn_line gl on gl.grn_id=g.id where g.status='posted' group by g.po_id,gl.sku_id) gr on gr.po_id=p.id and gr.sku_id=pl.sku_id where p.dest_location_id=:l and pl.sku_id=:s and p.status in ('approved','confirmed','in_transit','partial')"), dict(l=int(loc), s=int(sku)))
            oo[i] = float(pending.scalar() or 0)
            eta[i] = 0 if oo[i] <= 0 else int(max(1, min(lead[i], 7)))
    step = np.where(C.sk_loose[ps], 1000.0, 1.0)
    oh = np.floor(oh / step) * step; oo = np.floor(oo / step) * step
    # policy
    val = Dq[pw, ps, :].sum(1) * uc; a_cut = np.quantile(val, .75); z = np.where(val >= a_cut, Z_A, Z_B)
    rel = np.array([bt["categories"][CATS[C.sk_cat[s]]]["rel_wh"] or .15 for s in ps])
    need_lead, need_cycle = _cum(R, lead), _cum(R, lead + REVIEW)
    ov = np.clip((lead + REVIEW)[:, None] - 7 * np.arange(R.shape[1])[None, :], 0, 7); sigma = rel * np.sqrt(((R * ov) ** 2).sum(1)); ss = z * sigma
    ip = oh + oo; need = need_cycle + ss - ip
    t1 = _inv(R, oh); t2 = _inv(R, oh + oo); cov = np.where(t1 > eta, t2, t1)
    sug = (need > 0) & ((need >= moq) | (ip < need_lead))
    qty = np.ceil(np.maximum(need, moq) / step) * step
    urg = np.where(cov < lead, "critical", np.where(cov - lead < 7, "soon", "plan"))
    ob = [T_END + timedelta(days=int(max(0, min(365, math.floor(c - l))))) for c, l in zip(cov, lead)]
    tot, base_t, kit_t = Dq[pw, ps, :].sum(1), Db[pw, ps, :].sum(1), Dk[pw, ps, :].sum(1)
    fmt = lambda q, s: (f"{q / 1000:,.0f} kg" if q >= 1000 else f"{q:,.0f} g") if C.sk_loose[s] else f"{q:,.0f} units"
    sugg = []
    for i in np.nonzero(sug)[0]:
        s = ps[i]; lift = tot[i] / base_t[i] - 1 if base_t[i] > 0 else 0.0
        why = f"Stock lasts {cov[i]:.0f} days, lead time is {lead[i]:.0f} days." if cov[i] < 900 else f"Lead time is {lead[i]:.0f} days."
        why += f" Festival demand adds {lift * 100:.0f}% over eight weeks." if lift >= .1 else ""
        why += f" Gift boxes drive {kit_t[i] / tot[i] * 100:.0f}% of the need." if tot[i] and kit_t[i] / tot[i] >= .25 else ""
        why += f" Cheapest vendor price is up {chg[i] * 100:.0f}% in 3 months." if chg[i] and chg[i] >= .08 else ""
        sugg.append(dict(run_id=None, location_id=C.wh[pw[i]], sku_id=int(C.sk_id[s]), vendor_id=int(vid[i]), suggested_qty=int(qty[i]), approved_qty=None, unit_cost_paise=float(uc[i]), value_paise=int(qty[i] * uc[i]),
                         on_hand=int(oh[i]), on_order=int(oo[i]), need_lead=int(need_lead[i]), need_cycle=int(need_cycle[i]), cover_days=float(round(min(cov[i], 999), 1)), lead_time_days=int(lead[i]), moq=int(moq[i]),
                         urgency=str(urg[i]), order_by=ob[i], reason=why[:200], status="open", created_at=NOW, decided_by=None, decided_at=None))
    req = [dict(run_id=None, location_id=C.wh[pw[i]], sku_id=int(C.sk_id[ps[i]]), need_qty=int(tot[i]), base_qty=int(base_t[i]), kit_qty=int(kit_t[i]), need_lead=int(need_lead[i])) for i in range(n)]
    return sugg, req


def run(engine, data_dir="data", seed=42, shrink=SHRINK, log=print):
    C = Ctx(engine, data_dir); bt = backtest(C, shrink)
    beta = learn_beta(C, T_END); lv = levels(C, T_END, beta, shrink); fw = week_factors(C, beta, T_END + timedelta(1), HORIZON)
    Fq = project(C, lv, fw); Fb = project(C, lv, np.ones_like(fw))
    r = C.con.execute("select sum(s.net_paise), sum(s.qty * k.price) from sales s join dim_sku k on k.id=s.sku_id where s.date > ?", [T_END - timedelta(WINDOW)]).fetchone(); real = float(r[0] / r[1])
    ph = np.array([[bt["disp"][f"{c}_{u}"][0] for u in ("loose", "packed")] for c in CATS]); r2 = np.array([[bt["disp"][f"{c}_{u}"][1] for u in ("loose", "packed")] for c in CATS])
    u_ix = np.where(C.sk_loose[C.a_sk], 0, 1); sg = np.sqrt(ph[C.sk_cat[C.a_sk], u_ix][:, None] * Fq + r2[C.sk_cat[C.a_sk], u_ix][:, None] * Fq * Fq)
    lo, hi = np.maximum(0, Fq - 1.2816 * sg), Fq + 1.2816 * sg; price = C.sk_price[C.a_sk][:, None] * real
    base_week = ((Fb * C.sk_price[C.a_sk][:, None]).sum(0) * real).tolist()
    notes = dict(cutoff=str(T_END), beta=dict(zip(CATS, map(float, beta))), shrink_days=shrink, realisation=real, backtest=bt, chain_baseline_week_value=base_week,
                 band="80 percent interval from error variance fitted per category on the backtest weeks",
                 assumptions=["Festival shape comes from the festival calendar and is a planning assumption. Category sensitivity is learned from sales.",
                              "No festival is modelled after Diwali because the calendar has none. Christmas and New Year are open items.",
                              "On-hand comes from the Module 4 stock balance projection; open purchase commitments are derived from approved PO lines net of posted GRN receipts.",
                              "Vendor cost is before freight, duty and clearing. Landed cost comes with Module 3.",
                              "Packs roll up to bulk SKUs at 100 percent yield until repack yield is recorded.",
                              "Safety stock uses z of 1.65 for the top quarter of SKUs by value and 1.28 for the rest."])
    sugg, req = replenish(C, Fq, Fb, bt, seed, engine)
    # Legacy table may exist in databases created before the ledger cutover.
    # Drop it only after all dependent runtime reads/writes have been removed.
    if "stock_position" in metadata.tables:
        metadata.tables["stock_position"].drop(engine, checkfirst=True)
    tabs = [metadata.tables[t] for t in ("reorder_suggestion", "demand_requirement", "forecast", "forecast_run")]
    for t in tabs: t.drop(engine, checkfirst=True)          # rebuilt from the current schema, so the runner also works on older databases
    metadata.create_all(engine, tables=tabs[::-1])
    with engine.begin() as c:
        rid = c.execute(insert(metadata.tables["forecast_run"]), dict(run_at=NOW, method=METHOD, horizon_weeks=HORIZON, wape=bt["levels"]["city_category_week"]["wape"], notes=json.dumps(notes))).inserted_primary_key[0]
        ws = [T_END + timedelta(1 + 7 * w) for w in range(HORIZON)]; sid, kid = C.st_id[C.a_st], C.sk_id[C.a_sk]
        for lo_i in range(0, C.n, 4000):
            sl = slice(lo_i, lo_i + 4000)
            rows = [dict(run_id=rid, store_id=int(sid[i]), sku_id=int(kid[i]), week_start=ws[w], qty_forecast=round(float(Fq[i, w]), 2), lower=round(float(lo[i, w]), 2), upper=round(float(hi[i, w]), 2), value_paise=int(Fq[i, w] * price[i, 0]))
                    for i in range(lo_i, min(C.n, lo_i + 4000)) for w in range(HORIZON)]
            c.execute(insert(metadata.tables["forecast"]), rows)
        for x in sugg: x["run_id"] = rid
        for x in req: x["run_id"] = rid
        for name, rows in (("demand_requirement", req), ("reorder_suggestion", sugg)):
            for i in range(0, len(rows), 5000): c.execute(insert(metadata.tables[name]), rows[i:i + 5000])
    u = {k: sum(1 for x in sugg if x["urgency"] == k) for k in ("critical", "soon", "plan")}
    return dict(run=rid, forecast_rows=C.n * HORIZON, suggestions=len(sugg), urgency=u, wape_city_cat=round(bt["levels"]["city_category_week"]["wape"], 3),
                wape_store_sku=round(bt["levels"]["store_sku_week"]["wape"], 3), coverage=round(bt["band"]["coverage"], 3))    step = np.where(C.sk_loose[ps], 1000.0, 1.0)
    oh = np.floor(oh / step) * step; oo = np.floor(oo / step) * step
    # policy
    val = Dq[pw, ps, :].sum(1) * uc; a_cut = np.quantile(val, .75); z = np.where(val >= a_cut, Z_A, Z_B)
    rel = np.array([bt["categories"][CATS[C.sk_cat[s]]]["rel_wh"] or .15 for s in ps])
    need_lead, need_cycle = _cum(R, lead), _cum(R, lead + REVIEW)
    ov = np.clip((lead + REVIEW)[:, None] - 7 * np.arange(R.shape[1])[None, :], 0, 7); sigma = rel * np.sqrt(((R * ov) ** 2).sum(1)); ss = z * sigma
    ip = oh + oo; need = need_cycle + ss - ip
    t1 = _inv(R, oh); t2 = _inv(R, oh + oo); cov = np.where(t1 > eta, t2, t1)
    sug = (need > 0) & ((need >= moq) | (ip < need_lead))
    qty = np.ceil(np.maximum(need, moq) / step) * step
    urg = np.where(cov < lead, "critical", np.where(cov - lead < 7, "soon", "plan"))
    ob = [T_END + timedelta(days=int(max(0, min(365, math.floor(c - l))))) for c, l in zip(cov, lead)]
    tot, base_t, kit_t = Dq[pw, ps, :].sum(1), Db[pw, ps, :].sum(1), Dk[pw, ps, :].sum(1)
    fmt = lambda q, s: (f"{q / 1000:,.0f} kg" if q >= 1000 else f"{q:,.0f} g") if C.sk_loose[s] else f"{q:,.0f} units"
    sugg = []
    for i in np.nonzero(sug)[0]:
        s = ps[i]; lift = tot[i] / base_t[i] - 1 if base_t[i] > 0 else 0.0
        why = f"Stock lasts {cov[i]:.0f} days, lead time is {lead[i]:.0f} days." if cov[i] < 900 else f"Lead time is {lead[i]:.0f} days."
        why += f" Festival demand adds {lift * 100:.0f}% over eight weeks." if lift >= .1 else ""
        why += f" Gift boxes drive {kit_t[i] / tot[i] * 100:.0f}% of the need." if tot[i] and kit_t[i] / tot[i] >= .25 else ""
        why += f" Cheapest vendor price is up {chg[i] * 100:.0f}% in 3 months." if chg[i] and chg[i] >= .08 else ""
        sugg.append(dict(run_id=None, location_id=C.wh[pw[i]], sku_id=int(C.sk_id[s]), vendor_id=int(vid[i]), suggested_qty=int(qty[i]), approved_qty=None, unit_cost_paise=float(uc[i]), value_paise=int(qty[i] * uc[i]),
                         on_hand=int(oh[i]), on_order=int(oo[i]), need_lead=int(need_lead[i]), need_cycle=int(need_cycle[i]), cover_days=float(round(min(cov[i], 999), 1)), lead_time_days=int(lead[i]), moq=int(moq[i]),
                         urgency=str(urg[i]), order_by=ob[i], reason=why[:200], status="open", created_at=NOW, decided_by=None, decided_at=None))
    req = [dict(run_id=None, location_id=C.wh[pw[i]], sku_id=int(C.sk_id[ps[i]]), need_qty=int(tot[i]), base_qty=int(base_t[i]), kit_qty=int(kit_t[i]), need_lead=int(need_lead[i])) for i in range(n)]
    return sugg, req


def run(engine, data_dir="data", seed=42, shrink=SHRINK, log=print):
    C = Ctx(engine, data_dir); bt = backtest(C, shrink)
    beta = learn_beta(C, T_END); lv = levels(C, T_END, beta, shrink); fw = week_factors(C, beta, T_END + timedelta(1), HORIZON)
    Fq = project(C, lv, fw); Fb = project(C, lv, np.ones_like(fw))
    r = C.con.execute("select sum(s.net_paise), sum(s.qty * k.price) from sales s join dim_sku k on k.id=s.sku_id where s.date > ?", [T_END - timedelta(WINDOW)]).fetchone(); real = float(r[0] / r[1])
    ph = np.array([[bt["disp"][f"{c}_{u}"][0] for u in ("loose", "packed")] for c in CATS]); r2 = np.array([[bt["disp"][f"{c}_{u}"][1] for u in ("loose", "packed")] for c in CATS])
    u_ix = np.where(C.sk_loose[C.a_sk], 0, 1); sg = np.sqrt(ph[C.sk_cat[C.a_sk], u_ix][:, None] * Fq + r2[C.sk_cat[C.a_sk], u_ix][:, None] * Fq * Fq)
    lo, hi = np.maximum(0, Fq - 1.2816 * sg), Fq + 1.2816 * sg; price = C.sk_price[C.a_sk][:, None] * real
    base_week = ((Fb * C.sk_price[C.a_sk][:, None]).sum(0) * real).tolist()
    notes = dict(cutoff=str(T_END), beta=dict(zip(CATS, map(float, beta))), shrink_days=shrink, realisation=real, backtest=bt, chain_baseline_week_value=base_week,
                 band="80 percent interval from error variance fitted per category on the backtest weeks",
                 assumptions=["Festival shape comes from the festival calendar and is a planning assumption. Category sensitivity is learned from sales.",
                              "No festival is modelled after Diwali because the calendar has none. Christmas and New Year are open items.",
                              "On-hand comes from the Module 4 stock balance projection; open purchase commitments are derived from approved PO lines net of posted GRN receipts.",
                              "Vendor cost is before freight, duty and clearing. Landed cost comes with Module 3.",
                              "Packs roll up to bulk SKUs at 100 percent yield until repack yield is recorded.",
                              "Safety stock uses z of 1.65 for the top quarter of SKUs by value and 1.28 for the rest."])
    sugg, req = replenish(C, Fq, Fb, bt, seed, engine)
    tabs = [metadata.tables[t] for t in ("reorder_suggestion", "demand_requirement", "forecast", "forecast_run")]
    for t in tabs: t.drop(engine, checkfirst=True)          # rebuilt from the current schema, so the runner also works on older databases
    metadata.create_all(engine, tables=tabs[::-1])
    with engine.begin() as c:
        rid = c.execute(insert(metadata.tables["forecast_run"]), dict(run_at=NOW, method=METHOD, horizon_weeks=HORIZON, wape=bt["levels"]["city_category_week"]["wape"], notes=json.dumps(notes))).inserted_primary_key[0]
        ws = [T_END + timedelta(1 + 7 * w) for w in range(HORIZON)]; sid, kid = C.st_id[C.a_st], C.sk_id[C.a_sk]
        for lo_i in range(0, C.n, 4000):
            sl = slice(lo_i, lo_i + 4000)
            rows = [dict(run_id=rid, store_id=int(sid[i]), sku_id=int(kid[i]), week_start=ws[w], qty_forecast=round(float(Fq[i, w]), 2), lower=round(float(lo[i, w]), 2), upper=round(float(hi[i, w]), 2), value_paise=int(Fq[i, w] * price[i, 0]))
                    for i in range(lo_i, min(C.n, lo_i + 4000)) for w in range(HORIZON)]
            c.execute(insert(metadata.tables["forecast"]), rows)
        for x in sugg: x["run_id"] = rid
        for x in req: x["run_id"] = rid
        for name, rows in (("demand_requirement", req), ("reorder_suggestion", sugg)):
            for i in range(0, len(rows), 5000): c.execute(insert(metadata.tables[name]), rows[i:i + 5000])
    u = {k: sum(1 for x in sugg if x["urgency"] == k) for k in ("critical", "soon", "plan")}
    return dict(run=rid, forecast_rows=C.n * HORIZON, suggestions=len(sugg), urgency=u, wape_city_cat=round(bt["levels"]["city_category_week"]["wape"], 3),
                wape_store_sku=round(bt["levels"]["store_sku_week"]["wape"], 3), coverage=round(bt["band"]["coverage"], 3))
