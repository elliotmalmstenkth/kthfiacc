#!/usr/bin/env python3
"""
Deutsche Börse MiFID II-filer (mfs.deutsche-boerse.com) – arkivering och dagliga kurser.

Filerna (en per minut, JSON-rader i .gz) ligger bara kvar till midnatt nästa
bankdag, så 'sync' måste köras minst en gång per bankdag (helst kväll + morgon).

  python mfs.py list DFRA-pretrade                 # vad servern har just nu
  python mfs.py sync                               # hämta allt nytt för standardflödena
  python mfs.py sync --feeds DFRA-pretrade DFRA-posttrade DEUR-posttrade
  python mfs.py marks 2026-10-02                   # bygg kurstabell för en dag ur arkivet
  python mfs.py marks 2026-10-02 --snap 15:30 16:00
  python mfs.py prune --feed DFRA-pretrade --keep-days 14   # radera rådata för dagar som redan har marks

Arkivstruktur:  archive/<flöde>/<YYYY-MM-DD>/<filnamn>.json.gz   (filerna sparas oförändrade)

'marks' går igenom pre-trade-filerna i tidsordning och håller senaste meddelandet
per ISIN (sista meddelandet vinner; saknas bestBid/bestAsk i det meddelandet räknas
sidan som tom). Vid varje ögonblicksbild (--snap, UTC, samma tidszon som filnamnen)
och vid dagens slut ('close') sparas läget i SQLite:

  quotes(date, snap, isin, bid, ask, ccy, notation, quote_time, msg)
  days(date, feed, n_files, first_file, last_file, max_gap_min, built_at)

msg är hela senaste JSON-meddelandet, så inga fält går förlorade.

API (offentligt, ingen nyckel):
  GET https://mfs.deutsche-boerse.com/api/<flöde>            -> {"CurrentFiles": [...]}
  GET https://mfs.deutsche-boerse.com/api/download/<filnamn>
"""
import argparse, datetime as dt, gzip, json, os, re, shutil, sqlite3, sys, time, urllib.error, urllib.request

BASE = "https://mfs.deutsche-boerse.com/api"
FEEDS_DEFAULT = ["DFRA-pretrade", "DFRA-posttrade", "DEUR-posttrade"]
ARCHIVE_DEFAULT = os.environ.get("MFS_ARCHIVE", "archive")
DB_DEFAULT = os.environ.get("MFS_MARKS_DB", "marks.sqlite")
FNAME_RE = re.compile(r"^(?P<feed>.+)-(?P<date>\d{4}-\d{2}-\d{2})T(?P<hh>\d{2})_(?P<mm>\d{2})\.json\.gz$")
UA = {"User-Agent": "kth-fic-club/1.0 (non-commercial, academic)"}


# ---------------------------------------------------------------- nätverk
def _open(url, timeout=120, tries=4):
    err = None
    for attempt in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):  # varken nätverksfel eller tillfälligt: ge upp direkt
                raise
            err = e
        except Exception as e:
            err = e
        time.sleep(2 ** attempt)
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
    """'DFRA-pretrade-2026-10-02T08_15.json.gz' -> ('DFRA-pretrade', '2026-10-02', datetime UTC)"""
    m = FNAME_RE.match(os.path.basename(fname))
    if not m:
        return None
    t = dt.datetime.fromisoformat(f"{m['date']}T{m['hh']}:{m['mm']}")
    return m["feed"], m["date"], t


def _gzip_ok(path):
    try:
        with gzip.open(path, "rb") as f:
            while f.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def download(fname, path):
    tmp = path + ".part"
    with _open(f"{BASE}/download/{fname}", timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if not _gzip_ok(tmp):
        os.remove(tmp)
        raise RuntimeError(f"trasig gzip: {fname}")
    os.replace(tmp, path)


def sync(feeds=FEEDS_DEFAULT, archive=ARCHIVE_DEFAULT, pause=0.05):
    """Hämtar alla filer som servern har och som inte redan finns i arkivet."""
    total_new = failed = 0
    for feed in feeds:
        try:
            files = list_files(feed)
        except Exception as e:
            print(f"{feed}: kunde inte lista filer: {e}", file=sys.stderr)
            failed += 1
            continue
        new = skipped = 0
        for fn in files:
            p = parse_name(fn)
            day = p[1] if p else "okänt-datum"
            folder = os.path.join(archive, feed, day)
            path = os.path.join(folder, fn)
            if os.path.exists(path):
                skipped += 1
                continue
            os.makedirs(folder, exist_ok=True)
            try:
                download(fn, path)
                new += 1
            except Exception as e:
                print(f"  {fn}: {e}", file=sys.stderr)
                failed += 1
            time.sleep(pause)
        days = sorted({parse_name(f)[1] for f in files if parse_name(f)})
        rng = f"{days[0]}..{days[-1]}" if days else "-"
        print(f"{feed}: {len(files)} filer på servern ({rng}), {new} nya, {skipped} fanns redan", file=sys.stderr)
        total_new += new
    return total_new, failed


# ---------------------------------------------------------------- dagliga kurser
def connect(db=DB_DEFAULT):
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS quotes(date TEXT, snap TEXT, isin TEXT, bid REAL, ask REAL, ccy TEXT,
        notation INT, quote_time TEXT, msg TEXT, PRIMARY KEY(date, snap, isin));
    CREATE TABLE IF NOT EXISTS days(date TEXT, feed TEXT, n_files INT, first_file TEXT, last_file TEXT,
        max_gap_min REAL, built_at TEXT, PRIMARY KEY(date, feed));
    """)
    return con


def day_files(archive, feed, date):
    folder = os.path.join(archive, feed, date)
    if not os.path.isdir(folder):
        return []
    fs = [(parse_name(f)[2], os.path.join(folder, f)) for f in os.listdir(folder) if parse_name(f)]
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


def build_marks(date, archive=ARCHIVE_DEFAULT, db=DB_DEFAULT, feed="DFRA-pretrade", snaps=(), notation=2):
    """Ögonblicksbilder av senaste bud/sälj per ISIN. notation=None tar med alla instrument."""
    files = day_files(archive, feed, date)
    if not files:
        raise SystemExit(f"inga {feed}-filer för {date} i {archive}/")
    snap_times = sorted((dt.datetime.fromisoformat(f"{date}T{s}"), s) for s in snaps)
    state, out = {}, []

    def take(label):
        for isin, (t, m) in state.items():
            out.append((date, label, isin, _num(m.get("bestBid")), _num(m.get("bestAsk")), m.get("priceCurrency"),
                        m.get("priceNotation"), t.isoformat(timespec="minutes"), json.dumps(m, separators=(",", ":"))))

    for t, path in files:
        while snap_times and t > snap_times[0][0]:
            take(snap_times.pop(0)[1])
        for m in iter_messages(path):
            if notation is not None and m.get("priceNotation") != notation:
                continue
            isin = m.get("instrumentIdentificationCode")
            if isin:
                state[isin] = (t, m)
    for _, label in snap_times:  # ögonblicksbilder efter sista filen
        take(label)
    take("close")

    gaps = [(b[0] - a[0]).total_seconds() / 60 for a, b in zip(files, files[1:])]
    con = connect(db)
    con.execute("DELETE FROM quotes WHERE date=?", (date,))
    con.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?)", out)
    con.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?,?)",
                (date, feed, len(files), os.path.basename(files[0][1]), os.path.basename(files[-1][1]),
                 max(gaps, default=0), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    con.commit()
    close = [r for r in out if r[1] == "close"]
    two = sum(1 for r in close if r[3] is not None and r[4] is not None)
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
    m = sp.add_parser("marks"); m.add_argument("dates", nargs="+")
    m.add_argument("--feed", default="DFRA-pretrade")
    m.add_argument("--snap", nargs="*", default=[], help="ögonblicksbilder HH:MM (UTC) utöver 'close'")
    m.add_argument("--all", action="store_true", help="alla instrument, inte bara priceNotation 2")
    p = sp.add_parser("prune"); p.add_argument("--feed", required=True); p.add_argument("--keep-days", type=int, default=14)
    a = ap.parse_args()
    if a.cmd == "list":
        fs = list_files(a.feed)
        print("\n".join(fs[:5] + (["..."] if len(fs) > 10 else []) + fs[-5:] if len(fs) > 10 else fs))
        print(f"{len(fs)} filer", file=sys.stderr)
    elif a.cmd == "sync":
        _, failed = sync(a.feeds, a.archive)
        sys.exit(1 if failed else 0)
    elif a.cmd == "marks":
        for d in a.dates:
            build_marks(d, a.archive, a.db, a.feed, a.snap, None if a.all else 2)
    else:
        prune(a.feed, a.archive, a.db, a.keep_days)
