#!/usr/bin/env python3
"""
Eurex futures for hedging: daily price summary from DEUR-posttrade (the mfs.py archive).

  python eurex.py daily 2026-10-02                 # -> futures_daily in marks.sqlite
  python eurex.py daily 2026-10-02 --show
  python eurex.py products                         # print the product table

Eurex reports trades with the PRODUCT ISIN (e.g. DE0009652644 = Euro-Bund) plus contractDate
(expiry), not with the contract ISIN in FIRDS (DE000F...). The product ISINs below are taken
from the product pages on eurex.com (Oct 2026).

Post-trade flags:
  mmtTradingMode     2 = continuous trading (on-book), 5 = off-book (block/TES),
                     O = opening auction, K = closing auction
  mmtModificationInd C = cancellation (the trade with the same transactionIdentificationCode is removed)
'last' is the final on-book trade (2/O/K), a proxy for the Eurex daily settlement price,
which is not in the MiFID files.
"""
import argparse, datetime as dt, gzip, json, os, sqlite3, sys
from collections import defaultdict

import mfs

# code: (product ISIN, name, type, currency, value per index point / price point)
PRODUCTS = {
    # government bond futures: EUR 100,000 notional, quoted in % of par -> 1,000 per point
    "FGBS": ("DE0009652669", "Euro-Schatz (DE 1.75–2.25y)", "govt", "EUR", 1000),
    "FGBM": ("DE0009652651", "Euro-Bobl (DE 4.5–5.5y)", "govt", "EUR", 1000),
    "FGBL": ("DE0009652644", "Euro-Bund (DE 8.5–10.5y)", "govt", "EUR", 1000),
    "FGBX": ("DE0009652636", "Euro-Buxl (DE 24–35y)", "govt", "EUR", 1000),
    "FBTS": ("DE000A1EZJ09", "Short-Term Euro-BTP", "govt", "EUR", 1000),
    "FBTM": ("DE000A1KQR10", "Mid-Term Euro-BTP", "govt", "EUR", 1000),
    "FBTP": ("DE000A0ZW3V8", "Long-Term Euro-BTP", "govt", "EUR", 1000),
    "FOAM": ("DE000A1RRP48", "Mid-Term Euro-OAT", "govt", "EUR", 1000),
    "FOAT": ("DE000A1MAPW3", "Euro-OAT", "govt", "EUR", 1000),
    "FBON": ("DE000A163W29", "Euro-BONO", "govt", "EUR", 1000),
    "FBEU": ("DE000A3ETB78", "Euro-EU Bond", "govt", "EUR", 1000),
    "CONF": ("CH0002741988", "CONF (Swiss Confederation)", "govt", "CHF", 1000),
    # short-term interest rate futures: EUR 1m for 3 months, price = 100 - rate -> 2,500 per point (stir.py).
    # ISINs identified from the trades (prices 100 - rate, monthly/IMM expiries), Oct 2026.
    "FEU3": ("DE0009653147", "3-Month Euribor", "stir", "EUR", 2500),
    "FST3": ("DE000A3CNW06", "3-Month €STR", "stir", "EUR", 2500),
    # other products used for the markets view and as hedges (ISINs checked on eurex.com, Oct 2026):
    # FCEU EUR 100,000 against USD (price = USD per EUR, so the P&L is in dollars); FVS EUR 100 per VSTOXX point;
    # FESB EUR 50 per EURO STOXX Banks point
    "FCEU": ("DE000A1N53R4", "EUR/USD", "fx", "USD", 100000),
    "FVS": ("DE000A0Z3CW9", "VSTOXX (EURO STOXX 50 volatility)", "vol", "EUR", 100),
    "FESB": ("DE0005705651", "EURO STOXX Banks", "equity", "EUR", 50),
    # credit index futures: cash-settled, multiplier from FIRDS
    "FECX": ("DE000A2QQU00", "Bloomberg MSCI Euro Corporate Screened", "credit", "EUR", 1000),
    "FEHY": ("DE000A3DLQ96", "Bloomberg Liquidity Screened Euro High Yield", "credit", "EUR", 200),
    "FGBC": ("DE000A3EXVZ9", "Bloomberg Sterling Liquid Corporate", "credit", "GBP", 200),
    "FGGI": ("DE000A2QQU18", "Bloomberg MSCI Global Green Bond", "credit", "EUR", 1000),
    "FUIG": ("DE000A4AFJ46", "Bloomberg US Corporate", "credit", "USD", 25),
    "FUHY": ("DE000A4AFJ53", "Bloomberg US High Yield Very Liquid", "credit", "USD", 100),
    "FUEM": ("DE000A3EXW02", "Bloomberg EM USD Sovereign + Sov-Owned", "credit", "USD", 200),
}
BY_ISIN = {v[0]: k for k, v in PRODUCTS.items()}
ON_BOOK = {"2", "O", "K"}


def connect(db=mfs.DB_DEFAULT):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS futures_daily(date TEXT, code TEXT, contract TEXT, ccy TEXT,
        trades INT, lots REAL, block_lots REAL, open REAL, high REAL, low REAL, last REAL, last_time TEXT,
        vwap REAL, cal_spread REAL, cal_pairs INT, PRIMARY KEY(date, code, contract))""")
    return con


def day_trades(date, archive=mfs.ARCHIVE_DEFAULT, feed="DEUR-posttrade"):
    """Trades for the products in PRODUCTS. Uses the daily file if present, otherwise the minute files."""
    folder = os.path.join(archive, feed, date)
    daily = os.path.join(folder, f"{feed}-daily-{date}.json.gz")
    paths = [daily] if os.path.exists(daily) else [p for _, p in mfs.day_files(archive, feed, date)]
    if not paths:
        raise SystemExit(f"no {feed} files for {date} in {archive}/")
    trades, cancelled = [], set()
    for p in paths:
        for m in mfs.iter_messages(p):
            if m.get("messageId") != "posttrade" or m.get("instrumentIdentificationCode") not in BY_ISIN:
                continue
            if m.get("optionCategory"):  # options on the same product ISIN
                continue
            if m.get("mmtModificationInd") == "C":
                cancelled.add(m.get("transactionIdentificationCode"))
                continue
            trades.append(m)
    return [t for t in trades if t.get("transactionIdentificationCode") not in cancelled]


def calendar_spread(near, far, max_gap=60):
    """Median of (far price − near price) over the far contract's trades that have a near-contract trade within
    max_gap seconds: (spread, number of pairs), or (None, 0)."""
    import bisect, statistics
    ts = lambda t: dt.datetime.fromisoformat(t["tradingDateAndTime"][:19]).timestamp()
    nt = [ts(t) for t in near]
    diffs = []
    for t in far:
        x = ts(t)
        i = bisect.bisect_left(nt, x)
        best = min((j for j in (i - 1, i) if 0 <= j < len(nt)), key=lambda j: abs(nt[j] - x), default=None)
        if best is not None and abs(nt[best] - x) <= max_gap:
            diffs.append(t["price"] - near[best]["price"])
    return (statistics.median(diffs), len(diffs)) if diffs else (None, 0)


def build_daily(date, archive=mfs.ARCHIVE_DEFAULT, db=mfs.DB_DEFAULT):
    agg = defaultdict(list)
    for t in day_trades(date, archive):
        agg[(BY_ISIN[t["instrumentIdentificationCode"]], t["contractDate"])].append(t)
    rows, books = [], {}
    for (code, contract), ts in sorted(agg.items()):
        ts.sort(key=lambda t: t["tradingDateAndTime"])
        book = [t for t in ts if t.get("mmtTradingMode") in ON_BOOK and t.get("price") is not None]
        px = [t["price"] for t in book]
        lots = sum(t["quantity"] for t in book)
        vwap = sum(t["price"] * t["quantity"] for t in book) / lots if lots else None
        books[(code, contract)] = book
        rows.append([date, code, contract, PRODUCTS[code][3], len(ts), lots,
                     sum(t["quantity"] for t in ts if t.get("mmtTradingMode") == "5"),
                     px[0] if px else None, max(px, default=None), min(px, default=None), px[-1] if px else None,
                     book[-1]["tradingDateAndTime"][:19] if book else None, vwap, None, 0])
    # calendar spread of each later contract over the front one, from trades close in time (EUR/USD: the
    # forward points between the two delivery dates; bond futures: the roll)
    for row in rows:
        code, contract = row[1], row[2]
        front = min((c for (k, c) in books if k == code and books[(k, c)]), default=None)
        if front is None or contract == front:
            continue
        row[13], row[14] = calendar_spread(books[(code, front)], books[(code, contract)])
    con = connect(db)
    con.execute("DELETE FROM futures_daily WHERE date=?", (date,))
    con.executemany("INSERT INTO futures_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    print(f"{date}: {len(rows)} contracts in {len({r[1] for r in rows})} products -> {db}", file=sys.stderr)
    return rows


def show(rows):
    print(f"{'code':<5} {'expiry':<10} {'trades':>8} {'lots':>10} {'block':>8} {'low':>9} {'high':>9} {'last':>9} "
          f"{'last UTC':>20} {'contract value':>16}")
    for r in rows:
        if r[10] is None and not r[6]:
            continue
        val = f"{r[10] * PRODUCTS[r[1]][4]:,.0f} {r[3]}" if r[10] is not None else ""
        fmt = lambda x: f"{x:>9.3f}" if x is not None else f"{'':>9}"
        print(f"{r[1]:<5} {r[2]:<10} {r[4]:>8,} {r[5]:>10,.0f} {r[6]:>8,.0f} {fmt(r[9])} {fmt(r[8])} {fmt(r[10])} "
              f"{r[11] or '':>20} {val:>16}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=mfs.ARCHIVE_DEFAULT)
    ap.add_argument("--db", default=mfs.DB_DEFAULT)
    sp = ap.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("daily"); d.add_argument("dates", nargs="+"); d.add_argument("--show", action="store_true")
    sp.add_parser("products")
    a = ap.parse_args()
    if a.cmd == "products":
        for k, (isin, name, typ, ccy, mult) in PRODUCTS.items():
            print(f"{k:<5} {isin}  {typ:<6} {ccy} {mult:>5}/punkt  {name}")
    else:
        for dd in a.dates:
            rows = build_daily(dd, a.archive, a.db)
            if a.show:
                show(rows)
