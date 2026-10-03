#!/usr/bin/env python3
"""
Obligationsmatematik för fast- och nollkupongare: upplupen ränta, yield, duration,
Z-spread mot ECB-kurvan samt omvandlingsfaktor och CTD för Eurex statsobligationsterminer.

Konventioner (rimliga för EUR-marknaden, se README för undantag):
  - likviddag T+2 TARGET-bankdagar, ACT/ACT ICMA, kupong 1 gång/år (BTP: 2 gånger/år)
  - yield med årlig (BTP: halvårs-) sammansatt ränta
  - Z-spread: kontinuerlig spread över ECB:s Svensson-spotkurva (ecb_curve.Curve)
  - omvandlingsfaktor (Eurex): ren kurs / 100 vid nominell kupong som yield (6 %, Buxl 4 %) på leveransdagen
"""
import datetime as dt
import math

# Eurex: leveransbart löptidsintervall (år från leveransdagen) och nominell kupong (%)
FUTURES_BASKET = {"FGBS": (1.75, 2.25), "FGBM": (4.5, 5.5), "FGBL": (8.5, 10.5), "FGBX": (24.0, 35.0)}
NOTIONAL_COUPON = {"FGBS": 6.0, "FGBM": 6.0, "FGBL": 6.0, "FGBX": 4.0}


# ---------------------------------------------------------------- kalender
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


# ---------------------------------------------------------------- kassaflöden
def schedule(maturity, settle, freq=1):
    """(föregående kupongdag, [kommande kupongdagar]) – baklänges från förfall."""
    dates, k = [], 0
    while True:
        d = add_months(maturity, -12 * k // freq)
        if d <= settle:
            return d, dates[::-1]
        dates.append(d)
        k += 1


def cashflows(coupon, maturity, settle, freq=1):
    """[(tid i år, belopp per 100)] och upplupen ränta per 100. ACT/ACT ICMA."""
    prev, nxt = schedule(maturity, settle, freq)
    period = (nxt[0] - prev).days
    w = (nxt[0] - settle).days / period  # andel av första perioden som återstår
    c = coupon / freq
    flows = [((w + k) / freq, c + (100.0 if k == len(nxt) - 1 else 0.0)) for k in range(len(nxt))]
    accrued = c * (1 - w)
    return flows, accrued


def price(y, flows, freq=1):
    """Smutsig kurs per 100 givet yield y (decimal)."""
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
    """Kontinuerlig spread (decimal) över curve.spot (procent, kontinuerlig)."""
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
    """Nyckeltal för en fast-/nollkupongare. clean = ren kurs per 100. Förfall > 100 år (eviga) -> None."""
    if (maturity - settle).days > 100 * 365:
        return None
    flows, acc = cashflows(coupon, maturity, settle, freq)
    dirty = clean + acc
    y = ytm(dirty, flows, freq)
    out = dict(accrued=acc, dirty=dirty, ytm=y, years=flows[-1][0])
    if y is not None:
        md = mod_duration(y, flows, freq)
        out.update(mdur=md, dv01=md * dirty / 1e4)  # per 100 nominellt och baspunkt
    if curve is not None:
        out["zspread"] = zspread(dirty, flows, curve)
    return out


# ---------------------------------------------------------------- terminer
def delivery_date(contract_month_date):
    """Eurex: leverans den 10:e i kontraktsmånaden (eller nästa bankdag)."""
    d = dt.date(contract_month_date.year, contract_month_date.month, 10)
    while not is_target_day(d):
        d += dt.timedelta(1)
    return d


def conversion_factor(coupon, maturity, delivery, notional=6.0):
    flows, acc = cashflows(coupon, maturity, delivery, 1)
    return (price(notional / 100, flows, 1) - acc) / 100


def ctd(code, fut_price, delivery, bunds, settle, curve=None):
    """Billigaste leveransbara obligation bland bunds [(isin, coupon, maturity, clean)].
    Returnerar dict med CTD, omvandlingsfaktor, bruttobasis och terminens DV01 per kontrakt (EUR)."""
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
        # terminskurs ~ CTD-kurs / CF  =>  DV01(termin) ~ DV01(CTD) / CF; 100 000 nominellt per kontrakt
        best.update(ytm=a["ytm"], mdur=a.get("mdur"), dv01_contract=a["dv01"] / best["cf"] * 1000)
    return best
