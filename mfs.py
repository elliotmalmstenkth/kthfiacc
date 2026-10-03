#!/usr/bin/env python3
"""
Deutsche Börse MiFID II-filer (mfs.deutsche-boerse.com) – arkivering och dagliga kurser.

Filerna (en per minut, JSON-rader i .gz) ligger bara kvar till midnatt nästa
bankdag, så 'sync' måste köras minst en gång per bankdag (helst kväll + morgon).

  python mfs.py list DFRA-pretrade                 # vad servern har just nu
  python mfs.py sync                               # hämta allt nytt för standardflödena
  python mfs.py sync --feeds DFRA-pretrade DFRA-posttrade DEUR-posttrade
  python mfs.py marks 2026-10-02                   # bygg kurstabell för en dag ur arkivet
  python mfs.py marks 2026-10-02 --snap 17:30          # Frankfurttid
  python mfs.py sync --bonds-only                  # pre-trade: bara obligationsrader
  python mfs.py prune --feed DFRA-pretrade --keep-days 14   # radera rådata för dagar som redan har marks

Arkivstruktur:  archive/<flöde>/<handelsdag>/<filnamn>.json.gz
  Filnamnens tid är UTC och anger minutens början. Handelsdagen räknas i
  Frankfurttid, så t.ex. DFRA-pretrade-2026-10-01T23_00 hör till 2026-10-02.
  Filerna sparas oförändrade, utom med 'sync --bonds-only' där pre-trade-filer
  bara behåller rader med priceNotation 2 (ca 1/4 av raderna, ~0,5 i st.f. ~2 GB/dag).

Pre-trade-meddelandena är DELTOR: ett meddelande med bara bestAsk betyder att
säljkursen ändrats, inte att budet försvunnit. 'marks' slår därför ihop sidorna
per ISIN (varje sida behåller sitt senaste värde och sin tidsstämpel).
  pris 0 (och qty 0)  = sidan borttagen; vid handelsslut 17:30 får alla obligationer
                        bid = ask = 0 med tradingSystemPhase 202
  pris > 0, qty 0     = indikativ kurs utan volym (vanligt på säljsidan på FRAA)
Ögonblicksbilder:
  --snap HH:MM (Frankfurttid)  aktuella sidor vid tidpunkten (ensidiga tas med)
  close                        senaste tvåsidiga läget under dagen (dagens slutkurs)

  quotes(date, snap, isin, venue, ccy, bid, bid_qty, bid_time, ask, ask_qty, ask_time)
  days(date, feed, n_files, first_file, last_file, max_gap_min, built_at)

Obs: om en sida dras tillbaka utan att det syns i flödet blir den kvar som
gammal kurs; bid_time/ask_time visar hur färsk varje sida är.

API (offentligt, ingen nyckel):
  GET https://mfs.deutsche-boerse.com/api/<flöde>            -> {"CurrentFiles": [...]}
  GET https://mfs.deutsche-boerse.com/api/download/<filnamn>  (301 -> kortlivad Google Storage-länk)
Flöden: DFRA (Börse Frankfurt), DETR (Xetra), DEUR (Eurex), DETG, DGAT; -pretrade/-posttrade.
Vissa flöden har även en '-daily-<datum>'-fil (DEUR-posttrade ja, DFRA-pretrade nej/404).
"""
import argparse, datetime as dt, gzip, json, os, re, shutil, sqlite3, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

BASE = "https://mfs.deutsche-boerse.com/api"
FEEDS_DEFAULT = ["DFRA-pretrade", "DFRA-posttrade", "DEUR-posttrade"]
ARCHIVE_DEFAULT = os.environ.get("MFS_ARCHIVE", "archive")
DB_DEFAULT = os.environ.get("MFS_MARKS_DB", "marks.sqlite")
FNAME_RE = re.compile(r"^(?P<feed>.+)-(?P<date>\d{4}-\d{2}-\d{2})T(?P<hh>\d{2})_(?P<mm>\d{2})\.json\.gz$")
DAILY_RE = re.compile(r"^(?P<feed>.+)-daily-(?P<date>\d{4}-\d{2}-\d{2})\.json\.gz$")
TZ = ZoneInfo("Europe/Berlin")
UA = {"User-Agent": "kth-fic-club/1.0 (non-commercial, academic)"}


# ---------------------------------------------------------------- nätverk
_gate = threading.Lock()
_cooldown_until = 0.0  # delas av alla trådar: vid 429 pausar alla


def _wait_gate():
    while True:
        with _gate:
            left = _cooldown_until - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 5))


def _cool_down(seconds):
    global _cooldown_until
    with _gate:
        _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)


def _open(url, timeout=120, tries=8):
    """GET med omförsök. 429 (Too Many Requests) respekterar Retry-After och pausar alla trådar."""
    err = None
    for attempt in range(tries):
        _wait_gate()
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):  # varken nätverksfel eller tillfälligt: ge upp direkt
                raise
            err = e
            if e.code == 429:
                try:
                    wait = float(e.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    wait = 5 * 2 ** min(attempt, 4)
                _cool_down(wait)
                continue
        except Exception as e:
            err = e
        time.sleep(2 ** min(attempt, 5))
    raise RuntimeError(f"misslyckades: {url}: {err}")


def list_files(feed):
    """Filnamn som servern har för flödet just nu."""
    with _open(f"{BASE}/{feed}", timeout=60) as r:
        data = json.load(r)
    files = data.get("CurrentFiles") if isinstance(data, dict) else data
    if files is None:
        raise RuntimeError(f"oväntat svar för {feed}: nycklar {list(data)[:10]}")
    return sorted(files)


def parse_name(fname):
    """'DFRA-pretrade-2026-10-01T23_00.json.gz' -> ('DFRA-pretrade', '2026-10-02', datetime 23:00 UTC)
    Handelsdagen räknas i Frankfurttid. Dagsfiler ('-daily-<datum>') ger tid None."""
    base = os.path.basename(fname)
    m = DAILY_RE.match(base)
    if m:
        return m["feed"], m["date"], None
    m = FNAME_RE.match(base)
    if not m:
        return None
    t = dt.datetime.fromisoformat(f"{m['date']}T{m['hh']}:{m['mm']}").replace(tzinfo=dt.timezone.utc)
    return m["feed"], t.astimezone(TZ).date().isoformat(), t


def _gzip_ok(path):
    try:
        with gzip.open(path, "rb") as f:
            while f.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def download(fname, path, notation=None):
    """Hämtar en fil. notation=2 behåller bara obligationsrader (priceNotation 2)."""
    tmp = f"{path}.{os.getpid()}.part"  # unikt per process: två samtidiga sync krockar inte
    with _open(f"{BASE}/download/{fname}", timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if not _gzip_ok(tmp):
        os.remove(tmp)
        raise RuntimeError(f"trasig gzip: {fname}")
    if notation is not None:
        tag = re.compile(rb'"priceNotation"\s*:\s*%d\s*[,}]' % notation)
        with gzip.open(tmp, "rb") as src, gzip.open(tmp + "2", "wb", compresslevel=6) as dst:
            for line in src:
                if tag.search(line):
                    dst.write(line)
        os.replace(tmp + "2", tmp)
    os.replace(tmp, path)


def sync(feeds=FEEDS_DEFAULT, archive=ARCHIVE_DEFAULT, workers=4, bonds_only=False, days=None):
    """Hämtar alla filer som servern har och som inte redan finns i arkivet (days: bara dessa handelsdagar).
    Returnerar (nya, misslyckade). 404 räknas som saknad fil, inte som fel."""
    total_new = failed = 0
    for feed in feeds:
        try:
            files = list_files(feed)
        except Exception as e:
            print(f"{feed}: kunde inte lista filer: {e}", file=sys.stderr)
            failed += 1
            continue
        notation = 2 if (bonds_only and feed.endswith("-pretrade")) else None
        todo, skipped = [], 0
        for fn in files:
            p = parse_name(fn)
            if notation is not None and p and p[2] is None:
                continue  # dagsfil = dubblett av minutfilerna; hoppa över i filtrerat läge
            if days is not None and (not p or p[1] not in days):
                continue
            path = os.path.join(archive, feed, p[1] if p else "okänt-datum", fn)
            if os.path.exists(path):
                skipped += 1
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                todo.append((fn, path))

        def fetch(job):
            fn, path = job
            try:
                download(fn, path, notation)
                return "ok", os.path.getsize(path)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return "404", 0
                print(f"  {fn}: {e}", file=sys.stderr)
            except Exception as e:
                print(f"  {fn}: {e}", file=sys.stderr)
            return "fel", 0

        with ThreadPoolExecutor(max(1, workers)) as ex:
            res = list(ex.map(fetch, todo))
        new = sum(r == "ok" for r, _ in res)
        missing = sum(r == "404" for r, _ in res)
        failed += sum(r == "fel" for r, _ in res)
        mb = sum(b for _, b in res) / 1e6
        on_server = sorted({p[1] for f in files if (p := parse_name(f))})
        rng = f"{on_server[0]}..{on_server[-1]}" if on_server else "-"
        print(f"{feed}: {len(files)} filer på servern ({rng}), {new} nya ({mb:,.0f} MB), "
              f"{skipped} fanns redan, {missing} saknas (404)" + (" [bara obligationer]" if notation else ""),
              file=sys.stderr)
        total_new += new
    return total_new, failed


# ---------------------------------------------------------------- dagliga kurser
def connect(db=DB_DEFAULT):
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS quotes(date TEXT, snap TEXT, isin TEXT, venue TEXT, ccy TEXT,
        bid REAL, bid_qty REAL, bid_time TEXT, ask REAL, ask_qty REAL, ask_time TEXT,
        PRIMARY KEY(date, snap, isin));
    CREATE TABLE IF NOT EXISTS days(date TEXT, feed TEXT, n_files INT, first_file TEXT, last_file TEXT,
        max_gap_min REAL, built_at TEXT, PRIMARY KEY(date, feed));
    """)
    return con


def day_files(archive, feed, date):
    """Minutfilerna för en handelsdag i tidsordning (dagsfiler utelämnas)."""
    folder = os.path.join(archive, feed, date)
    if not os.path.isdir(folder):
        return []
    fs = [(p[2], os.path.join(folder, f)) for f in os.listdir(folder) if (p := parse_name(f)) and p[2]]
    return sorted(fs)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def iter_messages(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _snap_utc(date, hhmm):
    """'17:30' Frankfurttid på handelsdagen -> ISO-sträng i UTC jämförbar med updateDateAndTime."""
    t = dt.datetime.fromisoformat(f"{date}T{hhmm}").replace(tzinfo=TZ).astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def build_marks(date, archive=ARCHIVE_DEFAULT, db=DB_DEFAULT, feed="DFRA-pretrade", snaps=(), notation=2):
    """Ögonblicksbilder av bud/sälj per ISIN (deltor ihopslagna). notation=None tar med alla instrument."""
    files = day_files(archive, feed, date)
    if not files:
        raise SystemExit(f"inga {feed}-filer för {date} i {archive}/")
    pending = sorted((_snap_utc(date, s), s) for s in snaps)
    state, out = {}, []
    SIDES = ("bid", "bid_qty", "bid_time", "ask", "ask_qty", "ask_time")

    def take(label, key="live"):
        for isin, q in state.items():
            v = q[key]
            if v is not None:
                out.append((date, label, isin, q["venue"], q["ccy"], *v))

    for _, path in files:
        for m in iter_messages(path):
            if notation is not None and m.get("priceNotation") != notation:
                continue
            isin = m.get("instrumentIdentificationCode")
            if not isin:
                continue
            ts = m.get("updateDateAndTime") or m.get("publicationDateAndTime") or ""
            while pending and ts[:19] >= pending[0][0]:
                take(pending.pop(0)[1])
            q = state.get(isin)
            if q is None:
                q = state[isin] = dict(live=None, last2=None, side={k: None for k in SIDES})
            q["venue"], q["ccy"] = m.get("venueOfExecution"), m.get("priceCurrency")
            sd = q["side"]
            for side, key in (("bid", "bestBid"), ("ask", "bestAsk")):
                if key in m:
                    px = _num(m[key])
                    if px:  # pris 0 = sidan borttagen (t.ex. vid handelsslut, fas 202)
                        sd[side], sd[side + "_qty"], sd[side + "_time"] = px, _num(m.get(key + "Qty")), ts[:23]
                    else:
                        sd[side] = sd[side + "_qty"] = sd[side + "_time"] = None
            vals = tuple(sd[k] for k in SIDES)
            q["live"] = vals if (sd["bid"] is not None or sd["ask"] is not None) else None
            if sd["bid"] is not None and sd["ask"] is not None:
                q["last2"] = vals
    for _, label in pending:  # ögonblicksbilder efter sista meddelandet
        take(label)
    take("close", "last2")

    gaps = [(b[0] - a[0]).total_seconds() / 60 for a, b in zip(files, files[1:])]
    con = connect(db)
    con.execute("DELETE FROM quotes WHERE date=?", (date,))
    con.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    con.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?,?)",
                (date, feed, len(files), os.path.basename(files[0][1]), os.path.basename(files[-1][1]),
                 max(gaps, default=0), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    con.commit()
    close = [r for r in out if r[1] == "close"]
    two = sum(1 for r in close if r[5] is not None and r[8] is not None)
    print(f"{date}: {len(files)} filer ({os.path.basename(files[0][1])} .. {os.path.basename(files[-1][1])}, "
          f"största lucka {max(gaps, default=0):.0f} min), {len(close):,} ISIN vid stängning, {two:,} tvåsidiga "
          f"-> {db}", file=sys.stderr)
    return con


def prune(feed, archive=ARCHIVE_DEFAULT, db=DB_DEFAULT, keep_days=14, today=None):
    """Raderar rådata för dagar äldre än keep_days som redan finns i marks-databasen."""
    con = connect(db)
    built = {d for (d,) in con.execute("SELECT date FROM days WHERE feed=?", (feed,))}
    cutoff = (today or dt.date.today()) - dt.timedelta(days=keep_days)
    root = os.path.join(archive, feed)
    removed = []
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        try:
            day = dt.date.fromisoformat(d)
        except ValueError:
            continue
        if day < cutoff and d in built:
            shutil.rmtree(os.path.join(root, d))
            removed.append(d)
    print(f"{feed}: raderade {len(removed)} dagar {removed[:1]}..{removed[-1:]}", file=sys.stderr)
    return removed


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=ARCHIVE_DEFAULT)
    ap.add_argument("--db", default=DB_DEFAULT)
    sp = ap.add_subparsers(dest="cmd", required=True)
    l = sp.add_parser("list"); l.add_argument("feed")
    s = sp.add_parser("sync"); s.add_argument("--feeds", nargs="+", default=FEEDS_DEFAULT)
    s.add_argument("--bonds-only", action="store_true", help="pre-trade: spara bara priceNotation 2-rader")
    s.add_argument("--workers", type=int, default=4, help="parallella nedladdningar")
    s.add_argument("--days", nargs="+", help="bara dessa handelsdagar (YYYY-MM-DD)")
    m = sp.add_parser("marks"); m.add_argument("dates", nargs="+")
    m.add_argument("--feed", default="DFRA-pretrade")
    m.add_argument("--snap", nargs="*", default=[], help="ögonblicksbilder HH:MM (Frankfurttid) utöver 'close'")
    m.add_argument("--all", action="store_true", help="alla instrument, inte bara priceNotation 2")
    p = sp.add_parser("prune"); p.add_argument("--feed", required=True); p.add_argument("--keep-days", type=int, default=14)
    a = ap.parse_args()
    if a.cmd == "list":
        fs = list_files(a.feed)
        print("\n".join(fs[:5] + (["..."] if len(fs) > 10 else []) + fs[-5:] if len(fs) > 10 else fs))
        print(f"{len(fs)} filer", file=sys.stderr)
    elif a.cmd == "sync":
        _, failed = sync(a.feeds, a.archive, a.workers, a.bonds_only, set(a.days) if a.days else None)
        sys.exit(1 if failed else 0)
    elif a.cmd == "marks":
        for d in a.dates:
            build_marks(d, a.archive, a.db, a.feed, a.snap, None if a.all else 2)
    else:
        prune(a.feed, a.archive, a.db, a.keep_days)
