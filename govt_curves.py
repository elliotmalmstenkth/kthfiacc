#!/usr/bin/env python3
"""
Risk-free curves for the sterling and Swiss franc bonds, with the same interface as ecb_curve.Curve and
ust_curve.Curve (spot(t): continuously compounded zero rate in percent).

  GBP  Bank of England nominal spot curve (gilts, fitted by the Bank; continuously compounded zero rates at half
       years, the short end monthly), from its "latest yield curve data" file, published each business day.
  CHF  The SNB's daily Confederation curve is no longer published (its data portal series stops in 2025), so the
       curve is fitted here to the Swiss Confederation bonds quoted on Börse Frankfurt: a Nelson-Siegel curve
       through their yields to maturity (annual), the decay chosen on a grid and the three levels by least squares.
       Yields are taken as zero rates, a close approximation at Swiss franc yield levels.

  python govt_curves.py --day 2026-10-02            # prints the BoE curve
"""
import argparse, bisect, datetime as dt, io, math, urllib.request, zipfile

BOE_ZIP = "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip"


class Points:
    """A zero curve from points (years, continuous %), linear in between, flat outside."""
    def __init__(self, date, t, z, src):
        order = sorted(range(len(t)), key=lambda i: t[i])
        self.date, self.t, self.z, self.src = date, [t[i] for i in order], [z[i] for i in order], src

    def spot(self, t):
        t = max(t, 1e-6)
        if t <= self.t[0]:
            return self.z[0]
        if t >= self.t[-1]:
            return self.z[-1]
        i = bisect.bisect_right(self.t, t)
        w = (t - self.t[i - 1]) / (self.t[i] - self.t[i - 1])
        return self.z[i - 1] + w * (self.z[i] - self.z[i - 1])


def _rows(ws):
    """(years per column, [(date, values)]) from a BoE curve sheet."""
    years, out = None, []
    for r in ws.iter_rows(values_only=True):
        if r and r[0] == "years:":
            years = list(r[1:])
        elif r and isinstance(r[0], dt.datetime):
            out.append((r[0].date().isoformat(), list(r[1:])))
    return years, out


def boe(day, raw=None):
    """The Bank of England nominal spot curve for the latest date on or before `day`."""
    import openpyxl
    if raw is None:
        req = urllib.request.Request(BOE_ZIP, headers={"User-Agent": "Mozilla/5.0 (kth-fic-club)"})
        raw = urllib.request.urlopen(req, timeout=90).read()
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.startswith("GLC Nominal daily"))
    wb = openpyxl.load_workbook(io.BytesIO(z.read(name)), read_only=True, data_only=True)
    ys, long_ = _rows(wb["4. spot curve"])
    ys_s, short = _rows(wb["3. spot, short end"])
    dates = [d for d, v in long_ if d <= day and any(isinstance(x, (int, float)) for x in v)]
    if not dates:
        raise RuntimeError("no Bank of England curve on or before " + day)
    d = dates[-1]
    pts = {}
    for yrs, rows in ((ys_s, short), (ys, long_)):
        vals = dict(rows).get(d) or []
        for t, v in zip(yrs or [], vals):
            if isinstance(t, (int, float)) and isinstance(v, (int, float)):
                pts[round(t, 4)] = v
    return Points(d, list(pts), list(pts.values()), "Bank of England")


def nelson_siegel(t, b0, b1, b2, tau):
    x = t / tau
    f = (1 - math.exp(-x)) / x if x > 1e-9 else 1.0
    return b0 + b1 * f + b2 * (f - math.exp(-x))


def fit(points, date, src):
    """Nelson-Siegel through (years, yield %) points; returns Points sampled from 3 months to 50 years (yields
    converted to continuous compounding) and the fit's RMSE in bp. None with fewer than 6 points."""
    import numpy as np
    pts = [(t, y) for t, y in points if t > 0.25 and y is not None]
    if len(pts) < 6:
        return None
    T, Y = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    best = None
    for tau in np.arange(0.5, 15.01, 0.25):
        x = T / tau
        f = (1 - np.exp(-x)) / x
        A = np.column_stack([np.ones_like(T), f, f - np.exp(-x)])
        beta, *_ = np.linalg.lstsq(A, Y, rcond=None)
        err = float(np.sqrt(np.mean((A @ beta - Y) ** 2)))
        if best is None or err < best[0]:
            best = (err, beta, tau)
    err, beta, tau = best
    grid = [0.25] + [k / 2 for k in range(1, 101)]
    zs = [math.log(1 + nelson_siegel(t, *beta, tau) / 100) * 100 for t in grid]
    c = Points(date, grid, zs, src)
    c.rmse_bp, c.params = err * 100, dict(b0=float(beta[0]), b1=float(beta[1]), b2=float(beta[2]), tau=float(tau))
    return c


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", required=True)
    a = ap.parse_args()
    c = boe(a.day)
    print(c.date, c.src, " ".join(f"{t}y {c.spot(t):.3f}" for t in (1, 2, 5, 10, 30)))
