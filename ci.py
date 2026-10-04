#!/usr/bin/env python3
"""
Run by GitHub Actions (.github/workflows/daily.yml). Also runs locally with gh signed in.

  python ci.py daily            # every completed trading day on the server without a release
  python ci.py daily --dry-run  # build the files in dist/ but create no release
  python ci.py firds            # weekly FIRDS snapshot (debt + futures)
  python ci.py site             # build the portfolio site from the latest data release -> _site/index.html

Each trading day is stored as a DRAFT release tagged data-<date>. Drafts are visible only to
users with write access to the repo; the Deutsche Börse data is free for non-commercial use and
is therefore not redistributed publicly. Assets per day:

  dfra_quotes-<date>.csv.gz          bid/offer per bond at 12:00, 17:25 and close (mfs.py marks)
  bonds_classified-<date>.csv.gz     classification against FIRDS (classify.py), plus issuer list
  eurex_futures_daily-<date>.csv     government bond and credit index futures (eurex.py daily)
  eurex_options-<date>.csv.gz        Bund/Bobl/Schatz option trades with implied volatility (eurex_options.py)
  DFRA-pretrade-bonds-<date>.tar     raw minute files, bond rows only (priceNotation 2)
  DFRA-posttrade-<date>.tar          raw minute files
  DEUR-posttrade-<date>.tar          raw minute and daily files
  DETR-posttrade-daily-<date>.json.gz  Xetra trades (all instruments, incl. the bond ETFs), Deutsche Börse's daily file

Snapshots of sources that keep only their latest version go to one draft release per month,
snapshots-<YYYY-MM> (archive_snapshots, every run):

  ecb_eligible-<list date>.csv.gz    ECB list of eligible marketable assets (haircuts, issuer groups, issue dates)
  euronext_esg-<date>.xlsx           Euronext ESG bond list (green, social, sustainability, SLB labels)
  fred-<date>.csv.gz                 ICE BofA OAS and VIX from FRED (FRED shows only the last three years of ICE data)

One row per day is appended to data/log.csv (committed by the workflow, which also keeps the
schedule alive: GitHub disables schedules in public repos after 60 days without activity).
"""
import argparse, csv, datetime as dt, gzip, io, json, os, shutil, sqlite3, subprocess, sys, tarfile, urllib.request

import eurex, eurex_options, mfs

HERE = os.path.dirname(os.path.abspath(__file__))

FEEDS = ["DFRA-pretrade", "DFRA-posttrade", "DEUR-posttrade"]
SNAPS = ["12:00", "17:25"]
DAY_END = dt.time(23, 15)  # the trading day (Frankfurt time) ends after the last file at 23:00
LOG = "data/log.csv"
LOG_COLS = ["date", "pretrade_files", "posttrade_files", "eurex_files", "isin_close", "two_sided_firm_close",
            "eur_quotes_close", "FGBL", "FECX", "FEHY", "FGBC", "built_at"]


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()}")
    return r.stdout


def existing_tags():
    out = gh("release", "list", "--limit", "1000", "--json", "tagName")
    return {r["tagName"] for r in json.loads(out or "[]")}


def complete_days(server_days, now=None):
    """Trading days that are complete (after 23:15 Frankfurt time on the same day)."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(mfs.TZ)
    return [d for d in sorted(server_days)
            if now >= dt.datetime.combine(dt.date.fromisoformat(d), DAY_END, tzinfo=mfs.TZ)]


def server_days(feed="DFRA-pretrade"):
    return {p[1] for f in mfs.list_files(feed) if (p := mfs.parse_name(f))}


def _tar(folder, out):
    with tarfile.open(out, "w") as t:  # the files are already gzipped
        for f in sorted(os.listdir(folder)):
            if not f.endswith(".part"):
                t.add(os.path.join(folder, f), arcname=f)
    return out


def build_day(day, archive, dist, db, firds_db=None):
    """Downloads and builds one day. Returns (assets, log row), or None if the download was incomplete."""
    _, failed = mfs.sync(FEEDS, archive, workers=4, bonds_only=True, days={day})
    if failed:
        print(f"{day}: {failed} files failed; not publishing, the next run will retry", file=sys.stderr)
        return None
    os.makedirs(dist, exist_ok=True)
    if os.path.exists(db):
        os.remove(db)
    con = mfs.build_marks(day, archive, db, snaps=SNAPS)
    eurex.build_daily(day, archive, db)

    quotes = os.path.join(dist, f"dfra_quotes-{day}.csv.gz")
    fut = os.path.join(dist, f"eurex_futures_daily-{day}.csv")
    cur = con.execute("SELECT * FROM quotes ORDER BY snap, isin")
    with gzip.open(quotes, "wt", newline="") as f:
        w = csv.writer(f); w.writerow([c[0] for c in cur.description]); w.writerows(cur)
    cur = con.execute("SELECT * FROM futures_daily ORDER BY code, contract")
    with open(fut, "w", newline="") as f:
        w = csv.writer(f); w.writerow([c[0] for c in cur.description]); w.writerows(cur)

    assets = [quotes, fut]
    xetra = os.path.join(dist, f"DETR-posttrade-daily-{day}.json.gz")
    try:   # Xetra trades: not used by the site yet, kept because the server deletes them the next business day
        mfs.download(os.path.basename(xetra), xetra)
        assets.append(xetra)
    except Exception as e:
        print(f"{day}: Xetra daily file skipped: {e}", file=sys.stderr)
    try:   # implied volatility from the Bund/Bobl/Schatz options; the day still publishes without it
        assets.append(eurex_options.write(eurex_options.build(day, archive), os.path.join(dist, f"eurex_options-{day}.csv.gz")))
    except Exception as e:
        print(f"{day}: options skipped: {e}", file=sys.stderr)
    if firds_db and os.path.exists(firds_db):  # classification against FIRDS (sector, coupon, maturity ...)
        pre = [p for _, p in mfs.day_files(archive, "DFRA-pretrade", day)]
        out = os.path.join(dist, "bonds_classified.csv")
        subprocess.run([sys.executable, os.path.join(HERE, "classify.py"), "--db", firds_db, "--out", out, *pre], check=True)
        for f in (out, os.path.join(dist, "bonds_classified_issuers.csv")):
            with open(f, "rb") as src, gzip.open(f"{f[:-4]}-{day}.csv.gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(f)
        assets += [os.path.join(dist, f"bonds_classified-{day}.csv.gz"), os.path.join(dist, f"bonds_classified_issuers-{day}.csv.gz")]
    else:
        print(f"{day}: no FIRDS database ({firds_db}); skipping classification", file=sys.stderr)
    n = {}
    for feed in FEEDS:
        folder = os.path.join(archive, feed, day)
        n[feed] = len(os.listdir(folder)) if os.path.isdir(folder) else 0
        if n[feed]:
            name = "DFRA-pretrade-bonds" if feed == "DFRA-pretrade" else feed
            assets.append(_tar(folder, os.path.join(dist, f"{name}-{day}.tar")))

    close = con.execute("""SELECT count(*), sum(bid_qty > 0 AND ask_qty > 0), sum(ccy = 'EUR')
                           FROM quotes WHERE snap = 'close'""").fetchone()
    last = dict(con.execute("""SELECT code, last FROM futures_daily f WHERE contract =
        (SELECT min(contract) FROM futures_daily g WHERE g.code = f.code AND g.last IS NOT NULL)"""))
    row = dict(date=day, pretrade_files=n["DFRA-pretrade"], posttrade_files=n["DFRA-posttrade"],
               eurex_files=n["DEUR-posttrade"], isin_close=close[0], two_sided_firm_close=close[1] or 0,
               eur_quotes_close=close[2] or 0, **{k: last.get(k) for k in ("FGBL", "FECX", "FEHY", "FGBC")},
               built_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    con.close()
    return assets, row


def append_log(row, path=LOG):
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != row["date"]]
    rows.append({k: row.get(k) for k in LOG_COLS})
    rows.sort(key=lambda r: r["date"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, LOG_COLS); w.writeheader(); w.writerows(rows)


def notes(row, assets):
    sizes = "\n".join(f"- `{os.path.basename(a)}` ({os.path.getsize(a) / 1e6:,.1f} MB)" for a in assets)
    return (f"Börse Frankfurt + Eurex, trading day {row['date']} (Frankfurt time).\n\n"
            f"- Bonds with a closing mark: {row['isin_close']:,} (firm two-way: {row['two_sided_firm_close']:,}, "
            f"EUR: {row['eur_quotes_close']:,})\n"
            f"- Files: DFRA-pretrade {row['pretrade_files']}, DFRA-posttrade {row['posttrade_files']}, "
            f"DEUR-posttrade {row['eurex_files']}\n"
            f"- Last (front contract): FGBL {row['FGBL']}, FECX {row['FECX']}, FEHY {row['FEHY']}, FGBC {row['FGBC']}\n\n"
            f"{sizes}\n\nDeutsche Börse data: non-commercial use only. Do not redistribute.")


def cmd_daily(a):
    tags = set() if a.dry_run else existing_tags()
    days = a.days or complete_days(server_days())
    todo = [d for d in days if f"data-{d}" not in tags]
    print(f"completed days on the server: {days}; to build: {todo}", file=sys.stderr)
    built = 0
    for day in todo:
        res = build_day(day, a.archive, os.path.join(a.dist, day), os.path.join(a.dist, f"marks-{day}.sqlite"), a.firds_db)
        if res is None:
            continue
        assets, row = res
        append_log(row)
        if not a.dry_run:
            gh("release", "create", f"data-{day}", "--draft", "--title", f"Market data {day}",
               "--notes", notes(row, assets), *assets)
            print(f"{day}: draft release data-{day} created ({len(assets)} files)", file=sys.stderr)
        if a.cleanup:  # save disk space on the runner: only the processed day's folders
            for feed in FEEDS:
                shutil.rmtree(os.path.join(a.archive, feed, day), ignore_errors=True)
        built += 1
    print(f"done: {built} days", file=sys.stderr)
    try:
        archive_snapshots(os.path.join(a.dist, "snapshots"), a.dry_run)
    except Exception as e:   # never fails the day's collection
        print(f"snapshots skipped: {e}", file=sys.stderr)
    return 1 if built < len(todo) else 0


def archive_snapshots(dist, dry_run=False, today=None):
    """Saves today's copy of the sources that only publish their latest version (see the module docstring) to
    the draft release snapshots-<YYYY-MM>. Files already there are skipped; each source fails on its own."""
    import ecb_collateral, green, market
    today = today or dt.datetime.now(mfs.TZ).date().isoformat()
    tag = f"snapshots-{today[:7]}"
    os.makedirs(dist, exist_ok=True)
    have = set()
    if not dry_run:
        if tag in existing_tags():
            have = {x["name"] for x in json.loads(gh("release", "view", tag, "--json", "assets"))["assets"]}
        else:
            gh("release", "create", tag, "--draft", "--title", f"Snapshots {today[:7]}", "--notes",
               "Daily copies of sources that publish only their latest version (ci.py archive_snapshots): "
               "ECB eligible assets list, Euronext ESG bond list, FRED ICE BofA OAS and VIX.")
    out = []

    def save(name, fetch):
        if name in have:
            return
        try:
            path = os.path.join(dist, name)
            data = fetch()
            with open(path, "wb") as f:
                f.write(data)
            out.append(path)
        except Exception as e:
            print(f"snapshot {name} skipped: {e}", file=sys.stderr)

    try:
        url, listday = ecb_collateral.latest_url()
        save(f"ecb_eligible-{listday}.csv.gz", lambda: ecb_collateral._get(url))
    except Exception as e:
        print(f"snapshot ECB list skipped: {e}", file=sys.stderr)
    save(f"euronext_esg-{today}.xlsx", lambda: urllib.request.urlopen(
        urllib.request.Request(green.URL, headers={"User-Agent": "Mozilla/5.0 (kth-fic-club)"}), timeout=90).read())

    def fred_all():
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(["series", "date", "value"])
        for sid in market.FRED:
            d, v = market.fred(sid, today)
            w.writerows((sid, x, y) for x, y in zip(d, v))
        return gzip.compress(buf.getvalue().encode(), mtime=0)
    save(f"fred-{today}.csv.gz", fred_all)
    if out and not dry_run:
        gh("release", "upload", tag, *out, "--clobber")
    print(f"{tag}: {[os.path.basename(p) for p in out] or 'nothing new'}", file=sys.stderr)
    return out


def cmd_firds(a):
    import firds
    today = dt.date.today().isoformat()
    os.makedirs(a.dist, exist_ok=True)
    db = os.path.join(a.dist, "firds.sqlite")
    pub, _ = firds.list_fulins("D", today)
    tag = f"firds-{pub}"
    if not a.dry_run and tag in existing_tags():
        print(f"{tag} already exists", file=sys.stderr)
        return 0
    assets = []
    for cat in ("D", "F"):
        firds.build(cat, today, db, os.path.join(a.dist, "firds_raw"))
        out = os.path.join(a.dist, f"firds_{cat.lower()}-{pub}.csv.gz")
        con = sqlite3.connect(db)
        cur = con.execute(f"SELECT * FROM firds_{cat.lower()}")
        with gzip.open(out, "wt", newline="") as f:
            w = csv.writer(f); w.writerow([c[0] for c in cur.description]); w.writerows(cur)
        con.close()
        assets.append(out)
    if not a.dry_run:
        gh("release", "create", tag, "--draft", "--title", f"FIRDS {pub}",
           "--notes", "ESMA FIRDS FULINS (debt D, futures F), one row per ISIN (firds.py).", *assets)
    print(f"{tag}: {[os.path.basename(x) for x in assets]}", file=sys.stderr)
    return 0


def latest_data_day():
    out = gh("release", "list", "--limit", "200", "--json", "tagName")
    days = sorted(t["tagName"][5:] for t in json.loads(out or "[]") if t["tagName"].startswith("data-"))
    return days[-1] if days else None


def cmd_site(a):
    """Builds the portfolio site from the latest data release (or --day) into <out>/index.html."""
    try:
        day = a.day or latest_data_day()
    except RuntimeError as e:
        print(f"could not list releases: {e}", file=sys.stderr)
        day = None
    if not day:  # no release yet: the latest day stored in the repo
        local = sorted(d for d in os.listdir(os.path.join(HERE, "data")) if d[:2] == "20")
        day = local[-1] if local else None
    if not day:
        print("no data to build the site from", file=sys.stderr)
        return 1
    dl = os.path.join(a.dist, "site-input", day)
    os.makedirs(dl, exist_ok=True)
    gh("release", "download", f"data-{day}", "-D", dl, "--clobber",
       "-p", "dfra_quotes-*", "-p", "eurex_futures_daily-*", "-p", "bonds_classified-*", "-p", "eurex_options-*", check=False)
    files = {f.split("-")[0]: os.path.join(dl, f) for f in os.listdir(dl)}
    bonds = files.get("bonds_classified") or os.path.join(HERE, "data", day, "bonds_classified.csv.gz")
    quotes = files.get("dfra_quotes") or os.path.join(HERE, "data", day, "dfra_quotes.csv.gz")
    fut = files.get("eurex_futures_daily") or os.path.join(HERE, "data", day, "eurex_futures_daily.csv")
    opts = files.get("eurex_options") or os.path.join(HERE, "data", day, "eurex_options.csv.gz")
    missing = [p for p in (bonds, quotes, fut) if not os.path.exists(p)]
    if missing:
        print(f"{day}: missing {missing}", file=sys.stderr)
        return 1
    import ecb_curve
    ecb_curve.update(a.ecb_db, full=not os.path.exists(a.ecb_db), raw_dir=os.path.join(a.dist, "ecb_raw"))
    os.makedirs(a.out, exist_ok=True)
    subprocess.run([sys.executable, os.path.join(HERE, "site", "build.py"), "--day", day, "--ecb-db", a.ecb_db,
                    "--bonds", bonds, "--quotes", quotes, "--futures", fut, "--options", opts,
                    "--history", os.path.join(HERE, "data", "history"),
                    "--out", os.path.join(a.out, "index.html")], check=True)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default="archive")
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cleanup", action="store_true", help="delete the day's raw data after publishing (for CI)")
    ap.add_argument("--firds-db", default="firds.sqlite", help="FIRDS database for the classification (firds.py --cat D)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("daily"); d.add_argument("--days", nargs="+")
    sp.add_parser("firds")
    st = sp.add_parser("site"); st.add_argument("--day"); st.add_argument("--out", default="_site")
    st.add_argument("--ecb-db", default="ecb_curve.sqlite")
    a = ap.parse_args()
    sys.exit({"daily": cmd_daily, "firds": cmd_firds, "site": cmd_site}[a.cmd](a))
