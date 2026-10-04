#!/usr/bin/env python3
"""
Green, social and sustainability bonds: the Euronext ESG bond list (Excel, updated by Euronext):
https://live.euronext.com/en/products/fixed-income/esg-bonds

  GREEN   green bond (use of proceeds: environmental projects)
  SOCIAL  social bond
  SUST    sustainability bond (green and social)
  SLB     sustainability-linked bond (coupon tied to the issuer's ESG targets, not to the use of proceeds)

Coverage: bonds listed on a Euronext market (Paris, Amsterdam, Brussels, Dublin, Lisbon, Milan, Oslo). Labelled
bonds listed only elsewhere (e.g. Germany's green Bunds, Luxembourg-only listings) are not tagged. Bond names
(FIRDS) do not carry the label, so there is no fallback.

  python green.py                 # download and print a summary
"""
import collections, io, sys, urllib.request

URL = "https://live.euronext.com/sites/default/files/documentation/green-bonds/Euronext-Green-Bond-List.xlsx"
TYPES = {"green bond": "GREEN", "social bond": "SOCIAL", "sustainability bond": "SUST", "sustainability-linked bond": "SLB"}


def parse(raw):
    import pandas as pd
    df = pd.read_excel(io.BytesIO(raw), header=0)
    out = {}
    for isin, t in zip(df["ISIN"], df["Bond Type"]):
        code = TYPES.get(str(t).strip().lower())
        if isinstance(isin, str) and code:
            out[isin.strip()] = code
    return out


def load(timeout=90):
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (kth-fic-club)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse(r.read())


if __name__ == "__main__":
    d = load()
    print(f"Euronext ESG bond list: {len(d):,} ISINs {dict(collections.Counter(d.values()))}", file=sys.stderr)
