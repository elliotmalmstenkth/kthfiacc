"""Offline-tester (ingen nätverksåtkomst): syntetiska data i stället för ECB/ESMA/Deutsche Börse."""
import datetime as dt, gzip, io, json, os, sys, types, zipfile
from collections import namedtuple

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import classify, ecb_curve, firds, mfs  # noqa: E402

# ECB AAA-liknande parametrar (ungefärliga, rimlig kurvform)
P = dict(b0=0.9, b1=1.1, b2=6.0, b3=-4.5, t1=3.5, t2=6.0)


# ---------------------------------------------------------------- ecb_curve
def test_svensson_short_end_and_forward_consistency():
    c = ecb_curve.Curve("2026-10-01", "AAA", *P.values())
    assert c.spot(1e-9) == pytest.approx(P["b0"] + P["b1"], abs=1e-6)
    assert c.forward(0) == pytest.approx(P["b0"] + P["b1"], abs=1e-9)
    for m in (0.5, 2, 7, 15, 30):  # f(m) = d/dm [m * z(m)]
        h = 1e-5
        num = ((m + h) * c.spot(m + h) - (m - h) * c.spot(m - h)) / (2 * h)
        assert c.forward(m) == pytest.approx(num, abs=1e-6)
    assert c.forward_rate(5, 10) == pytest.approx((10 * c.spot(10) - 5 * c.spot(5)) / 5)


def test_par_rate_prices_bond_at_par():
    c = ecb_curve.Curve("2026-10-01", "AAA", *P.values())
    for m in (1, 3.25, 10):
        cpn = c.par(m, 1) / 100
        times = [m - k for k in range(int(m) + 1) if m - k > 1e-9][::-1]
        accr = [times[0]] + [1] * (len(times) - 1)
        price = sum(cpn * a * c.df(t) for t, a in zip(times, accr)) + c.df(m)
        assert price == pytest.approx(1.0, abs=1e-12)
    # kontinuerlig kupong ligger nära årlig för en jämn kurva
    assert abs(c.par(10) - c.par(10, 1)) < 0.05


def test_tenor_years():
    assert ecb_curve.tenor_years("3M") == 0.25
    assert ecb_curve.tenor_years("10Y6M") == 10.5
    assert ecb_curve.tenor_years("30Y") == 30


def _ecb_csv(rows):
    out = io.StringIO()
    out.write("KEY,FREQ,REF_AREA,DATA_TYPE_FM,TIME_PERIOD,OBS_VALUE\n")
    for d, k, v in rows:
        out.write(f"YC.X,B,U2,{k},{d},{v}\n")
    return out.getvalue().encode()


def test_update_and_load(tmp_path, monkeypatch):
    days = ["2026-09-30", "2026-10-01"]

    def fake_get(keys, code, start=None, end=None):
        if keys == ecb_curve.PARAMS:
            rows = [(d, k, v) for d in days for k, v in zip(ecb_curve.PARAMS, P.values())]
        else:
            c = ecb_curve.Curve("x", "x", *P.values())
            f = {"SR": c.spot, "IF": c.forward, "PY": c.par}
            rows = [(d, k, f[k.split("_")[0]](ecb_curve.tenor_years(k.split("_")[1]))) for d in days for k in keys]
        return "fake://", _ecb_csv(rows)

    monkeypatch.setattr(ecb_curve, "_get", fake_get)
    db = str(tmp_path / "c.sqlite")
    ecb_curve.update(db, raw_dir=str(tmp_path / "raw"))
    c = ecb_curve.Curve.load("2026-10-02", "AAA", db)  # helg/ingen kurva -> senaste före
    assert c.date == "2026-10-01" and c.b2 == P["b2"]
    with pytest.raises(LookupError):
        ecb_curve.Curve.load("2026-01-01", "AAA", db)
    ecb_curve.cmd_check(types.SimpleNamespace(db=db))  # ska inte krascha
    n = ecb_curve.connect(db).execute("SELECT count(*) FROM grid").fetchone()[0]
    assert n == 2 * len(days) * len(ecb_curve.GRID_TYPES) * len(ecb_curve.GRID_TENORS)


# ---------------------------------------------------------------- firds
FULINS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BizData xmlns="urn:iso:std:iso:20022:tech:xsd:head.003.001.01"><Pyld>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:auth.017.001.02"><FinInstrmRptgRefDataRpt>
<RefData>
  <FinInstrmGnlAttrbts><Id>XS0000000001</Id><FullNm>ACME 1.5% 2031</FullNm><ShrtNm>ACME SA/1.5 MTN 20310115</ShrtNm>
    <ClssfctnTp>DBFTFB</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
  <Issr>5299000000000000AC01</Issr>
  <TradgVnRltdAttrbts><Id>XFRA</Id></TradgVnRltdAttrbts>
  <DebtInstrmAttrbts><TtlIssdNmnlAmt Ccy="EUR">750000000</TtlIssdNmnlAmt><MtrtyDt>2031-01-15</MtrtyDt>
    <NmnlValPerUnit Ccy="EUR">100000</NmnlValPerUnit><IntrstRate><Fxd>1.5</Fxd></IntrstRate><DebtSnrty>SNDB</DebtSnrty></DebtInstrmAttrbts>
  <TechAttrbts><RlvntCmptntAuthrty>FR</RlvntCmptntAuthrty></TechAttrbts>
</RefData>
<RefData>
  <FinInstrmGnlAttrbts><Id>XS0000000001</Id><FullNm>ACME 1.5% 2031</FullNm><ShrtNm>ACME SA/1.5 MTN 20310115</ShrtNm>
    <ClssfctnTp>DBFTFB</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
  <Issr>5299000000000000AC01</Issr><TradgVnRltdAttrbts><Id>XSTU</Id></TradgVnRltdAttrbts>
</RefData>
<RefData>
  <FinInstrmGnlAttrbts><Id>DE000F0000001</Id><FullNm>FGBL DEC26</FullNm><ShrtNm>EUREX/FGBL 20261208</ShrtNm>
    <ClssfctnTp>FFDPSX</ClssfctnTp><NtnlCcy>EUR</NtnlCcy></FinInstrmGnlAttrbts>
  <Issr>529900UT4DG0LG5R9O07</Issr><TradgVnRltdAttrbts><Id>XEUR</Id></TradgVnRltdAttrbts>
  <DerivInstrmAttrbts><XpryDt>2026-12-08</XpryDt><PricMltplr>1000</PricMltplr>
    <UndrlygInstrm><Sngl><ISIN>DE0001102000</ISIN></Sngl></UndrlygInstrm><DlvryTp>PHYS</DlvryTp></DerivInstrmAttrbts>
</RefData>
</FinInstrmRptgRefDataRpt></Document></Pyld></BizData>"""


def test_firds_parse_and_build(tmp_path, monkeypatch):
    zp = tmp_path / "FULINS_D_20261003_01of01.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("FULINS_D_20261003_01of01.xml", FULINS_XML)
    recs = list(firds.parse_records(str(zp)))
    assert len(recs) == 3
    b = recs[0]
    assert (b["isin"], b["cfi"], b["ccy"], b["venue"], b["rca"]) == ("XS0000000001", "DBFTFB", "EUR", "XFRA", "FR")
    assert (b["issued_amt"], b["coupon_fixed"], b["seniority"], b["maturity"]) == (7.5e8, 1.5, "SNDB", "2031-01-15")
    f = recs[2]
    assert (f["expiry"], f["multiplier"], f["underlying_isin"], f["delivery"]) == ("2026-12-08", 1000.0, "DE0001102000", "PHYS")

    monkeypatch.setattr(firds, "list_fulins", lambda cat, date: ("2026-10-03", ["https://x/" + zp.name]))
    monkeypatch.setattr(firds, "download", lambda url, folder: str(zp))
    con = firds.build("D", None, str(tmp_path / "f.sqlite"), str(tmp_path))
    rows = con.execute("SELECT isin, venues, n_venues, issued_amt FROM firds_d ORDER BY isin").fetchall()
    assert ("XS0000000001", "XFRA,XSTU", 2, "750000000.0") in rows


# ---------------------------------------------------------------- classify
R = namedtuple("R", "cfi fisn issuer_lei")


@pytest.mark.parametrize("cfi,fisn,lei,want", [
    ("DBFTFB", "BUNDESREP.DEUTSCHLAND/2.5 ANL 20350215", "", "SOV"),
    ("DBFTFB", "KFW/2.0 MTN 20290115", "", "AGENCY"),
    ("DBFTFB", "EUROPEAN UNION/3.0 MTN 20340304", "", "SUPRA"),
    ("DBFTFB", "NORDRHEIN-WESTF/2.75 LSA 20330101", "", "SUBSOV"),
    ("DBFTFB", "DEUTSCHE BANK AG/4.0 MTN 20300101", "", "CORP_FIN"),
    ("DBFTFB", "SIEMENS FINANCI/3.0 MTN 20330101", "", "CORP_NONFIN"),
    ("DBFTFB", "BERLIN HYP AG/1.0 PF 20300101", "", "COVERED"),
    ("DAFTFB", "NORDEA KIINNITY/3.0 COV 20300101", "", "COVERED"),
    ("DCFTFB", "ACME SA/0.0 CV 20300101", "", "CONVERTIBLE"),
    ("DBFTFB", "SOME BANK/CLN 20300101", "", "STRUCTURED"),
    ("DBFTFB", "INTERNATIONAL B/3.0 NT 20300101", "VGRQXHF3J8VDLUA7XE92", "CORP_NONFIN"),
    ("", "", "", "UNKNOWN"),
])
def test_sector_rules(cfi, fisn, lei, want):
    assert classify.sector_of(R(cfi, fisn, lei), {})[0] == want


def test_sector_override_wins():
    assert classify.sector_of(R("DBFTFB", "KFW/2.0 MTN", "LEI1"), {"LEI1": "CORP_FIN"}) == ("CORP_FIN", "manuell överstyrning")


def _write_gz(path, msgs):
    with gzip.open(path, "wt") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")


def test_load_quotes(tmp_path):
    p = tmp_path / "DFRA-pretrade-2026-10-02T08_00.json.gz"
    _write_gz(p, [
        {"instrumentIdentificationCode": "A", "priceNotation": 2, "bestBid": 99.1, "priceCurrency": "EUR"},
        {"instrumentIdentificationCode": "A", "priceNotation": 2, "bestAsk": 99.5, "priceCurrency": "EUR"},
        {"instrumentIdentificationCode": "B", "priceNotation": 2, "bestBid": 98, "bestAsk": 99, "priceCurrency": "EUR"},
        {"instrumentIdentificationCode": "S", "priceNotation": 1, "bestBid": 10, "bestAsk": 11},
    ])
    q = classify.load_quotes([str(p)])
    assert set(q) == {"A", "B"}
    assert q["A"]["bid"] and q["A"]["ask"] and not q["A"]["both_in_one_msg"] and q["A"]["msgs"] == 2
    assert q["B"]["both_in_one_msg"]


# ---------------------------------------------------------------- mfs
def test_parse_name():
    feed, day, t = mfs.parse_name("DFRA-pretrade-2026-10-02T08_15.json.gz")
    assert (feed, day, t) == ("DFRA-pretrade", "2026-10-02", dt.datetime(2026, 10, 2, 8, 15))
    assert mfs.parse_name("README.txt") is None


def _make_day(archive, feed, day, minutes):
    folder = os.path.join(archive, feed, day)
    os.makedirs(folder, exist_ok=True)
    for hhmm, msgs in minutes.items():
        _write_gz(os.path.join(folder, f"{feed}-{day}T{hhmm.replace(':', '_')}.json.gz"), msgs)


def q(isin, bid=None, ask=None, notation=2):
    m = {"instrumentIdentificationCode": isin, "priceNotation": notation, "priceCurrency": "EUR"}
    if bid is not None: m["bestBid"] = bid
    if ask is not None: m["bestAsk"] = ask
    return m


def test_build_marks_snapshots(tmp_path):
    arch, db, day = str(tmp_path / "a"), str(tmp_path / "m.sqlite"), "2026-10-02"
    _make_day(arch, "DFRA-pretrade", day, {
        "08:00": [q("A", 99.0, 99.4), q("B", 50, 51), q("STOCK", 10, 11, notation=1)],
        "08:01": [q("A", 99.1, 99.5)],
        "15:00": [q("B", bid=50.5)],  # B blir ensidig
        "15:45": [q("A", 98.0, 98.6), q("C", 101, 102)],
    })
    con = mfs.build_marks(day, arch, db, snaps=["15:30"])
    rows = {(s, i): (b, a, t) for s, i, b, a, t in con.execute("SELECT snap, isin, bid, ask, quote_time FROM quotes")}
    assert rows[("15:30", "A")] == (99.1, 99.5, "2026-10-02T08:01")
    assert rows[("15:30", "B")] == (50.5, None, "2026-10-02T15:00")
    assert ("15:30", "C") not in rows
    assert rows[("close", "A")][:2] == (98.0, 98.6)
    assert ("close", "STOCK") not in rows
    n, gap = con.execute("SELECT n_files, max_gap_min FROM days").fetchone()
    assert (n, gap) == (4, 419)
    mfs.build_marks(day, arch, db)  # ombyggnad ersätter dagen
    assert con.execute("SELECT count(*) FROM quotes").fetchone()[0] == 3


def test_sync_downloads_only_new(tmp_path, monkeypatch):
    files = ["DFRA-pretrade-2026-10-02T08_00.json.gz", "DFRA-pretrade-2026-10-02T08_01.json.gz"]
    payload = gzip.compress(b'{"instrumentIdentificationCode":"A"}\n')
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): self.close()

    def fake_open(url, timeout=120, tries=4):
        calls.append(url)
        if url.endswith("/DFRA-pretrade"):
            return Resp(json.dumps({"SourcePrefix": "DFRA-pretrade", "CurrentFiles": files}).encode())
        if url.endswith("T08_01.json.gz"):
            return Resp(b"not gzip")
        return Resp(payload)

    monkeypatch.setattr(mfs, "_open", fake_open)
    arch = str(tmp_path / "a")
    new, failed = mfs.sync(["DFRA-pretrade"], arch, pause=0)
    assert (new, failed) == (1, 1)  # trasig fil sparas inte
    folder = os.path.join(arch, "DFRA-pretrade", "2026-10-02")
    assert os.listdir(folder) == [files[0]]
    calls.clear()
    files.pop()
    assert mfs.sync(["DFRA-pretrade"], arch, pause=0) == (0, 0)
    assert len(calls) == 1  # bara listningen, inga nya nedladdningar


def test_prune_only_built_days(tmp_path):
    arch, db = str(tmp_path / "a"), str(tmp_path / "m.sqlite")
    for d in ("2026-09-01", "2026-09-02", "2026-10-01"):
        _make_day(arch, "DFRA-pretrade", d, {"08:00": [q("A", 1, 2)]})
    mfs.build_marks("2026-09-01", arch, db)
    mfs.build_marks("2026-10-01", arch, db)
    removed = mfs.prune("DFRA-pretrade", arch, db, keep_days=14, today=dt.date(2026, 10, 3))
    assert removed == ["2026-09-01"]  # 09-02 saknar marks, 10-01 för ny
    assert sorted(os.listdir(os.path.join(arch, "DFRA-pretrade"))) == ["2026-09-02", "2026-10-01"]
