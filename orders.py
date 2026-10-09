#!/usr/bin/env python3
"""
Limit orders for the club book (portfolio/positions.json, key "orders"), matched each evening against the day's
Deutsche Börse minute files in the archive (ci.py daily, before the raw files are deleted).

An order: {id, kind: "bond" | "future", isin | code + contract, qty (signed: + buy, − sell), limit, at (ISO UTC,
when it was placed), day (first trading day it works, Frankfurt), expires (last trading day), by, status}.
status: working -> filled (fill_px, fill_at) | expired | cancelled (from the page).

Fills, conservatively:
  bonds    the first firm quote after the order was placed that crosses the limit: a buy fills at the offer when
           offer <= limit (with size on the offer), a sell at the bid when bid >= limit (with size on the bid)
  futures  a trade on the book strictly through the limit (below it for a buy, above for a sell) fills the
           order at the limit: a trade exactly at the limit may have filled orders ahead of ours in the queue
Orders still working after their last day expire. A fill becomes a position with the fill's price and time.
"""
import analytics as an, datetime as dt, mfs


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def active(orders, day):
    return [o for o in orders if o.get("status") == "working" and o.get("day", "") <= day <= o.get("expires", "")]


def match_bonds(orders, files):
    """{order id: (price, time)} from DFRA pre-trade files [(time, path)] (deltas merged per ISIN)."""
    want = {}
    for o in orders:
        want.setdefault(o["isin"], []).append(o)
    fills, side = {}, {}
    for _, path in files:
        for m in mfs.iter_messages(path):
            isin = m.get("instrumentIdentificationCode")
            if isin not in want:
                continue
            ts = (m.get("updateDateAndTime") or m.get("publicationDateAndTime") or "")[:23]
            q = side.setdefault(isin, {})
            for s, key in (("bid", "bestBid"), ("ask", "bestAsk")):
                if key in m:
                    px = _num(m[key])
                    q[s] = (px, _num(m.get(key + "Qty")) or 0) if px else None
            for o in want[isin]:
                if o["id"] in fills or ts < o["at"][:23]:
                    continue
                if o["qty"] > 0 and q.get("ask") and q["ask"][1] > 0 and q["ask"][0] <= o["limit"]:
                    fills[o["id"]] = (q["ask"][0], ts)
                elif o["qty"] < 0 and q.get("bid") and q["bid"][1] > 0 and q["bid"][0] >= o["limit"]:
                    fills[o["id"]] = (q["bid"][0], ts)
    return fills


def match_futures(orders, trades):
    """{order id: (price, time)} from Eurex trades (eurex.day_trades: product ISIN, contractDate, price)."""
    import eurex
    fills = {}
    for t in sorted(trades, key=lambda t: t["tradingDateAndTime"]):
        if t.get("mmtTradingMode") not in eurex.ON_BOOK or t.get("price") is None:
            continue
        code, ts = eurex.BY_ISIN.get(t["instrumentIdentificationCode"]), t["tradingDateAndTime"][:23]
        for o in orders:
            if o["id"] in fills or o["code"] != code or o.get("contract") != t["contractDate"] or ts < o["at"][:23]:
                continue
            if (o["qty"] > 0 and t["price"] < o["limit"]) or (o["qty"] < 0 and t["price"] > o["limit"]):
                fills[o["id"]] = (o["limit"], ts)
    return fills


def run(doc, day, bond_files, fut_trades, now=None):
    """Applies one trading day to the portfolio document: fills become positions, orders past their last day
    expire. Returns (new doc, number filled, number expired)."""
    orders = doc.get("orders", [])
    act = active(orders, day)
    fills = {**match_bonds([o for o in act if o["kind"] == "bond"], bond_files),
             **match_futures([o for o in act if o["kind"] == "future"], fut_trades)}
    settle = an.add_business_days(dt.date.fromisoformat(day), 2).isoformat()
    positions, n_exp = list(doc.get("positions", [])), 0
    out = []
    for o in orders:
        o = dict(o)
        if o.get("status") == "working" and o["id"] in fills:
            px, ts = fills[o["id"]]
            o.update(status="filled", fill_px=px, fill_at=ts)
            base = dict(id=f"{o['id']}f", qty=o["qty"], price=px, date=day, by=o.get("by"), at=ts[:19] + "Z", order=o["id"])
            if o.get("j"):   # the trade journal (thesis, target, stop, catalyst) follows the order to the position
                base["j"] = o["j"]
            positions.append({**base, "kind": "bond", "isin": o["isin"], "settle": settle} if o["kind"] == "bond"
                             else {**base, "kind": "future", "code": o["code"], "contract": o.get("contract")})
        elif o.get("status") == "working" and o.get("expires", "") <= day:
            o["status"] = "expired"; n_exp += 1
        out.append(o)
    return {**doc, "positions": positions, "orders": out}, len(fills), n_exp
