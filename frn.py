#!/usr/bin/env python3
"""
Floating-rate notes (FRNs) on 3-month Euribor: discount margin and risk, against the Euribor forward curve from
the Eurex 3-month Euribor futures (stir.py).

Assumptions, as the free data does not say more:
  - the index is 3-month Euribor and the coupon resets quarterly, on dates rolled back from maturity by 3 months;
  - the coupon of the current period was fixed at the last reset; its Euribor fixing is licensed, so it is
    taken as the nearest futures-implied 3-month Euribor;
  - coupons (index + quoted margin) and discounting (index + discount margin) on ACT/360;
  - beyond the last futures contract the forward stays at the last contract's rate.
Discount margin (DM): the spread over the index forwards that discounts the projected coupons to the dirty price,
the FRN counterpart of a fixed bond's Z-spread. An FRN priced at 100 has DM = its quoted margin.
Rates duration is about the time to the next reset (the coupon catches up after that); spread duration is the
price sensitivity to the DM, close to the time to maturity.
"""
import bisect, datetime as dt

import analytics as an


class Forwards:
    """3-month Euribor forwards by deposit start date (stir.py 'euribor': start, rate in %)."""
    def __init__(self, euribor):
        pts = sorted((dt.date.fromisoformat(q["start"]), q["rate"]) for q in euribor)
        self.d, self.r = [p[0] for p in pts], [p[1] for p in pts]

    def at(self, d):
        i = bisect.bisect_right(self.d, d) - 1
        return self.r[max(0, i)]


def reset_dates(maturity, settle):
    """Previous reset (on or before settle) and the future payment dates, quarterly back from maturity."""
    out, k = [], 0
    while True:
        d = an.add_months(maturity, -3 * k)
        if d <= settle:
            return d, out[::-1]
        out.append(d)
        k += 1


def _pv(margin, dm, prev, pays, settle, fw, current):
    """Dirty price per 100 at discount margin dm (both in %), and the accrued interest."""
    pv, df, start = 0.0, 1.0, prev
    for i, end in enumerate(pays):
        idx = current if i == 0 else fw.at(start)
        tau = (end - start).days / 360
        cpn = (idx + margin) / 100 * tau * 100
        if i == 0:   # discount only the part of the first period after settlement
            df /= 1 + (idx + dm) / 100 * (end - settle).days / 360
        else:
            df /= 1 + (idx + dm) / 100 * tau
        pv += df * (cpn + (100 if i == len(pays) - 1 else 0))
        start = end
    acc = (current + margin) / 100 * (settle - prev).days / 360 * 100
    return pv, acc


def analyse(clean, margin_bp, maturity, settle, fw):
    """{dm (bp), accrued, dirty, cpn (current coupon %), rdur, sdur, ytm (index + DM, %)} or None."""
    if (maturity - settle).days > 60 * 365 or maturity <= settle:
        return None
    prev, pays = reset_dates(maturity, settle)
    margin = margin_bp / 100
    current = fw.at(settle)
    _, acc = _pv(margin, 0.0, prev, pays, settle, fw, current)
    dirty = clean + acc
    lo, hi = -5.0, 30.0
    f = lambda dm: _pv(margin, dm, prev, pays, settle, fw, current)[0] - dirty
    if f(lo) < 0 or f(hi) > 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    dm = (lo + hi) / 2
    h = 0.01                      # 1 bp in percent
    p0 = _pv(margin, dm, prev, pays, settle, fw, current)[0]
    sdur = (_pv(margin, dm - h, prev, pays, settle, fw, current)[0] - _pv(margin, dm + h, prev, pays, settle, fw, current)[0]) / (2 * h / 100) / p0
    t_reset = (pays[0] - settle).days / 365.25
    rdur = t_reset / (1 + (current + dm) / 100 * t_reset)
    years = (maturity - settle).days / 365.25
    avg_idx = sum(fw.at(an.add_months(settle, 3 * k)) for k in range(max(1, int(years * 4)))) / max(1, int(years * 4))
    return dict(dm=dm * 100, accrued=acc, dirty=dirty, cpn=current + margin, rdur=rdur, sdur=sdur, ytm=avg_idx + dm, years=years)
