"""Offline tests (no network access): synthetic data in place of ECB/ESMA/Deutsche Börse."""
import datetime as dt, gzip, io, json, math, os, sys, types, zipfile
from collections import namedtuple

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ci, classify, ecb_curve, eurex, firds, mfs  # noqa: E402

# ECB AAA-like parameters (approximate, plausible curve shape)
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
    # continuous coupon is close to annual for a smooth curve
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
    c = ecb_curve.Curve.load("2026-10-02", "AAA", db)  # weekend/no curve -> latest before
    assert c.date == "2026-10-01" and c.b2 == P["b2"]
    with pytest.raises(LookupError):
        ecb_curve.Curve.load("2026-01-01", "AAA", db)
    ecb_curve.cmd_check(types.SimpleNamespace(db=db))  # must not crash
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


R2 = namedtuple("R2", "cfi fisn issuer_lei full_name")


@pytest.mark.parametrize("cfi,fisn,name,want", [
    ("DBFTXN", "AGENTSCHAP MINI/3.75 BD 20420115", "NETHER 3 3/4 01/15/42", "SOV"),             # Dutch State Treasury Agency
    ("DBFTFB", "CAISSE AMORT DE/0.1 BD 20310915", "CADES 0 1/8 09/15/31", "AGENCY"),
    ("DBZUFB", "NEDERLANDSE WAT/0 MTN 20370216", "NEDWBK 0 02/16/37", "AGENCY"),
    ("DTFUFB", "ABN AMRO BANK N/0.01 MTN 20350228", "ABN AMRO Bank N.V. EO-Med.-T.Cov.Bds 2026(35)", "COVERED"),
    ("DBFTFB", "DZ HYP AG/0.875 MTH 20300118 R. 358", "DZ HYP 0.875 01/18/30", "COVERED"),  # MTH = mortgage Pfandbrief
    ("DTFUFB", "ABN AMRO BANK N/2.830 MTN 20300228", "ABNANV 2.83 02/28/30 BOND", "CORP_FIN"),
    ("DBFUFR", "MINISTRY OF FI/2.375 BD 20291125", "CHINA 2 3/8 11/25/29", "SOV"),             # issuer name cut at 14 characters
])
def test_sector_rules_full_name(cfi, fisn, name, want):
    assert classify.sector_of(R2(cfi, fisn, "", name), {})[0] == want


def test_sector_override_wins():
    assert classify.sector_of(R("DBFTFB", "KFW/2.0 MTN", "LEI1"), {"LEI1": "CORP_FIN"}) == ("CORP_FIN", "manual override")


def _write_gz(path, msgs):
    with gzip.open(path, "wt") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")


def test_load_quotes(tmp_path):
    p = tmp_path / "DFRA-pretrade-2026-10-02T08_00.json.gz"
    _write_gz(p, [
        {"instrumentIdentificationCode": "A", "priceNotation": 2, "bestBid": 99.1, "priceCurrency": "EUR"},
        {"instrumentIdentificationCode": "A", "priceNotation": 2, "bestAsk": 99.5, "priceCurrency": "EUR"},
        {"instrumentIdentificationCode": "B", "priceNotation": 2, "bestBid": 98, "bestAsk": 99, "priceCurrency": "EUR",
         "bestBidQty": 1e5, "bestAskQty": 0.0},                                    # indicative offer
        {"instrumentIdentificationCode": "Z", "priceNotation": 2, "bestBid": 0.0, "bestAsk": 0.0,
         "bestBidQty": 0.0, "bestAskQty": 0.0},                                    # handelsslut
        {"instrumentIdentificationCode": "S", "priceNotation": 1, "bestBid": 10, "bestAsk": 11},
    ])
    q = classify.load_quotes([str(p)])
    assert set(q) == {"A", "B", "Z"}
    assert q["A"]["bid"] and q["A"]["ask"] and q["A"]["msgs"] == 2
    assert q["B"]["bid"] and q["B"]["ask"] and q["B"]["firm_bid"] and not q["B"]["firm_ask"]
    assert not (q["Z"]["bid"] or q["Z"]["ask"])


# ---------------------------------------------------------------- mfs
UTC = dt.timezone.utc


def test_parse_name_uses_frankfurt_trading_day():
    assert mfs.parse_name("DFRA-pretrade-2026-10-02T08_15.json.gz") == (
        "DFRA-pretrade", "2026-10-02", dt.datetime(2026, 10, 2, 8, 15, tzinfo=UTC))
    # 23:00 UTC = 01:00 CEST the next day
    assert mfs.parse_name("DFRA-pretrade-2026-10-01T23_00.json.gz")[1] == "2026-10-02"
    # winter time: 23:00 UTC = 00:00 CET the next day
    assert mfs.parse_name("DFRA-pretrade-2026-12-01T23_00.json.gz")[1] == "2026-12-02"
    assert mfs.parse_name("DEUR-posttrade-daily-2026-10-02.json.gz") == ("DEUR-posttrade", "2026-10-02", None)
    assert mfs.parse_name("README.txt") is None


def _write_minute(archive, feed, utc_hhmm, msgs, day="2026-10-02"):
    fn = f"{feed}-{day}T{utc_hhmm.replace(':', '_')}.json.gz"
    folder = os.path.join(archive, feed, mfs.parse_name(fn)[1])
    os.makedirs(folder, exist_ok=True)
    _write_gz(os.path.join(folder, fn), msgs)


def q(isin, t, bid=None, ask=None, notation=2):
    """Pre-trade delta; t = UTC HH:MM:SS on 2026-10-02."""
    m = {"messageId": "pretrade", "instrumentIdentificationCode": isin, "priceNotation": notation,
         "priceCurrency": "EUR", "venueOfExecution": "FRAB", "updateDateAndTime": f"2026-10-02T{t}.123456789Z"}
    if bid is not None: m.update(bestBid=bid, bestBidQty=100000.0)
    if ask is not None: m.update(bestAsk=ask, bestAskQty=50000.0)
    return m


def test_build_marks_merges_deltas_and_snapshots(tmp_path):
    arch, db, day = str(tmp_path / "a"), str(tmp_path / "m.sqlite"), "2026-10-02"
    W = lambda hhmm, msgs: _write_minute(arch, "DFRA-pretrade", hhmm, msgs)
    W("06:00", [q("A", "06:00:01", 99.0, 99.4), q("B", "06:00:02", 50, 51), q("STOCK", "06:00:03", 10, 11, notation=1)])
    W("06:01", [q("A", "06:01:05", ask=99.5)])        # only the offer changes
    W("15:29", [q("A", "15:29:59", bid=99.2), q("B", "15:30:00", bid=50.5)])  # 17:30 CEST = 15:30 UTC
    W("15:45", [q("A", "15:45:00", 98.0, 98.6), q("C", "15:45:10", ask=102)])
    W("15:50", [q("A", "15:50:00", 0.0, 0.0), q("B", "15:50:00", 0.0, 0.0)])  # close: price 0 = no quote
    con = mfs.build_marks(day, arch, db, snaps=["17:30", "17:55"])
    rows = {(s, i): tuple(r) for s, i, *r in con.execute(
        "SELECT snap, isin, bid, ask, bid_time, ask_time, bid_qty, venue FROM quotes")}
    a = rows[("17:30", "A")]
    assert a[:4] == (99.2, 99.5, "2026-10-02T15:29:59.123", "2026-10-02T06:01:05.123")  # two-way despite deltas
    assert a[4:] == (100000.0, "FRAB")
    assert rows[("17:30", "B")][:2] == (50, 51)  # B's 15:30:00 change comes after the snapshot
    assert ("17:30", "C") not in rows
    assert rows[("close", "A")][:2] == (98.0, 98.6)
    assert rows[("close", "B")][:2] == (50.5, 51)
    assert ("close", "C") not in rows                    # never two-way -> no closing mark
    assert rows[("17:55", "C")][:2] == (None, 102)       # but shows one-sided in a snapshot
    assert ("17:55", "A") not in rows                    # withdrawn after the close
    assert ("close", "STOCK") not in rows
    n, gap = con.execute("SELECT n_files, max_gap_min FROM days").fetchone()
    assert (n, gap) == (5, 568)
    mfs.build_marks(day, arch, db)  # a rebuild replaces the day
    assert con.execute("SELECT count(*) FROM quotes").fetchone()[0] == 2


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _fake_server(monkeypatch, feed, files, bodies):
    calls = []

    def fake_open(url, timeout=120, tries=4):
        calls.append(url)
        if url.endswith("/" + feed):
            return Resp(json.dumps({"SourcePrefix": feed, "CurrentFiles": files}).encode())
        name = url.rsplit("/", 1)[1]
        if name not in bodies:
            raise mfs.urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return Resp(bodies[name])

    monkeypatch.setattr(mfs, "_open", fake_open)
    return calls


def test_sync_downloads_only_new(tmp_path, monkeypatch):
    f = ["DFRA-pretrade-2026-10-01T23_00.json.gz", "DFRA-pretrade-2026-10-02T08_00.json.gz",
         "DFRA-pretrade-2026-10-02T08_01.json.gz", "DFRA-pretrade-daily-2026-10-02.json.gz"]
    bodies = {f[0]: gzip.compress(b""), f[1]: gzip.compress(b'{"instrumentIdentificationCode":"A"}\n'),
              f[2]: b"not gzip"}  # f[3] -> 404
    calls = _fake_server(monkeypatch, "DFRA-pretrade", f, bodies)
    arch = str(tmp_path / "a")
    assert mfs.sync(["DFRA-pretrade"], arch, workers=1) == (2, 1)  # corrupt file is not saved, a 404 is not a failure
    folder = os.path.join(arch, "DFRA-pretrade", "2026-10-02")
    assert sorted(os.listdir(folder)) == f[:2]  # 23:00 UTC on 1 Oct belongs to trading day 2 Oct
    calls.clear()
    del f[2]
    assert mfs.sync(["DFRA-pretrade"], arch, workers=1) == (0, 0)
    assert len(calls) == 2  # listing + a new 404 check of the daily file


def test_sync_bonds_only_filters_pretrade(tmp_path, monkeypatch):
    fn = "DFRA-pretrade-2026-10-02T08_00.json.gz"
    lines = [json.dumps(q("A", "08:00:00", 99, 100)), json.dumps(q("S", "08:00:01", 1, 2, notation=1)),
             '{"instrumentIdentificationCode":"Z","priceNotation":2}']
    _fake_server(monkeypatch, "DFRA-pretrade", [fn, "DFRA-pretrade-daily-2026-10-02.json.gz"],
                 {fn: gzip.compress("\n".join(lines).encode() + b"\n")})
    arch = str(tmp_path / "a")
    assert mfs.sync(["DFRA-pretrade"], arch, workers=1, bonds_only=True) == (1, 0)  # the daily file is skipped
    kept = [json.loads(x)["instrumentIdentificationCode"]
            for x in gzip.open(os.path.join(arch, "DFRA-pretrade", "2026-10-02", fn), "rt")]
    assert kept == ["A", "Z"]


def test_prune_only_built_days(tmp_path):
    arch, db = str(tmp_path / "a"), str(tmp_path / "m.sqlite")
    for d in ("2026-09-01", "2026-09-02", "2026-10-01"):
        _write_minute(arch, "DFRA-pretrade", "08:00", [q("A", "08:00:00", 1, 2)], day=d)
    mfs.build_marks("2026-09-01", arch, db)
    mfs.build_marks("2026-10-01", arch, db)
    removed = mfs.prune("DFRA-pretrade", arch, db, keep_days=14, today=dt.date(2026, 10, 3))
    assert removed == ["2026-09-01"]  # 09-02 has no marks, 10-01 is too recent
    assert sorted(os.listdir(os.path.join(arch, "DFRA-pretrade"))) == ["2026-09-02", "2026-10-01"]


def test_open_backs_off_on_429(monkeypatch):
    calls, sleeps = [], []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise mfs.urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, None)
        return Resp(b"ok")

    monkeypatch.setattr(mfs.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mfs.time, "sleep", sleeps.append)
    assert mfs._open("https://x/y").read() == b"ok"
    assert len(calls) == 3 and sleeps == []  # 429 -> Retry-After (0 s), no extra backoff


# ---------------------------------------------------------------- eurex
def _pt(isin, t, px, qty, mode="2", tx=None, mod="-", contract="2026-12-08", **kw):
    return dict(messageId="posttrade", instrumentIdentificationCode=isin, contractDate=contract, price=px, quantity=qty,
                mmtTradingMode=mode, mmtModificationInd=mod, tradingDateAndTime=f"2026-10-02T{t}.000000000Z",
                transactionIdentificationCode=tx or f"{isin}{t}{px}", **kw)


def test_eurex_daily(tmp_path):
    arch, db = str(tmp_path / "a"), str(tmp_path / "m.sqlite")
    bund, fehy = eurex.PRODUCTS["FGBL"][0], eurex.PRODUCTS["FEHY"][0]
    msgs = [_pt(bund, "07:00:00", 121.0, 10, mode="O"), _pt(bund, "08:00:00", 121.5, 30),
            _pt(bund, "09:00:00", 120.5, 500, mode="5"),                   # block: not in OHLC
            _pt(bund, "10:00:00", 99.0, 1, tx="FEL"), _pt(bund, "10:00:01", 99.0, 1, tx="FEL", mod="C"),  # cancelled
            _pt(bund, "11:00:00", 121.2, 10, optionCategory="C"),          # option, ignored
            _pt(fehy, "12:00:00", 309.0, 5, contract="2026-12-18"),
            _pt("DE0000000000", "12:00:00", 1, 1)]                         # unknown product
    folder = os.path.join(arch, "DEUR-posttrade", "2026-10-02")
    os.makedirs(folder)
    _write_gz(os.path.join(folder, "DEUR-posttrade-daily-2026-10-02.json.gz"), msgs)
    rows = {r[1]: r for r in eurex.build_daily("2026-10-02", arch, db)}
    b = rows["FGBL"]
    assert (b[4], b[5], b[6]) == (3, 40, 500)                     # trades, on-book lots, block lots
    assert b[7:11] == (121.0, 121.5, 121.0, 121.5)                # open, high, low, last
    assert b[12] == pytest.approx((121.0 * 10 + 121.5 * 30) / 40)
    assert rows["FEHY"][2] == "2026-12-18" and rows["FEHY"][10] == 309.0


# ---------------------------------------------------------------- ci
def test_complete_days_frankfurt_time():
    days = {"2026-10-01", "2026-10-02"}
    at = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=UTC)
    assert ci.complete_days(days, at("2026-10-02T21:00")) == ["2026-10-01"]           # 23:00 CEST: not complete
    assert ci.complete_days(days, at("2026-10-02T21:20")) == ["2026-10-01", "2026-10-02"]
    assert ci.complete_days({"2026-12-01"}, at("2026-12-01T22:10")) == []              # 23:10 CET
    assert ci.complete_days({"2026-12-01"}, at("2026-12-01T22:20")) == ["2026-12-01"]


def test_build_day_and_log(tmp_path, monkeypatch):
    arch, dist = str(tmp_path / "a"), str(tmp_path / "dist")
    _write_minute(arch, "DFRA-pretrade", "08:00", [q("A", "08:00:00", 99, 100), q("B", "08:00:01", 50, 51)])
    os.makedirs(os.path.join(arch, "DEUR-posttrade", "2026-10-02"))
    _write_gz(os.path.join(arch, "DEUR-posttrade", "2026-10-02", "DEUR-posttrade-daily-2026-10-02.json.gz"),
              [_pt(eurex.PRODUCTS["FEHY"][0], "12:00:00", 309.0, 5, contract="2026-12-18")])
    monkeypatch.setattr(ci.mfs, "sync", lambda *a, **k: (0, 0))
    monkeypatch.setattr(ci.mfs, "download", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))   # Xetra file
    assets, row = ci.build_day("2026-10-02", arch, dist, str(tmp_path / "m.sqlite"))
    names = sorted(os.path.basename(x) for x in assets)
    assert names == ["DEUR-posttrade-2026-10-02.tar", "DFRA-pretrade-bonds-2026-10-02.tar",
                     "dfra_quotes-2026-10-02.csv.gz", "eurex_futures_daily-2026-10-02.csv", "eurex_options-2026-10-02.csv.gz"]
    assert (row["isin_close"], row["two_sided_firm_close"], row["FEHY"], row["FGBL"]) == (2, 2, 309.0, None)
    log = str(tmp_path / "log.csv")
    ci.append_log(row, log); ci.append_log(row, log)  # the same day is overwritten
    assert len(open(log).read().strip().splitlines()) == 2
    assert "309.0" in ci.notes(row, assets)
    monkeypatch.setattr(ci.mfs, "sync", lambda *a, **k: (0, 3))
    assert ci.build_day("2026-10-02", arch, dist, str(tmp_path / "m2.sqlite")) is None  # incomplete -> no release


# ---------------------------------------------------------------- analytics
import analytics as an  # noqa: E402


def test_calendar():
    assert an.add_business_days(dt.date(2026, 10, 2), 2) == dt.date(2026, 10, 6)
    assert an.easter(2027) == dt.date(2027, 3, 28)
    assert not an.is_target_day(dt.date(2027, 3, 26))   # Good Friday
    assert an.add_months(dt.date(2026, 3, 31), -1) == dt.date(2026, 2, 28)


def test_bond_math_par_and_accrued():
    settle = dt.date(2026, 10, 6)
    # 4% coupon, maturing exactly 5 years out on a coupon date -> price 100 gives a 4% yield
    a = an.analyse(100.0, 4.0, dt.date(2031, 10, 6), settle)
    assert a["accrued"] == pytest.approx(0.0) and a["ytm"] == pytest.approx(0.04, abs=1e-9)
    assert a["mdur"] == pytest.approx(4.4518, abs=1e-3)       # Macaulay 4.6299 / 1.04
    # halfway through the period: accrued = half the coupon
    b = an.analyse(100.0, 4.0, dt.date(2031, 4, 6), settle)
    assert b["accrued"] == pytest.approx(4.0 * (settle - dt.date(2026, 4, 6)).days / 365)
    z = an.analyse(95.0, 0.0, dt.date(2031, 10, 6), settle)  # zero coupon
    assert z["ytm"] == pytest.approx((100 / 95) ** (1 / 5) - 1, abs=1e-9)
    # BTP semi-annual coupon
    s = an.analyse(100.0, 4.0, dt.date(2031, 10, 6), settle, freq=2)
    assert s["ytm"] == pytest.approx(0.04, abs=1e-9)


def test_zspread_zero_on_curve():
    c = ecb_curve.Curve("x", "AAA", *P.values())
    settle = dt.date(2026, 10, 6)
    flows, acc = an.cashflows(3.0, dt.date(2033, 5, 15), settle)
    dirty = sum(cf * c.df(t) for t, cf in flows)
    a = an.analyse(dirty - acc, 3.0, dt.date(2033, 5, 15), settle, c)
    assert a["zspread"] == pytest.approx(0.0, abs=1e-9)
    b = an.analyse(dirty - acc - 2, 3.0, dt.date(2033, 5, 15), settle, c)
    assert 0.002 < b["zspread"] < 0.005   # 2 points cheaper ~ 30 bp over ~6 years


def test_conversion_factor_and_ctd():
    deliv = dt.date(2026, 12, 10)
    # 6% coupon maturing on a coupon date -> CF = 1
    assert an.conversion_factor(6.0, dt.date(2035, 12, 10), deliv) == pytest.approx(1.0, abs=1e-12)
    assert an.conversion_factor(4.0, dt.date(2052, 12, 10), deliv, 4.0) == pytest.approx(1.0, abs=1e-12)
    assert an.conversion_factor(2.5, dt.date(2035, 8, 15), deliv) < 0.8
    bunds = [("A", 2.5, dt.date(2035, 8, 15), 97.0), ("B", 2.6, dt.date(2036, 2, 15), 97.5),
             ("C", 2.2, dt.date(2030, 2, 15), 99.0)]                # C not deliverable into FGBL
    cf = {i: an.conversion_factor(c, m, deliv) for i, c, m, _ in bunds}
    f = 121.0
    r = an.ctd("FGBL", f, deliv, bunds, dt.date(2026, 10, 6))
    want = min(("A", "B"), key=lambda i: dict((b[0], b[3]) for b in bunds)[i] - f * cf[i])
    assert r["isin"] == want and r["cf"] == pytest.approx(cf[want])
    assert 50 < r["dv01_contract"] < 150     # EUR per bp per contract, plausible magnitude


def test_sync_prefers_daily_file_for_eurex(tmp_path, monkeypatch):
    f = ["DEUR-posttrade-daily-2026-10-02.json.gz", "DEUR-posttrade-2026-10-02T08_00.json.gz",
         "DEUR-posttrade-2026-10-02T08_01.json.gz"]
    body = gzip.compress(b'{"messageId":"posttrade"}\n')
    calls = _fake_server(monkeypatch, "DEUR-posttrade", f, {x: body for x in f})
    arch = str(tmp_path / "a")
    assert mfs.sync(["DEUR-posttrade"], arch, workers=1) == (1, 0)  # only the daily file is downloaded
    assert os.listdir(os.path.join(arch, "DEUR-posttrade", "2026-10-02")) == [f[0]]
    assert [c for c in calls if "/download/" in c] == [f"{mfs.BASE}/download/{f[0]}"]
    # daily file missing (404) -> minute files
    calls = _fake_server(monkeypatch, "DEUR-posttrade", f, {x: body for x in f[1:]})
    arch2 = str(tmp_path / "b")
    assert mfs.sync(["DEUR-posttrade"], arch2, workers=1) == (2, 0)
    assert sorted(os.listdir(os.path.join(arch2, "DEUR-posttrade", "2026-10-02"))) == sorted(f[1:])


def test_site_coupon_cleaning():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site"))
    import build
    assert build.name_coupon("DANBNK 4 1/2 11/09/28") == 4.5
    assert build.name_coupon("BGOSK 1 ⅝ 04/30/28 ") == 1.625
    assert build.name_coupon("IBESM 4 7/8 PERP") == 4.875
    assert build.name_coupon("Landesbank Saar Inh.-Schv. Serie 0GA v.20(35)") is None
    assert build.clean_coupon(45.0, "DANBNK 4 1/2 11/09/28") == 4.5
    assert build.clean_coupon(1125.0, "FINPOW 1  1/8  11/23/27 BOND") == 1.125
    assert build.clean_coupon(2.7, "BKO 0 09/13/28") == 2.7       # FIRDS wins below 20%
    assert build.clean_coupon(36.0, "No coupon in this name") is None


def test_rich_cheap_flags_outlier_and_skips_short_and_thin_issuers():
    import numpy as np
    T = np.array([0.6, 1, 2, 3, 5, 7, 10, 15.0, 2, 4])
    z = 50 + 20 * np.log(np.maximum(T, 0.25))
    z[4] += 15                                   # the 5y bond trades 15 bp cheap
    groups = ["A"] * 8 + ["B"] * 2               # issuer B has too few bonds for a curve
    rv, coefs = an.rich_cheap(groups, T, z)
    assert rv[4] == pytest.approx(15, abs=0.5)
    assert all(abs(rv[i]) < 0.5 for i in (1, 2, 3, 5, 6, 7))
    assert rv[0] is None                          # under 1 year
    assert rv[8] is None and rv[9] is None and "B" not in coefs


def test_history_files_and_shards(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site"))
    import build
    cols = ["isin", "bid", "ask", "ytm", "z", "rv"]
    for day, px in (("2026-10-01", 99.0), ("2026-10-02", 99.5)):
        data = dict(asof=day, cols=cols, bonds=[["DE0001102580", px - 0.1, px + 0.1, 2.5, -12.0, 1.5]],
                    futures=[dict(code="FGBL", last=121.0, ctd=dict(ytm=2.6))])
        build.write_history(data, str(tmp_path / "h"))
    data = dict(asof="2026-10-03", cols=cols, bonds=[["XS0000000001", 100.0, 100.2, 3.0, 80.0, None]], futures=[])
    build.write_history(data, str(tmp_path / "h"))
    dates = build.build_shards(str(tmp_path / "h"), str(tmp_path / "hist"))
    assert dates == ["2026-10-01", "2026-10-02", "2026-10-03"]
    sh = json.load(open(tmp_path / "hist" / f"{build.shard_of('DE0001102580')}.json"))
    assert sh["d"] == dates and sh["s"]["DE0001102580"] == [[99.0, 2.5, -12.0, 1.5, None], [99.5, 2.5, -12.0, 1.5, None], None]
    fut = json.load(open(tmp_path / "hist" / f"{build.shard_of('FUT:FGBL')}.json"))["s"]["FUT:FGBL"]
    assert fut[0] == [121.0, 2.6, None, None, None]
    assert json.load(open(tmp_path / "hist" / f"{build.shard_of('XS0000000001')}.json"))["s"]["XS0000000001"][2][3] is None


def test_estr_parse(monkeypatch):
    body = (b"KEY,FREQ,TIME_PERIOD,OBS_VALUE\n"
            b"EST.B.EU000A2X2A25.WT,B,2026-10-01,2.442\n")

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(ecb_curve.urllib.request, "urlopen", lambda req, timeout=0: R(body))
    assert ecb_curve.estr("2026-10-02") == ("2026-10-01", 2.442)


def test_market_sources_parse_and_fail_soft(monkeypatch, tmp_path):
    import market
    pages = {
        "fred": "observation_date,VIXCLS\n2026-09-30,16.34\n2026-10-01,.\n2026-10-02,16.39\n2026-10-05,17.0\n",
        "ecb": "KEY,TIME_PERIOD,OBS_VALUE\nX,2026-10-01,1.12\nX,2026-10-02,1.1225\n",
        "sofr": '{"refRates":[{"effectiveDate":"2026-10-01","percentRate":3.87},{"effectiveDate":"2026-09-30","percentRate":3.9}]}',
    }

    def fake(url, timeout=0):
        if "fred" in url: return pages["fred"]
        if "newyorkfed" in url: return pages["sofr"]
        if "EXR" in url: return pages["ecb"]
        raise RuntimeError("offline")
    monkeypatch.setattr(market, "_get", fake)
    assert market.fred("VIXCLS", "2026-10-02") == (["2026-09-30", "2026-10-02"], [16.34, 16.39])   # no data after the day
    assert market.sofr() == (["2026-09-30", "2026-10-01"], [3.9, 3.87])
    S = market.build("2026-10-02", str(tmp_path / "none.sqlite"))   # no curve DB and €STR offline: left out
    assert S["EURUSD"]["v"] == [1.12, 1.1225] and S["VIXCLS"]["src"] == "FRED"
    assert "ESTR" not in S and "AAA10" not in S


def test_live_merges_quotes_and_futures(tmp_path):
    import live
    pre = tmp_path / "pre.json.gz"
    with gzip.open(pre, "wt") as f:
        for m in [dict(priceNotation=2, instrumentIdentificationCode="DE0001", bestBid="99.5", bestBidQty="100000", updateDateAndTime="2026-10-05T08:00:01.1"),
                  dict(priceNotation=2, instrumentIdentificationCode="DE0001", bestAsk="99.7", bestAskQty="50000", updateDateAndTime="2026-10-05T08:00:02.1"),
                  dict(priceNotation=2, instrumentIdentificationCode="XS9999", bestBid="80", updateDateAndTime="2026-10-05T08:00:03"),   # not on the site
                  dict(priceNotation=1, instrumentIdentificationCode="DE0001", bestBid="1", updateDateAndTime="2026-10-05T08:00:04"),    # equity row
                  dict(priceNotation=2, instrumentIdentificationCode="DE0001", bestBid="0", updateDateAndTime="2026-10-05T08:00:05")]:  # bid withdrawn
            f.write(json.dumps(m) + "\n")
    post = tmp_path / "post.json.gz"
    with gzip.open(post, "wt") as f:
        for m in [dict(messageId="posttrade", instrumentIdentificationCode="DE0009652644", contractDate="2027-03-08", price=120.0, quantity=1, mmtTradingMode="2", tradingDateAndTime="2026-10-05T08:00:01"),
                  dict(messageId="posttrade", instrumentIdentificationCode="DE0009652644", contractDate="2026-12-08", price=121.5, quantity=10, mmtTradingMode="2", tradingDateAndTime="2026-10-05T08:00:02"),
                  dict(messageId="posttrade", instrumentIdentificationCode="DE0009652644", contractDate="2026-12-08", price=121.4, quantity=5, mmtTradingMode="5", tradingDateAndTime="2026-10-05T08:00:03"),   # block
                  dict(messageId="posttrade", instrumentIdentificationCode="DE0009652644", contractDate="2026-12-08", price=121.6, quantity=2, mmtTradingMode="2", tradingDateAndTime="2026-10-05T08:00:04")]:
            f.write(json.dumps(m) + "\n")
    lv = live.Live("2026-10-05", {"DE0001"})
    lv.apply_pre(str(pre)); lv.apply_post(str(post))
    out = lv.to_json("2026-10-05T06:00")
    assert out["bonds"] == {"DE0001": [None, 99.7, None, 50000.0, "08:00:05"]}
    assert out["fut"]["FGBL"] == ["2026-12-08", 121.6, 121.6, 121.5, 2, 12, "08:00:04"]   # front month, on-book only
    assert out["msg"] == "2026-10-05T08:00:05"
    resumed = live.Live("2026-10-05", {"DE0001"}, json.loads(json.dumps(out)))
    assert resumed.bonds == out["bonds"] and resumed.done == out["cursor"]
    assert live.Live("2026-10-06", None, out).bonds == {}                                    # a new day starts empty


def test_ecb_collateral_credit_quality_from_haircuts():
    import ecb_collateral as ec
    hdr = ["ISIN_CODE", "HAIRCUT_CATEGORY", "COUPON_DEFINITION", "DENOMINATION", "MATURITY_DATE", "HAIRCUT", "TYPE"]
    rows = [dict(zip(hdr, r)) for r in [
        *[[f"A{i}", "L1D", "CD4", "EUR", "15/03/2030 00:00:00", "12", "AT02"] for i in range(5)],   # usual level, 3-5y
        ["BBB1", "L1D", "CD4", "EUR", "15/03/2030 00:00:00", "23", "AT02"],                          # markedly higher
        ["EDGE", "L1D", "CD4", "EUR", "10/10/2028 00:00:00", "10", "AT02"],                          # 1-3y level, near the cut-off
        ["ABS1", "L1E", "CD4", "EUR", "15/03/2040 00:00:00", "9", "AT11"],
        ["USD1", "L1D", "CD4", "USD", "15/03/2029 00:00:00", "30", "AT02"]]]
    rows += [dict(zip(hdr, [f"E{i}", "L1D", "CD4", "EUR", "15/03/2028 00:00:00", "10", "AT02"])) for i in range(3)]
    raw = ("\t".join(hdr) + "\n" + "\n".join("\t".join(r[h] for h in hdr) for r in rows) + "\n").encode("utf-16")
    q = ec.classify(ec.parse(gzip.compress(raw)), "2026-10-02")
    assert q["A0"]["cqs"] == "1-2" and q["BBB1"]["cqs"] == "3" and q["EDGE"]["cqs"] == "1-2"
    assert q["ABS1"]["cqs"] is None and "USD1" not in q


def test_ecb_collateral_step3_majority():
    """Category L1C holds many BBB corporates: the higher haircut can be the most common one."""
    import ecb_collateral as ec
    hdr = ["ISIN_CODE", "HAIRCUT_CATEGORY", "COUPON_DEFINITION", "DENOMINATION", "MATURITY_DATE", "HAIRCUT", "TYPE", "ISSUANCE_DATE"]
    rows = [[f"C{i}", "L1C", "CD4", "EUR", "15/03/2030 00:00:00", "3", "AT03", "28/09/2026 00:00:00"] for i in range(4)]
    rows += [[f"B{i}", "L1C", "CD4", "EUR", "15/03/2030 00:00:00", "13", "AT03", "01/01/2020 00:00:00"] for i in range(9)]
    raw = ("\t".join(hdr) + "\n" + "\n".join("\t".join(r) for r in rows) + "\n").encode("utf-16")
    q = ec.classify(ec.parse(raw), "2026-10-02")
    assert {q[f"C{i}"]["cqs"] for i in range(4)} == {"1-2"} and {q[f"B{i}"]["cqs"] for i in range(9)} == {"3"}
    assert q["C0"]["issued"] == "2026-09-28"


def test_weekly_summary(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site"))
    import build
    cols = ["isin", "issuer", "name", "sector", "cpn", "mat", "amt", "bid", "ask", "firm", "ytm", "z", "rv", "ecb", "iss", "esg"]
    def bond(isin, issuer, z, rv, ecb, iss=None, firm=1):
        return [isin, issuer, isin, "CORP_NONFIN", 3.0, "2031-01-01", 500, 99.0, 99.2, firm, 3.5, z, rv, ecb, iss, None]
    last = dict(asof="2026-09-25", settle="2026-09-29", cols=cols, futures=[],
                bonds=[bond("A1", "ACME", 100, -8, "1-2"), bond("A2", "ACME", 110, 0, "1-2"), bond("B1", "BETA", 50, 0, "3")])
    build.write_history(last, str(tmp_path))
    now = dict(asof="2026-10-02", settle="2026-10-06", cols=cols, futures=[],
               bonds=[bond("A1", "ACME", 140, 7, "3"), bond("A2", "ACME", 135, 0, "1-2"), bond("B1", "BETA", 45, 0, None),
                      bond("N1", "NEWCO", 80, None, "1-2", iss="2026-09-30")])
    build.write_history(now, str(tmp_path))
    w = build.weekly(now, str(tmp_path))
    assert w["base"] == "2026-09-25" and w["mon"] == "2026-09-28"
    assert [x["isin"] for x in w["new"]] == ["N1"]
    assert [x["isin"] for x in w["wide"]] == ["A1", "A2"] and w["wide"][0]["dz"] == 40
    assert w["iss_wide"][0] == dict(issuer="ACME", n=2, dz=32.5, up=2)
    assert {(x["isin"], x["ecb0"], x["ecb"], x["dir"]) for x in w["ratings"]} == {("A1", "1-2", "3", "down"), ("B1", "3", None, "down")}
    assert [x["isin"] for x in w["rv"]] == ["A1"]


@pytest.mark.parametrize("fisn,want", [
    ("KOREA HOUSING F/4.0 BD 20300101", "AGENCY"),          # was caught by the sovereign rule ^KOREA
    ("JAPAN FINANCE O/3.0 MTN 20310101", "AGENCY"),
    ("CHINA CON BK CO/3.5 MTN 20300101", "CORP_FIN"),
    ("ICELAND BONDCO/10.875 NT 20300101", "CORP_NONFIN"),   # Iceland Foods, not the Republic
    ("UTD MEXICAN STS/4.5 NT 20330101", "SOV"),
    ("SAECHS.AUFB/2.0 ANL 20300101", "AGENCY"),
    ("COMPAGNIE DE FI/3.0 BD 20300101", "COVERED"),
    ("KOREA, REPUBLIC/3.0 BD 20300101", "SOV"),
])
def test_sector_exceptions(fisn, want):
    assert classify.sector_of(R2("DBFTFB", fisn, "", ""), {})[0] == want


def test_build_reclassify_and_quote_flags():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site"))
    import build, pandas as pd
    b = pd.DataFrame(dict(cfi=["DBFTFB"] * 4, fisn=["KOREA HOUSING F/4 BD", "BIG CORP/3 MTN", "SOME BANK/2 MTN", "CNCL.EU DEV.BK/2 MTN"],
                          issuer_lei=[None] * 4, full_name=[None] * 4, issuer=["KHF", "BIG", "SB", "CEB"],
                          sector=["SOV", "SOV", "CORP_NONFIN", "CORP_FIN"]), index=["I1", "I2", "I3", "I4"])
    ecb = {"I2": {"group": "IG3"}, "I3": {"group": "IG4"}, "I4": {"group": "IG6"}}
    ch = build.reclassify(b, ecb, overrides_csv="/nonexistent")
    assert list(b.sector) == ["AGENCY", "CORP_NONFIN", "CORP_FIN", "SUPRA"]
    assert {(c["issuer"], c["to"]) for c in ch} == {("KHF", "AGENCY"), ("BIG", "CORP_NONFIN"), ("SB", "CORP_FIN"), ("CEB", "SUPRA")}
    assert build.quote_flags(99.0, 98.9, 99.1, 80) == []
    assert build.quote_flags(60.0, 55.0, 65.0, 900) == ["wide"]
    assert build.quote_flags(8.0, 7.9, 8.1, None) == ["price", "spread"]


# ---------------------------------------------------------------- eurex_options
def test_black76_implied_vol_round_trip_and_bounds():
    import eurex_options as eo
    for cp, k in (("C", 123.0), ("P", 118.0)):
        px = eo.black76(121.0, k, 30 / 365.25, 0.082, cp)
        assert eo.implied_vol(px, 121.0, k, 30 / 365.25, cp) == pytest.approx(0.082, abs=1e-5)
    assert eo.implied_vol(0.5, 121.0, 118.0, 0.1, "C") is None      # below intrinsic value (3.0)
    # put-call parity without discounting: C - P = F - K
    c, p = eo.black76(121.0, 120.0, 0.2, 0.08, "C"), eo.black76(121.0, 120.0, 0.2, 0.08, "P")
    assert c - p == pytest.approx(1.0, abs=1e-9)


def test_option_underlying_and_futures_price_matching():
    import eurex_options as eo
    series = {("FGBL", "2026-12-08"): [], ("FGBL", "2027-03-08"): [], ("FGBM", "2026-12-08"): []}
    assert eo.underlying("FGBL", "2026-11-20", series) == "2026-12-08"
    assert eo.underlying("FGBL", "2026-12-23", series) == "2027-03-08"   # January options: on the March future
    t0 = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.timezone.utc)
    pts = [(t0, 121.0), (t0 + dt.timedelta(minutes=10), 121.2)]
    assert eo.future_at(pts, t0 + dt.timedelta(minutes=5)) == 121.0
    assert eo.future_at(pts, t0 - dt.timedelta(minutes=2)) == 121.0     # first trade within 5 minutes after
    assert eo.future_at(pts, t0 - dt.timedelta(minutes=20)) is None


def test_option_atm_vol_and_summary():
    import eurex_options as eo
    f, rows = 121.0, []
    for k in (116, 117, 118, 119, 120, 122, 123, 124, 125):     # smile 8% + 2 m + 60 m^2, m = ln(K/F)
        m = math.log(k / f)
        rows.append(dict(code="FGBL", expiry="2026-10-23", cp="C" if k > f else "P", strike=k, price=0, qty=10,
                         time="2026-10-02T09:00:00", fut_contract="2026-12-08", fut=f, t=21 / 365.25, k=m, iv=0.08 + 2 * m + 60 * m * m))
    v, fit = eo.atm_vol(rows)
    assert v == pytest.approx(0.08, abs=1e-6) and fit[2] == pytest.approx(60, rel=1e-4)
    rows2 = [dict(r, expiry="2026-11-20", t=49 / 365.25, iv=r["iv"] - 0.002) for r in rows]
    s = eo.summary(rows + rows2, "2026-10-02", {"FGBL": 7.74})["FGBL"]
    assert [e["expiry"] for e in s["expiries"]] == ["2026-10-23", "2026-11-20"]
    assert 0.078 < s["atm1m"] < 0.08                             # 30 days lies between the two expiries
    assert s["bp1m"] == pytest.approx(s["atm1m"] / 7.74 * 1e4 / math.sqrt(252), abs=0.01)


# ---------------------------------------------------------------- stir (ECB path)
def test_stir_periods_and_meeting_effective_date():
    import stir
    assert stir.third_wednesday(2026, 12) == dt.date(2026, 12, 16)
    assert stir.estr_period("2026-09-16") == (dt.date(2026, 9, 16), dt.date(2026, 12, 16))
    assert stir.euribor_period("2026-12-14") == (dt.date(2026, 12, 16), dt.date(2027, 3, 16))
    assert stir.effective("2026-10-29") == dt.date(2026, 11, 4)          # Thursday -> next week's Wednesday


def test_stir_path_rest_of_quarter_and_next_meeting():
    import stir
    days = [(dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat() for i in range(31)]
    estr = (days, [2.44] * len(days))
    # quarter Sep 16 - Dec 16 (91 days), 16 days fixed at 2.44; the rest priced at 2.54 (a 10 bp move on Nov 4)
    d1, d2 = (dt.date(2026, 11, 4) - dt.date(2026, 10, 2)).days, (dt.date(2026, 12, 16) - dt.date(2026, 11, 4)).days
    rem = (2.44 * d1 + 2.54 * d2) / (d1 + d2)
    q_rate = (2.44 * 16 + rem * 75) / 91
    rows = [dict(code="FST3", contract="2026-09-16", last=100 - q_rate, trades=50, lots=100),
            dict(code="FST3", contract="2026-12-16", last=97.29, trades=50, lots=100),
            dict(code="FEU3", contract="2026-12-14", last=97.09, trades=50, lots=100)]
    S = stir.build(rows, "2026-10-02", estr, 2.5, ["2026-10-29", "2026-12-17"])
    q0, q1 = S["estr_q"]
    assert q0["remaining"] == pytest.approx(rem, abs=1e-3)
    assert S["next"]["meeting"] == "2026-10-29" and S["next"]["bp"] == pytest.approx(10, abs=0.2)
    assert q1["dfr"] == pytest.approx(2.71 + 0.06, abs=1e-6) and q1["chg"] == pytest.approx(27.0)
    assert S["euribor"][0]["basis"] == pytest.approx(20.0)


def test_index_duration_estimate_applies_the_index_rules():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site"))
    import build
    cols = ["sector", "ecb", "amt", "mat", "dq", "mdur", "bid", "ask", "acc"]
    ok = ["CORP_NONFIN", "3", 500, "2030-01-01", None, 4.0, 99.0, 99.0, 1.0]
    rows = [ok] * 150 + [["CORP_FIN", "1-2", 1000, "2032-01-01", None, 5.5, 99.0, 99.0, 1.0]] * 50
    rows += [["SOV", "1-2", 5000, "2035-01-01", None, 9.0, 99, 99, 1],          # not a corporate
             ["CORP_NONFIN", None, 5000, "2035-01-01", None, 9.0, 99, 99, 1],   # not on the ECB list
             ["CORP_NONFIN", "3", 200, "2035-01-01", None, 9.0, 99, 99, 1],     # under EUR 300m
             ["CORP_NONFIN", "3", 500, "2027-03-01", None, 0.3, 99, 99, 1],     # under a year
             ["CORP_NONFIN", "3", 500, "2035-01-01", "wide", 9.0, 99, 99, 1]]   # flagged quote
    e = build.index_durations(rows, cols, dt.date(2026, 10, 6))["FECX"]
    assert e["n"] == 200 and e["dur"] == pytest.approx((150 * 500 * 4.0 + 50 * 1000 * 5.5) / (150 * 500 + 50 * 1000), abs=0.01)
    assert build.index_durations(rows[:10], cols, dt.date(2026, 10, 6)) == {}    # too few bonds: keep the assumption


@pytest.mark.parametrize("fisn,want", [("THE REPUBLIC OF/4.875 BD 20320119", "SOV"), ("ARAB REPUBLIC O/6.375EMTN 20310411", "SOV"),
                                       ("BANQUE OUEST AF/2.75 BD 20330122", "SUPRA")])
def test_sovereign_and_supra_exceptions(fisn, want):
    assert classify.sector_of(R2("DBFTFB", fisn, "", ""), {})[0] == want


def test_archive_snapshots_saves_each_source_and_survives_failures(tmp_path, monkeypatch):
    import ecb_collateral, market
    monkeypatch.setattr(ecb_collateral, "latest_url", lambda: ("u", "2026-10-02"))
    monkeypatch.setattr(ecb_collateral, "_get", lambda url: b"ecb")
    monkeypatch.setattr(ci.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))   # Euronext fails
    monkeypatch.setattr(market, "fred", lambda sid, day: (["2026-10-01", "2026-10-02"], [3.0, 3.1]))
    out = ci.archive_snapshots(str(tmp_path), dry_run=True, today="2026-10-04")
    assert sorted(os.path.basename(p) for p in out) == ["ecb_eligible-2026-10-02.csv.gz", "fred-2026-10-04.csv.gz"]
    rows = gzip.decompress(open(tmp_path / "fred-2026-10-04.csv.gz", "rb").read()).decode().splitlines()
    assert rows[0] == "series,date,value" and len(rows) == 1 + 2 * len(market.FRED)
