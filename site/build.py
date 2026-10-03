#!/usr/bin/env python3
"""
Builds the portfolio site (site/portfolio.html) from one day of data.

  python site/build.py --day 2026-10-02 --ecb-db ecb_curve.sqlite

Reads data/<date>/ (bonds_classified, dfra_quotes, eurex_futures_daily) and the ECB curve,
computes analytics with analytics.py and embeds the result as JSON in site/template.html.
"""
import argparse, csv, datetime as dt, glob, gzip, json, math, os, re, sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import analytics as an, ecb_curve, eurex  # noqa: E402

SECTORS = ["CORP_NONFIN", "CORP_FIN", "COVERED", "SOV", "SUBSOV", "AGENCY", "SUPRA"]
STRIP_RE = r"Kupons|Kapitalanteil|\bDBRS\b|\bDBRR\b|STRIP|I/L|Inflat|\bDBRI\b|\bOBLI\b|\bBTPS?I\b|\bOATI\b|\bOATE\b"
# assumed spread duration of the credit index futures (editable on the site)
INDEX_DURATION = {"FECX": 4.5, "FEHY": 3.0, "FGBC": 6.0, "FUIG": 6.5, "FUHY": 3.2, "FUEM": 6.5, "FGGI": 6.5}


FRACTIONS = {"⅛": " 1/8", "¼": " 1/4", "⅜": " 3/8", "½": " 1/2", "⅝": " 5/8", "¾": " 3/4", "⅞": " 7/8"}
NAME_CPN = re.compile(r"^\S+\s+(\d+(?:\.\d+)?)(?:\s+(\d)/(\d+))?\s+(?:PERP|\d{1,2}/\d{1,2}/\d{2})")


def name_coupon(name):
    """Coupon from a Bloomberg-style name, e.g. 'DANBNK 4 1/2 11/09/28' -> 4.5."""
    n = str(name)
    for k, v in FRACTIONS.items():
        n = n.replace(k, v)
    m = NAME_CPN.match(n)
    if not m:
        return None
    return float(m.group(1)) + (int(m.group(2)) / int(m.group(3)) if m.group(2) else 0.0)


def clean_coupon(firds_cpn, name):
    """FIRDS has a few coupons in the wrong scale (45 instead of 4.5; 1125 instead of 1.125).
    Coupons above 20% are replaced with the coupon in the name; if there is none, the coupon is unknown."""
    if firds_cpn is None or math.isnan(firds_cpn) or firds_cpn <= 20:
        return firds_cpn
    return name_coupon(name)


def r(x, n):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def add_relative_value(rows, cols, keys, curve):
    """Appends rv (Z-spread residual vs the issuer's fitted spread curve, bp, + = cheap) and roll
    (3-month roll-down in bp of yield along the ECB AAA curve plus the issuer spread curve, + = gain)."""
    iz = cols.index("z")
    groups = [k[0] for k in keys]
    years = [k[1] for k in keys]
    z = [row[iz] if row[iz] is not None else float("nan") for row in rows]
    firm = [k[2] for k in keys]
    rv, coefs = an.rich_cheap(groups, years, z, firm)          # fit on firm quotes
    rv_all, coefs_all = an.rich_cheap(groups, years, z)        # issuers without enough firm quotes
    for i, g in enumerate(groups):
        if rv[i] is None and g not in coefs:
            rv[i] = rv_all[i]
    coefs = {**coefs_all, **coefs}
    for row, g, t, v in zip(rows, groups, years, rv):
        roll = None
        if t >= 1:
            roll = (curve.spot(t) - curve.spot(t - 0.25)) * 100
            if g in coefs and v is not None:   # spread roll only where the issuer curve covers the bond
                roll += an.spread_curve_value(coefs[g], t) - an.spread_curve_value(coefs[g], t - 0.25)
        row += [r(v, 1), r(roll, 2)]
    cols += ["rv", "roll"]


def build(day, ecb_db, bonds_csv=None, quotes_csv=None, futures_csv=None):
    ddir = os.path.join(ROOT, "data", day)
    b = pd.read_csv(bonds_csv or os.path.join(ddir, "bonds_classified.csv.gz"), index_col="isin", low_memory=False)
    q = pd.read_csv(quotes_csv or os.path.join(ddir, "dfra_quotes.csv.gz")).query("snap == 'close'").set_index("isin")
    fut = pd.read_csv(futures_csv or os.path.join(ddir, "eurex_futures_daily.csv"))
    curve = ecb_curve.Curve.load(day, "AAA", ecb_db)
    settle = an.add_business_days(dt.date.fromisoformat(day), 2)

    u = b[(b.ccy == "EUR") & b.coupon_type.isin(["fixed", "zero"]) & b.sector.isin(SECTORS)]
    u = u[~u.full_name.fillna("").str.contains(STRIP_RE, case=False)]
    u = u.join(q[["bid", "ask", "bid_qty", "ask_qty", "ask_time", "bid_time"]], rsuffix="_px", how="inner")
    u["mat"] = pd.to_datetime(u.maturity, errors="coerce").dt.date
    u = u[u.mat.notna() & (u.mat > settle + dt.timedelta(30))]

    rows, keys = [], []
    for isin, x in u.iterrows():
        cpn = 0.0 if x.coupon_type == "zero" else clean_coupon(x.coupon_fixed, x.full_name)
        if cpn is None or pd.isna(cpn):
            continue
        freq = 2 if (isin.startswith("IT") and x.sector == "SOV") else 1
        mid = (x.bid_px + x.ask_px) / 2
        a = an.analyse(mid, cpn, x.mat, settle, curve, freq)
        if not a or a.get("ytm") is None:
            continue
        firm = bool(x.bid_qty > 0 and x.ask_qty > 0)
        bench = bool(x.benchmark_500m) and not bool(x.subordinated)
        # minimum denomination (FIRDS nominal value per unit); Bunds have EUR 0.01, which rounds to 1
        unit = max(1, round(x.nominal_unit)) if pd.notna(x.nominal_unit) and 0 < x.nominal_unit <= 1e6 else 1000
        rows.append([isin, str(x.issuer), str(x.full_name)[:48], x.sector, int(bool(x.subordinated)), r(cpn, 4),
                     x.mat.isoformat(), r(x.issued_amt / 1e6, 0) if pd.notna(x.issued_amt) else None,
                     r(x.bid_px, 3), r(x.ask_px, 3), int(firm), r(a["ytm"] * 100, 3),
                     r(a["zspread"] * 1e4, 1) if a.get("zspread") is not None else None,
                     r(a["mdur"], 3), r(a["accrued"], 4), int(bench), freq, int(unit)])
        keys.append((f"{x.issuer_lei if pd.notna(x.issuer_lei) else x.issuer}|{int(bool(x.subordinated))}", a["years"], firm))
    cols = ["isin", "issuer", "name", "sector", "sub", "cpn", "mat", "amt", "bid", "ask", "firm", "ytm", "z",
            "mdur", "acc", "bench", "freq", "unit"]
    add_relative_value(rows, cols, keys, curve)

    # futures: front contract with on-book trades
    bunds = [(i, x.coupon_fixed, x.mat, (x.bid_px + x.ask_px) / 2) for i, x in u.iterrows()
             if x.sector == "SOV" and i.startswith("DE000") and x.coupon_type == "fixed"]
    futures = []
    for code, (pisin, name, typ, ccy, mult) in eurex.PRODUCTS.items():
        f = fut[(fut.code == code) & fut["last"].notna()].sort_values("contract")
        if f.empty:
            continue
        x = f.iloc[0]
        item = dict(code=code, name=name, type=typ, ccy=ccy, mult=mult, contract=x.contract, last=r(x["last"], 4),
                    low=r(x.low, 4), high=r(x.high, 4), trades=int(x.trades), lots=r(x.lots, 0),
                    block=r(x.block_lots, 0), time=x.last_time)
        if code in an.FUTURES_BASKET:
            c = an.ctd(code, x["last"], an.delivery_date(dt.date.fromisoformat(x.contract)), bunds, settle, curve)
            if c:
                item.update(dv01=r(c["dv01_contract"], 2), ctd=dict(isin=c["isin"], name=str(u.loc[c["isin"], "full_name"]),
                            cpn=r(c["coupon"], 3), mat=c["maturity"], cf=r(c["cf"], 6), basis=r(c["basis"], 4),
                            ytm=r(c["ytm"] * 100, 3)))
        if typ == "credit":
            item["dur"] = INDEX_DURATION.get(code)
        futures.append(item)

    estr = None
    for attempt in range(3):
        try:
            e = ecb_curve.estr(day)
            estr = dict(date=e[0], rate=e[1]) if e else None
            break
        except Exception as ex:  # the site still builds; the repo rate falls back to a default on the page
            print(f"€STR unavailable: {ex}", file=sys.stderr)

    return dict(asof=day, settle=settle.isoformat(), estr=estr, built=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                curve=dict(date=curve.date, b0=curve.b0, b1=curve.b1, b2=curve.b2, b3=curve.b3, t1=curve.t1, t2=curve.t2),
                cols=cols, bonds=rows, futures=futures)


# ---------------------------------------------------------------- history
HIST_BOND_COLS = ["isin", "mid", "ytm", "z", "rv"]
SHARDS = 256


def shard_of(key):
    """FNV-1a (32-bit) of the id; the page computes the same hash to find the shard."""
    h = 2166136261
    for c in key.encode():
        h = ((h ^ c) * 16777619) & 0xFFFFFFFF
    return h % SHARDS


def write_history(data, hist_dir):
    """One gzipped CSV per day (data/history/<day>.csv.gz): mid, YTM, Z and rich/cheap per bond, plus the
    futures (last price and the CTD yield). Small enough to keep in git."""
    os.makedirs(hist_dir, exist_ok=True)
    c = {k: i for i, k in enumerate(data["cols"])}
    path = os.path.join(hist_dir, f"{data['asof']}.csv.gz")
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id"] + HIST_BOND_COLS[1:])
        for b in data["bonds"]:
            w.writerow([b[c["isin"]], r((b[c["bid"]] + b[c["ask"]]) / 2, 4), b[c["ytm"]], b[c["z"]], b[c["rv"]]])
        for fu in data["futures"]:
            w.writerow([f"FUT:{fu['code']}", fu["last"], (fu.get("ctd") or {}).get("ytm"), None, None])
    return path


def build_shards(hist_dir, out_dir):
    """Time series for the site: hist/<n>.json holds every day for the ids hashing to shard n, as
    {"d": [dates], "s": {id: [[mid, ytm, z, rv] or null per date]}}. The page fetches one shard per instrument."""
    days = sorted(glob.glob(os.path.join(hist_dir, "*.csv.gz")))
    dates = [os.path.basename(p)[:10] for p in days]
    series = {}
    for i, p in enumerate(days):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                vals = [None if row[k] in ("", None) else float(row[k]) for k in HIST_BOND_COLS[1:]]
                series.setdefault(row["id"], [None] * len(days))[i] = vals
    shards = [{} for _ in range(SHARDS)]
    for k, v in series.items():
        shards[shard_of(k)][k] = v
    os.makedirs(out_dir, exist_ok=True)
    for n, sh in enumerate(shards):
        with open(os.path.join(out_dir, f"{n}.json"), "w", encoding="utf-8") as f:
            json.dump({"d": dates, "s": sh}, f, separators=(",", ":"))
    return dates


def as_document(page):
    """The template is written as page content (title, style, markup); wrap it in a standalone document for GitHub Pages."""
    m = re.match(r"\s*(<title>.*?</title>)", page, re.S)
    title, body = (m.group(1), page[m.end():]) if m else ("", page)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'{title}<style>:root{{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}}'
            'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>'
            f'{body}</body></html>')


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--ecb-db", default="ecb_curve.sqlite")
    ap.add_argument("--bonds", help="bonds_classified.csv(.gz); default data/<date>/")
    ap.add_argument("--quotes", help="dfra_quotes csv; default data/<date>/")
    ap.add_argument("--futures", help="eurex_futures_daily csv; default data/<date>/")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "elliotmalmstenkth/kthfiacc"))
    ap.add_argument("--branch", default=os.environ.get("PORTFOLIO_BRANCH", "claude/compassionate-edison-wxgg9h"))
    ap.add_argument("--positions", default="portfolio/positions.json")
    ap.add_argument("--template", default=os.path.join(ROOT, "site", "template.html"))
    ap.add_argument("--out", default=os.path.join(ROOT, "site", "portfolio.html"))
    ap.add_argument("--history", help="history folder (data/history): writes the day's file and builds hist/ shards next to --out")
    a = ap.parse_args()
    data = build(a.day, a.ecb_db, a.bonds, a.quotes, a.futures)
    if a.history:
        write_history(data, a.history)
        data["hist"] = build_shards(a.history, os.path.join(os.path.dirname(os.path.abspath(a.out)), "hist"))
        print(f"history: {len(data['hist'])} days", file=sys.stderr)
    owner, name = a.repo.split("/")
    data["repo"] = dict(owner=owner, name=name, branch=a.branch, path=a.positions)
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = open(a.template, encoding="utf-8").read().replace("/*__DATA__*/null", js)
    html = as_document(html)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{a.out}: {len(data['bonds']):,} bonds, {len(data['futures'])} futures, {len(html) / 1e6:.1f} MB",
          file=sys.stderr)
