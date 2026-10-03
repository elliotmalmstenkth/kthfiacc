#!/usr/bin/env python3
"""
Bygger portföljsidan (site/portfolio.html) från en dags data.

  python site/build.py --day 2026-10-02 --ecb-db ecb_curve.sqlite

Läser data/<dag>/ (bonds_classified, dfra_quotes, eurex_futures_daily) och ECB-kurvan,
räknar nyckeltal med analytics.py och bäddar in resultatet som JSON i site/template.html.
"""
import argparse, datetime as dt, json, math, os, sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import analytics as an, ecb_curve, eurex  # noqa: E402

SECTORS = ["CORP_NONFIN", "CORP_FIN", "COVERED", "SOV", "SUBSOV", "AGENCY", "SUPRA"]
STRIP_RE = r"Kupons|Kapitalanteil|\bDBRS\b|\bDBRR\b|STRIP|I/L|Inflat|\bDBRI\b|\bOBLI\b|\bBTPS?I\b|\bOATI\b|\bOATE\b"
# antagen spreadduration för kreditindexterminerna (ändras i sidan)
INDEX_DURATION = {"FECX": 4.5, "FEHY": 3.0, "FGBC": 6.0, "FUIG": 6.5, "FUHY": 3.2, "FUEM": 6.5, "FGGI": 6.5}


def r(x, n):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def build(day, ecb_db):
    ddir = os.path.join(ROOT, "data", day)
    b = pd.read_csv(os.path.join(ddir, "bonds_classified.csv.gz"), index_col="isin", low_memory=False)
    q = pd.read_csv(os.path.join(ddir, "dfra_quotes.csv.gz")).query("snap == 'close'").set_index("isin")
    fut = pd.read_csv(os.path.join(ddir, "eurex_futures_daily.csv"))
    curve = ecb_curve.Curve.load(day, "AAA", ecb_db)
    settle = an.add_business_days(dt.date.fromisoformat(day), 2)

    u = b[(b.ccy == "EUR") & b.coupon_type.isin(["fixed", "zero"]) & b.sector.isin(SECTORS)]
    u = u[~u.full_name.fillna("").str.contains(STRIP_RE, case=False)]
    u = u.join(q[["bid", "ask", "bid_qty", "ask_qty", "ask_time", "bid_time"]], rsuffix="_px", how="inner")
    u["mat"] = pd.to_datetime(u.maturity, errors="coerce").dt.date
    u = u[u.mat.notna() & (u.mat > settle + dt.timedelta(30))]

    rows = []
    for isin, x in u.iterrows():
        cpn = 0.0 if x.coupon_type == "zero" else x.coupon_fixed
        if pd.isna(cpn):
            continue
        freq = 2 if (isin.startswith("IT") and x.sector == "SOV") else 1
        mid = (x.bid_px + x.ask_px) / 2
        a = an.analyse(mid, cpn, x.mat, settle, curve, freq)
        if not a or a.get("ytm") is None:
            continue
        firm = bool(x.bid_qty > 0 and x.ask_qty > 0)
        bench = bool(x.benchmark_500m) and not bool(x.subordinated)
        rows.append([isin, str(x.issuer), str(x.full_name)[:48], x.sector, int(bool(x.subordinated)), r(cpn, 4),
                     x.mat.isoformat(), r(x.issued_amt / 1e6, 0) if pd.notna(x.issued_amt) else None,
                     r(x.bid_px, 3), r(x.ask_px, 3), int(firm), r(a["ytm"] * 100, 3),
                     r(a["zspread"] * 1e4, 1) if a.get("zspread") is not None else None,
                     r(a["mdur"], 3), r(a["accrued"], 4), int(bench), freq])
    cols = ["isin", "issuer", "name", "sector", "sub", "cpn", "mat", "amt", "bid", "ask", "firm", "ytm", "z",
            "mdur", "acc", "bench", "freq"]

    # terminer: närmaste kontrakt med orderboksaffärer
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

    return dict(asof=day, settle=settle.isoformat(), built=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                curve=dict(date=curve.date, b0=curve.b0, b1=curve.b1, b2=curve.b2, b3=curve.b3, t1=curve.t1, t2=curve.t2),
                cols=cols, bonds=rows, futures=futures)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--ecb-db", default="ecb_curve.sqlite")
    ap.add_argument("--template", default=os.path.join(ROOT, "site", "template.html"))
    ap.add_argument("--out", default=os.path.join(ROOT, "site", "portfolio.html"))
    a = ap.parse_args()
    data = build(a.day, a.ecb_db)
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = open(a.template, encoding="utf-8").read().replace("/*__DATA__*/null", js)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{a.out}: {len(data['bonds']):,} obligationer, {len(data['futures'])} terminer, {len(html) / 1e6:.1f} MB",
          file=sys.stderr)
