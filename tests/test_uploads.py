"""Demo uploads: each source system accepts its own format, bad files are rejected with
nothing written, every upload is re-validated and rolled back byte for byte if a check
fails, reset removes every upload, and the endpoints can be switched off."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from fabric import gateway, uploads
from mock_systems.build import build_all
from mock_systems.synthea import load_estate


@pytest.fixture()
def estate(tmp_path, monkeypatch):
    data = tmp_path / "_data"
    build_all(load_estate(), data)
    monkeypatch.setattr(uploads, "DATA_DIR", data)
    return {"ehr": {"id": "ehr-rest", "kind": "http"},
            "lab": {"id": "lab-sql", "kind": "sqlite", "path": str(data / "lab" / "lab.db")},
            "adt": {"id": "adt-feed", "kind": "hl7v2_files", "path": str(data / "adt")},
            "rx": {"id": "pharmacy-csv", "kind": "delimited_files", "path": str(data / "pharmacy")},
            "data": data}


def test_every_system_accepts_its_template(estate):
    data = estate["data"]
    labs_before = sqlite3.connect(data / "lab" / "lab.db").execute("SELECT COUNT(*) FROM LAB_RSLT").fetchone()[0]
    adt_before, rx_before = len(list((data / "adt").glob("*.hl7"))), len(list((data / "pharmacy").glob("*.csv")))
    cons_before = len(json.loads((data / "ehr" / "conditions.json").read_text()))
    for key in ("ehr", "lab", "adt", "rx"):
        _, content = uploads.template(estate[key], "7849455")
        assert uploads.ingest(estate[key], content.encode())["added"] >= 1
    assert sqlite3.connect(data / "lab" / "lab.db").execute("SELECT COUNT(*) FROM LAB_RSLT").fetchone()[0] == labs_before + 2
    assert len(list((data / "adt").glob("*.hl7"))) == adt_before + 1
    assert len(list((data / "pharmacy").glob("rx_export_*.csv"))) == rx_before + 1
    assert len(json.loads((data / "ehr" / "conditions.json").read_text())) == cons_before + 1


@pytest.mark.parametrize("key, content, why", [
    ("adt", b"hello", "first segment must be MSH"),
    ("adt", b"MSH|^~\\&|PAS|H|I|H|20261122||ORU^R01|1|P|2.5\rPID|1||7849455^^^H^MR", "Only ADT"),
    ("lab", b"MRN,TEST_CD,RSLT_VAL,ABN_FLG,RSLT_STAT,COLL_DTTM,RSLT_DTTM\n7849455,NOPE,1,N,F,20261122101010,20261122101010\n",
     "unknown test code"),
    ("lab", b"MRN,TEST_CD,RSLT_VAL,ABN_FLG,RSLT_STAT,COLL_DTTM,RSLT_DTTM\n9999999,GLU,1,N,F,20261122101010,20261122101010\n",
     "no lab patient"),
    ("rx", b"RX|MRN\nRX1|7849455\n", "header must be"),
    ("rx", b"RX_NO|MRN|DRUG_DESC|RXNORM_CD|SIG_TXT|QTY|START_DT|STAT\nRX9|7849455|X|1|Y|1|31/02/2026|A\n", "real date"),
    ("ehr", b'[{"conditionId": "X1", "mrn": "0000000", "code": {"value": "1"}, "onsetDate": "2026-01-01", "state": "A"}]',
     "no EHR patient"),
    ("ehr", b"{not json", "Not valid JSON"),
    ("ehr", b"", "empty"),
])
def test_bad_uploads_are_rejected_and_nothing_is_written(estate, key, content, why):
    data = estate["data"]
    snapshot = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    with pytest.raises(uploads.UploadError, match=why):
        uploads.ingest(estate[key], content)
    assert {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()} == snapshot


def test_reset_removes_every_upload(estate):
    exports = len(list((estate["data"] / "pharmacy").glob("*.csv")))
    for key in ("adt", "rx", "lab"):
        uploads.ingest(estate[key], uploads.template(estate[key])[1].encode())
    uploads.reset()
    data = estate["data"]
    assert not list((data / "adt").glob("*_upload.hl7"))
    assert len(list((data / "pharmacy").glob("*.csv"))) == exports


def test_upload_endpoints_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("FABRIC_DEMO_UPLOADS", "0")
    client = TestClient(gateway.app)
    assert client.get("/sources").json()["uploads"] is False
    assert client.post("/sources/adt-feed/upload", content=b"MSH|").status_code == 403
    assert client.post("/sources/reset").status_code == 403


def test_uploads_are_on_by_default(monkeypatch):
    monkeypatch.delenv("FABRIC_DEMO_UPLOADS", raising=False)
    assert uploads.enabled() is True
    assert TestClient(gateway.app).get("/sources").json()["uploads"] is True


# ------------------------------------------------------------ validate after upload, keep or roll back

def _files(data):
    return {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}


PASS = {"healthy": True, "checks_passed": 14, "checks_total": 14, "resources": 1, "drift": None, "failed": []}
FAIL = {"healthy": False, "checks_passed": 13, "checks_total": 14, "resources": 1, "drift": None,
        "failed": [{"id": "fhir.schema", "title": "FHIR R4 schema", "detail": "1 resource invalid"}]}


@pytest.mark.parametrize("key", ["ehr", "lab", "adt", "rx"])
def test_failed_validation_rolls_every_system_back_byte_for_byte(estate, key):
    before = _files(estate["data"])
    with pytest.raises(uploads.ValidationFailed, match="fhir.schema") as err:
        uploads.ingest_validated(estate[key], uploads.template(estate[key])[1].encode(), lambda s: FAIL)
    assert err.value.validation["checks_passed"] == 13
    assert _files(estate["data"]) == before


@pytest.mark.parametrize("key", ["ehr", "lab", "adt", "rx"])
def test_upload_is_kept_when_every_check_passes(estate, key):
    before = _files(estate["data"])
    seen = []
    result = uploads.ingest_validated(estate[key], uploads.template(estate[key])[1].encode(),
                                      lambda s: seen.append(s["id"]) or PASS)
    assert seen == [estate[key]["id"]]                 # the check ran on the system that received the data
    assert result["validation"]["passed"] and result["validation"]["checks_passed"] == 14
    assert _files(estate["data"]) != before


def test_drift_alone_is_enough_to_roll_back(estate):
    before = _files(estate["data"])
    drifted = {**PASS, "healthy": False, "drift": {"added": ["QTY2"], "removed": [], "changed": []}}
    with pytest.raises(uploads.ValidationFailed, match="no longer matches"):
        uploads.ingest_validated(estate["rx"], uploads.template(estate["rx"])[1].encode(), lambda s: drifted)
    assert _files(estate["data"]) == before


def test_a_check_that_cannot_run_rolls_back(estate):
    before = _files(estate["data"])

    def broken(system):
        raise RuntimeError("source unreachable")
    with pytest.raises(uploads.ValidationFailed):
        uploads.ingest_validated(estate["adt"], uploads.template(estate["adt"])[1].encode(), broken)
    assert _files(estate["data"]) == before


def test_real_contract_accepts_good_lab_data_and_rejects_a_text_result(estate):
    from fabric.config import GENERATED, load_config
    from fabric.operate import check_system

    folder = GENERATED / "lab-sql"
    if not (folder / "adapter.py").exists():
        pytest.skip("no promoted lab connector in generated/")
    mrn_system = load_config()["settings"]["mrn_system"]
    lab = {**next(s for s in load_config()["systems"] if s["id"] == "lab-sql"), "path": estate["lab"]["path"]}

    def verify(system):
        return check_system(system, mrn_system, folder, with_truth=False)

    good = uploads.ingest_validated(lab, uploads.template(lab)[1].encode(), verify)
    assert good["validation"]["checks_passed"] == good["validation"]["checks_total"] == 14
    before = _files(estate["data"])
    bad = (b"MRN,TEST_CD,RSLT_VAL,ABN_FLG,RSLT_STAT,COLL_DTTM,RSLT_DTTM\n"
           b"7849455,GLU,high,H,F,20261122101010,20261122101010\n")
    with pytest.raises(uploads.ValidationFailed) as err:
        uploads.ingest_validated(lab, bad, verify)
    assert err.value.validation["drift"]["changed"][0]["field"] == "LAB_RSLT.RSLT_VAL"
    assert _files(estate["data"]) == before


def test_uploads_in_the_same_second_never_overwrite_each_other(estate):
    for key, pattern in (("adt", "*_upload.hl7"), ("rx", "rx_export_*.csv")):
        folder = estate["data"] / ("adt" if key == "adt" else "pharmacy")
        before = len(list(folder.glob(pattern)))
        content = uploads.template(estate[key])[1].encode()
        names = []
        for _ in range(3):
            uploads.ingest(estate[key], content)
            names.append(max(p.name for p in folder.glob(pattern)))
        assert len(list(folder.glob(pattern))) == before + 3
        assert names == sorted(names) and len(set(names)) == 3      # each new upload sorts after the last


def test_a_new_upload_sorts_after_a_file_already_ahead_of_the_clock(estate):
    folder = estate["data"] / "adt"
    (folder / "adt_29991231235959_upload.hl7").write_text("MSH|", encoding="utf-8")
    uploads.ingest(estate["adt"], uploads.template(estate["adt"])[1].encode())
    assert max(p.name for p in folder.glob("adt_*.hl7")) > "adt_29991231235959_upload.hl7"


# ------------------------------------------------------------ quarantine: keep the good records, hold back the bad

LAB_H = "MRN,TEST_CD,RSLT_VAL,ABN_FLG,RSLT_STAT,COLL_DTTM,RSLT_DTTM"
RX_H = "RX_NO|MRN|DRUG_DESC|RXNORM_CD|SIG_TXT|QTY|START_DT|STAT"


def _lab_rows(data):
    return sqlite3.connect(data / "lab" / "lab.db").execute("SELECT COUNT(*) FROM LAB_RSLT").fetchone()[0]


def test_mixed_lab_file_keeps_good_rows_and_quarantines_the_bad_one(estate):
    from fabric.config import GENERATED, load_config
    from fabric.operate import check_system

    folder = GENERATED / "lab-sql"
    if not (folder / "adapter.py").exists():
        pytest.skip("no promoted lab connector in generated/")
    mrn_system = load_config()["settings"]["mrn_system"]
    lab = {**next(s for s in load_config()["systems"] if s["id"] == "lab-sql"), "path": estate["lab"]["path"]}
    before = _lab_rows(estate["data"])
    body = "\n".join([LAB_H, "7849455,GLU,118.0,H,F,20261122080000,20261122100000",
                      "7849455,K,high,H,F,20261122080000,20261122100000",
                      "7849455,NA,139.0,N,F,20261122080000,20261122100000"]).encode()
    out = uploads.ingest_with_quarantine(lab, body, lambda s: check_system(s, mrn_system, folder, with_truth=False))
    assert out["outcome"] == "partial"
    assert out["records"]["accepted"] == ["Line 2: MRN 7849455, test GLU", "Line 4: MRN 7849455, test NA"]
    (q,) = out["records"]["quarantined"]
    assert q["record"] == "Line 3: MRN 7849455, test K" and q["drift"]["changed"][0]["field"] == "LAB_RSLT.RSLT_VAL"
    assert _lab_rows(estate["data"]) == before + 2                     # only the two good rows were written
    assert out["validation"]["passed"]                                  # and the system is healthy afterwards


def test_a_bad_row_is_quarantined_not_the_whole_file(estate):
    body = "\n".join([RX_H, "RXQ1|7849455|A|1|B|28|22/11/2026|A", "RXQ2|7849455|A|1|B|28|31/02/2026|A",
                      "RXQ3|7849455|A|1|B|28|22/11/2026|A"]).encode()
    out = uploads.ingest_with_quarantine(estate["rx"], body, lambda s: PASS)
    assert out["outcome"] == "partial" and len(out["records"]["accepted"]) == 2
    (q,) = out["records"]["quarantined"]
    assert q["record"] == "Line 3: prescription RXQ2" and q["format"].startswith("START_DT must be a real date")


def test_a_file_in_the_wrong_format_is_still_refused_whole(estate):
    before = _files(estate["data"])
    with pytest.raises(uploads.UploadError, match="header must be"):
        uploads.ingest_with_quarantine(estate["rx"], b"RX|MRN\nRX1|7849455\nRX2|7849455\n", lambda s: PASS)
    assert _files(estate["data"]) == before


def test_when_no_record_passes_everything_is_rolled_back(estate):
    before = _files(estate["data"])
    body = "\n".join([LAB_H, "7849455,GLU,1.0,N,F,20261122080000,20261122100000",
                      "7849455,NA,2.0,N,F,20261122080000,20261122100000"]).encode()
    with pytest.raises(uploads.ValidationFailed) as err:
        uploads.ingest_with_quarantine(estate["lab"], body, lambda s: FAIL)
    assert len(err.value.records["quarantined"]) == 2 and not err.value.records["accepted"]
    assert _files(estate["data"]) == before


def test_a_single_record_upload_behaves_as_before(estate):
    before = _files(estate["data"])
    with pytest.raises(uploads.ValidationFailed) as err:
        uploads.ingest_with_quarantine(estate["adt"], uploads.template(estate["adt"])[1].encode(), lambda s: FAIL)
    assert not hasattr(err.value, "records")
    assert _files(estate["data"]) == before


def test_a_good_file_is_accepted_whole_in_one_check(estate):
    calls = []
    out = uploads.ingest_with_quarantine(estate["lab"], uploads.template(estate["lab"])[1].encode(),
                                         lambda s: calls.append(1) or PASS)
    assert out["outcome"] == "accepted" and calls == [1]


def test_hl7_files_split_into_one_record_per_message(estate):
    one = uploads.template(estate["adt"])[1]
    records = uploads.split_records(estate["adt"], (one + one).encode())
    assert len(records) == 2 and all(r[0].startswith("Message ") and "ADT^A04" in r[0] for r in records)
