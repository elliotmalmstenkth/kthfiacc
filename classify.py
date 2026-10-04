#!/usr/bin/env python3
"""
Classify the bonds in Börse Frankfurt's pre-trade files using FIRDS.

  python classify.py --db firds.sqlite DFRA-pretrade-*.json.gz --out bonds.csv

Steps:
 1. Collect ISINs with priceNotation 2 (percent of par = bonds) and their quote sides
    (bid/offer) from one or more DFRA-pretrade files.
 2. Look them up in FIRDS (table firds_d from firds.py).
 3. Assign one sector per ISIN with rules in order:
      instrument type (CFI + FISN) -> manual LEI overrides -> known issuers
      (sovereign/sub-sovereign/supra/agency) -> covered -> financial/non-financial.
    The rules are heuristic. Override with overrides.csv (lei,sector,comment).

Sectors:
  SOV            central government
  SUBSOV         state/region/municipality
  SUPRA          supranational
  AGENCY         government-related development or funding bank
  COVERED        covered bond (Pfandbrief, SFH, OBG, cédulas ...)
  CORP_FIN       corporate, financial sector (banks, insurers, finance companies)
  CORP_NONFIN    corporate, non-financial
  CONVERTIBLE    convertible
  SECURITISED    ABS/MBS
  STRUCTURED     structured/credit-linked (CLN) or other
  UNKNOWN        not in FIRDS
"""
import argparse, collections, csv, gzip, json, os, re, sqlite3, sys
import pandas as pd

# --- issuer lists (matched against the FISN issuer part, upper case, before "/") ----
SOV = [r"^BUND DEUTSCHLAN", r"^BUNDESREP", r"^ITALIA\b", r"^ESTADO\b", r"^ESPANA", r"^REP OEST",
       r"^OESTERREICH", r"^BELGIQUE", r"^BELGIUM", r"^FINLAND\b", r"^REP PORTUGUESA", r"^GR GOVT",
       r"^HELLENIC REP", r"^ROMANIA\b", r"^MINFINSLOREP", r"^DIRECTION GENER", r"^FRANCE\b",
       r"^NETHERLANDS\b", r"^NEDERLAND\b(?!.WATER)", r"^STAAT DER NED", r"^IRELAND\b", r"^SLOVAKIA",
       r"^SLOVAK REP", r"^LITHUANIA", r"^LATVIA", r"^ESTONIA", r"^CROATIA", r"^CYPRUS", r"^MALTA\b",
       r"^LUXEMBOURG\b", r"^GRAND DUCHY", r"^POLAND\b", r"^REP POLAND", r"^REPUBLIC OF", r"^REP\.? ",
       r"^HUNGARY", r"^BULGARIA", r"^CZECH", r"^SLOVENIA", r"^ICELAND", r"^DENMARK\b", r"^SWEDEN\b",
       r"^KINGDOM OF", r"^NORWAY\b", r"^MEXICO\b", r"^UNITED MEXICAN", r"^US T(REAS| NOTES| BONDS|SY)",
       r"^UNITED STATES", r"^UK\b", r"^UNITED KINGDOM", r"^CANADA\b", r"^AUSTRALIA$", r"^COMMONWEALTH OF AUS", r"^JAPAN\b",
       r"^ISRAEL\b", r"^TURKEY\b", r"^TURKIYE", r"^CHILE\b", r"^PERU\b", r"^COLOMBIA\b", r"^BRAZIL\b",
       r"^INDONESIA\b", r"^PHILIPPINES\b", r"^SOUTH AFRICA", r"^SERBIA", r"^MONTENEGRO", r"^NORTH MACED",
       r"^ALBANIA", r"^BOSNIA", r"^UKRAINE", r"^GEORGIA\b", r"^MOROCCO", r"^EGYPT", r"^IVORY COAST",
       r"^COTE D", r"^BENIN", r"^SENEGAL", r"^SCHWEIZ", r"^SWISS CONFED", r"^EIDGENOSS", r"^NEW ZEALAND",
       r"^GOVT OF", r"^AGENTSCHAP", r"^DUTCH STATE", r"STAAT DER NE", r"^MINISTERIE VAN", r"^MINISTRY OF FI", r"^PRC MOF", r"^HKSAR", r"^STATE OF (SERBIA|ISRAEL)", r"^REPUBLIC SERBIA", r"^FEDERATIVE REPU", r"^PRESIDENCIA DE", r"^FISCO DE LA REP", r"^IR GV", r"^ROI\b", r"^GOVERNMENT OF", r"^CHINA\b", r"^KOREA\b", r"^SAUDI ARAB", r"^ABU DHABI",
       r"^QATAR\b", r"^ARGENTIN", r"^PANAMA\b", r"^URUGUAY", r"^DOMINICAN", r"^ECUADOR", r"^NIGERIA"]
SUBSOV = [r"^(LAND |FREISTAAT)", r", LAND\b", r"^HESSEN", r"^BERLIN(, LAND|$)", r"^SACHSEN", r"^BRANDENBURG",
          r"^NIEDERSACHSEN", r"^NORDRHEIN-WESTF", r"^RHEINLAND-PFALZ", r"^SCHLESW", r"^SAARLAND",
          r"^BREMEN", r"^HAMBURG$", r"^FREIE.*HAMBURG", r"^BAD\.?-WUERTT", r"^BADEN-WUERTT(?!.*BANK)", r"^THUERINGEN",
          r"^BAYERN", r"^MECKLENB", r"^COMMUN(AUTE)? ", r"^REGION ", r"^ILE-DE-FRANCE", r"^VILLE DE",
          r"^COMUNIDAD", r"^JUNTA DE", r"^GENERALITAT", r"^XUNTA", r"^PROVINCE", r"^PROV(INCIA)?\b",
          r"^ONTARIO", r"^QUEBEC", r"^BRITISH COLUMB", r"^ALBERTA", r"^MANITOBA", r"^NEW SOUTH WALES",
          r"^NSW TREAS", r"^QUEENSLAND", r"^QLD TREAS", r"^TREAS(URY)? CORP", r"^VICTORIA\b", r"^NTTY\b",
          r"^WESTERN AUST", r"^SOUTH AUST", r"^TASMANIA", r"^STADT ", r"^CITY OF", r"^KANTON",
          r"^CANTON", r"^WIEN\b", r"^REGIONE", r"^CITTA", r"^AUTONOMOUS", r"^VLAAMS", r"^CC\.?AA\.", r"^JUNTA", r"^GENCAT", r"^AUCKLAND COUNCI", r"^GOUVERNEMENT DE", r"^LDHST"]
SUPRA = [r"^EUROP(AE|Ä)ISCHE UN", r"^EUROPEAN UNION", r"^EUROP(EAN|\.)? ?INV", r"^EUROPEAN INV",
         r"^EFSF", r"^EUROPEAN FIN", r"^ESM\b", r"^EUROPEAN STAB", r"^NORDIC INVEST", r"^ASIAN DEVELOP",
         r"^ASIAN INFRA", r"^AFRICAN DEV", r"^INTER-AMERICAN", r"^INTER AMERICAN", r"^EUROPEAN BK",
         r"^EBRD", r"^EUROP.BK", r"^COUNCIL OF EUR", r"^COUNCIL EUR", r"^CEB\b", r"^EUROFIMA",
         r"^INTL FIN", r"^INTERNATIONAL FIN", r"^IBRD", r"^WORLD BANK", r"^EUROPEAN ATOMIC", r"^EURATOM", r"^OPEC FUND", r"^ISDB", r"^ISLAMIC DEV", r"^DEVELOPMENT BAN", r"^INTERNATIONAL D", r"^AFRICA FIN"]
AGENCY = [r"^KFW", r"^KREDITANST", r"^LANDWIRT", r"^RENTENBANK", r"^NRW\.?BANK", r"^WI ?BANK",
          r"^INV\.?BK", r"^INVESTITIONSBANK", r"^L-BANK", r"^LFA ", r"^IFB HAMBURG", r"^SAECHS.*AUFBAU",
          r"^ISB ", r"^BNG\b", r"^BK NEDERLANDSE GEM", r"^NEDERLAND\.?WATER", r"^NEDERLANDSE WAT", r"^NWB\b",
          r"^CAISSE D'AMORT", r"^CAISSE AMORT", r"^CADES", r"^AGENCE FRANCAIS", r"^AGENCE FRANCE", r"^BPIFRANCE",
          r"^UNEDIC", r"^SFIL\b", r"^CAISSE DES DEP", r"^SNCF RESEAU", r"^SOCIETE DU GRAND",
          r"^BANK GOSPODARST", r"^MUNICIPALITY FI", r"^KUNTARAHOITUS", r"^KOMMUNALBANK", r"^KOMMUNINVEST",
          r"^KOMMUNEKREDIT", r"^INSTITUTO DE CR", r"^ICO\b", r"^CASSA DEPOSITI", r"^OESTERR.*KONTROLL",
          r"^OEKB", r"^EXPORT DEV", r"^SVENSK EXPORT", r"^KOMMUNALKREDIT AUSTRIA", r"^EXPORTFINANS",
          r"^JAPAN BANK FOR", r"^JAPAN FIN", r"^KOREA DEV", r"^EXPORT-IMPORT", r"^ERSTE ABWICKL",
          r"^FMS WERTMANAG", r"^CDP\b", r"^KOREA HOUSING", r"^EXPORT DEVELOPM", r"^BAY\.? ?LDESBODEN", r"^LKB BW", r"^OEST\.?KONTROLL", r"^AB SVENSK EXP", r"^FINNVERA", r"^CDC\b", r"^SOCIETE DU GRAN", r"^ACTION LOGEMENT", r"^AFD\b", r"^ASFINAG", r"^OEBB", r"^RATP\b", r"^REGIE AUTONOME", r"^UNION NATIONALE", r"^SAGESS", r"^KOMMUNALKR", r"^EFA\b", r"^SOCIETE NATIONA", r"^SNCF", r"^ADIF", r"^FADE\b", r"^HEIMSTADEN BOSTAD NEVER"]
# Issuers caught by a broader rule above (e.g. ^CHINA, ^KOREA, ^ICELAND are sovereign patterns), checked first.
# FISN issuer names are cut at 15 characters, so patterns match the cut name.
EXCEPTIONS = [
    (r"^CHINA CON(STR)?\.? ?BK|^BANK OF CHINA|^ICBC|^AGRICULTURAL BK", "CORP_FIN"),
    (r"^CHINA THREE GOR|^CHINA SOUTHERN|^STATE GRID|^SINOPEC|^CNOOC|^PETROCHINA", "CORP_NONFIN"),
    (r"^ICELAND BONDCO", "CORP_NONFIN"),                               # Iceland Foods (UK supermarket), not the state
    (r"^CHINA DEV|^KOREA (HOUSING|HSG|LAND|DEV|EXIM|EXPRESSWAY)|^JAPAN (BANK FOR|BK FOR|FINANCE|FIN\.)|^DEVELOPMENT BK OF JAPAN|"
     r"^THE EXPORT-IMPO|^MAGYAR EXPORT|^HUNGARIAN DEV|^SAECHS\.? ?AUFB|^SAECHSISCHE AUF|^HAMB\.? ?INV|^HAMBURGISCHE IN|"
     r"^SID - SLOVENSKA|^SID SLOVENSKA", "AGENCY"),
    (r"^COMPAGNIE DE FI|^FINANCEMENT FON|^CIE DE FINANCEM|^ARKEA PUBLIC SE", "COVERED"),  # SCF issuers (obligations foncières)
    (r"^UTD\.? ?MEXICAN|^PERUSAHAAN PENE|^PERUSAHAAN PERS", "SOV"),        # Mexico; Indonesia's sukuk issuer
    (r"^THE REPUBLIC OF|^REP\.? ?COTE D|^ARAB REPUBLIC|^MACEDONIA$", "SOV"),  # Argentina, Benin, Cameroon, Côte d'Ivoire, Egypt, N. Macedonia
    (r"^BANQUE OUEST AF", "SUPRA"),                                         # West African Development Bank (BOAD)
]

# LEI-based cases where the FISN name is ambiguous
LEI_SECTOR = {
    "ZTMSNXROF84AHWJNKQ93": "SUPRA",   # IBRD (World Bank)
    "P41R60HC414IWQA1XW02": "SUPRA",   # IDA
    "QKL54NQY28TCDAI75F60": "SUPRA",   # IFC
    "VGRQXHF3J8VDLUA7XE92": "CORP_NONFIN",  # IBM ("INTERNATIONAL B")
    "969500KCGF3SUYJHPV70": "SOV",     # Republique Francaise (AFT)
    "5493007SJLLCTM6J6M37": "CORP_FIN",  # Unicaja Banco ("UNI")
    "A6NZLYKYN1UV7VVGFX65": "CORP_FIN",  # Argenta Spaarbank ("ASPA")
    "96950015LNMQ336X4W81": "AGENCY",    # SAGESS (French state strategic stockpile agency)
}
COVERED_DESC = r"\b(MTH|HPF|OPF|PF|PFE|PFB|HYPF|HYP\.?PF|OEPF|COV|COVERED|CB|OBG|CEDHIP|CED|OHF|SCF|SFH|OMH|PANDBR|OBLIGATIONS FONC)\b"
COVERED_ISS = r"(\bSFH\b|\bSCF\b|HOME LOA|BOLIGKREDIT|^CFF\b|CAISSE DE REFIN|JELZALOG|CAISSE FRANCAIS|COVERED|HYPOTEKSBANK|PANDBRIEF|CEDULAS|\bHL SFH)"
COVERED_NAME = r"(?i)\bCov\.?\s?Bds?\b|\bCovered\b|Pfandbr|Hyp\.?-?Pfe|Öff\.?\s?Pfe|Obligations? Foncières|\bC[ée]dulas\b|\bOBG\b|\bSFH\b"
STRUCT_DESC = r"\b(CLN|CLF|CLS|CERT|ZERT|WAR|EXPRESS|BONUS|AKTIENANL|RELAX|DISCOUNT|TURBO)\b"
FIN = (r"(BANK|\bBCO\b|\bBCA\b|\bBQUE\b|PASCHI|\bCGD\b|\bHCOB\b|SPORITELN|SPOR\b|IBERCAJA|AUSTRALIA AND N|TORONTO-DOM|\bBFCM|\bBCFM|CR\.?MUTUEL|CA EUSKADI|ASSICURA|METROPOLITAN LI|RUCKV|RUECKV|NATIONALE-NEDER|\bSCOR\b|AVIVA|AGEAS|ACHMEA|\bASR\b|LEGAL & GEN|LA MONDIALE|MACIF|MALAKOFF|COVEA|ZURICH|SWISS RE|AXASA|CHUBB|LIBERTY MUTUAL|AMERICAN INTL\.?G|MARSH &|FAIRFAX|SAMPO|UNIPOL|FIDELIDADE|ATHORA|LANSFORSAKR|ICCREA|CASSA CENTRALE|\bSPK\b|\bVB WIEN|ZUERCHER KB|\bKB\b|\bBKT\b|S-PANKKI|PERMANENT TSB|ALLIED IRISH|ALPHA SERVICES|INVESTEC|CRELAN|ERSTE|CESOBCBAN|SLOSPO|VSEUVEBAN|VSEOBECNAUVER|\bJYK\b|^SAN$|^CABK$|FIRST ABU DHABI|DEXIA|EUROCLEAR|DT\.?BOERSE|DEUTSCHE BOERSE|EURONEXT|LSEG|LONDON STOCK EX|GRENKE|ARVAL|LEASYS|HYUNDAI CAPITAL|CATERPILLAR FIN|CI FINANCIAL|MOODY|MASTERCARD|\bVISA\b|FISERV|NEXI|WORLDLINE|EDENRED|PLUXEE|SVENSKA HBN|\bBBVA\b|TATBAN|AYVENS|\bALD\b|CPPIB|PENSION|ONTARIO TEACHER|PSP CAP|OMERS|\bCDPQ\b|\bBK\b|BANQUE|BANCA|BANCO|BANKA|BANCAIRE|CAIXA|CAISSE|CREDIT|KREDIT|SPARKASSE|\bSSK\b|"
       r"\bKSK\b|\bLB\b|LANDESBANK|DEKA|\bHYP\b|HYPO|PFANDBRIEF|BAUSPAR|\bBSK\b|\bRLB\b|RAIFFEISEN|\bBPCE\b|"
       r"\bING\b|ABN AMRO|RABOBANK|COOPERATIEVE|SANTANDER|UNICREDIT|INTESA|MEDIOBANCA|\bBPER\b|NORDEA|"
       r"\bDNB\b|SKANDINAVISKA|SWEDBANK|HANDELSBANK|NYKREDIT|REALKREDIT|DANSKE|JYSKE|\bHSBC\b|BARCLAYS|"
       r"NATWEST|LLOYDS|SANTANDER|STANDARD CHART|MORGAN STANLEY|GOLDMAN|JPMORGAN|JP MORGAN|CITIGROUP|"
       r"BK OF AMERICA|BANK OF AMERICA|WELLS FARGO|MIZUHO|SUMITOMO MITSUI|\bMUFG\b|MITSUBISHI UFJ|NOMURA|"
       r"\bUBS\b|JULIUS BAER|ALLIANZ|\bAXA\b|GENERALI|ASSICURAZ|MUENCHENER RUE|MUNICH RE|HANNOVER RUE|"
       r"ZURICH INS|AEGON|NN GROUP|ASSUR|VERSICHER|INSURANCE|RUECKVERS|REINSUR|\bLIFE\b|TALANX|"
       r"\bSOGECAP|\bCNP\b|ATHENE|METLIFE|PRUDENTIAL|GLOBAL FUNDI|FUNDING AGREE|BLACKROCK|APOLLO|"
       r"BLUE OWL|ARES CAP|\bKKR\b|BLACKSTONE|CARLYLE|BROOKFIELD FIN|INVESTMENT CORP|\bBDC\b|ERSTE GRP|"
       r"SOCIETE GENERAL|BNP PARIBAS|NATIXIS|DEUTSCHE BANK|COMMERZBANK|AAREAL|NORD ?LB|BAYERISCHE LB|"
       r"\bBAWAG|BELFIUS|\bKBC\b|\bAIB\b|BANK OF IRELAND|\bCA ITALIA|CRED\.?AGR|AMERICAN EXPRESS|"
       r"CAPITAL ONE|\bRCI\b|LEASING|FACTORING|\bMACQUARIE|WESTPAC|COMMONWEALTH BK|NATIONAL AUSTRALIA|"
       r"\bANZ\b|ROYAL BK|TORONTO-DOMIN|\bBNS\b|SCOTIABANK|BANK OF MONTREAL|\bCIBC\b|NATIONWIDE BUIL|"
       r"BUILDING SOC|COVENTRY BUIL|YORKSHIRE BUIL|SKIPTON|\bEXOR\b|DEUTSCHE PFAND|WUESTENROT|"
       r"BERLIN HYP|DZ HYP|MUENCHENER HYPO|\bDZ\b|HELABA|\bOP CORP|OP-YHTYMA|\bLBBW\b|HAMBURG COMMERC|"
       r"\bOLB\b|\bIKB\b|\bVOLKSBANK|VOLKSWAGEN BANK|VOLKSWAGEN FIN|VOLKSWAGEN LEAS|SANTANDER CONS|"
       r"TOYOTA MOTOR CR|\bFCE BANK|FORD MOTOR CRED|GENERAL MOTORS FIN|PSA BANQUE|STELLANTIS FIN|"
       r"MERCEDES-BENZ FIN SERV|FINANCIAL SERV)")


def first_match(patterns, s):
    return any(re.search(p, s) for p in patterns)


def sector_of(row, overrides):
    cfi = row.cfi or ""
    iss = (row.fisn or "").split("/")[0].strip().upper()
    desc = (row.fisn or "").split("/", 1)[1].upper() if "/" in (row.fisn or "") else ""
    lei = row.issuer_lei or ""
    if not cfi:
        return "UNKNOWN", "not in FIRDS"
    if lei in overrides:
        return overrides[lei], "manual override"
    if cfi[:2] == "DC":
        return "CONVERTIBLE", "CFI DC"
    if re.search(STRUCT_DESC, desc):
        return "STRUCTURED", "FISN: credit-linked/structured"
    if cfi[:2] in ("DS", "DE", "DW", "DD", "DM"):
        return "STRUCTURED", f"CFI {cfi[:2]}"
    if lei in LEI_SECTOR:
        return LEI_SECTOR[lei], "LEI list"
    for pat, sec in EXCEPTIONS:
        if re.search(pat, iss):
            return sec, "issuer name (exception)"
    if first_match(SUPRA, iss):
        return "SUPRA", "issuer name"
    if first_match(AGENCY, iss):            # before SOV: ^KOREA / ^JAPAN would otherwise catch the agencies
        return "AGENCY", "issuer name"
    if first_match(SOV, iss):
        return "SOV", "issuer name"
    if re.search(r"\b(GOVT|BTP|OAT|BONOS)\b", desc) and cfi[3:4] == "T" and not re.search(FIN, iss):
        return "SOV", "FISN-beskrivning + CFI statsgaranti"
    if first_match(SUBSOV, iss) or cfi[:2] == "DN":
        return "SUBSOV", "issuer name" if cfi[:2] != "DN" else "CFI DN (municipal)"
    if cfi[:2] in ("DA", "DG"):
        # banks' covered bonds are often reported as DG/DA in FIRDS
        if re.search(r"GLOBAL F|FUNDING|LIFE G", iss):
            return "CORP_FIN", f"CFI {cfi[:2]} but insurance FABN"
        if re.search(COVERED_DESC, desc) or re.search(COVERED_ISS, iss) or re.search(
                r"BANK|BK\b|BOLIGKRED|BUIL|HYP|KIINNITY|MORTGAGE BA|SPAREBANK|RAIFFEISEN|CREDIT|BANQUE|"
                r"COOPERATIEVE|ERSTE|WESTPAC|COMMNW|NATL\.AU|NORDEA|\bING\b|ABN AMRO|DEKA|LANDESBANK|"
                r"FEDERATION DES|AB SVERIGES SAK|STADSHYPOTEK|SWEDBANK", iss):
            return "COVERED", f"CFI {cfi[:2]} + bank issuer"
        return "SECURITISED", f"CFI {cfi[:2]}"
    if re.search(COVERED_DESC, desc) or re.search(COVERED_ISS, iss):
        return "COVERED", "FISN: covered"
    if re.search(COVERED_NAME, str(getattr(row, "full_name", "") or "")):
        return "COVERED", "full name: covered"
    if re.search(FIN, iss):
        return "CORP_FIN", "issuer name (financial)"
    return "CORP_NONFIN", "other (not financial/public)"


def load_quotes(paths):
    """Per ISIN: bid/offer seen (price > 0), firm bid/offer (also size > 0), price currency and venue."""
    q = {}
    bond = re.compile(r'"priceNotation"\s*:\s*2\s*[,}]')  # fast pre-filter before json.loads
    for p in paths:
        with gzip.open(p, "rt") as fh:
            for line in fh:
                if not bond.search(line):
                    continue
                r = json.loads(line)
                if r.get("priceNotation") != 2:
                    continue
                i = r["instrumentIdentificationCode"]
                d = q.setdefault(i, {"bid": False, "ask": False, "firm_bid": False, "firm_ask": False, "msgs": 0,
                                     "price_ccy": r.get("priceCurrency"), "venue": r.get("venueOfExecution")})
                # price 0 = no quote (e.g. at the close); qty 0 with price > 0 = indicative quote
                b, a = bool(r.get("bestBid")), bool(r.get("bestAsk"))
                d["bid"] |= b; d["ask"] |= a; d["msgs"] += 1
                d["firm_bid"] |= b and bool(r.get("bestBidQty")); d["firm_ask"] |= a and bool(r.get("bestAskQty"))
    return q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="DFRA-pretrade-*.json.gz")
    ap.add_argument("--db", default="firds.sqlite")
    ap.add_argument("--overrides", default="overrides.csv")
    ap.add_argument("--out", default="bonds_classified.csv")
    a = ap.parse_args()

    quotes = load_quotes(a.files)
    con = sqlite3.connect(a.db)
    firds = pd.read_sql("SELECT * FROM firds_d", con).set_index("isin")
    df = firds.reindex(sorted(quotes))
    for c in ("issued_amt", "nominal_unit", "coupon_fixed", "float_spread_bp"):
        df[c] = pd.to_numeric(df[c])
    q = pd.DataFrame.from_dict(quotes, orient="index")
    df = df.join(q)
    for c in ("cfi", "fisn", "issuer_lei", "seniority", "full_name"):
        df[c] = df[c].fillna("").astype(str)

    overrides = {}
    if os.path.exists(a.overrides):
        with open(a.overrides) as f:
            overrides = {r["lei"]: r["sector"] for r in csv.DictReader(f)}

    res = [sector_of(r, overrides) for r in df.itertuples()]
    df["sector"] = [s for s, _ in res]
    df["rule"] = [w for _, w in res]
    df["issuer"] = df.fisn.str.split("/").str[0].str.strip()
    sub = (df.seniority.isin(["SBOD", "JUND", "MZZD"])
           | df.fisn.str.contains(r"\b(?:SUB|JR|PERP|T2|AT1)\b", na=False)
           | df.full_name.fillna("").str.contains(r"\bPERP\b|Und\.\)|Hybrid", case=False)  # hybrids are often reported as senior
           | (df.cfi.str[:1] == "D") & df.maturity.isna())                                # perpetual = subordinated in practice
    df["subordinated"] = sub
    df["coupon_type"] = df.cfi.str[2].map({"F": "fixed", "Z": "zero", "V": "floating", "C": "cash", "K": "payment-in-kind"}).fillna("other")
    df["benchmark_500m"] = df.issued_amt >= 5e8
    df["two_sided"] = df.bid & df.ask
    df["two_sided_firm"] = df.firm_bid & df.firm_ask
    df.index.name = "isin"
    df.to_csv(a.out)
    # issuer list for manual review -> copy rows to overrides.csv to fix errors
    iss = (df[df.cfi != ""].groupby(["issuer_lei", "sector"])
           .agg(issuer=("issuer", "first"), example=("full_name", "first"), n_bonds=("ccy", "size"),
                n_eur=("ccy", lambda c: (c == "EUR").sum()), rule=("rule", "first"))
           .reset_index().sort_values("n_eur", ascending=False))
    iss.to_csv(os.path.splitext(a.out)[0] + "_issuers.csv", index=False)

    # --- summary ----------------------------------------------------------------
    e = df[df.ccy == "EUR"]
    print(f"Bond ISINs in the files: {len(df):,}   found in FIRDS: {(df.cfi != '').sum():,}   EUR-denominated: {len(e):,}\n")
    t = pd.DataFrame({"count": e.groupby("sector").size(), "two-way": e.groupby("sector").two_sided.sum(),
                      "firm two-way": e.groupby("sector").two_sided_firm.sum()})
    t.loc["total"] = t.sum()
    print("EUR-denominated by sector (two-way = bid and offer > 0 seen; firm = also with size):")
    print(t.to_string(), "\n")
    corp = e[e.sector.isin(["CORP_FIN", "CORP_NONFIN"])]
    def row(lbl, x): print(f"  {lbl:<55}{len(x):>6,}{x.two_sided.sum():>8,}{x.two_sided_firm.sum():>8,}")
    print(f"  {'EUR corporates (excl. covered/ABS/structured)':<55}{'count':>6}{'2-way':>8}{'firm':>8}")
    row("all", corp)
    row("  non-financial", corp[corp.sector == "CORP_NONFIN"])
    row("  financial", corp[corp.sector == "CORP_FIN"])
    row("  issue size >= EUR 500m", corp[corp.benchmark_500m])
    row("  >= 500m, fixed/zero coupon, senior", corp[corp.benchmark_500m & corp.coupon_type.isin(["fixed", "zero"]) & ~corp.subordinated])
    row("  >= 500m, fixed/zero coupon, senior, non-financial", corp[corp.benchmark_500m & corp.coupon_type.isin(["fixed", "zero"]) & ~corp.subordinated & (corp.sector == "CORP_NONFIN")])
    print(f"\n-> {a.out}")


if __name__ == "__main__":
    main()
