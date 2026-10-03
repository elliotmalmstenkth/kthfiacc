#!/usr/bin/env python3
"""
ECB:s euroområdeskurvor (dataset YC) – arkivering och hämtning.

Kurvor:
  AAA = G_N_A  statsobligationer från AAA-länder (riskfri referens)
  ALL = G_N_C  alla euroländers statsobligationer (för landspreadar)

ECB skattar en Svensson-modell varje TARGET-dag och publicerar dagen efter
(ca 12:00 CET). Räntor är i procent, kontinuerlig räntesats.

Arkivet (SQLite) innehåller:
  params  – BETA0..BETA3, TAU1, TAU2 per dag och kurva (full historik sedan 2004-09-06)
  grid    – ECB:s publicerade spot-, termins- och parräntor för standardlöptider
            (används för att kontrollera rekonstruktionen och som facit)
  fetches – logg över varje hämtning
Råsvaren sparas gzippade i raw/ för spårbarhet (ECB kan revidera).

  python ecb_curve.py update                 # inkrementell uppdatering (kör dagligen)
  python ecb_curve.py update --full          # hela historiken
  python ecb_curve.py show 2026-10-01        # spot/termin/par för standardlöptider
  python ecb_curve.py show 2026-10-01 --curve ALL --tenors 0.5 2 5 10 30
  python ecb_curve.py check                  # rekonstruktion mot ECB:s egna värden

Som modul:
  from ecb_curve import Curve
  c = Curve.load("2026-10-01")               # senaste kurva <= datum
  c.spot(7.5), c.forward(10), c.par(5), c.df(3.25)
"""
import argparse, csv, datetime as dt, gzip, io, math, os, sqlite3, sys, urllib.request

API = "https://data-api.ecb.europa.eu/service/data/YC"
CURVES = {"AAA": "G_N_A", "ALL": "G_N_C"}
PARAMS = ["BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2"]
GRID_TENORS = ["3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y", "7Y", "10Y", "15Y", "20Y", "25Y", "30Y"]
GRID_TYPES = ["SR", "IF", "PY"]  # spot, instantaneous forward, par
DB_DEFAULT = os.environ.get("ECB_CURVE_DB", "ecb_curve.sqlite")


# ---------------------------------------------------------------- hämtning
def _get(series_keys, curve_code, start=None, end=None):
    key = "B.U2.EUR.4F.{}.SV_C_YM.{}".format(curve_code, "%2B".join(series_keys))
    q = ["format=csvdata"]
    if start: q.append(f"startPeriod={start}")
    if end: q.append(f"endPeriod={end}")
    url = f"{API}/{key}?{'&'.join(q)}"
    req = urllib.request.Request(url, headers={"Accept": "text/csv", "User-Agent": "kth-fic-club/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return url, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:  # inga nya observationer
                return url, b""
            err = e
        except Exception as e:  # nätverksfel: försök igen
            err = e
        import time; time.sleep(2 ** attempt)
    raise RuntimeError(f"ECB-hämtning misslyckades: {url}: {err}")


def _parse(raw):
    if not raw.strip():
        return []
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    out = []
    for r in rows:
        if r.get("OBS_VALUE") in (None, "", "NaN"):
            continue
        out.append((r["TIME_PERIOD"], r["DATA_TYPE_FM"], float(r["OBS_VALUE"])))
    return out


def tenor_years(code):
    """'10Y6M' -> 10.5, '3M' -> 0.25"""
    y = m = 0
    num = ""
    for ch in code:
        if ch.isdigit():
            num += ch
        elif ch == "Y":
            y, num = int(num), ""
        elif ch == "M":
            m, num = int(num), ""
    return y + m / 12


def connect(db=DB_DEFAULT):
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS params(date TEXT, curve TEXT, beta0 REAL, beta1 REAL, beta2 REAL, beta3 REAL,
        tau1 REAL, tau2 REAL, fetched_at TEXT, PRIMARY KEY(date, curve));
    CREATE TABLE IF NOT EXISTS grid(date TEXT, curve TEXT, type TEXT, tenor TEXT, years REAL, value REAL,
        PRIMARY KEY(date, curve, type, tenor));
    CREATE TABLE IF NOT EXISTS fetches(fetched_at TEXT, curve TEXT, start TEXT, n_days INT, url TEXT, raw_file TEXT);
    """)
    return con


def update(db=DB_DEFAULT, full=False, overlap_days=10, raw_dir="raw"):
    """Hämtar nya dagar (+ några dagars överlapp så att ECB-revisioner fångas)."""
    con = connect(db)
    os.makedirs(raw_dir, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name, code in CURVES.items():
        last = con.execute("SELECT max(date) FROM params WHERE curve=?", (name,)).fetchone()[0]
        start = None if (full or not last) else (dt.date.fromisoformat(last) - dt.timedelta(days=overlap_days)).isoformat()
        # parametrar
        url, raw = _get(PARAMS, code, start)
        fn = os.path.join(raw_dir, f"YC_{name}_params_{now[:10]}_{start or 'full'}.csv.gz")
        with gzip.open(fn, "wb") as f:
            f.write(raw)
        by_day = {}
        for d, k, v in _parse(raw):
            by_day.setdefault(d, {})[k] = v
        rows = [(d, name, *[p[k] for k in PARAMS], now) for d, p in sorted(by_day.items()) if all(k in p for k in PARAMS)]
        con.executemany("INSERT OR REPLACE INTO params VALUES (?,?,?,?,?,?,?,?,?)", rows)
        # standardgrid
        keys = [f"{t}_{x}" for t in GRID_TYPES for x in GRID_TENORS]
        url2, raw2 = _get(keys, code, start)
        with gzip.open(fn.replace("_params_", "_grid_"), "wb") as f:
            f.write(raw2)
        g = [(d, name, k.split("_")[0], k.split("_")[1], tenor_years(k.split("_")[1]), v) for d, k, v in _parse(raw2)]
        con.executemany("INSERT OR REPLACE INTO grid VALUES (?,?,?,?,?,?)", g)
        con.execute("INSERT INTO fetches VALUES (?,?,?,?,?,?)", (now, name, start, len(rows), url, fn))
        con.commit()
        rng = f"{rows[0][0]}..{rows[-1][0]}" if rows else "inga nya"
        print(f"{name}: {len(rows)} dagar ({rng}), {len(g)} gridpunkter", file=sys.stderr)
    return con


# ---------------------------------------------------------------- Svensson
class Curve:
    """Svensson-kurva. Löptid i år, räntor i procent (kontinuerlig räntesats)."""

    def __init__(self, date, curve, b0, b1, b2, b3, t1, t2):
        self.date, self.curve = date, curve
        self.b0, self.b1, self.b2, self.b3, self.t1, self.t2 = b0, b1, b2, b3, t1, t2

    @classmethod
    def load(cls, date=None, curve="AAA", db=DB_DEFAULT):
        con = connect(db)
        q = "SELECT date, curve, beta0, beta1, beta2, beta3, tau1, tau2 FROM params WHERE curve=?"
        args = [curve]
        if date:
            q += " AND date<=?"; args.append(str(date))
        r = con.execute(q + " ORDER BY date DESC LIMIT 1", args).fetchone()
        if not r:
            raise LookupError(f"Ingen {curve}-kurva på eller före {date}. Kör 'update' först.")
        return cls(*r)

    @staticmethod
    def _l(m, t):
        x = m / t
        return 1.0 if x < 1e-10 else (1 - math.exp(-x)) / x

    def spot(self, m):
        """Nollkupongränta (%, kontinuerlig) för löptid m år."""
        m = max(m, 1e-6)
        l1, l2 = self._l(m, self.t1), self._l(m, self.t2)
        return (self.b0 + self.b1 * l1 + self.b2 * (l1 - math.exp(-m / self.t1))
                + self.b3 * (l2 - math.exp(-m / self.t2)))

    def forward(self, m):
        """Momentan terminsränta (%) vid m år."""
        e1, e2 = math.exp(-m / self.t1), math.exp(-m / self.t2)
        return self.b0 + self.b1 * e1 + self.b2 * (m / self.t1) * e1 + self.b3 * (m / self.t2) * e2

    def df(self, m):
        """Diskonteringsfaktor."""
        return math.exp(-self.spot(m) / 100 * m)

    def par(self, m, freq=None, steps=400):
        """Parränta (%).
        freq=None: ECB:s definition (kontinuerligt betald kupong): (1-DF(m)) / integral_0^m DF(s) ds.
                   Matchar ECB:s publicerade PY-serier exakt.
        freq=1/2/4: kupong freq ggr per år med kupongdatum räknade bakåt från förfall
                   (första perioden får kortare längd); kupongen anges per år.
                   Använd freq=1 för vanliga EUR-obligationer."""
        if freq is None:
            h = m / steps  # Simpson
            s = self.df(1e-9) + self.df(m) + sum((4 if i % 2 else 2) * self.df(i * h) for i in range(1, steps))
            return (1 - self.df(m)) / (s * h / 3) * 100
        times, t = [], m
        while t > 1e-9:
            times.append(t); t -= 1 / freq
        times = times[::-1]
        lengths = [times[0]] + [1 / freq] * (len(times) - 1)
        annuity = sum(self.df(ti) * li for ti, li in zip(times, lengths))
        return (1 - self.df(m)) / annuity * 100

    def forward_rate(self, t1, t2):
        """Terminsränta (%, kontinuerlig) mellan t1 och t2 år, t.ex. 5y5y = forward_rate(5, 10)."""
        return (self.spot(t2) * t2 - self.spot(t1) * t1) / (t2 - t1)

    def __repr__(self):
        return f"Curve({self.curve} {self.date})"


# ---------------------------------------------------------------- CLI
def cmd_show(a):
    c = Curve.load(a.date, a.curve, a.db)
    tenors = a.tenors or [0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30]
    print(f"{c.curve}-kurvan {c.date}  (Svensson: b0={c.b0:.4f} b1={c.b1:.4f} b2={c.b2:.4f} b3={c.b3:.4f} "
          f"t1={c.t1:.4f} t2={c.t2:.4f})")
    print(f"{'år':>6} {'spot %':>9} {'termin %':>9} {'par %':>9} {'par årl %':>10} {'DF':>9}")
    for m in tenors:
        print(f"{m:>6g} {c.spot(m):>9.4f} {c.forward(m):>9.4f} {c.par(m):>9.4f} {c.par(m, 1):>10.4f} {c.df(m):>9.6f}")
    print(f"5y5y termin: {c.forward_rate(5, 10):.4f} %   2s10s: {(c.spot(10) - c.spot(2)) * 100:.1f} bp")


def cmd_check(a):
    con = connect(a.db)
    rows = con.execute("""SELECT g.date, g.curve, g.type, g.tenor, g.years, g.value FROM grid g
                          WHERE g.date >= (SELECT date(max(date), '-30 day') FROM grid)""").fetchall()
    worst = {}
    cache = {}
    for d, cv, t, ten, y, v in rows:
        c = cache.get((d, cv)) or cache.setdefault((d, cv), Curve.load(d, cv, a.db))
        if c.date != d:
            continue
        mine = {"SR": c.spot, "IF": c.forward, "PY": c.par}[t](y)
        err = abs(mine - v) * 100  # bp
        k = (cv, t)
        worst[k] = max(worst.get(k, 0), err)
    for (cv, t), e in sorted(worst.items()):
        print(f"{cv} {t}: största avvikelse mot ECB senaste 30 dagar = {e:.4f} bp")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_DEFAULT)
    sp = ap.add_subparsers(dest="cmd", required=True)
    u = sp.add_parser("update"); u.add_argument("--full", action="store_true"); u.add_argument("--raw-dir", default="raw")
    s = sp.add_parser("show"); s.add_argument("date", nargs="?"); s.add_argument("--curve", default="AAA", choices=CURVES)
    s.add_argument("--tenors", nargs="*", type=float)
    sp.add_parser("check")
    a = ap.parse_args()
    if a.cmd == "update":
        update(a.db, a.full, raw_dir=a.raw_dir)
    elif a.cmd == "show":
        cmd_show(a)
    else:
        cmd_check(a)
