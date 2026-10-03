#!/usr/bin/env python3
"""
Bond maths for fixed and zero-coupon bonds: accrued interest, yield, duration, Z-spread vs
the ECB curve, plus conversion factors and CTD for the Eurex government bond futures.

Conventions (reasonable for the EUR market, see README for exceptions):
  - T+2 TARGET settlement, ACT/ACT ICMA, annual coupons (BTPs: semi-annual)
  - yield with annual (BTPs: semi-annual) compounding
  - Z-spread: continuously compounded spread over the ECB Svensson spot curve (ecb_curve.Curve)
  - conversion factor (Eurex): clean price / 100 at a yield equal to the notional coupon
    (6%, Buxl 4%) on the delivery date
"""
import datetime as dt
import math

# Eurex: deliverable maturity range (years from delivery) and notional coupon (%)
FUTURES_BASKET = {"FGBS": (1.75, 2.25), "FGBM": (4.5, 5.5), "FGBL": (8.5, 10.5), "FGBX": (24.0, 35.0)}
NOTIONAL_COUPON = {"FGBS": 6.0, "FGBM": 6.0, "FGBL": 6.0, "FGBX": 4.0}


# ---------------------------------------------------------------- calendar
def easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(y, month, day + 1)


def is_target_day(d):
    if d.weekday() >= 5:
        return False
    e = easter(d.year)
    return d not in {dt.date(d.year, 1, 1), e - dt.timedelta(2), e + dt.timedelta(1),
                     dt.date(d.year, 5, 1), dt.date(d.year, 12, 25), dt.date(d.year, 12, 26)}


def add_business_days(d, n):
    while n:
        d += dt.timedelta(1)
        if is_target_day(d):
            n -= 1
    return d


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return dt.date(y, m, day)
        except ValueError:
            continue


# ---------------------------------------------------------------- cash flows
def schedule(maturity, settle, freq=1):
    """(previous coupon date, [future coupon dates]), rolled back from maturity."""
    dates, k = [], 0
    while True:
        d = add_months(maturity, -12 * k // freq)
        if d <= settle:
            return d, dates[::-1]
        dates.append(d)
        k += 1


def cashflows(coupon, maturity, settle, freq=1):
    """[(time in years, amount per 100)] and accrued interest per 100. ACT/ACT ICMA."""
    prev, nxt = schedule(maturity, settle, freq)
    period = (nxt[0] - prev).days
    w = (nxt[0] - settle).days / period  # fraction of the first period remaining
    c = coupon / freq
    flows = [((w + k) / freq, c + (100.0 if k == len(nxt) - 1 else 0.0)) for k in range(len(nxt))]
    accrued = c * (1 - w)
    return flows, accrued


def price(y, flows, freq=1):
    """Dirty price per 100 for yield y (decimal)."""
    return sum(cf / (1 + y / freq) ** (freq * t) for t, cf in flows)


def ytm(dirty, flows, freq=1):
    lo, hi = -0.2, 2.0
    if not (price(hi, flows, freq) < dirty < price(lo, flows, freq)):
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if price(mid, flows, freq) > dirty:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2


def mod_duration(y, flows, freq=1):
    h = 1e-5
    p = price(y, flows, freq)
    return -(price(y + h, flows, freq) - price(y - h, flows, freq)) / (2 * h) / p


def zspread(dirty, flows, curve):
    """Continuously compounded spread (decimal) over curve.spot (percent, continuous)."""
    f = lambda s: sum(cf * math.exp(-(curve.spot(t) / 100 + s) * t) for t, cf in flows) - dirty
    lo, hi = -0.2, 1.0
    if f(lo) < 0 or f(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2


def analyse(clean, coupon, maturity, settle, curve=None, freq=1):
    """Analytics for a fixed/zero-coupon bond. clean = clean price per 100. Maturity > 100y (perpetuals) -> None."""
    if (maturity - settle).days > 100 * 365:
        return None
    flows, acc = cashflows(coupon, maturity, settle, freq)
    dirty = clean + acc
    y = ytm(dirty, flows, freq)
    out = dict(accrued=acc, dirty=dirty, ytm=y, years=flows[-1][0])
    if y is not None:
        md = mod_duration(y, flows, freq)
        out.update(mdur=md, dv01=md * dirty / 1e4)  # per 100 nominal per basis point
    if curve is not None:
        out["zspread"] = zspread(dirty, flows, curve)
    return out


# ---------------------------------------------------------------- futures
def delivery_date(contract_month_date):
    """Eurex: delivery on the 10th of the contract month (or the next business day)."""
    d = dt.date(contract_month_date.year, contract_month_date.month, 10)
    while not is_target_day(d):
        d += dt.timedelta(1)
    return d


def conversion_factor(coupon, maturity, delivery, notional=6.0):
    flows, acc = cashflows(coupon, maturity, delivery, 1)
    return (price(notional / 100, flows, 1) - acc) / 100


def ctd(code, fut_price, delivery, bunds, settle, curve=None):
    """Cheapest-to-deliver among bunds [(isin, coupon, maturity, clean)].
    Returns a dict with the CTD, conversion factor, gross basis and the futures DV01 per contract (EUR)."""
    lo, hi = FUTURES_BASKET[code]
    best = None
    for isin, cpn, mat, clean in bunds:
        yrs = (mat - delivery).days / 365.25
        if not (lo <= yrs <= hi):
            continue
        cf = conversion_factor(cpn, mat, delivery, NOTIONAL_COUPON[code])
        basis = clean - fut_price * cf
        if best is None or basis < best["basis"]:
            best = dict(isin=isin, coupon=cpn, maturity=mat.isoformat(), clean=clean, cf=cf, basis=basis)
    if best:
        a = analyse(best["clean"], best["coupon"], dt.date.fromisoformat(best["maturity"]), settle, curve)
        # futures price ~ CTD price / CF  =>  DV01(future) ~ DV01(CTD) / CF; EUR 100,000 notional per contract
        best.update(ytm=a["ytm"], mdur=a.get("mdur"), dv01_contract=a["dv01"] / best["cf"] * 1000)
    return best


# ---------------------------------------------------------------- relative value
def fit_spread_curve(years, z, min_n=3):
    """Least-squares Z-spread curve z = a + b ln(T) (+ c ln(T)^2 with 6+ bonds), refitted once without outliers
    (residual > 3 robust standard deviations, at least 5 bp). Returns the polynomial coefficients
    (highest power first, for numpy.polyval on ln(T)) or None if there are too few bonds."""
    import numpy as np
    x, y = np.log(np.maximum(np.asarray(years, float), 0.25)), np.asarray(z, float)
    keep = np.isfinite(x) & np.isfinite(y)
    for _ in range(2):
        if keep.sum() < min_n or np.ptp(x[keep]) < 0.1:
            return None
        deg = 2 if keep.sum() >= 6 else 1
        coef = np.polyfit(x[keep], y[keep], deg)
        res = y - np.polyval(coef, x)
        mad = np.median(np.abs(res[keep] - np.median(res[keep]))) * 1.4826
        out = np.abs(res) > max(3 * mad, 5.0)
        if not (keep & out).any():
            break
        keep &= ~out
    return coef


def spread_curve_value(coef, years):
    import numpy as np
    return float(np.polyval(coef, np.log(max(years, 0.25))))


def rich_cheap(groups, years, z, fit_mask=None, min_n=4):
    """Residual Z-spread (bp) of each bond against a curve fitted to its group (issuer).
    Groups with fewer than min_n bonds in the fit, bonds under 1 year and bonds outside the fitted maturity range
    get None. Positive = cheap (spread above the curve).
    Returns (residual, fitted coefficients per group)."""
    import numpy as np
    groups, years, z = np.asarray(groups, object), np.asarray(years, float), np.asarray(z, float)
    fit_mask = np.ones(len(z), bool) if fit_mask is None else np.asarray(fit_mask, bool)
    res, coefs = np.full(len(z), np.nan), {}
    order = np.argsort(groups, kind="stable")
    bounds = np.flatnonzero(np.r_[True, groups[order][1:] != groups[order][:-1], True])
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        idx = order[lo:hi]
        fit = idx[fit_mask[idx] & np.isfinite(z[idx]) & (years[idx] >= 0.5)]
        if len(fit) < min_n:
            continue
        coef = fit_spread_curve(years[fit], z[fit], min_n)
        if coef is None:
            continue
        coefs[groups[idx[0]]] = coef
        # only inside the fitted maturity range (no extrapolation) and from 1 year: Z-spreads of very short
        # bonds are dominated by the bid-offer
        lo, hi = years[fit].min() / 1.5, years[fit].max() * 1.5
        ok = idx[(years[idx] >= max(lo, 1.0)) & (years[idx] <= hi)]
        res[ok] = z[ok] - np.polyval(coef, np.log(years[ok]))
    return [None if not np.isfinite(r) else float(r) for r in res], coefs
