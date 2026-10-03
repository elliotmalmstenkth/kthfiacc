#!/usr/bin/env python3
"""
Eurex-terminer för hedgning: daglig prissammanställning ur DEUR-posttrade (mfs.py-arkivet).

  python eurex.py daily 2026-10-02                 # -> futures_daily i marks.sqlite
  python eurex.py daily 2026-10-02 --show
  python eurex.py products                         # visa produkttabellen

Eurex rapporterar affärer med PRODUKTENS ISIN (t.ex. DE0009652644 = Euro-Bund) plus
contractDate (förfallodag), inte med kontraktets ISIN i FIRDS (DE000F...). Produkt-ISIN
nedan är hämtade från produktsidorna på eurex.com (okt 2026).

Flaggor i posttrade:
  mmtTradingMode     2 = kontinuerlig handel (orderbok), 5 = off-book (block/TES),
                     O = öppningsauktion, K = stängningsauktion
  mmtModificationInd C = makulering (affären med samma transactionIdentificationCode räknas bort)
'last' är senaste orderboksaffär (2/O/K) – en approximation av Eurex dagliga avräkningskurs,
som inte finns i MiFID-filerna.
"""
import argparse, gzip, json, os, sqlite3, sys
from collections import defaultdict

import mfs

# kod: (produkt-ISIN, namn, typ, valuta, värde per indexpunkt/procentenhet)
PRODUCTS = {
    # statsobligationsterminer: nominellt 100 000, kurs i procent -> 1 000 per punkt
    "FGBS": ("DE0009652669", "Euro-Schatz (DE 1,75–2,25 år)", "govt", "EUR", 1000),
    "FGBM": ("DE0009652651", "Euro-Bobl (DE 4,5–5,5 år)", "govt", "EUR", 1000),
    "FGBL": ("DE0009652644", "Euro-Bund (DE 8,5–10,5 år)", "govt", "EUR", 1000),
    "FGBX": ("DE0009652636", "Euro-Buxl (DE 24–35 år)", "govt", "EUR", 1000),
    "FBTS": ("DE000A1EZJ09", "Short-Term Euro-BTP", "govt", "EUR", 1000),
    "FBTM": ("DE000A1KQR10", "Mid-Term Euro-BTP", "govt", "EUR", 1000),
    "FBTP": ("DE000A0ZW3V8", "Long-Term Euro-BTP", "govt", "EUR", 1000),
    "FOAM": ("DE000A1RRP48", "Mid-Term Euro-OAT", "govt", "EUR", 1000),
    "FOAT": ("DE000A1MAPW3", "Euro-OAT", "govt", "EUR", 1000),
    "FBON": ("DE000A163W29", "Euro-BONO", "govt", "EUR", 1000),
    "FBEU": ("DE000A3ETB78", "Euro-EU Bond", "govt", "EUR", 1000),
    "CONF": ("CH0002741988", "CONF (Schweiz)", "govt", "CHF", 1000),
    # kreditindexterminer: kontant avräkning, multiplikator från FIRDS
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
        vwap REAL, PRIMARY KEY(date, code, contract))""")
    return con


def day_trades(date, archive=mfs.ARCHIVE_DEFAULT, feed="DEUR-posttrade"):
    """Affärer för produkterna i PRODUCTS. Använder dagsfilen om den finns, annars minutfilerna."""
    folder = os.path.join(archive, feed, date)
    daily = os.path.join(folder, f"{feed}-daily-{date}.json.gz")
    paths = [daily] if os.path.exists(daily) else [p for _, p in mfs.day_files(archive, feed, date)]
    if not paths:
        raise SystemExit(f"inga {feed}-filer för {date} i {archive}/")
    trades, cancelled = [], set()
    for p in paths:
        for m in mfs.iter_messages(p):
            if m.get("messageId") != "posttrade" or m.get("instrumentIdentificationCode") not in BY_ISIN:
                continue
            if m.get("optionCategory"):  # optioner på samma produkt-ISIN
                continue
            if m.get("mmtModificationInd") == "C":
                cancelled.add(m.get("transactionIdentificationCode"))
                continue
            trades.append(m)
    return [t for t in trades if t.get("transactionIdentificationCode") not in cancelled]


def build_daily(date, archive=mfs.ARCHIVE_DEFAULT, db=mfs.DB_DEFAULT):
    agg = defaultdict(list)
    for t in day_trades(date, archive):
        agg[(BY_ISIN[t["instrumentIdentificationCode"]], t["contractDate"])].append(t)
    rows = []
    for (code, contract), ts in sorted(agg.items()):
        ts.sort(key=lambda t: t["tradingDateAndTime"])
        book = [t for t in ts if t.get("mmtTradingMode") in ON_BOOK and t.get("price") is not None]
        px = [t["price"] for t in book]
        lots = sum(t["quantity"] for t in book)
        vwap = sum(t["price"] * t["quantity"] for t in book) / lots if lots else None
        rows.append((date, code, contract, PRODUCTS[code][3], len(ts), lots,
                     sum(t["quantity"] for t in ts if t.get("mmtTradingMode") == "5"),
                     px[0] if px else None, max(px, default=None), min(px, default=None), px[-1] if px else None,
                     book[-1]["tradingDateAndTime"][:19] if book else None, vwap))
    con = connect(db)
    con.execute("DELETE FROM futures_daily WHERE date=?", (date,))
    con.executemany("INSERT INTO futures_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    print(f"{date}: {len(rows)} kontrakt i {len({r[1] for r in rows})} produkter -> {db}", file=sys.stderr)
    return rows


def show(rows):
    print(f"{'kod':<5} {'förfall':<10} {'affärer':>8} {'lots':>10} {'block':>8} {'låg':>9} {'hög':>9} {'senast':>9} "
          f"{'senast UTC':>20} {'kontraktsvärde':>16}")
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
