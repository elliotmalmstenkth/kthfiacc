#!/usr/bin/env python3
"""
US Treasury zero curve for the dollar bonds, from the Treasury's constant-maturity yields on FRED (DGS1MO ... DGS30,
par yields on a bond-equivalent, semi-annual basis, published daily with a one-day lag).

The par yields are bootstrapped into zero rates: up to 1 year the yield is taken as the zero rate (bills); beyond,
par bonds paying semi-annual coupons are priced at 100 at every half year (par yields interpolated linearly
between the published tenors) and solved for the discount factors one by one. spot(t) returns the continuously
compounded zero rate in percent, the same interface as ecb_curve.Curve, so analytics.zspread works unchanged.

  python ust_curve.py --day 2026-10-02
"""
import argparse, bisect, math

TENORS = [("DGS1MO", 1 / 12), ("DGS3MO", 0.25), ("DGS6MO", 0.5), ("DGS1", 1), ("DGS2", 2), ("DGS3", 3), ("DGS5", 5),
          ("DGS7", 7), ("DGS10", 10), ("DGS20", 20), ("DGS30", 30)]


def _interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)
    w = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
    return ys[i - 1] + w * (ys[i] - ys[i - 1])


class Curve:
    def __init__(self, date, par):
        """par: {tenor in years: par yield in percent}."""
        self.date = date
        ts = sorted(par)
        pys = [par[t] for t in ts]
        zt, zr = [], []
        for t in ts:                                   # bills: the yield as a continuous zero rate
            if t <= 1:
                y = par[t] / 100
                zt.append(t); zr.append(2 * math.log(1 + y / 2) * 100)
        dfs = {}                                       # bootstrap semi-annual par bonds from 1.5 years
        for k in range(1, int(ts[-1] * 2) + 1):
            t = k / 2
            c = _interp(ts, pys, t) / 100 / 2
            if t <= 1:
                dfs[t] = math.exp(-_interp(zt, zr, t) / 100 * t)
                continue
            df = (1 - c * sum(dfs[j / 2] for j in range(1, k))) / (1 + c)
            dfs[t] = df
            zt.append(t); zr.append(-math.log(df) / t * 100)
        order = sorted(range(len(zt)), key=lambda i: zt[i])
        self.t, self.z = [zt[i] for i in order], [zr[i] for i in order]
        self.par = dict(zip(ts, pys))

    def spot(self, t):
        return _interp(self.t, self.z, max(t, 1e-6))


def load(day):
    """The curve for the latest date on or before `day` on which every tenor is published."""
    import market
    series = {sid: dict(zip(*market.fred(sid, day))) for sid, _ in TENORS}
    dates = sorted(set.intersection(*(set(s) for s in series.values())))
    if not dates:
        raise RuntimeError("no US Treasury yields")
    d = dates[-1]
    return Curve(d, {t: series[sid][d] for sid, t in TENORS})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", required=True)
    a = ap.parse_args()
    c = load(a.day)
    print(c.date, " ".join(f"{t}y {c.par[t]:.2f}/{c.spot(t):.2f}" for t in (1, 2, 5, 10, 30)))
