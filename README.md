# KTH Fixed Income and Credit Club – paper portfolio

Free euro-market data for a rates and credit paper portfolio with hedging. Python ≥ 3.10.

**Portfolio site: https://elliotmalmstenkth.github.io/kthfiacc/**

## Components

| Script | Source | What it does |
|---|---|---|
| `mfs.py` | Deutsche Börse MiFID II files (mfs.deutsche-boerse.com) | Archives the per-minute pre-/post-trade files before they expire and builds daily bid/offer marks per ISIN |
| `eurex.py` | Eurex post-trade (DEUR-posttrade via `mfs.py`) | Daily OHLC, VWAP, volume and block volume for government bond and credit index futures, keyed by product ISIN |
| `firds.py` | ESMA FIRDS (FULINS) | Reference data per ISIN → SQLite (`--cat D` debt, `--cat F` futures) |
| `classify.py` | FIRDS + DFRA pre-trade | Sector classification (sovereign, covered, financial/non-financial corporate …) and coverage summary |
| `ecb_curve.py` | ECB YC (data-api.ecb.europa.eu) | Archives the ECB Svensson parameters (AAA and all euro area) since 2004; spot, forward, par and discount factors |
| `analytics.py` | – | Accrued interest, YTM, modified duration, Z-spread vs the ECB curve, conversion factors and CTD for the Bund-family futures |
| `site/build.py` | data + ECB curve | Builds the portfolio site (`site/template.html` → `index.html`) with the day's data embedded |
| `ci.py` | all of the above | Daily GitHub Actions job: fetch, build, archive each business day as a draft release, publish the site |

```bash
pip install -r requirements.txt

# every business day, evening (files expire at midnight of the next business day)
python mfs.py sync --bonds-only                 # DFRA-pretrade (bond rows only), DFRA-posttrade, DEUR-posttrade
python mfs.py marks 2026-10-02 --snap 17:30     # snapshot times in CET/CEST
python eurex.py daily 2026-10-02 --show
python ecb_curve.py update

# weekly (ESMA publishes FULINS on Saturdays)
python firds.py --cat D
python firds.py --cat F --db firds.sqlite

# classification
python classify.py --db firds.sqlite archive/DFRA-pretrade/2026-10-02/*.json.gz --out bonds.csv

# site
python site/build.py --day 2026-10-02 --ecb-db ecb_curve.sqlite

python -m pytest -q tests                       # offline tests on synthetic data
```

## Portfolio site

Anyone with the link can see the bond screener, the futures and the club portfolio. The portfolio is the file
`portfolio/positions.json` in this repo; every trade is a commit, so the commit history is the blotter.

To trade: become a collaborator on the repo, create a classic personal access token with the `public_repo` scope,
and sign in on the site (the token is stored only in your browser). Buys execute at the offer and sells at the bid.

## Automation (GitHub Actions)

`.github/workflows/daily.yml` runs `ci.py` every business day at 22:37 UTC, with a fallback run at 05:17 UTC
Tue–Sat and a FIRDS snapshot on Sundays. Each business day becomes a **draft release** `data-<date>` (visible only
to collaborators): marks at 12:00/17:25/close, bond classification, Eurex futures, and raw data (bond rows from
DFRA-pretrade ~340 MB, post-trade ~75 MB). The site is then rebuilt and deployed to GitHub Pages, and
`data/log.csv` gets one row per day. Manual run: Actions → Market data → Run workflow (`daily`, `site` or `firds`).

Download a day: `gh release download data-2026-10-02 -D dl/` (requires write access, as releases are drafts).

GitHub emails the repo owner when a run fails. One failure is not critical (the files stay on the server until
midnight of the next business day and the morning run retries); two failures for the same day are.

Data files (`archive/`, `*.sqlite`, `raw/`, `firds_raw/`) are git-ignored. `overrides.csv` (`lei,sector,comment`)
corrects misclassified issuers.

## Deutsche Börse file format (verified 3 Oct 2026)

- API: `GET /api/<feed>` → `{"CurrentFiles": [...]}`; `GET /api/download/<file>` returns a 301 to a Google
  Storage URL valid for 2 seconds. The server rate-limits (HTTP 429); downloads from GitHub runners take ~45 min
  per per-minute feed.
- Feeds: `DFRA-`, `DETR-`, `DEUR-`, `DETG-`, `DGAT-` × `pretrade`/`posttrade`. `DEUR-pretrade` is listed but its
  files return 404.
- File timestamps are **UTC**, start of the minute. A trading day (Frankfurt time) runs from `<day-1>T23_00` to
  `<day>T21_00` UTC. The archive is organised by trading day.
- The server keeps ~1 trading day (`DaysToKeepOnWebpage: 1`).
- Some feeds have a daily file `<feed>-daily-<date>.json.gz` (DEUR-posttrade: 28 MB, used instead of the minute
  files). DFRA has none.
- **DFRA-pretrade is large:** ~2.5 MB per minute compressed, ~90,000 messages per minute intraday, ~26% bonds
  (`priceNotation` 2): ~2 GB per day raw. `sync --bonds-only` keeps bond rows only.
- **Pre-trade messages are deltas**: each carries only the side(s) that changed (`bestBid`/`bestBidQty` and/or
  `bestAsk`/`bestAskQty`) plus `updateDateAndTime`. `mfs.py marks` merges them.
- Price 0 = side withdrawn. At the close (17:30 CET, `tradingSystemPhase` 202) every bond goes to bid = offer = 0.
  Price > 0 with size 0 = indicative quote. The `close` mark is therefore the **last two-way quote** of the day.
- Eurex (DEUR-posttrade): ~825,000 trades per day, identified by product ISIN + `contractDate`. `mmtTradingMode`
  2 = on-book, 5 = off-book/block, O/K = auctions; `mmtModificationInd` C = cancellation.
- Bonds on Börse Frankfurt trade on venues `FRAB` (most) and `FRAA`.
- FIRDS occasionally reports coupons in the wrong scale (45 for 4.5%); `site/build.py` replaces coupons above 20%
  with the coupon in the bond's ticker name.

## Universe on 2 Oct 2026 (Börse Frankfurt + FIRDS 3 Oct)

35,790 bond ISINs quoted (`priceNotation` 2), 35,777 found in FIRDS, 18,858 EUR-denominated.

| EUR, sector | count | two-way | firm two-way |
|---|---:|---:|---:|
| Financials (CORP_FIN) | 8,367 | 8,366 | 3,171 |
| Non-financial corporates (CORP_NONFIN) | 4,958 | 4,954 | 4,659 |
| Sovereigns (SOV) | 1,198 | 1,198 | 1,015 |
| Covered (COVERED) | 1,141 | 1,141 | 822 |
| Structured / CLN | 1,007 | 1,007 | 208 |
| Agencies | 862 | 862 | 461 |
| Sub-sovereigns | 763 | 763 | 350 |
| Supranationals | 330 | 330 | 263 |
| ABS/MBS, convertibles | 232 | 229 | 189 |

**EUR corporates: 13,325**, but ~6,000 of the financials are German Landesbank/DZ retail notes (Helaba 2,322,
DZ Bank 1,759, NordLB 608, LBBW 564, Deka 390). The investable universe:

- issue size ≥ EUR 500m, fixed/zero coupon, senior (hybrids and perpetuals excluded): **4,862**, of which 4,781 with
  firm two-way quotes
- non-financials: **3,517** (3,480 firm)
- closing quotes: median bid-offer 0.50 points (10th–90th percentile 0.20–1.01); 90% updated in the last hour
  before 17:30

## Notes

- ECB curve: `ecb_curve.py check` reproduces the ECB's published spot, forward and par yields to 0.0000 bp.
- **Credit index futures trade on Eurex** (confirmed 2 Oct 2026): FEHY (Euro HY, 37 trades), FECX (Euro Corporate
  "MSCI Screened", Eurex's euro IG contract, 25 trades), FGBC (Sterling Corporate, 6 trades), plus FUIG/FUHY/FUEM.
  Eurex reports by product ISIN + `contractDate`, not the FIRDS contract ISIN (see `eurex.PRODUCTS`). Index
  durations are not in the feed and are editable assumptions on the site.
- No credit ratings are available in the free data, so IG and HY cannot be separated.
- Licence: the Deutsche Börse files are free for non-commercial use.
