# Colour system

The site follows the colour roles of the Bloomberg Terminal. Amber is Bloomberg's base text colour, but it is used
as thin text for identifiers and as the fill of small editable fields, never for large blocks.

## Roles

| Role | Bloomberg Terminal | This site | Token |
|---|---|---|---|
| Background | black | `#000000` | `--bg` |
| Numbers / data | white or amber text | white `#e6e6e6` | `--fg` |
| Identifiers (security names, tickers, contract codes) | amber text | amber `#f39a1e` | `--amber` |
| Editable fields | amber fill, black text ("amber fields") | amber fill `#fc9c2a`, black text | `--field` |
| Command line / search | black with blue cursor | black, amber text, blue focus | `input.cmd` |
| Column headers, section rows | grey bands, light text | grey bands `#1a1a1a`/`#262626`, grey text | `--band-2`, `--band` |
| Function menu bar | red band, white text | active workspace in red, red rule under the function bar; red dialog headers | `--red-bar` |
| Clickable rows | white outline on hover | 1px white outline on hover | – |
| Asset-class tabs (SECF: Corp, Govt …) | dark grey tabs, active tab light grey with black text | same; CORP / GOVT / SSA / COVERED in the screener | `.atabs` |
| Selection / focus | blue cursor, highlighted row | dark-blue row, blue left marker, blue focus ring | `--sel`, `--blue` |
| Up / down | green / red | green `#1ee36f` / red `#ff3b30` | `--pos`, `--neg` |
| Up / down, CVD mode | blue / red | blue `#3fa9ff` / red (toggle **CVD** in the status line) | `[data-cvd="1"]` |
| Exposure bars (DV01, MV) | – | neutral grey; green/red only for P&L | – |

## Measured distribution (share of non-background pixels)

Same classifier for all images (hue bands, near-greys counted as neutral). The Bloomberg figures come from screenshots
in Bloomberg's own "Getting started" guide; images marked * contain the guide's cyan annotation boxes, which inflate
blue.

| Screen | neutral | amber | red | green | blue |
|---|---:|---:|---:|---:|---:|
| Bloomberg WEI (World Equity Indices) | 60% | 8% | 11% | 3% | 16%* |
| Bloomberg security search | 72% | 7% | 1% | 0% | 17%* |
| Bloomberg FX menu + rates table | 44% | 24% | 11% | 1% | 15%* |
| This site before (dashboard) | 27% | 42% | 1% | 4% | 16% |
| This site now (dashboard) | 53% | 13% | 7% | 3% | 17% |

Red/green passes a colour-vision-deficiency check (deutan ΔE 13.6, target ≥ 8); blue/red gives ΔE 28.2, which is why
Bloomberg offers it as the CVD scheme. Every change also carries a +/− sign, so colour is never the only cue.

## Sources

- Bloomberg, *Getting started on the Bloomberg Terminal* (via University of Kent): red menu bar, amber fields,
  highlight on clickable items; screenshots used for the measurements.
- Bloomberg, *Getting started in Launchpad* (via University of Delaware): yellow cells for entry, orange highlight
  for selection in monitors.
- Bloomberg UX, *Designing the Terminal for color accessibility* (2021): blue/red CVD scheme, amber kept for
  non-semantic information.
- Imperial College London library guide, *Display and navigation*: "amber fields indicate areas on the screen that
  you can change".
- Ted Merz, *Amber on Black* (2021): history of the amber-on-black scheme.
