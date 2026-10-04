#!/usr/bin/env python3
"""
Implied volatility of the options on the German government bond futures (Eurex OGBS, OGBM, OGBL), from the
trades in DEUR-posttrade (the mfs.py archive).

  python eurex_options.py 2026-10-02                     # -> eurex_options-2026-10-02.csv.gz, one row per trade

Eurex reports option trades with the option PRODUCT ISIN plus contractDate (expiry), optionCategory (C/P) and
originalStrikePrice. The ISINs below were identified from a day of trades (their strikes centre on the price of
the future they belong to). Options on Buxl, BTP and OAT futures trade too rarely to be useful.

Pricing: the options are on the next quarterly future (the first futures contract expiring on or after the
option's expiry) and are margined futures-style, so the premium is not discounted: Black-76 with r = 0.
Each option trade is paired with the last on-book trade of that future at or before it (or, failing that, the
first one within 5 minutes after). Only standard on-book trades are used (continuous trading or auction, no
strategy legs or block trades), only out-of-the-money options (calls above the future, puts below: those are
the ones that trade, and their price is mostly volatility) and only expiries more than a day away.
"""
import argparse, bisect, csv, datetime as dt, gzip, math, os, sys
from collections import defaultdict

import eurex, mfs

# option product ISIN -> futures code
OPTIONS = {"DE0009652701": "FGBS", "DE0009652693": "FGBM", "DE0009652685": "FGBL"}
ON_BOOK = {"2", "O", "K"}
EXPIRY_TIME = dt.time(17, 15)          # last trading time on the expiry day, Frankfurt
MAX_GAP = 300                          # seconds between the option trade and the futures price
COLS = ["code", "expiry", "cp", "strike", "price", "qty", "time", "fut_contract", "fut", "t", "k", "iv"]


def _ts(s):
    return dt.datetime.fromisoformat(s[:26].rstrip("Z") + "+00:00") if "." in s else dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _ncdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def black76(f, k, t, vol, cp):
    """Undiscounted Black-76 price of a call (cp = 'C') or put."""
    if vol <= 0 or t <= 0:
        return max(0.0, (f - k) if cp == "C" else (k - f))
    s = vol * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * s * s) / s
    d2 = d1 - s
    return f * _ncdf(d1) - k * _ncdf(d2) if cp == "C" else k * _ncdf(-d2) - f * _ncdf(-d1)


def implied_vol(price, f, k, t, cp, lo=1e-4, hi=2.0):
    """Black-76 implied volatility by bisection; None when the price is outside the no-arbitrage range."""
    intrinsic = max(0.0, (f - k) if cp == "C" else (k - f))
    if not (price > intrinsic and price < (f if cp == "C" else k)):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if black76(f, k, t, mid, cp) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    v = 0.5 * (lo + hi)
    return v if 1e-3 < v < 1.0 else None


def read_trades(date, archive=mfs.ARCHIVE_DEFAULT, feed="DEUR-posttrade"):
    """(option trades, futures trades by (code, contract) as sorted [(time, price)])."""
    folder = os.path.join(archive, feed, date)
    daily = os.path.join(folder, f"{feed}-daily-{date}.json.gz")
    paths = [daily] if os.path.exists(daily) else [p for _, p in mfs.day_files(archive, feed, date)]
    if not paths:
        raise SystemExit(f"no {feed} files for {date} in {archive}/")
    fut_isin = {eurex.PRODUCTS[c][0]: c for c in set(OPTIONS.values())}
    opts, futs, cancelled = [], defaultdict(list), set()
    for p in paths:
        for m in mfs.iter_messages(p):
            if m.get("messageId") != "posttrade":
                continue
            isin = m.get("instrumentIdentificationCode")
            if isin not in OPTIONS and isin not in fut_isin:
                continue
            if m.get("mmtModificationInd") == "C":
                cancelled.add(m.get("transactionIdentificationCode"))
                continue
            if m.get("mmtTradingMode") not in ON_BOOK or m.get("price") is None:
                continue
            if isin in OPTIONS:
                if m.get("optionCategory") in ("C", "P") and m.get("mmtTransactionCategory") == "-" and m.get("originalStrikePrice"):
                    opts.append(m)
            elif not m.get("optionCategory"):
                futs[(fut_isin[isin], m["contractDate"])].append(m)
    opts = [m for m in opts if m.get("transactionIdentificationCode") not in cancelled]
    series = {}
    for key, ts in futs.items():
        ts = [t for t in ts if t.get("transactionIdentificationCode") not in cancelled]
        series[key] = sorted((_ts(t["tradingDateAndTime"]), t["price"]) for t in ts)
    return opts, series


def underlying(code, expiry, series):
    """The futures contract an option expiring on `expiry` is written on: the first one expiring on or after it."""
    cs = sorted(c for (k, c) in series if k == code and c >= expiry)
    return cs[0] if cs else None


def future_at(points, when):
    """Futures price at `when`: the last trade at or before it, else the first within MAX_GAP after."""
    i = bisect.bisect_right(points, (when, float("inf")))
    if i and (when - points[i - 1][0]).total_seconds() <= 3600:
        return points[i - 1][1]
    if i < len(points) and (points[i][0] - when).total_seconds() <= MAX_GAP:
        return points[i][1]
    return None


def build(date, archive=mfs.ARCHIVE_DEFAULT):
    opts, series = read_trades(date, archive)
    rows = []
    for m in opts:
        code, expiry, cp, k = OPTIONS[m["instrumentIdentificationCode"]], m["contractDate"], m["optionCategory"], float(m["originalStrikePrice"])
        when = _ts(m["tradingDateAndTime"])
        exp = dt.datetime.combine(dt.date.fromisoformat(expiry), EXPIRY_TIME, tzinfo=mfs.TZ)
        t = (exp - when).total_seconds() / (365.25 * 86400)
        if t < 1.5 / 365.25:
            continue
        uc = underlying(code, expiry, series)
        f = future_at(series[(code, uc)], when) if uc else None
        if not f or (cp == "C" and k <= f) or (cp == "P" and k >= f):
            continue
        iv = implied_vol(float(m["price"]), f, k, t, cp)
        if iv is None:
            continue
        rows.append(dict(code=code, expiry=expiry, cp=cp, strike=k, price=m["price"], qty=m.get("quantity") or 0,
                         time=m["tradingDateAndTime"][:19], fut_contract=uc, fut=f, t=round(t, 6),
                         k=round(math.log(k / f), 6), iv=round(iv, 6)))
    rows.sort(key=lambda r: (r["code"], r["expiry"], r["time"]))
    return rows


def write(rows, path):
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, COLS); w.writeheader(); w.writerows(rows)
    return path


def read(path):
    num = {"strike", "price", "qty", "fut", "t", "k", "iv"}
    with gzip.open(path, "rt", newline="") as f:
        return [{c: float(v) if c in num else v for c, v in r.items()} for r in csv.DictReader(f)]


# ---------------------------------------------------------------- summary for the site
def _wls_quad(xs, ys, ws):
    """Weighted least squares y = a + b x + c x^2; returns (a, b, c) or None."""
    S = [[0.0] * 3 for _ in range(3)]; v = [0.0] * 3
    for x, y, w in zip(xs, ys, ws):
        p = (1.0, x, x * x)
        for i in range(3):
            v[i] += w * p[i] * y
            for j in range(3):
                S[i][j] += w * p[i] * p[j]
    try:   # Cramer's rule on the 3x3 normal equations
        def det(m):
            return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                    + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
        d = det(S)
        if abs(d) < 1e-18:
            return None
        out = []
        for c in range(3):
            m = [row[:] for row in S]
            for r in range(3):
                m[r][c] = v[r]
            out.append(det(m) / d)
        return tuple(out)
    except (ZeroDivisionError, OverflowError):
        return None


def atm_vol(trades):
    """At-the-money implied volatility of one expiry: a quadratic smile in log-moneyness fitted to the trades
    (weights: sqrt of the lots), evaluated at the money. Needs trades on both sides of the money; otherwise the
    lot-weighted mean of the trades within 1% of the future. None if neither is possible."""
    xs, ys = [r["k"] for r in trades], [r["iv"] for r in trades]
    ws = [math.sqrt(max(r["qty"], 1)) for r in trades]
    near = [(r["iv"], w) for r, w in zip(trades, ws) if abs(r["k"]) < 0.01]
    if len(trades) >= 6 and min(xs) < -0.002 and max(xs) > 0.002:
        fit = _wls_quad(xs, ys, ws)
        if fit and 0.005 < fit[0] < 0.5:
            return fit[0], fit
    if len(near) >= 2:
        return sum(v * w for v, w in near) / sum(w for _, w in near), None
    return None, None


def summary(rows, asof, durations):
    """Per futures code: expiries with ATM vol (price and bp/day), the smile (points binned by strike) and a
    constant-maturity 1-month ATM vol interpolated in total variance. durations: {code: modified duration of
    the future} for the conversion to yield volatility (bp per day = price vol / duration / sqrt(252))."""
    out = {}
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[r["code"]][r["expiry"]].append(r)
    for code, exps in by.items():
        D = durations.get(code)
        es = []
        for e, ts in sorted(exps.items()):
            v, fit = atm_vol(ts)
            f = sorted(ts, key=lambda r: r["time"])[-1]["fut"]
            bins = defaultdict(list)
            for r in ts:
                bins[(r["strike"], r["cp"])].append(r)
            smile = [dict(strike=s, cp=cp, iv=round(sum(x["iv"] * x["qty"] for x in g) / max(1, sum(x["qty"] for x in g)), 5),
                          lots=sum(x["qty"] for x in g), n=len(g)) for (s, cp), g in sorted(bins.items())]
            t = sum(r["t"] for r in ts) / len(ts)
            es.append(dict(expiry=e, days=round(t * 365.25, 1), t=t, fut=f, contract=ts[0]["fut_contract"], atm=v and round(v, 5),
                           bp=v and D and round(v / D * 1e4 / math.sqrt(252), 2), fit=fit and [round(c, 5) for c in fit],
                           trades=len(ts), lots=sum(r["qty"] for r in ts), smile=smile))
        ok = [x for x in es if x["atm"]]
        cm = None
        for a, b in zip(ok, ok[1:]):          # 30-day constant maturity: linear in total variance
            ta, tb, tm = a["t"], b["t"], 30 / 365.25
            if ta <= tm <= tb:
                w = (tm - ta) / (tb - ta)
                cm = math.sqrt(((1 - w) * a["atm"] ** 2 * ta + w * b["atm"] ** 2 * tb) / tm)
        if cm is None and ok:                  # outside the traded range: the nearest expiry
            cm = min(ok, key=lambda x: abs(x["days"] - 30))["atm"]
        for x in es:
            x.pop("t")
        out[code] = dict(expiries=es, atm1m=cm and round(cm, 5), bp1m=cm and D and round(cm / D * 1e4 / math.sqrt(252), 2), dur=D)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date")
    ap.add_argument("--archive", default=mfs.ARCHIVE_DEFAULT)
    ap.add_argument("--out")
    a = ap.parse_args()
    rows = build(a.date, a.archive)
    path = write(rows, a.out or f"eurex_options-{a.date}.csv.gz")
    s = summary(rows, a.date, {})
    for code, x in s.items():
        print(f"{code}: 1M ATM {x['atm1m']}", "; ".join(f"{e['expiry']} {e['atm']} ({e['trades']} trades)" for e in x["expiries"]), file=sys.stderr)
    print(f"{len(rows)} option trades -> {path}", file=sys.stderr)
