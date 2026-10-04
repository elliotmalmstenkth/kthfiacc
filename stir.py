#!/usr/bin/env python3
"""
The ECB path and the money-market curve priced by the Eurex short-term interest rate futures (eurex.py):

  FST3  3-month €STR futures: quarterly (IMM) contracts. The contract date is the START of the reference quarter
        (third Wednesday of Mar/Jun/Sep/Dec); it settles on €STR compounded over the quarter, which ends on the
        next IMM date. Price = 100 − rate.
  FEU3  3-month Euribor futures: monthly and quarterly contracts. The contract date is the last trading day, two
        business days before the third Wednesday; it settles on the 3-month Euribor fixed that day, for a
        deposit from the third Wednesday to three months later. Price = 100 − rate.

From these, on the site:
  - the implied average €STR per quarter, and the implied ECB deposit facility rate (DFR): €STR + today's
    DFR − €STR spread (€STR trades a few bp under the DFR);
  - for the quarter already under way, the part of the €STR fixings still to come: the realized €STR since the
    start is known, so the remaining days' rate = (quarter rate × all days − realized × elapsed days) / days left;
  - the move priced for the next ECB meeting, when it is the only one taking effect in the rest of the current
    quarter (decisions apply from the Wednesday after the meeting);
  - 3-month Euribor forwards and the Euribor − €STR basis for the same quarter (bank credit and term premium).
Simple (ACT/360) averages in place of compounding: the difference is a fraction of a basis point.
"""
import datetime as dt

QUARTER_MONTHS = (3, 6, 9, 12)


def third_wednesday(y, m):
    d = dt.date(y, m, 15)                       # the third Wednesday falls on the 15th to the 21st
    return d + dt.timedelta(days=(2 - d.weekday()) % 7)


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def euribor_period(contract):
    """Deposit period of a Euribor contract (contract = last trading day): from the third Wednesday of its month
    for three months (same day of the month)."""
    c = dt.date.fromisoformat(contract)
    s = third_wednesday(c.year, c.month)
    e = add_months(s, 3)
    return s, e.replace(day=s.day)


def estr_period(contract):
    """Reference quarter of a 3-month €STR contract (contract = its start): to the next IMM date."""
    s = dt.date.fromisoformat(contract)
    e = add_months(s, 3)
    return s, third_wednesday(e.year, e.month)


def effective(meeting):
    """ECB decisions apply from the Wednesday of the week after the meeting (Thursday + 6 days)."""
    d = dt.date.fromisoformat(meeting)
    return d + dt.timedelta(days=(2 - d.weekday()) % 7 or 7)


def realized_avg(dates, values, start, asof):
    """Average €STR fixing over the calendar days from start to asof (each fixing holds until the next)."""
    pts = [(dt.date.fromisoformat(d), v) for d, v in zip(dates, values)]
    if not pts:
        return None
    tot, n, day, last = 0.0, 0, start, None
    i = 0
    while day < asof:
        while i < len(pts) and pts[i][0] <= day:
            last = pts[i][1]; i += 1
        if last is None:
            return None
        tot += last; n += 1; day += dt.timedelta(days=1)
    return tot / n if n else None


def build(futures_rows, asof, estr=None, dfr=None, meetings=()):
    """futures_rows: [{code, contract, last, trades, lots, last_time}] for FST3/FEU3 (eurex futures_daily).
    estr: (dates, values) €STR fixings; dfr: latest deposit facility rate (%); meetings: ISO dates of upcoming
    monetary policy decisions. Returns the structure the site shows, or None without €STR futures."""
    asof = dt.date.fromisoformat(asof)
    e_now = estr[1][-1] if estr and estr[1] else None
    spread = dfr - e_now if dfr is not None and e_now is not None else None
    out = dict(asof=asof.isoformat(), estr=e_now, estr_date=estr[0][-1] if estr and estr[0] else None, dfr=dfr,
               spread=spread and round(spread, 4), meetings=[m for m in meetings if m >= asof.isoformat()][:12], estr_q=[], euribor=[])
    for r in sorted((r for r in futures_rows if r["code"] == "FST3" and r.get("last")), key=lambda r: r["contract"]):
        s, e = estr_period(r["contract"])
        if e <= asof:
            continue
        rate = 100 - r["last"]
        q = dict(contract=r["contract"], start=s.isoformat(), end=e.isoformat(), price=r["last"], rate=round(rate, 4),
                 trades=int(r.get("trades") or 0), lots=r.get("lots"), dfr=round(rate + spread, 4) if spread is not None else None)
        if s < asof and estr:                 # under way: back out the rest of the quarter
            real = realized_avg(estr[0], estr[1], s, asof)
            t, T = (asof - s).days, (e - s).days
            if real is not None and T > t:
                rem = (rate * T - real * t) / (T - t)
                q.update(realized=round(real, 4), elapsed=t, days=T, remaining=round(rem, 4),
                         dfr=round(rem + spread, 4) if spread is not None else None)
                # the next meeting, if it is the only decision taking effect before the quarter ends
                eff = [(m, effective(m)) for m in out["meetings"] if asof < effective(m) < e]
                if len(eff) == 1 and e_now is not None:
                    m, ef = eff[0]
                    d1, d2 = (ef - asof).days, (e - ef).days
                    x = (rem * (d1 + d2) - e_now * d1) / d2
                    out["next"] = dict(meeting=m, effective=ef.isoformat(), bp=round((x - e_now) * 100, 1))
        out["estr_q"].append(q)
    for q in out["estr_q"]:                   # priced change from today's €STR, bp and in 25 bp moves
        if e_now is not None:
            q["chg"] = round((q.get("remaining", q["rate"]) - e_now) * 100, 1)
            q["moves"] = round(q["chg"] / 25, 2)
    by_start = {q["start"]: q for q in out["estr_q"]}
    for r in sorted((r for r in futures_rows if r["code"] == "FEU3" and r.get("last")), key=lambda r: r["contract"]):
        if dt.date.fromisoformat(r["contract"]) < asof:
            continue
        s, e = euribor_period(r["contract"])
        rate = 100 - r["last"]
        q = by_start.get(s.isoformat())
        out["euribor"].append(dict(contract=r["contract"], start=s.isoformat(), end=e.isoformat(), price=r["last"], rate=round(rate, 4),
                                   trades=int(r.get("trades") or 0), lots=r.get("lots"), quarterly=s.month in QUARTER_MONTHS,
                                   basis=round((rate - q["rate"]) * 100, 1) if q else None))
    return out if out["estr_q"] else None
