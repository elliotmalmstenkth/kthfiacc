# Sparade resultat

Små, komprimerade utdrag som går att använda utan att köra om hela pipelinen.
Rådata (Deutsche Börse-arkivet, FIRDS, ECB) ligger utanför git – se huvud-README.

## 2026-10-02 (fredag; FIRDS 2026-10-03)

| Fil | Innehåll |
|---|---|
| `bonds_classified.csv.gz` | 35 790 obligationer från DFRA-pretrade med FIRDS-data, sektor, regel, efterställd, kupongtyp, ≥ 500 mn, tvåsidig/fast tvåsidig (`classify.py`) |
| `bonds_classified_issuers.csv.gz` | Emittentlista per LEI och sektor – granska och rätta via `overrides.csv` |
| `dfra_quotes.csv.gz` | Bud/sälj per ISIN: `12:00`, `17:25` (Frankfurttid) och `close` = dagens senaste tvåsidiga kurs (`mfs.py marks`). Volym 0 = indikativ kurs |
| `eurex_futures_daily.csv` | Stats- och kreditindexterminer: affärer, volym, block, OHLC, VWAP (`eurex.py daily`) |

```python
import pandas as pd
b = pd.read_csv("data/2026-10-02/bonds_classified.csv.gz", index_col="isin", low_memory=False)
q = pd.read_csv("data/2026-10-02/dfra_quotes.csv.gz").query("snap == 'close'").set_index("isin")
universe = b[(b.ccy == "EUR") & b.sector.isin(["CORP_FIN", "CORP_NONFIN"]) & b.benchmark_500m
             & b.coupon_type.isin(["fixed", "zero"]) & ~b.subordinated].join(q[["bid", "ask"]], rsuffix="_px")
```

Deutsche Börse-data: gratis för icke-kommersiell användning.
