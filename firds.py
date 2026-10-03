#!/usr/bin/env python3
"""
FIRDS referensdata -> SQLite.

Hämtar senaste fullständiga FIRDS-filerna (FULINS) för en CFI-kategori från ESMA
och bygger en deduplicerad tabell med en rad per ISIN.

  python firds.py --cat D            # skuldinstrument (obligationer m.m.)
  python firds.py --cat F            # terminer (för att identifiera Eurex-hedgar)
  python firds.py --cat D --date 2026-10-03 --db firds.sqlite

ESMA publicerar FULINS en gång i veckan (lördag) och dagliga deltor (DLTINS).
För klubbens behov räcker veckovis fullfil. Kräver nätverksåtkomst till
registers.esma.europa.eu och firds.esma.europa.eu.
"""
import argparse, datetime as dt, io, json, os, sqlite3, subprocess, sys, urllib.request, zipfile
from lxml import etree

SOLR = "https://registers.esma.europa.eu/solr/esma_registers_firds_files/select"
NS = "{urn:iso:std:iso:20022:tech:xsd:auth.017.001.02}"


def list_fulins(cat, date=None, lookback_days=10):
    """Returnerar (publiceringsdatum, [download_links]) för senaste FULINS-uppsättningen."""
    end = dt.date.fromisoformat(date) if date else dt.date.today()
    start = end - dt.timedelta(days=lookback_days)
    q = (f"?q=*&fq=file_type:FULINS&fq=publication_date:%5B{start}T00:00:00Z+TO+{end}T23:59:59Z%5D"
         f"&wt=json&rows=500&fl=file_name,publication_date,download_link")
    docs = json.load(urllib.request.urlopen(SOLR + q, timeout=60))["response"]["docs"]
    docs = [d for d in docs if d["file_name"].startswith(f"FULINS_{cat}_")]
    if not docs:
        raise SystemExit(f"Ingen FULINS_{cat} hittad {start}..{end}")
    latest = max(d["publication_date"] for d in docs)
    return latest[:10], sorted(d["download_link"] for d in docs if d["publication_date"] == latest)


def download(url, folder):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        print("hämtar", url, file=sys.stderr)
        tmp = path + ".part"
        with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(tmp, path)
    return path


def _t(el, path):
    x = el.find(_p(path))
    return x.text if x is not None else None


def _p(path):
    return "/".join(NS + p for p in path.split("/"))


def parse_records(zip_path):
    """Strömmar <RefData>-poster ur en FULINS-zip utan att läsa in hela XML:en."""
    with zipfile.ZipFile(zip_path) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            for _, el in etree.iterparse(fh, tag=NS + "RefData", huge_tree=True):
                g = el.find(_p("FinInstrmGnlAttrbts"))
                rec = {
                    "isin": _t(g, "Id"),
                    "full_name": _t(g, "FullNm"),
                    "fisn": _t(g, "ShrtNm"),
                    "cfi": _t(g, "ClssfctnTp"),
                    "ccy": _t(g, "NtnlCcy"),
                    "issuer_lei": _t(el, "Issr"),
                    "venue": _t(el, "TradgVnRltdAttrbts/Id"),
                    "rca": _t(el, "TechAttrbts/RlvntCmptntAuthrty"),
                }
                d = el.find(_p("DebtInstrmAttrbts"))
                if d is not None:
                    amt = d.find(_p("TtlIssdNmnlAmt"))
                    rec.update(
                        maturity=_t(d, "MtrtyDt"),
                        issued_amt=float(amt.text) if amt is not None else None,
                        issued_ccy=amt.get("Ccy") if amt is not None else None,
                        nominal_unit=_float(_t(d, "NmnlValPerUnit")),
                        coupon_fixed=_float(_t(d, "IntrstRate/Fxd")),
                        float_index=_t(d, "IntrstRate/Fltg/RefRate/Indx") or _t(d, "IntrstRate/Fltg/RefRate/Nm"),
                        float_spread_bp=_float(_t(d, "IntrstRate/Fltg/BsisPtSprd")),
                        seniority=_t(d, "DebtSnrty"),
                    )
                v = el.find(_p("DerivInstrmAttrbts"))
                if v is not None:
                    rec.update(
                        expiry=_t(v, "XpryDt"),
                        multiplier=_float(_t(v, "PricMltplr")),
                        underlying_isin=_t(v, "UndrlygInstrm/Sngl/ISIN"),
                        underlying_index=(_t(v, "UndrlygInstrm/Sngl/Indx/Nm/RefRate/Nm")
                                          or _t(v, "UndrlygInstrm/Sngl/Indx/Nm/RefRate/Indx")
                                          or _t(v, "UndrlygInstrm/Sngl/Indx/ISIN")),
                        delivery=_t(v, "DlvryTp"),
                    )
                yield rec
                el.clear()
                while el.getprevious() is not None:
                    del el.getparent()[0]


def _float(s):
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


DEBT_COLS = ["isin", "full_name", "fisn", "cfi", "ccy", "issuer_lei", "rca", "maturity", "issued_amt",
             "issued_ccy", "nominal_unit", "coupon_fixed", "float_index", "float_spread_bp", "seniority"]
DERIV_COLS = ["isin", "full_name", "fisn", "cfi", "ccy", "issuer_lei", "rca", "expiry", "multiplier",
              "underlying_isin", "underlying_index", "delivery"]


def build(cat, date, db, folder):
    pub, links = list_fulins(cat, date)
    cols = DEBT_COLS if cat == "D" else DERIV_COLS
    table = f"firds_{cat.lower()}"
    con = sqlite3.connect(db)
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(f"CREATE TABLE {table} ({', '.join(c + ' TEXT' for c in cols)}, venues TEXT, n_venues INT,"
                f" firds_date TEXT, PRIMARY KEY(isin))")
    seen = {}
    for url in links:
        zp = download(url, folder)
        n = 0
        for r in parse_records(zp):
            n += 1
            isin = r["isin"]
            if isin in seen:
                seen[isin][1].add(r["venue"])
                continue
            seen[isin] = ([r.get(c) for c in cols], {r["venue"]})
        print(f"{os.path.basename(zp)}: {n:,} poster, {len(seen):,} unika ISIN hittills", file=sys.stderr)
    rows = [vals + [",".join(sorted(v for v in ven if v)), len(ven), pub] for vals, ven in seen.values()]
    con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * (len(cols) + 3))})", rows)
    con.commit()
    print(f"{table}: {len(rows):,} ISIN från FIRDS {pub} -> {db}", file=sys.stderr)
    return con


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="D", help="CFI-kategori: D=skuld, F=terminer, O=optioner ...")
    ap.add_argument("--date", help="senaste publiceringsdatum att söka bakåt från (YYYY-MM-DD)")
    ap.add_argument("--db", default="firds.sqlite")
    ap.add_argument("--folder", default="firds_raw")
    a = ap.parse_args()
    build(a.cat.upper(), a.date, a.db, a.folder)
