#!/usr/bin/env python3
"""
Deutsche Börse MiFID II files (mfs.deutsche-boerse.com): archiving and daily marks.

The files (one per minute, JSON lines in .gz) are only kept until midnight of the next
business day, so 'sync' must run at least once per business day (ideally evening + morning).

  python mfs.py list DFRA-pretrade                 # what the server has right now
  python mfs.py sync                               # fetch everything new for the default feeds
  python mfs.py sync --feeds DFRA-pretrade DFRA-posttrade DEUR-posttrade
  python mfs.py marks 2026-10-02                   # build the quote table for one day from the archive
  python mfs.py marks 2026-10-02 --snap 17:30      # Frankfurt time
  python mfs.py sync --bonds-only                  # pre-trade: bond rows only
  python mfs.py prune --feed DFRA-pretrade --keep-days 14   # delete raw data for days that already have marks

Archive layout:  archive/<feed>/<trading day>/<file name>.json.gz
  File-name timestamps are UTC and mark the start of the minute. The trading day is in
  Frankfurt time, so e.g. DFRA-pretrade-2026-10-01T23_00 belongs to 2026-10-02.
  Files are stored unchanged, except with 'sync --bonds-only', where pre-trade files keep
  only rows with priceNotation 2 (about 1/4 of rows, ~0.5 instead of ~2 GB per day).

Pre-trade messages are DELTAS: a message carrying only bestAsk means the offer changed,
not that the bid was pulled. 'marks' therefore merges the sides per ISIN (each side keeps
its latest value and its own timestamp).
  price 0 (and qty 0)  = side withdrawn; at the 17:30 close every bond goes to
                         bid = ask = 0 with tradingSystemPhase 202
  price > 0, qty 0     = indicative quote without size (common on the offer side on FRAA)
Snapshots:
  --snap HH:MM (Frankfurt time)  live sides at that time (one-sided quotes included)
  close                          last two-way quote of the day (the closing mark)

  quotes(date, snap, isin, venue, ccy, bid, bid_qty, bid_time, ask, ask_qty, ask_time)
  days(date, feed, n_files, first_file, last_file, max_gap_min, built_at)

Note: if a side is pulled without showing up in the feed, it stays as a stale quote;
bid_time/ask_time show how fresh each side is.

API (public, no key):
  GET https://mfs.deutsche-boerse.com/api/<feed>            -> {"CurrentFiles": [...]}
  GET https://mfs.deutsche-boerse.com/api/download/<file>   (301 -> short-lived Google Storage URL)
Feeds: DFRA (Börse Frankfurt), DETR (Xetra), DEUR (Eurex), DETG, DGAT; -pretrade/-posttrade.
Some feeds also have a '-daily-<date>' file (DEUR-posttrade yes, DFRA-pretrade no/404).
"""
import argparse, datetime as dt, gzip, json, os, re, shutil, sqlite3, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

BASE = "https://mfs.deutsche-boerse.com/api"
FEEDS_DEFAULT = ["DFRA-pretrade", "DFRA-posttrade", "DEUR-posttrade"]
# feeds whose daily file is complete enough to replace the minute files (1 request instead of ~1,300)
DAILY_FEEDS = {"DEUR-posttrade"}
ARCHIVE_DEFAULT = os.environ.get("MFS_ARCHIVE", "archive")
DB_DEFAULT = os.environ.get("MFS_MARKS_DB", "marks.sqlite")
FNAME_RE = re.compile(r"^(?P<feed>.+)-(?P<date>\d{4}-\d{2}-\d{2})T(?P<hh>\d{2})_(?P<mm>\d{2})\.json\.gz$")
DAILY_RE = re.compile(r"^(?P<feed>.+)-daily-(?P<date>\d{4}-\d{2}-\d{2})\.json\.gz$")
TZ = ZoneInfo("Europe/Berlin")
UA = {"User-Agent": "kth-fic-club/1.0 (non-commercial, academic)"}


# ---------------------------------------------------------------- network
_gate = threading.Lock()
_cooldown_until = 0.0  # shared by all threads: on a 429 everyone pauses
STATS = {"429": 0, "cooldown_s": 0.0}


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
        STATS["429"] += 1
        new = max(_cooldown_until, time.monotonic() + seconds)
        STATS["cooldown_s"] += max(0.0, new - max(_cooldown_until, time.monotonic()))
        _cooldown_until = new


def _open(url, timeout=120, tries=8):
    """GET with retries. 429 (Too Many Requests) honours Retry-After and pauses all threads."""
    err = None
    for attempt in range(tries):
        _wait_gate()
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):  # neither a network error nor transient: give up at once
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
    raise RuntimeError(f"failed: {url}: {err}")


def list_files(feed):
    """File names the server currently lists for the feed."""
    with _open(f"{BASE}/{feed}", timeout=60) as r:
        data = json.load(r)
    files = data.get("CurrentFiles") if isinstance(data, dict) else data
    if files is None:
        raise RuntimeError(f"unexpected response for {feed}: keys {list(data)[:10]}")
    return sorted(files)


def parse_name(fname):
    """'DFRA-pretrade-2026-10-01T23_00.json.gz' -> ('DFRA-pretrade', '2026-10-02', datetime 23:00 UTC)
    The trading day is in Frankfurt time. Daily files ('-daily-<date>') return time None."""
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
    """Downloads one file. notation=2 keeps bond rows only (priceNotation 2)."""
    tmp = f"{path}.{os.getpid()}.part"  # unique per process: two concurrent syncs never collide
    with _open(f"{BASE}/download/{fname}", timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if not _gzip_ok(tmp):
        os.remove(tmp)
        raise RuntimeError(f"corrupt gzip: {fname}")
    if notation is not None:
        tag = re.compile(rb'"priceNotation"\s*:\s*%d\s*[,}]' % notation)
        with gzip.open(tmp, "rb") as src, gzip.open(tmp + "2", "wb", compresslevel=6) as dst:
            for line in src:
                if tag.search(line):
                    dst.write(line)
        os.replace(tmp + "2", tmp)
    os.replace(tmp, path)


def sync(feeds=FEEDS_DEFAULT, archive=ARCHIVE_DEFAULT, workers=4, bonds_only=False, days=None):
    """Downloads every file the server lists that is not yet in the archive (days: only these trading days).
    Returns (new, failed). A 404 counts as a missing file, not as a failure."""
    total_new = failed = 0
    for feed in feeds:
        try:
            files = list_files(feed)
        except Exception as e:
            print(f"{feed}: could not list files: {e}", file=sys.stderr)
            failed += 1
            continue
        notation = 2 if (bonds_only and feed.endswith("-pretrade")) else None
        todo, skipped, by_day, daily_new = [], 0, {}, 0
        for fn in files:
            p = parse_name(fn)
            if days is not None and (not p or p[1] not in days):
                continue
            if notation is not None and p and p[2] is None:
                continue  # daily file duplicates the minute files; skip it in filtered mode
            path = os.path.join(archive, feed, p[1] if p else "unknown-date", fn)
            by_day.setdefault(p[1] if p else None, []).append((fn, path, p))
        for day, items in by_day.items():
            daily = [x for x in items if x[2] and x[2][2] is None]
            if daily and feed in DAILY_FEEDS and notation is None:
                fn, path, _ = daily[0]
                if os.path.exists(path):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                try:
                    download(fn, path)
                    daily_new += 1
                    print(f"  {feed} {day}: daily file downloaded ({os.path.getsize(path) / 1e6:,.0f} MB), skipping the minute files",
                          file=sys.stderr, flush=True)
                    continue
                except Exception as e:
                    print(f"  {feed} {day}: could not download the daily file ({e}), using the minute files", file=sys.stderr)
                items = [x for x in items if x is not daily[0]]
            for fn, path, _ in items:
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

        t0, res = time.monotonic(), []
        with ThreadPoolExecutor(max(1, workers)) as ex:
            for i, r in enumerate(ex.map(fetch, todo), 1):
                res.append(r)
                if i % 200 == 0 or i == len(todo):
                    print(f"  {feed}: {i}/{len(todo)} files, {time.monotonic() - t0:,.0f} s, "
                          f"429 responses so far {STATS['429']} (paused {STATS['cooldown_s']:,.0f} s)", file=sys.stderr, flush=True)
        new = sum(r == "ok" for r, _ in res) + daily_new
        missing = sum(r == "404" for r, _ in res)
        failed += sum(r == "fel" for r, _ in res)
        mb = sum(b for _, b in res) / 1e6
        on_server = sorted({p[1] for f in files if (p := parse_name(f))})
        rng = f"{on_server[0]}..{on_server[-1]}" if on_server else "-"
        print(f"{feed}: {len(files)} files on server ({rng}), {new} new ({mb:,.0f} MB), "
              f"{skipped} already archived, {missing} missing (404)" + (" [bonds only]" if notation else ""),
              file=sys.stderr)
        total_new += new
    return total_new, failed


# ---------------------------------------------------------------- daily marks
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
    """The minute files of one trading day in time order (daily files excluded)."""
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
    """'17:30' Frankfurt time on the trading day -> UTC ISO string comparable with updateDateAndTime."""
    t = dt.datetime.fromisoformat(f"{date}T{hhmm}").replace(tzinfo=TZ).astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def build_marks(date, archive=ARCHIVE_DEFAULT, db=DB_DEFAULT, feed="DFRA-pretrade", snaps=(), notation=2):
    """Bid/offer snapshots per ISIN (deltas merged). notation=None includes all instruments."""
    files = day_files(archive, feed, date)
    if not files:
        raise SystemExit(f"no {feed} files for {date} in {archive}/")
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
                    if px:  # price 0 = side withdrawn (e.g. at the close, phase 202)
                        sd[side], sd[side + "_qty"], sd[side + "_time"] = px, _num(m.get(key + "Qty")), ts[:23]
                    else:
                        sd[side] = sd[side + "_qty"] = sd[side + "_time"] = None
            vals = tuple(sd[k] for k in SIDES)
            q["live"] = vals if (sd["bid"] is not None or sd["ask"] is not None) else None
            if sd["bid"] is not None and sd["ask"] is not None:
                q["last2"] = vals
    for _, label in pending:  # snapshots after the last message
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
    print(f"{date}: {len(files)} files ({os.path.basename(files[0][1])} .. {os.path.basename(files[-1][1])}, "
          f"largest gap {max(gaps, default=0):.0f} min), {len(close):,} ISINs at the close, {two:,} two-way "
          f"-> {db}", file=sys.stderr)
    return con


def prune(feed, archive=ARCHIVE_DEFAULT, db=DB_DEFAULT, keep_days=14, today=None):
    """Deletes raw data for days older than keep_days that are already in the marks database."""
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
    print(f"{feed}: deleted {len(removed)} days {removed[:1]}..{removed[-1:]}", file=sys.stderr)
    return removed


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=ARCHIVE_DEFAULT)
    ap.add_argument("--db", default=DB_DEFAULT)
    sp = ap.add_subparsers(dest="cmd", required=True)
    l = sp.add_parser("list"); l.add_argument("feed")
    s = sp.add_parser("sync"); s.add_argument("--feeds", nargs="+", default=FEEDS_DEFAULT)
    s.add_argument("--bonds-only", action="store_true", help="pre-trade: keep priceNotation 2 rows only")
    s.add_argument("--workers", type=int, default=4, help="parallel downloads")
    s.add_argument("--days", nargs="+", help="only these trading days (YYYY-MM-DD)")
    m = sp.add_parser("marks"); m.add_argument("dates", nargs="+")
    m.add_argument("--feed", default="DFRA-pretrade")
    m.add_argument("--snap", nargs="*", default=[], help="snapshots HH:MM (Frankfurt time) in addition to 'close'")
    m.add_argument("--all", action="store_true", help="all instruments, not only priceNotation 2")
    p = sp.add_parser("prune"); p.add_argument("--feed", required=True); p.add_argument("--keep-days", type=int, default=14)
    a = ap.parse_args()
    if a.cmd == "list":
        fs = list_files(a.feed)
        print("\n".join(fs[:5] + (["..."] if len(fs) > 10 else []) + fs[-5:] if len(fs) > 10 else fs))
        print(f"{len(fs)} files", file=sys.stderr)
    elif a.cmd == "sync":
        _, failed = sync(a.feeds, a.archive, a.workers, a.bonds_only, set(a.days) if a.days else None)
        sys.exit(1 if failed else 0)
    elif a.cmd == "marks":
        for d in a.dates:
            build_marks(d, a.archive, a.db, a.feed, a.snap, None if a.all else 2)
    else:
        prune(a.feed, a.archive, a.db, a.keep_days)
