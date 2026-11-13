"""The estate's integration standards: the fixed target every generated adapter
must hit. Agents decide *how* to reach each system; these standards make the
results interchangeable, which is where the consistency gain comes from.
"""
from __future__ import annotations

RESOURCE_TYPES = ["Patient", "Encounter", "Observation", "Condition", "MedicationRequest"]

PATTERNS = {
    "api_facade": "The system already exposes an HTTP API. Wrap it: call the API on demand and translate "
                  "responses to FHIR. No copy of the data is kept.",
    "database_polling": "No API. Read the database directly with read-only SQL on a schedule and translate rows "
                        "to FHIR. Suits systems where the vendor schema is stable and no change feed exists.",
    "message_feed_ingest": "The system emits event messages (for example HL7 v2). Parse each message, merge the "
                           "events that describe the same business record, and emit FHIR.",
    "file_drop_ingest": "The system drops periodic export files. Read every file, de-duplicate re-exported "
                        "records (latest file wins) and translate to FHIR.",
}

ALLOWED_IMPORTS = {"sqlite3", "csv", "json", "re", "datetime", "pathlib", "glob", "os", "typing", "collections",
                   "itertools", "functools", "decimal", "math", "urllib.parse", "httpx", "io", "__future__",
                   "os.path", "string", "operator", "dataclasses"}

CONFIG_HELP = {
    "sqlite": "config['path'] is the SQLite database file. Open it read-only: "
              "sqlite3.connect(pathlib.Path(config['path']).as_uri() + '?mode=ro', uri=True).",
    "http": "config['base_url'] is the API root and config['headers'] holds the authentication headers. "
            "Use httpx.Client(base_url=config['base_url'], headers=config['headers'], timeout=30). "
            "Follow pagination until every item has been read.",
    "hl7v2_files": "config['path'] is a folder and config['pattern'] a glob. Each matching file holds one HL7 v2 "
                   "message. Read with open(path, encoding='utf-8', newline=''). Segments are separated by '\\r', "
                   "fields by '|', components by '^'. Parse by splitting; no HL7 library is available.",
    "delimited_files": "config['path'] is a folder and config['pattern'] a glob. Read every matching file in "
                       "sorted file-name order with the csv module.",
}

COUNT_BASIS_HELP = {
    "sqlite": '{"entity": "<table name>", "distinct_key": "<column name>"}',
    "http": '{"entity": "<list endpoint path, e.g. /api/v2/things>", "distinct_key": "<item field, dot path>"}',
    "hl7v2_files": '{"entity": "messages" (all) or a message type such as "ADT^A01", '
                   '"distinct_key": "<HL7 path such as PV1.19 or PID.3.1>"}',
    "delimited_files": '{"entity": "<file pattern>", "distinct_key": "<column name>"}',
}


def fhir_conventions(mrn_system: str) -> str:
    return f"""FHIR R4 conventions for this estate (all mandatory):

- Every resource has "resourceType", "id" and "meta": {{"source": "urn:system:<SYSTEM_ID>"}}.
- "id" is "<SYSTEM_ID>-<source record key>", using only A-Z a-z 0-9 '-' '.', at most 64 characters,
  unique within a resource type. Replace any other character with '-'.
- The shared patient key is the MRN. Patient.identifier contains
  {{"system": "{mrn_system}", "value": "<MRN>"}}.
- Every other resource links to its patient with a logical reference:
  "subject": {{"identifier": {{"system": "{mrn_system}", "value": "<MRN>"}}}}.
- Omit an element when the source value is empty. Never emit null, "" or empty lists/objects.
- date is YYYY-MM-DD. dateTime with a time MUST carry a timezone. Source timestamps without a timezone
  are treated as UTC: emit YYYY-MM-DDThh:mm:ssZ.
- Patient: name [{{"family", "given": [..]}}], gender (male|female|other|unknown), birthDate, telecom, address.
- Encounter: status (in-progress|finished|...), class is ONE Coding
  {{"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"|"EMER"|"IMP"}},
  subject, period {{"start", "end"}} (omit "end" while the visit is open; then status is in-progress).
- Observation: status (final|preliminary|corrected|...), category laboratory
  [{{"coding": [{{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}}]}}],
  code {{"coding": [{{"system": "http://loinc.org", "code", "display"}}]}}, subject, effectiveDateTime,
  valueQuantity {{"value": <number>, "unit", "system": "http://unitsofmeasure.org", "code"}},
  interpretation [{{"coding": [{{"system":
  "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": "H"|"L"|"N"}}]}}].
- Condition: clinicalStatus {{"coding": [{{"system":
  "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"|"resolved"}}]}},
  code {{"coding": [{{"system": "http://snomed.info/sct", "code", "display"}}]}}, subject, onsetDateTime.
- MedicationRequest: status (active|stopped|completed|...), intent "order",
  medicationCodeableConcept {{"coding": [{{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code",
  "display"}}]}}, subject, authoredOn, dosageInstruction [{{"text"}}],
  dispenseRequest {{"quantity": {{"value": <number>}}}}."""


def adapter_contract(system: dict, mrn_system: str) -> str:
    return f"""ADAPTER CONTRACT

Write ONE Python module with exactly this public surface:

    SYSTEM_ID = "{system['id']}"
    SUPPORTED = [...]   # the resource types from the approved design, as strings

    def fetch(resource_type: str, config: dict) -> list[dict]:
        \"\"\"Return ALL resources of that type as FHIR R4 JSON dicts, sorted by id.
        Raise ValueError for a resource type not in SUPPORTED.\"\"\"

Runtime configuration: {CONFIG_HELP[system['kind']]}

Rules:
1. Read-only. Never write, update or delete anything in the source system or on disk.
2. Import only from: {', '.join(sorted(ALLOWED_IMPORTS - {'__future__'}))}.
3. No print(), no logging, no file writes. Never put a record's values into an exception message.
4. Deterministic: the same source data gives the same output in the same order. No random ids, no clock.
5. Secrets come only from config. Never write a credential into the code.
6. Emit exactly one resource per distinct source record, as defined by the design's count_basis. When the
   source repeats a record (several messages about one visit, a record re-exported in a later file),
   merge them so each id appears once and carries the latest state.
7. Use the exact table, column, field and path names shown in the discovery output.

{fhir_conventions(mrn_system)}"""
