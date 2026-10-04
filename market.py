#!/usr/bin/env python3
"""
Market monitor and risk-factor history for the portfolio site (all free sources, end of day):

  ECB YC        AAA and all-euro-area Svensson curves (ecb_curve.sqlite): spot 2Y/5Y/10Y/30Y
  ECB EST       €STR (overnight unsecured)
  ECB EXR       EUR/USD reference rate
  NY Fed        SOFR
  FRED (ICE)    ICE BofA option-adjusted spreads: Euro High Yield, US Corporate (IG), US High Yield;
                CBOE VIX close

  python market.py --day 2026-10-02 --ecb-db ecb_curve.sqlite      # prints the latest values

Each source is fetched independently; one that fails is left out and the page shows it as unavailable.
The same series drive the historical-simulation VaR on the site (rates: ECB AAA spot changes per
futures bucket; credit: Euro HY OAS changes scaled by each position's spread).
"""
import argparse, csv, datetime as dt, io, json, re, sqlite3, sys, urllib.request

import ecb_curve

UA = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}   # FRED stalls on unfamiliar user agents
TENORS = [2, 5, 10, 30]
FRED = {  # id: (name, group, unit)
    "BAMLHE00EHYIOAS": ("Euro High Yield OAS (ICE BofA)", "credit", "%"),
    "BAMLC0A0CM": ("US Corporate IG OAS (ICE BofA)", "credit", "%"),
    "BAMLH0A0HYM2": ("US High Yield OAS (ICE BofA)", "credit", "%"),
    "VIXCLS": ("VIX (CBOE, close)", "macro", "index"),
    "DGS2": ("US Treasury 2Y", "rates", "%"),
    "DGS5": ("US Treasury 5Y", "rates", "%"),
    "DGS10": ("US Treasury 10Y", "rates", "%"),
    "DGS30": ("US Treasury 30Y", "rates", "%"),
}


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # network error: retry
            last = e
    raise RuntimeError(f"{url}: {last}")


def curve_series(db, day, n=520):
    """Spot yields (%, cont. comp.) at 2/5/10/30Y for the AAA curve and 10Y for all euro-area govts."""
    con = sqlite3.connect(db)
    out = {}
    for curve in ("AAA", "ALL"):
        rows = con.execute("""SELECT date, curve, beta0, beta1, beta2, beta3, tau1, tau2 FROM params
                              WHERE curve=? AND date<=? ORDER BY date DESC LIMIT ?""", (curve, day, n)).fetchall()[::-1]
        cs = [ecb_curve.Curve(*r) for r in rows]
        for t in (TENORS if curve == "AAA" else [10]):
            out[f"{curve}{t}"] = ([c.date for c in cs], [round(c.spot(t), 4) for c in cs])
    return out


def ecb_series(key, n=300, end=None):
    url = f"https://data-api.ecb.europa.eu/service/data/{key}?format=csvdata&lastNObservations={n}" + (f"&endPeriod={end}" if end else "")
    rows = [r for r in csv.DictReader(io.StringIO(_get(url))) if r["OBS_VALUE"]]
    return [r["TIME_PERIOD"] for r in rows], [float(r["OBS_VALUE"]) for r in rows]


def ecb_meetings():
    """Monetary policy decision dates (ISO) from the ECB's calendar of Governing Council meetings: the day of a
    monetary policy meeting followed by the press conference."""
    html = _get("https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html")
    out = set()
    for d, txt in re.findall(r"<dt>\s*(\d{2}/\d{2}/\d{4})\s*</dt>\s*<dd>(.*?)</dd>", html, re.S):
        if "monetary policy meeting" in txt and "press conference" in txt:
            out.add(dt.datetime.strptime(d, "%d/%m/%Y").date().isoformat())
    return sorted(out)


def boe_series(code, day, start="01/Jan/2023"):
    """Daily series from the Bank of England's database (IADB): SONIA (IUDSOIA), nominal par gilt yields 5/10/20Y
    (IUDSNPY, IUDMNPY, IUDLNPY)."""
    url = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes"
           f"&Datefrom={start}&Dateto=now&SeriesCodes={code}&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
    rows = list(csv.reader(io.StringIO(_get(url, timeout=60))))[1:]
    out = [(dt.datetime.strptime(r[0], "%d %b %Y").date().isoformat(), float(r[1])) for r in rows if len(r) > 1 and r[1] not in ("", ".")]
    out = [x for x in out if x[0] <= day]
    return [d for d, _ in out], [v for _, v in out]


def saron(day):
    """SARON, monthly average from the SNB data portal (the daily fixing is published by SIX, not as open data)."""
    rows = [r for r in csv.reader(io.StringIO(_get("https://data.snb.ch/api/cube/zimoma/data/csv/en")), delimiter=";")
            if len(r) == 3 and r[1] == "SARON" and r[2]]
    out = [(r[0] + "-15", float(r[2])) for r in rows if r[0] + "-15" <= day]   # mid-month date for the monthly value
    return [d for d, _ in out], [v for _, v in out]


def sofr(n=300):
    j = json.loads(_get(f"https://markets.newyorkfed.org/api/rates/secured/sofr/last/{n}.json"))["refRates"][::-1]
    return [x["effectiveDate"] for x in j], [float(x["percentRate"]) for x in j]


def fred(sid, day):
    rows = list(csv.reader(io.StringIO(_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"))))[1:]
    rows = [(d, float(v)) for d, v in rows if v not in ("", ".") and d <= day]
    return [d for d, _ in rows], [v for _, v in rows]


def build(day, ecb_db):
    """{key: {name, group, unit, src, d: [dates], v: [values]}}; keys missing when a source failed."""
    S = {}

    def add(key, name, group, unit, src, fn):
        try:
            d, v = fn()
            if d and len(d) == len(v):   # about three years: enough for a 500-day VaR window
                S[key] = dict(name=name, group=group, unit=unit, src=src, d=d[-780:], v=v[-780:])
        except Exception as e:
            print(f"market: {key} unavailable: {e}", file=sys.stderr)

    try:
        cv = curve_series(ecb_db, day)
        for t in TENORS:
            add(f"AAA{t}", f"EUR AAA govt {t}Y spot", "rates", "%", "ECB", lambda t=t: cv[f"AAA{t}"])
        add("ALL10", "EUR all-govt 10Y spot", "rates", "%", "ECB", lambda: cv["ALL10"])
    except Exception as e:
        print(f"market: ECB curves unavailable: {e}", file=sys.stderr)
    add("ESTR", "€STR", "money", "%", "ECB", lambda: ecb_series("EST/B.EU000A2X2A25.WT", end=day))
    add("DFR", "ECB deposit facility rate", "money", "%", "ECB", lambda: ecb_series("FM/D.U2.EUR.4F.KR.DFR.LEV", end=day))
    add("SOFR", "SOFR", "money", "%", "NY Fed", sofr)
    add("EURUSD", "EUR/USD", "macro", "", "ECB", lambda: ecb_series("EXR/D.USD.EUR.SP00.A", end=day))
    add("EURGBP", "EUR/GBP", "macro", "", "ECB", lambda: ecb_series("EXR/D.GBP.EUR.SP00.A", end=day))
    add("EURCHF", "EUR/CHF", "macro", "", "ECB", lambda: ecb_series("EXR/D.CHF.EUR.SP00.A", end=day))
    add("SONIA", "SONIA", "money", "%", "BoE", lambda: boe_series("IUDSOIA", day))
    for code, key, name in (("IUDSNPY", "GB5", "UK gilt 5Y"), ("IUDMNPY", "GB10", "UK gilt 10Y"), ("IUDLNPY", "GB20", "UK gilt 20Y")):
        add(key, f"{name} (par)", "rates", "%", "BoE", lambda code=code: boe_series(code, day))
    add("SARON", "SARON (monthly average)", "money", "%", "SNB", lambda: saron(day))
    for sid, (name, group, unit) in FRED.items():
        add(sid, name, group, unit, "FRED", lambda sid=sid: fred(sid, day))
    return S


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", default=dt.date.today().isoformat())
    ap.add_argument("--ecb-db", default="ecb_curve.sqlite")
    a = ap.parse_args()
    for k, s in build(a.day, a.ecb_db).items():
        print(f"{k:18} {s['d'][-1]}  {s['v'][-1]:>10.4f}  ({len(s['v'])} obs, {s['src']})  {s['name']}")
