#!/usr/bin/env python3
"""
Intraday prices for the portfolio site, one update per minute.

Deutsche Börse publishes the MiFID II files every minute (free data is delayed, MiFIR art. 13). This loop
picks up each new minute file as it appears:

  DFRA-pretrade   best bid/offer per bond (deltas merged, as in mfs.build_marks), site universe only
  DEUR-posttrade  Eurex trades: last, high, low, trades and lots per futures contract (eurex.PRODUCTS)

and writes live.json:
  {"day", "gen" (UTC), "file" (latest minute file, UTC), "msg" (latest message time, UTC),
   "bonds": {isin: [bid, ask, bid_qty, ask_qty, "HH:MM:SS" UTC]},
   "fut": {code: [contract, last, high, low, trades, lots, "HH:MM:SS" UTC]}}

  python live.py once --day 2026-10-02 --last 30      # process the last 30 minute files, write live.json
  python live.py run --publish                        # GitHub Actions: loop until the close, push every minute

--publish force-pushes live.json as the only file of the orphan branch 'live'. The page reads the branch
head's commit id from the GitHub API and fetches raw.githubusercontent.com/<repo>/<sha>/live.json: a new
URL each minute, so the 5-minute raw cache never serves an old file.
"""
import argparse, datetime as dt, glob, gzip, json, os, subprocess, sys, tempfile, time

import eurex, mfs

PRE, POST = "DFRA-pretrade", "DEUR-posttrade"
OPEN, CLOSE = dt.time(7, 45), dt.time(17, 45)      # Frankfurt time; bonds trade 08:00-17:30
MAX_CATCHUP = 60                                   # minute files to read when starting without state
HERE = os.path.dirname(os.path.abspath(__file__))


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def universe(hist_dir=os.path.join(HERE, "data", "history")):
    """ISINs on the site: the latest data/history file."""
    files = sorted(glob.glob(os.path.join(hist_dir, "*.csv.gz")))
    if not files:
        return None
    with gzip.open(files[-1], "rt", encoding="utf-8") as f:
        next(f)
        return {line.split(",", 1)[0] for line in f if not line.startswith("FUT:")}


class Live:
    def __init__(self, day, isins=None, state=None):
        self.day, self.isins = day, isins
        s = state if state and state.get("day") == day else {}
        self.bonds = {k: list(v) for k, v in s.get("bonds", {}).items()}
        self.fut = {k: list(v) for k, v in s.get("fut", {}).items()}
        self.done = {PRE: s.get("cursor", {}).get(PRE, ""), POST: s.get("cursor", {}).get(POST, "")}
        self.msg = s.get("msg")
        self.cancelled = set()

    def apply_pre(self, path):
        for m in mfs.iter_messages(path):
            if m.get("priceNotation") != 2:
                continue
            isin = m.get("instrumentIdentificationCode")
            if not isin or (self.isins is not None and isin not in self.isins):
                continue
            q = self.bonds.setdefault(isin, [None, None, None, None, None])
            ts = (m.get("updateDateAndTime") or m.get("publicationDateAndTime") or "")[:19]
            for i, key in ((0, "bestBid"), (1, "bestAsk")):
                if key in m:
                    px = mfs._num(m[key])
                    q[i], q[i + 2] = (px, mfs._num(m.get(key + "Qty"))) if px else (None, None)  # 0 = withdrawn
            q[4] = ts[11:19] or q[4]
            if ts > (self.msg or ""):
                self.msg = ts

    def apply_post(self, path):
        for m in mfs.iter_messages(path):
            if m.get("messageId") != "posttrade" or m.get("instrumentIdentificationCode") not in eurex.BY_ISIN:
                continue
            if m.get("optionCategory") or m.get("mmtModificationInd") == "C":
                continue
            code, px = eurex.BY_ISIN[m["instrumentIdentificationCode"]], m.get("price")
            if px is None or m.get("mmtTradingMode") not in eurex.ON_BOOK:
                continue
            f = self.fut.get(code)
            contract = m.get("contractDate")
            if f and f[0] < contract:      # keep the front contract (the earliest expiry that trades)
                continue
            if not f or f[0] > contract:
                f = self.fut[code] = [contract, px, px, px, 0, 0, None]
            t = (m.get("tradingDateAndTime") or "")[:19]
            if t[11:19] >= (f[6] or ""):
                f[1], f[6] = px, t[11:19]
            f[2], f[3] = max(f[2], px), min(f[3], px)
            f[4] += 1
            f[5] += m.get("quantity") or 0

    def to_json(self, last_file):
        two = {k: v for k, v in self.bonds.items() if v[0] is not None or v[1] is not None}
        return {"day": self.day, "gen": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "file": last_file, "msg": self.msg,
                "cursor": self.done, "bonds": two, "fut": self.fut}


def todays_files(feed, day):
    out = []
    for f in mfs.list_files(feed):
        p = mfs.parse_name(f)
        if p and p[2] and p[1] == day:
            out.append((p[2].strftime("%Y-%m-%dT%H:%M"), f))
    return sorted(out)


def step(lv, tmp, limit=None, until="9999"):
    """Downloads and applies the minute files not yet processed. Returns the latest file time or None."""
    latest = None
    for feed, apply in ((PRE, lv.apply_pre), (POST, lv.apply_post)):
        new = [(t, f) for t, f in todays_files(feed, lv.day) if lv.done[feed] < t <= until]
        if limit and len(new) > limit:
            new = new[-limit:]
        for t, f in new:
            path = os.path.join(tmp, f)
            mfs.download(f, path, notation=2 if feed == PRE else None)
            apply(path)
            os.remove(path)
            lv.done[feed] = t
        latest = max(latest or "", lv.done[feed]) or None
    return latest


# ---------------------------------------------------------------- publishing (orphan branch 'live')
def git(*args, cwd=HERE, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True).stdout


def load_published():
    if subprocess.run(["git", "fetch", "-q", "--depth", "1", "origin", "live"], cwd=HERE, capture_output=True).returncode:
        return None
    try:
        return json.loads(git("show", "FETCH_HEAD:live.json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


class Publisher:
    def __init__(self):
        self.wt = tempfile.mkdtemp(prefix="live-")
        git("worktree", "add", "--detach", self.wt)
        git("checkout", "-q", "--orphan", "live", cwd=self.wt)
        git("rm", "-rfq", "--cached", ".", cwd=self.wt)
        for name in os.listdir(self.wt):
            if name != ".git":
                subprocess.run(["rm", "-rf", os.path.join(self.wt, name)])
        self.first = True

    def push(self, payload):
        with open(os.path.join(self.wt, "live.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        git("add", "live.json", cwd=self.wt)
        msg = f"live {payload['day']} {payload['file']}"
        git("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-q", *([] if self.first else ["--amend"]), "-m", msg, cwd=self.wt)
        self.first = False
        git("push", "-q", "-f", "origin", "HEAD:refs/heads/live", cwd=self.wt)


# ---------------------------------------------------------------- commands
def frankfurt_now():
    return now_utc().astimezone(mfs.TZ)


def cmd_once(a):
    lv = Live(a.day, universe())
    with tempfile.TemporaryDirectory() as tmp:
        last = step(lv, tmp, limit=a.last, until=a.until)
    payload = lv.to_json(last)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"{a.out}: {len(payload['bonds']):,} bonds, {len(payload['fut'])} futures, last file {last}, "
          f"last message {payload['msg']}", file=sys.stderr)


def cmd_run(a):
    started = time.time()
    loc = frankfurt_now()
    if loc.weekday() >= 5 or loc.time() >= CLOSE:
        print("market closed", file=sys.stderr)
        return
    if loc.time() < OPEN:  # started early by the schedule: wait for the open
        wait = (dt.datetime.combine(loc.date(), OPEN, mfs.TZ) - loc).total_seconds()
        print(f"waiting {wait / 60:.0f} min for the open", file=sys.stderr)
        time.sleep(max(0, wait))
    day = frankfurt_now().date().isoformat()
    state = load_published() if a.publish else None
    lv = Live(day, universe(), state)
    pub = Publisher() if a.publish else None
    print(f"{day}: {'resuming at ' + lv.done[PRE] if lv.done[PRE] else 'fresh start'}, "
          f"{len(lv.isins or ()):,} ISINs in the universe", file=sys.stderr)
    tmp = tempfile.mkdtemp(prefix="mfs-")
    while time.time() - started < a.max_minutes * 60 and frankfurt_now().time() < CLOSE:
        t0 = time.time()
        try:
            last = step(lv, tmp, limit=MAX_CATCHUP)
            payload = lv.to_json(last)
            if pub:
                pub.push(payload)
            else:
                with open(a.out, "w", encoding="utf-8") as f:
                    json.dump(payload, f, separators=(",", ":"))
            lag = (now_utc() - dt.datetime.fromisoformat(payload["msg"] + "+00:00")).total_seconds() / 60 if payload["msg"] else None
            print(f"{frankfurt_now():%H:%M:%S} file {last} · {len(payload['bonds']):,} bonds · "
                  f"latest message {lag:.1f} min old" if lag is not None else f"{frankfurt_now():%H:%M:%S} no data yet",
                  file=sys.stderr, flush=True)
        except Exception as e:  # keep looping: one bad minute must not stop the day
            print(f"{frankfurt_now():%H:%M:%S} error: {e}", file=sys.stderr, flush=True)
        time.sleep(max(5, 60 - (time.time() - t0)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    o = sp.add_parser("once"); o.add_argument("--day", required=True); o.add_argument("--last", type=int, default=30)
    o.add_argument("--out", default="live.json")
    o.add_argument("--until", default="9999", help="replay: only files up to this UTC minute, e.g. 2026-10-02T12:00")
    r = sp.add_parser("run"); r.add_argument("--publish", action="store_true"); r.add_argument("--out", default="live.json")
    r.add_argument("--max-minutes", type=float, default=345, help="stop before GitHub's 6-hour job limit")
    a = ap.parse_args()
    {"once": cmd_once, "run": cmd_run}[a.cmd](a)
