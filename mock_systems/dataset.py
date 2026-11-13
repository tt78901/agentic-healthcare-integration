"""Canonical synthetic dataset shared by all mock systems.

Everything here is generated from a fixed seed. No real patient data is used
anywhere in this project. The clinical codes (LOINC, SNOMED CT, RxNorm) are
illustrative values for a demo and are not clinically validated.

The same canonical shape is produced by ``mock_systems.synthea.load_synthea``
so the mock systems can also be seeded from a real Synthea FHIR export.
"""
from __future__ import annotations

import datetime as dt
import random

ANCHOR = dt.datetime(2026, 10, 31, 12, 0, 0)  # "today" for the synthetic estate

FIRST_F = ["Aoife", "Niamh", "Ciara", "Saoirse", "Emma", "Grace", "Sophie", "Hannah",
           "Maria", "Priya", "Fatima", "Elena", "Chloe", "Ruth", "Orla", "Zainab"]
FIRST_M = ["Conor", "Sean", "Liam", "Patrick", "Daniel", "James", "Adam", "Luke",
           "Arjun", "Mateusz", "Omar", "David", "Eoin", "Cian", "Ravi", "Tomas"]
LAST = ["Murphy", "Kelly", "Byrne", "Ryan", "Walsh", "Doyle", "McCarthy", "Gallagher",
        "Kennedy", "Lynch", "Murray", "Quinn", "Nowak", "Sharma", "Hassan", "Popescu",
        "Fitzgerald", "Brennan", "Kavanagh", "Reilly", "Dunne", "Nolan", "Farrell", "Costa"]
STREETS = ["Abbey Road", "Church Street", "Mill Lane", "Harbour View", "Oak Avenue",
           "Station Road", "Park Drive", "Castle Street", "River Walk", "Elm Grove"]
CITIES = [("Dublin", "D"), ("Cork", "T"), ("Galway", "H"), ("Limerick", "V"), ("Waterford", "X")]

# (local lab code, LOINC, name, UCUM unit, ref low, ref high, mean, sd)
LAB_TESTS = [
    ("HBA1C", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", 4.0, 5.6, 5.9, 0.9),
    ("GLU", "2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL", 70, 99, 98, 18),
    ("CREA", "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL", 0.6, 1.3, 0.95, 0.25),
    ("HGB", "718-7", "Hemoglobin [Mass/volume] in Blood", "g/dL", 12.0, 17.5, 14.0, 1.5),
    ("CHOL", "2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "mg/dL", 0, 200, 195, 35),
    ("HDL", "2085-9", "Cholesterol in HDL [Mass/volume] in Serum or Plasma", "mg/dL", 40, 100, 52, 12),
    ("TRIG", "2571-8", "Triglyceride [Mass/volume] in Serum or Plasma", "mg/dL", 0, 150, 135, 50),
    ("WBC", "6690-2", "Leukocytes [#/volume] in Blood by Automated count", "10*3/uL", 4.0, 11.0, 7.2, 1.9),
    ("PLT", "777-3", "Platelets [#/volume] in Blood by Automated count", "10*3/uL", 150, 400, 260, 60),
    ("NA", "2951-2", "Sodium [Moles/volume] in Serum or Plasma", "mmol/L", 136, 145, 140, 2.5),
    ("K", "2823-3", "Potassium [Moles/volume] in Serum or Plasma", "mmol/L", 3.5, 5.1, 4.2, 0.4),
    ("ALT", "1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "U/L", 7, 56, 28, 12),
    ("TSH", "3016-3", "Thyrotropin [Units/volume] in Serum or Plasma", "m[IU]/L", 0.4, 4.0, 2.1, 1.0),
]

CONDITIONS = [
    ("44054006", "Diabetes mellitus type 2"),
    ("38341003", "Hypertensive disorder"),
    ("195967001", "Asthma"),
    ("55822004", "Hyperlipidemia"),
    ("35489007", "Depressive disorder"),
    ("13645005", "Chronic obstructive lung disease"),
    ("49436004", "Atrial fibrillation"),
    ("235595009", "Gastroesophageal reflux disease"),
    ("69896004", "Rheumatoid arthritis"),
    ("40930008", "Hypothyroidism"),
]

MEDICATIONS = [
    ("860975", "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet", "Take 1 tablet by mouth twice daily"),
    ("314076", "Lisinopril 10 MG Oral Tablet", "Take 1 tablet by mouth once daily"),
    ("197361", "Amlodipine 5 MG Oral Tablet", "Take 1 tablet by mouth once daily"),
    ("312961", "Simvastatin 20 MG Oral Tablet", "Take 1 tablet by mouth at bedtime"),
    ("310798", "Hydrochlorothiazide 25 MG Oral Tablet", "Take 1 tablet by mouth every morning"),
    ("259255", "Atorvastatin 80 MG Oral Tablet", "Take 1 tablet by mouth once daily"),
    ("309362", "Clopidogrel 75 MG Oral Tablet", "Take 1 tablet by mouth once daily"),
    ("313782", "Acetaminophen 325 MG Oral Tablet", "Take 2 tablets by mouth every 6 hours as needed"),
    ("895994", "120 ACTUAT Fluticasone propionate 0.044 MG/ACTUAT Metered Dose Inhaler", "Inhale 2 puffs twice daily"),
    ("966247", "Levothyroxine Sodium 0.075 MG Oral Tablet", "Take 1 tablet by mouth every morning"),
]

DOCTORS = [("D1041", "Okafor", "Grace"), ("D1077", "Brady", "Michael"), ("D1102", "Chen", "Wei"),
           ("D1136", "Hughes", "Sarah"), ("D1159", "Rossi", "Paolo"), ("D1183", "Daly", "Fiona")]
WARDS = {"ambulatory": ["OPD1", "OPD2", "CLINIC3"], "emergency": ["ED"], "inpatient": ["MED1", "SURG2", "CARD3"]}
REASONS = ["Routine review", "Chest pain", "Shortness of breath", "Medication review",
           "Abdominal pain", "Follow-up", "Fall", "Blood pressure check", "Fever"]


def generate(n_patients: int = 40, seed: int = 20261113) -> dict:
    """Return the canonical dataset. Deterministic for a given seed."""
    rng = random.Random(seed)
    patients, encounters, labs, conditions, medications = [], [], [], [], []
    used_mrn: set[str] = set()
    visit_seq = result_seq = cond_seq = rx_seq = 0

    for i in range(n_patients):
        sex = rng.choice(["female", "male"])
        first = rng.choice(FIRST_F if sex == "female" else FIRST_M)
        last = rng.choice(LAST)
        while True:
            mrn = str(rng.randint(7000000, 7999999))
            if mrn not in used_mrn:
                used_mrn.add(mrn)
                break
        dob = dt.date(rng.randint(1938, 2006), rng.randint(1, 12), rng.randint(1, 28))
        city, area = rng.choice(CITIES)
        patients.append({
            "mrn": mrn, "first": first, "last": last, "dob": dob.isoformat(), "sex": sex,
            "phone": f"+353 {rng.randint(21, 91)} 555 {rng.randint(0, 9999):04d}",
            "line": f"{rng.randint(1, 180)} {rng.choice(STREETS)}", "city": city,
            "postcode": f"{area}{rng.randint(10, 99)} {rng.choice('ACDEFHKNPRTVWXY')}{rng.randint(0, 9)}"
                        f"{rng.choice('ACDEFHKNPRTVWXY')}{rng.randint(0, 9)}",
            "country": "IE",
        })

        # Encounters over the last two years
        for _ in range(rng.randint(2, 6)):
            visit_seq += 1
            klass = rng.choices(["ambulatory", "emergency", "inpatient"], weights=[70, 15, 15])[0]
            start = ANCHOR - dt.timedelta(days=rng.randint(0, 720), hours=rng.randint(0, 9), minutes=rng.choice([0, 15, 30, 45]))
            if klass == "inpatient":
                end = start + dt.timedelta(days=rng.randint(1, 7), hours=rng.randint(0, 6))
                if end > ANCHOR:  # still admitted at the anchor date
                    end = None
            elif klass == "emergency":
                end = start + dt.timedelta(hours=rng.randint(2, 9))
            else:
                end = start + dt.timedelta(minutes=rng.choice([20, 30, 45, 60]))
            doc = rng.choice(DOCTORS)
            encounters.append({
                "visit_no": f"V{visit_seq:07d}", "mrn": mrn, "class": klass,
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds") if end else None,
                "ward": rng.choice(WARDS[klass]),
                "attending": {"id": doc[0], "last": doc[1], "first": doc[2]},
                "reason": rng.choice(REASONS),
            })

        # Lab results: roughly one in ten patients has never had bloods taken
        if rng.random() > 0.1:
            for _ in range(rng.randint(1, 4)):  # lab panels
                when = ANCHOR - dt.timedelta(days=rng.randint(0, 720), hours=rng.randint(0, 8), minutes=rng.randint(0, 59))
                for code, loinc, name, unit, lo, hi, mean, sd in rng.sample(LAB_TESTS, rng.randint(2, 6)):
                    result_seq += 1
                    value = round(max(0.1, rng.gauss(mean, sd)), 1 if mean < 20 else 0)
                    labs.append({
                        "result_id": result_seq, "mrn": mrn, "local_code": code, "loinc": loinc,
                        "name": name, "value": value, "unit": unit, "ref_low": lo, "ref_high": hi,
                        "flag": "H" if value > hi else ("L" if value < lo else "N"),
                        "status": rng.choices(["final", "preliminary", "corrected"], weights=[90, 6, 4])[0],
                        "collected": when.isoformat(timespec="seconds"),
                        "resulted": (when + dt.timedelta(hours=rng.randint(1, 30))).isoformat(timespec="seconds"),
                    })

        for snomed, display in rng.sample(CONDITIONS, rng.choices([0, 1, 2, 3], weights=[20, 40, 30, 10])[0]):
            cond_seq += 1
            onset = ANCHOR.date() - dt.timedelta(days=rng.randint(60, 5000))
            conditions.append({"condition_id": f"C{cond_seq:06d}", "mrn": mrn, "snomed": snomed,
                               "display": display, "onset": onset.isoformat(),
                               "status": rng.choices(["active", "resolved"], weights=[80, 20])[0]})

        for rxnorm, drug, sig in rng.sample(MEDICATIONS, rng.choices([0, 1, 2, 3, 4], weights=[15, 30, 30, 15, 10])[0]):
            rx_seq += 1
            start = ANCHOR.date() - dt.timedelta(days=rng.randint(5, 700))
            medications.append({"rx_no": f"RX{rx_seq:07d}", "mrn": mrn, "rxnorm": rxnorm, "drug": drug,
                                "sig": sig, "qty": rng.choice([28, 30, 56, 60, 90]), "start": start.isoformat(),
                                "status": rng.choices(["active", "stopped", "completed"], weights=[65, 15, 20])[0]})

    # Two patients are still admitted at the anchor date (admission message, no discharge yet)
    for k, e in enumerate([e for e in encounters if e["class"] == "inpatient"][-2:]):
        e["start"] = (ANCHOR - dt.timedelta(days=k + 1, hours=3)).isoformat(timespec="seconds")
        e["end"] = None

    return {"source": f"built-in generator (seed {seed})", "patients": patients, "encounters": encounters,
            "labs": labs, "conditions": conditions, "medications": medications}
