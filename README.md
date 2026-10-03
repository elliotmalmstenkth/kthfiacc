# KTH Fixed Income and Credit Club – data för pappersportföljen

Verktyg för gratis eurodata till en ränte-/kreditportfölj med hedgning. Kräver Python ≥ 3.10.

| Skript | Källa | Vad det gör |
|---|---|---|
| `mfs.py` | Deutsche Börse MiFID II-filer (mfs.deutsche-boerse.com) | Arkiverar minutfilerna (pre-/post-trade) innan de försvinner och bygger dagliga bud/sälj-kurser per ISIN |
| `firds.py` | ESMA FIRDS (FULINS) | Referensdata per ISIN → SQLite (`--cat D` obligationer, `--cat F` terminer) |
| `classify.py` | FIRDS + DFRA-pretrade | Sektorklassning (stat, säkerställd, företag finans/icke-finans …) och sammanfattning av hur många EUR-företagsobligationer som har kurser |
| `ecb_curve.py` | ECB YC (data-api.ecb.europa.eu) | Arkiverar ECB:s Svensson-parametrar (AAA + alla euroländer) sedan 2004 och räknar spot/termin/par/DF |

```bash
pip install -r requirements.txt

# varje bankdag, kväll (filerna raderas vid midnatt nästa bankdag)
python mfs.py sync                      # DFRA-pretrade, DFRA-posttrade, DEUR-posttrade
python mfs.py marks 2026-10-02 --snap 15:30
python ecb_curve.py update

# varje vecka (FULINS publiceras på lördagar)
python firds.py --cat D
python firds.py --cat F --db firds.sqlite

# klassning
python classify.py --db firds.sqlite archive/DFRA-pretrade/2026-10-02/*.json.gz --out bonds.csv

python -m pytest -q tests               # offline-tester med syntetiska data
```

Exempel på cron (Stockholmstid), t.ex. på en dator som alltid är på:

```
15 22 * * 1-5  cd ~/kthfiacc && python mfs.py sync && python mfs.py marks $(date +\%F) && python ecb_curve.py update
15 07 * * 1-6  cd ~/kthfiacc && python mfs.py sync    # fångar gårdagens sena filer
```

Datafiler (`archive/`, `*.sqlite`, `raw/`, `firds_raw/`) är undantagna från git.
`overrides.csv` (`lei,sector,kommentar`) används för att rätta felklassade emittenter.

## Status och öppna frågor

- **Testat:** logiken är testad offline med syntetiska data. API-anropen mot ECB, ESMA och Deutsche Börse har inte körts från den här miljön.
- **`mfs.py`** antar API:t `GET /api/<flöde>` → `{"CurrentFiles": [...]}` och `GET /api/download/<fil>`. Flödesnamnet för Eurex (`DEUR-posttrade`) är inte bekräftat – kontrollera med `python mfs.py list DEUR-posttrade`.
- **Tidszon:** tiderna i filnamnen behandlas som UTC. Kontrollera det mot handelstiderna.
- **Kreditindexterminer** (Euro IG/HY, GBP Corporate) är inte bekräftade i Eurex-filerna. Leta upp deras ISIN med `firds.py --cat F` och sök sedan i arkivet.
- Licens: Deutsche Börse-filerna är gratis för icke-kommersiell användning.
