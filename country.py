#!/usr/bin/env python3
"""
Country of risk per bond: where the issuer (or, for a guaranteed bond or a financing vehicle, the group behind it)
is based. In order of preference:

  1. ECB list of eligible assets: the guarantor's residence (GUARANTOR_RESIDENCE) for a guaranteed bond
  2. GLEIF (the global LEI register, free API): for a financing vehicle (a name with FIN, FUNDING, CAPITAL,
     INTERNATIONAL, B.V. ...) the legal country of its ultimate parent, where GLEIF has one
  3. the ECB list's issuer residence, else GLEIF's legal address of the issuer's LEI
  4. the ISIN prefix, unless it is an international one (XS, EU, ...)

A vehicle without a parent in GLEIF keeps its own country (often NL, LU, IE or US). GLEIF answers are cached in
data/history/lei_country.csv (lei, country, parent_country), so a build only asks for LEIs it has not seen.

  python country.py --bonds data/2026-10-02/bonds_classified.csv.gz      # fill the cache, print a summary
"""
import argparse, csv, json, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "history", "lei_country.csv")   # committed by the site job with the history
API = "https://api.gleif.org/api/v1/lei-records"
VEHICLE = re.compile(r"\bFIN|FUND|CAPITAL|\bINT(?:L|\.|ERNAT)|TREASURY|ISSUER|\bB\.? ?V\b|\bDAC\b|\bLLC\b", re.I)
INTERNATIONAL = {"XS", "EU", "XC", "XD", "XF", "QS", "CS"}


def _get(url, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.api+json", "User-Agent": "kth-fic-club"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(10 * (k + 1)); continue
            raise
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(3 * (k + 1))
    return None


def load_cache(path=CACHE):
    if not os.path.exists(path):
        return {}
    with open(path, newline="") as f:
        return {r["lei"]: r for r in csv.DictReader(f)}


def save_cache(cache, path=CACHE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, ["lei", "country", "parent_country"], lineterminator="\n")
        w.writeheader()
        for k in sorted(cache):
            w.writerow({c: cache[k].get(c, "") for c in ("lei", "country", "parent_country")})


def update_cache(leis, vehicles, cache, max_parent_calls=800, pause=1.1):
    """Asks GLEIF for the LEIs not in the cache (200 per request) and the ultimate parents of new vehicles.
    Fails soft: whatever was fetched is kept."""
    new = sorted(l for l in leis if l and l not in cache)
    for i in range(0, len(new), 200):
        chunk = new[i:i + 200]
        j = _get(f"{API}?filter%5Blei%5D={','.join(chunk)}&page%5Bsize%5D=200")
        for r in (j or {}).get("data", []):
            cache[r["id"]] = dict(lei=r["id"], country=r["attributes"]["entity"]["legalAddress"]["country"], parent_country="")
        for l in chunk:                                  # LEIs GLEIF does not know: remember them as unknown
            cache.setdefault(l, dict(lei=l, country="", parent_country=""))
    calls = 0
    for l in sorted(vehicles & set(new)):
        if calls >= max_parent_calls:
            break
        j = _get(f"{API}/{l}/ultimate-parent"); calls += 1
        if j and j.get("data"):
            cache[l]["parent_country"] = j["data"]["attributes"]["entity"]["legalAddress"]["country"]
        time.sleep(pause)                               # GLEIF allows about 60 requests a minute
    return cache


def country_of(isin, lei, issuer, ecb, cache):
    """(country code, source) for one bond. ecb: the bond's ECB-list fields (residence, guarantor) or None."""
    if ecb and ecb.get("guarantor"):
        return ecb["guarantor"], "ECB guarantor"
    c = cache.get(lei) if isinstance(lei, str) else None
    if c and c.get("parent_country") and VEHICLE.search(issuer or ""):
        return c["parent_country"], "GLEIF parent"
    if ecb and ecb.get("residence"):
        return ecb["residence"], "ECB issuer"
    if c and c.get("country"):
        return c["country"], "GLEIF"
    if isin[:2].isalpha() and isin[:2] not in INTERNATIONAL:
        return isin[:2], "ISIN"
    return None, None


if __name__ == "__main__":
    import pandas as pd
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bonds", required=True)
    a = ap.parse_args()
    b = pd.read_csv(a.bonds, low_memory=False)
    b = b[b.ccy.isin(["EUR", "USD"])]
    leis = set(b.issuer_lei.dropna())
    veh = set(b[b.issuer.fillna("").str.contains(VEHICLE)].issuer_lei.dropna())
    cache = update_cache(leis, veh, load_cache())
    save_cache(cache)
    print(f"{len(cache):,} LEIs cached; parents for {sum(1 for c in cache.values() if c.get('parent_country')):,}", file=sys.stderr)
