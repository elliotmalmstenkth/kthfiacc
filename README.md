# KTH Fixed Income and Credit Club – data för pappersportföljen

Verktyg för gratis eurodata till en ränte-/kreditportfölj med hedgning. Kräver Python ≥ 3.10.

| Skript | Källa | Vad det gör |
|---|---|---|
| `mfs.py` | Deutsche Börse MiFID II-filer (mfs.deutsche-boerse.com) | Arkiverar minutfilerna (pre-/post-trade) innan de försvinner och bygger dagliga bud/sälj-kurser per ISIN |
| `eurex.py` | Eurex post-trade (DEUR-posttrade via `mfs.py`) | Dagliga priser (OHLC, VWAP, volym, block) för stats- och kreditindexterminer, identifierade via produkt-ISIN |
| `firds.py` | ESMA FIRDS (FULINS) | Referensdata per ISIN → SQLite (`--cat D` obligationer, `--cat F` terminer) |
| `classify.py` | FIRDS + DFRA-pretrade | Sektorklassning (stat, säkerställd, företag finans/icke-finans …) och sammanfattning av hur många EUR-företagsobligationer som har kurser |
| `ci.py` | allt ovan | Daglig körning i GitHub Actions: hämtar, bygger och sparar varje handelsdag som utkast-release |
| `analytics.py` | – | Yield, duration, Z-spread mot ECB-kurvan, omvandlingsfaktor och CTD för Bund-terminerna |
| `site/build.py` | data/<dag>/ + ECB | Bygger portföljsidan (`site/template.html` → `site/portfolio.html`) med inbäddad data |
| `ecb_curve.py` | ECB YC (data-api.ecb.europa.eu) | Arkiverar ECB:s Svensson-parametrar (AAA + alla euroländer) sedan 2004 och räknar spot/termin/par/DF |

```bash
pip install -r requirements.txt

# varje bankdag, kväll (filerna raderas vid midnatt nästa bankdag)
python mfs.py sync --bonds-only         # DFRA-pretrade (bara obligationer), DFRA-posttrade, DEUR-posttrade
python mfs.py marks 2026-10-02 --snap 17:30     # Frankfurttid
python eurex.py daily 2026-10-02 --show
python ecb_curve.py update

# varje vecka (FULINS publiceras på lördagar)
python firds.py --cat D
python firds.py --cat F --db firds.sqlite

# klassning
python classify.py --db firds.sqlite archive/DFRA-pretrade/2026-10-02/*.json.gz --out bonds.csv

python -m pytest -q tests               # offline-tester med syntetiska data
```

### Portföljsidan

**https://elliotmalmstenkth.github.io/kthfiacc/** (GitHub Pages, byggs om efter varje dagskörning).
Alla med länken ser obligationerna, terminerna och klubbens portfölj. Portföljen är filen
`portfolio/positions.json` i repot; varje köp/sälj blir en commit. För att handla: bli collaborator i repot,
skapa en klassisk GitHub-nyckel med `public_repo` och logga in på sidan (nyckeln sparas bara i webbläsaren).
Bygg lokalt: `python site/build.py --day 2026-10-02 --ecb-db ecb_curve.sqlite`.

### Automatiskt: GitHub Actions

`.github/workflows/daily.yml` kör `ci.py` varje bankdag kl. 22:37 UTC, med en reservkörning 05:17 UTC tis–lör och
FIRDS på söndagar. Efter dagskörningen byggs portföljsidan och publiceras på GitHub Pages. Varje handelsdag blir en **utkast-release** `data-<dag>` (bara synlig för dem med skrivrätt):
kurser 12:00/17:25/close, Eurex-terminer och rådata (obligationsrader ur DFRA-pretrade ~340 MB, post-trade ~75 MB).
`data/log.csv` får en rad per dag. Manuell körning: Actions → Marknadsdata → Run workflow (valfritt `days`).

Hämta en dag lokalt: `gh release download data-2026-10-02 -D dl/` (kräver skrivrätt eftersom det är ett utkast).

Exempel på cron (Stockholmstid) om ni hellre kör på en egen dator:

```
15 22 * * 1-5  cd ~/kthfiacc && python mfs.py sync --bonds-only && python mfs.py marks $(date +\%F) && python ecb_curve.py update
15 07 * * 1-6  cd ~/kthfiacc && python mfs.py sync --bonds-only    # fångar gårdagens sena filer
```

Datafiler (`archive/`, `*.sqlite`, `raw/`, `firds_raw/`) är undantagna från git.
`overrides.csv` (`lei,sector,kommentar`) används för att rätta felklassade emittenter.

## Vad vi vet om Deutsche Börse-filerna (verifierat 3 okt 2026)

- API: `GET /api/<flöde>` → `{"CurrentFiles": [...]}`; nedladdning `GET /api/download/<fil>` ger 301 till en
  Google Storage-länk som gäller i 2 sekunder (urllib följer den automatiskt).
- Flöden som finns: `DFRA-`, `DETR-`, `DEUR-`, `DETG-`, `DGAT-` × `pretrade`/`posttrade`. `DEUR-pretrade` listas
  men minutfilerna ger 404.
- Filnamnens tid är **UTC** och anger minutens början. En handelsdag (Frankfurttid) går från
  `<dag-1>T23_00` till `<dag>T21_00` UTC. Arkivet sorterar på handelsdag.
- Servern håller ~1 handelsdag (`DaysToKeepOnWebpage: 1`). En lördag finns alltså bara fredagens filer.
- Vissa flöden har en dagsfil `<flöde>-daily-<datum>.json.gz` (DEUR-posttrade: 28 MB). DFRA-pretrade: 404.
- **DFRA-pretrade är stort:** ~2,5 MB/minut komprimerat, ~90 000 meddelanden/minut dagtid, varav ~26 % obligationer
  (`priceNotation` 2). Det blir ~2 GB/dag rått; `sync --bonds-only` sparar bara obligationsraderna.
- **Pre-trade-meddelanden är deltor**: varje meddelande innehåller bara de sidor som ändrats (`bestBid`/`bestBidQty`
  och/eller `bestAsk`/`bestAskQty`) plus `updateDateAndTime`. `mfs.py marks` slår ihop sidorna.
- Pris 0 = sidan borttagen. Vid handelsslut (17:30 Frankfurt, `tradingSystemPhase` 202) får alla obligationer
  bid = ask = 0. Pris > 0 med volym 0 = indikativ kurs. `marks` sparar därför `close` som dagens **senaste tvåsidiga**
  läge, och `classify` skiljer på tvåsidig (priser) och fast tvåsidig (även volym).
- Eurex (DEUR-posttrade): ~825 000 affärer/dag; identifieras med produkt-ISIN + `contractDate`. `mmtTradingMode` 2 =
  orderbok, 5 = off-book/block, O/K = auktioner; `mmtModificationInd` C = makulering.
- Obligationer på Börse Frankfurt handlas på venue `FRAB` (de flesta) och `FRAA`.

## Resultat 2 okt 2026 (Börse Frankfurt + FIRDS 3 okt)

35 790 obligations-ISIN med kurser (priceNotation 2), varav 35 777 i FIRDS och 18 858 EUR-denominerade.

| EUR, sektor | antal | tvåsidig | fast tvåsidig |
|---|---:|---:|---:|
| Företag, finans (CORP_FIN) | 8 623 | 8 622 | 3 311 |
| Företag, icke-finans (CORP_NONFIN) | 4 961 | 4 957 | 4 662 |
| Stat (SOV) | 1 196 | 1 196 | 1 013 |
| Strukturerade/CLN | 1 007 | 1 007 | 208 |
| Säkerställda (COVERED) | 886 | 886 | 683 |
| Agency | 860 | 860 | 459 |
| Delstat/kommun | 763 | 763 | 350 |
| Supra | 330 | 330 | 263 |
| ABS/MBS, konvertibler | 232 | 229 | 189 |

**EUR-företagsobligationer: 13 584**, men ~6 000 av finansbolagen är tyska Landesbank-/DZ-privatkundsobligationer
(Helaba 2 322, DZ Bank 1 759, NordLB 608, LBBW 564, Deka 390). Det investerbara universumet:

- emission ≥ 500 mn EUR, fast/nollkupong, senior (hybrider/eviga exkl.): **5 058**, varav 4 911 med fasta tvåsidiga kurser
- varav icke-finansiella: **3 520** (3 483 fasta)
- slutkurser för dessa: median-spread 0,50 per 100 nominellt (10–90 %: 0,20–1,01), 90 % uppdaterade sista timmen före 17:30

## Status och öppna frågor

- ECB-kurvan: `ecb_curve.py check` återskapar ECB:s publicerade spot/termin/par med 0,0000 bp avvikelse.
- Arkivet behöver köras på en dator som alltid är på – molnsessionerna är tillfälliga.
- **Kreditindexterminer finns i Eurex-filerna** (bekräftat 2 okt 2026): FEHY (Euro HY, 37 affärer), FECX (Euro Corporate
  "MSCI Screened" – Eurex Euro IG-produkt, 25 affärer), FGBC (Sterling Corporate, 6 affärer), samt FUIG/FUHY/FUEM.
  Eurex rapporterar med produkt-ISIN + `contractDate`, inte FIRDS kontrakts-ISIN – se `eurex.PRODUCTS`.
- Licens: Deutsche Börse-filerna är gratis för icke-kommersiell användning.
