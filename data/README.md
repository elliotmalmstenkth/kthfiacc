# Saved results

Small compressed extracts you can use without re-running the pipeline.
Raw data (the Deutsche Börse archive, FIRDS, ECB) is kept out of git; see the main README.

## 2026-10-02 (Friday; FIRDS 2026-10-03)

| File | Contents |
|---|---|
| `bonds_classified.csv.gz` | 35,790 bonds from DFRA-pretrade with FIRDS data, sector, rule, subordinated flag, coupon type, ≥ EUR 500m, two-way/firm two-way (`classify.py`) |
| `bonds_classified_issuers.csv.gz` | Issuer list by LEI and sector; review and correct via `overrides.csv` |
| `dfra_quotes.csv.gz` | Bid/offer per ISIN at `12:00`, `17:25` (Frankfurt time) and `close` = last two-way quote of the day (`mfs.py marks`). Size 0 = indicative quote |
| `eurex_futures_daily.csv` | Government bond and credit index futures: trades, volume, block volume, OHLC, VWAP (`eurex.py daily`) |

```python
import pandas as pd
b = pd.read_csv("data/2026-10-02/bonds_classified.csv.gz", index_col="isin", low_memory=False)
q = pd.read_csv("data/2026-10-02/dfra_quotes.csv.gz").query("snap == 'close'").set_index("isin")
universe = b[(b.ccy == "EUR") & b.sector.isin(["CORP_FIN", "CORP_NONFIN"]) & b.benchmark_500m
             & b.coupon_type.isin(["fixed", "zero"]) & ~b.subordinated].join(q[["bid", "ask"]], rsuffix="_px")
```

Deutsche Börse data: free for non-commercial use.
