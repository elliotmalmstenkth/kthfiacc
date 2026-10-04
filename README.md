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
| `analytics.py` | – | Accrued interest, YTM, modified duration, Z-spread vs the ECB curve, conversion factors and CTD for the Bund-family futures, issuer spread curves (rich/cheap) |
| `site/build.py` | data + ECB curve + €STR | Builds the portfolio site (`site/template.html` → `index.html`) with the day's data embedded; writes `data/history/<date>.csv.gz` and the `hist/` time series |
| `market.py` | ECB (curves, €STR, EUR/USD), NY Fed (SOFR), FRED (ICE BofA OAS, VIX) | End-of-day market monitor and the risk-factor history for VaR |
| `live.py` | Deutsche Börse minute files (DFRA-pretrade, DEUR-posttrade) | Intraday quotes for the site: a GitHub Actions job streams each minute file and pushes `live.json` to the branch `live` every minute |
| `ecb_collateral.py` | ECB list of eligible marketable assets (daily) | Credit quality per bond: Eurosystem credit quality step 1–2 (A− or better) or 3 (BBB), read from the haircut; not on the list = high yield or an issuer the ECB does not accept |
| `green.py` | Euronext ESG bond list | Green / social / sustainability / sustainability-linked labels (GRN, SOC, SUS, SLB) |
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
CLOSE trades a position out (a long is sold at the bid, a short bought back at the offer, a future at the last
price) and moves it to `closed` in the same file with its realized P&L: clean P&L at the exit price plus carry.
Total P&L = open positions marked at mid + realized P&L of closed trades.

Relative value:

- **Pairs and butterflies** (Trade → ticket → PAIR / FLY): click a leg, then a bond or a Schatz/Bobl/Bund/Buxl or
  credit future. A pair's second leg is sized to the first leg's DV01; a fly's wings each carry half the body's DV01
  (50/50). Bonds round to their minimum denomination, futures to whole lots. All legs are booked in one commit and
  grouped as a strategy (Holdings → Strategies) with level, P&L, carry and net DV01/CS01. Level (bp of yield): pair
  = long leg − short leg, fly = 2 × body − wings.
- **Rich/cheap** (RV BP column): Z-spread minus a curve fitted to the issuer's other bonds (least squares in
  ln(maturity), linear with 4–5 bonds, quadratic with 6+, one pass without outliers; fitted on firm quotes where
  possible). Positive = cheap. Only from 1 year and within the fitted maturity range.
- **Carry and roll-down**: carry = coupon accrual since the trade settled minus funding at the repo rate (ACT/360) on
  the dirty trade value; shorts pay the coupon and earn repo less an editable specialness. The repo rate defaults to
  €STR (ECB data API). Roll-down (3M) = yield change from ageing three months along the ECB AAA curve plus the
  issuer spread curve.
- **VaR / CVaR** (Risk): historical simulation over the last 500 trading days. Rates: −DV01 per futures bucket ×
  the change in the ECB AAA spot yield (2/5/10/30Y). Credit: spreads move in proportion to their level (DTS)
  against the ICE BofA Euro HY OAS; non-AAA sovereigns against the all-govt − AAA 10Y spread. 95/97.5/99% VaR and
  expected shortfall, 10-day by √10. Linear (no convexity), no idiosyncratic spread risk.
- **Markets** (key 5): government yields (ECB AAA and all-govt curves, Bund benchmarks), credit (Euro HY / US IG /
  US HY OAS as proxies for iTraxx Crossover / CDX, Eurex credit index futures), €STR and SOFR, Eurex rates futures,
  VIX and EUR/USD, with 1D/1W/1M changes. End of day: tick data, iTraxx/CDX, swap rates and Euribor are licensed.
- **Credit quality** (ECB column and filter): the Eurosystem accepts bonds rated BBB− or better. Its daily list of
  eligible assets has no ratings, but the haircut gives the credit quality step: within a haircut category, coupon
  type and maturity bucket, steps 1–2 (AAA to A−) share the lowest haircut and step 3 (BBB) has a markedly higher
  one. The ECB uses the best rating from S&P, Moody's, Fitch, DBRS and Scope, so a step can be better than one
  agency's rating (Italy: A (low) from DBRS).
  On 2 Oct 2026 the 9,259 benchmark bonds split into 4,836 ≥A−, 1,252 BBB and 3,170 not on the list (mostly bank
  MREL/holding-company bonds and issuers outside the EEA, which are ineligible whatever their rating).
- **ESG labels**: green, social, sustainability and sustainability-linked bonds from the Euronext ESG bond list
  (949 of the site's bonds on 2 Oct 2026). Bonds listed only outside Euronext (e.g. green Bunds) are not tagged.
- **This week** (key 6): the week so far against the last day before Monday: market moves, the club's week, new
  issues (issued this week per the ECB list, or quoted for the first time), the largest Z-spread moves per bond
  and per issuer, ECB credit quality changes (including bonds leaving the list), rich/cheap flips, and the club's
  coupons, maturities and futures deliveries next week. Sections that compare with last week fill once the history
  has a day before Monday.
- **Data quality**: each build re-applies the current `classify.py` rules (so older files get today's fixes) and
  then the ECB's issuer groups where they disagree (central government, regional, supranational, agency, bank). Closing
  quotes are flagged ⚠ when the bid-offer is very wide, the price implausible, the Z-spread extreme or the bond far off
  its issuer's curve; flagged quotes are left out of the issuer curves and the weekly movers. The Guide page (key 7)
  shows the collection log, the sources' latest dates, the quote checks and every reclassification.
- **Guide** (key 7): workspaces, how to trade, glossary, data sources, data status and known limitations, for new
  members.
- **History**: each site build stores the day's mid, YTM, Z-spread and rich/cheap per bond (and futures with the CTD
  yield) in `data/history/`, and publishes them as time series (Z-spread per bond, level per strategy).

## Automation (GitHub Actions)

`.github/workflows/daily.yml` runs `ci.py` every business day at 22:37 UTC, with a fallback run at 05:17 UTC
Tue–Sat and a FIRDS snapshot on Sundays. Each business day becomes a **draft release** `data-<date>` (visible only
to collaborators): marks at 12:00/17:25/close, bond classification, Eurex futures, and raw data (bond rows from
DFRA-pretrade ~340 MB, post-trade ~75 MB). The site is then rebuilt and deployed to GitHub Pages, and
`data/log.csv` gets one row per day. Manual run: Actions → Market data → Run workflow (`daily`, `site` or `firds`).

`.github/workflows/live.yml` runs `live.py` on business days from 07:45 to 17:45 Frankfurt time: one long job per
half day (GitHub schedules cannot run every minute) reads each new minute file and force-pushes `live.json` (one
commit, no history) to the branch `live`. The page polls the branch every minute when signed in (every two minutes
otherwise, for GitHub's anonymous API limit) and marks bonds and futures at the intraday prices; yields and Z-spreads
are moved by −Δprice / (dirty × modified duration) from the evening's analytics. Free MiFID data is delayed.

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
