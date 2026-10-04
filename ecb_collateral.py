#!/usr/bin/env python3
"""
Credit quality from the ECB list of eligible marketable assets (Eurosystem collateral), published daily:
https://www.ecb.europa.eu/mopo/coll/assets/html/list-MID.en.html

The Eurosystem accepts bonds rated at least BBB- (credit quality step 3) by an accepted rating agency
(S&P, Moody's, Fitch, DBRS Morningstar, Scope; the best rating counts). The list does not show ratings, but the
haircut does: within a haircut category, coupon type and maturity bucket, bonds in credit quality steps 1-2
(AAA to A-) share one haircut and step 3 (BBB+ to BBB-) has a markedly higher one. Each bond is classified
against the most common haircut of its group:

  "1-2"  haircut at the group's lowest level       -> rated A- or better
  "3"    haircut above it                          -> rated BBB+ to BBB- (investment grade, lowest bucket)
  None   on the list but not classifiable (e.g. ABS category L1E)

A bond NOT on the list is not necessarily high yield: issuers outside the EEA (e.g. US corporates), some
subordinated and structured bonds and non-euro bonds are not eligible whatever their rating.

  python ecb_collateral.py                        # download the latest list, print a summary
"""
import argparse, collections, csv, datetime as dt, gzip, io, re, sys, urllib.request

PAGE = "https://www.ecb.europa.eu/mopo/coll/assets/html/list-MID.en.html"
BASE = "https://www.ecb.europa.eu"
UA = {"User-Agent": "Mozilla/5.0 (kth-fic-club)"}
BUCKETS = [0, 1, 3, 5, 7, 10, 15, 30]          # residual maturity buckets of the haircut schedule (years)


def _get(url, timeout=120):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def latest_url():
    """The full list (not the daily update file) linked from the ECB page; returns (url, list date)."""
    html = _get(PAGE).decode("utf-8", "replace")
    m = sorted(set(re.findall(r"(/paym/coll/assets/html/dla/ea_MID/ea_csv_(\d{6})\.csv\.gz)", html)), key=lambda x: x[1])
    if not m:
        raise RuntimeError("no ea_csv_<yymmdd>.csv.gz link on the ECB page")
    path, d = m[-1]
    return BASE + path, dt.date(2000 + int(d[:2]), int(d[2:4]), int(d[4:])).isoformat()


def parse(raw):
    """Rows of the tab-separated UTF-16 list."""
    text = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    return list(csv.DictReader(io.StringIO(text.decode("utf-16")), delimiter="\t"))


def _bucket(maturity, asof):
    try:
        y = (dt.datetime.strptime(maturity[:10], "%d/%m/%Y").date() - asof).days / 365.25
    except ValueError:
        return None
    return sum(1 for b in BUCKETS if y >= b) - 1 if y >= 0 else None


def classify(rows, asof):
    """{isin: dict(cqs, haircut, category, type)} for euro-denominated bonds."""
    asof = dt.date.fromisoformat(asof) if isinstance(asof, str) else asof
    items, groups = [], collections.defaultdict(collections.Counter)
    for x in rows:
        if x.get("DENOMINATION") != "EUR" or not x.get("HAIRCUT"):
            continue
        b = _bucket(x.get("MATURITY_DATE", ""), asof)
        key = (x["HAIRCUT_CATEGORY"], x["COUPON_DEFINITION"])
        h = float(x["HAIRCUT"])
        items.append((x, key, b, h))
        if b is not None:
            groups[key + (b,)][h] += 1
    # the step 1-2 level is the LOWEST haircut held by at least 3 bonds in the group (step 3 can be the majority,
    # e.g. corporates in category L1C, so the most common level is not a safe reference)
    usual = {k: min([h for h, n in c.items() if n >= 3] or c) for k, c in groups.items()}
    out = {}
    for x, key, b, h in items:
        cqs = None
        if b is not None and key[0] != "L1E":   # L1E (asset-backed): one schedule, no split by rating
            # neighbouring buckets too: the list date and the schedule's own maturity cut-offs can differ by days
            ref = [usual[k] for k in (key + (b - 1,), key + (b,), key + (b + 1,)) if k in usual]
            if ref:
                cqs = "1-2" if h <= max(ref) * 1.1 else "3"
        try:
            issued = dt.datetime.strptime(x.get("ISSUANCE_DATE", "")[:10], "%d/%m/%Y").date().isoformat()
        except ValueError:
            issued = None
        out[x["ISIN_CODE"]] = dict(cqs=cqs, haircut=h, category=x["HAIRCUT_CATEGORY"], type=x.get("TYPE"), issued=issued, group=x.get("ISSUER_GROUP"))
    return out


def load():
    """Downloads the latest list: ({isin: ...}, list date)."""
    url, day = latest_url()
    return classify(parse(_get(url)), day), day


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    data, day = load()
    c = collections.Counter(v["cqs"] for v in data.values())
    print(f"ECB eligible assets {day}: {len(data):,} EUR bonds; CQS 1-2: {c['1-2']:,}, CQS 3: {c['3']:,}, "
          f"not classified: {c[None]:,}", file=sys.stderr)
