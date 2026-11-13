"""The PHI guard: what the agents are allowed to see."""
import re

from fabric.privacy import Masker, field_stats, hl7_flatten, hl7_mask, scrub_error


def test_pseudonym_keeps_shape_and_is_stable():
    m = Masker("salt")
    out = m.pseudonym("MRN-7381763 Walsh")
    assert out != "MRN-7381763 Walsh"
    assert re.fullmatch(r"[A-Z]{3}-\d{7} [A-Z][a-z]{4}", out)
    assert out == Masker("salt").pseudonym("MRN-7381763 Walsh")
    assert out != Masker("other").pseudonym("MRN-7381763 Walsh")
    assert isinstance(m.pseudonym(5000), int)


def test_identifiers_masked_dates_shifted_codes_kept():
    rows = [{"MRN_NO": str(7000000 + i), "LNAME": f"NAME{i}", "DOB_DT": 19600101 + i, "SEX_CD": "FM"[i % 2],
             "TEST_CD": ["GLU", "HBA1C", "CREA"][i % 3]} for i in range(30)]
    stats = field_stats(rows)
    assert stats["SEX_CD"]["values"] == ["F", "M"] and stats["TEST_CD"]["values"] == ["CREA", "GLU", "HBA1C"]
    assert "values" not in stats["MRN_NO"] and "values" not in stats["LNAME"]
    assert stats["DOB_DT"]["format"].startswith("date YYYYMMDD")
    masked = Masker("salt").mask_flat(rows[0], stats)
    assert masked["SEX_CD"] == "F" and masked["TEST_CD"] == "GLU"          # value domain passes through
    assert masked["MRN_NO"] != rows[0]["MRN_NO"] and len(masked["MRN_NO"]) == 7
    assert masked["LNAME"] != rows[0]["LNAME"]
    assert masked["DOB_DT"] != rows[0]["DOB_DT"] and isinstance(masked["DOB_DT"], int)
    assert 19000101 < masked["DOB_DT"] < 19600101                             # still a plausible date, shifted back


def test_slash_date_order_is_inferred_from_the_full_column():
    assert field_stats([{"d": "25/03/2024"}, {"d": "01/02/2024"}])["d"]["format"] == "date DD/MM/YYYY"
    assert field_stats([{"d": "03/25/2024"}, {"d": "01/02/2024"}])["d"]["format"] == "date MM/DD/YYYY"
    assert "cannot be determined" in field_stats([{"d": "03/04/2024"}])["d"]["format"]


def test_hl7_person_segments_are_masked():
    msg = ("MSH|^~\\&|PAS|GENHOSP|X|GENHOSP|20260101101500||ADT^A04|MSG1|P|2.5\r"
           "PID|1||7381763^^^GENHOSP^MR||WALSH^AOIFE||19390803|F|||13 Park Drive^^Waterford^^X16 P1R4^IE\r"
           "PV1|1|O|OPD1^^^GENHOSP||||D1041^OKAFOR^GRACE^^^DR||||||||||||V0000001\r")
    flat = hl7_flatten(msg)
    assert flat["PID.3.1"] == "7381763" and flat["PV1.19"] == "V0000001" and flat["MSH.9.2"] == "A04"
    masked = hl7_mask(msg, field_stats([flat]), Masker("salt"))
    for secret in ("7381763", "WALSH", "AOIFE", "19390803", "Park Drive", "Waterford", "OKAFOR"):
        assert secret not in masked
    assert masked.splitlines()[1].split("|")[8] == "F"                        # sex is a code, kept
    assert masked.count("|") == msg.count("|")                                # structure intact


def test_error_text_is_scrubbed_before_repair():
    code = "value = row['RSLT_VAL']\n"
    text = scrub_error("could not convert 'Walsh 7381763' for key 'RSLT_VAL' at 20260128054000", code)
    assert "Walsh" not in text and "7381763" not in text and "20260128054000" not in text
    assert "'RSLT_VAL'" in text                                               # names from the code survive
