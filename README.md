# Agentic healthcare integration fabric

EXL × UCD Data & AI Hackathon 2026, Challenge 01. Team Datarex: Tarun Teja Magendran, Vishal Krish.

A hospital group runs about 45 systems and only a quarter expose a usable API. This prototype shows AI agents
doing the integration work an engineer would otherwise do by hand. The agents discover a source system, choose
an integration pattern, design the FHIR mapping and write the adapter. A fixed validation contract, an
independent correctness check and a human gate then decide whether that adapter is allowed to serve data. Once
it is live, the fabric watches for the source changing underneath it.

It integrates four mock systems of four different kinds into one FHIR R4 API:

| System | Kind | What the agents have to work out |
|---|---|---|
| `ehr-rest` MediCore EHR | REST API (custom JSON, API key, paging) | US-style dates, single-letter codes |
| `lab-sql` LabTrak LIS | Legacy SQL database, no API | Terse column names, a three-table join, dates stored as integers |
| `adt-feed` Patient administration | HL7 v2.5 ADT message files | An inpatient stay is two messages that must be merged |
| `pharmacy-csv` Pharmacy | Nightly pipe-delimited exports | Re-exported prescriptions must be de-duplicated |

All data is synthetic.

## How it works

```
                 ┌──────────── human gate ───────────┐  ┌── human gate ──┐   ┌──── human gate ────┐
 source ──► 1 DISCOVER ──► 2 DESIGN ──► 3 GENERATE ──► 4 VALIDATE ──► EVIDENCE ──► PROMOTE ──► 5 OPERATE
 system     probe (tool)    agent        agent          14 checks      ground-truth             gateway + console
            + agent                        ▲            (no LLM)       comparison               drift check ──┐
                                           └── repair ◄─ failed checks (self-healing)                         │
                                           └── repair ◄─ reviewer feedback                                    │
            ▲─────────────────────────────── re-integrate (python -m fabric check --heal) ◄───────────────────┘
```

1. **Discover.** A deterministic, read-only probe inspects the system: structure, field statistics and a few
   samples. Samples are privacy-masked before any model sees them. The Discover agent turns the probe output
   into a system profile: entities, field meanings, keys, code systems, data-quality notes and open questions.
2. **Design.** The Design agent picks one pattern from a catalogue (`api_facade`, `database_polling`,
   `message_feed_ingest`, `file_drop_ingest`), explains the choice and writes the field-level FHIR mapping. It
   also states how source records can be counted independently, and the tools then measure that count.
3. **Generate.** The Generate agent writes a Python adapter to a fixed contract: `fetch(resource_type, config)`
   returning FHIR R4 resources.
4. **Validate.** Fourteen deterministic checks, the same for every adapter:
   - static safety: import allow-list, no dynamic execution or file writes, read-only SQL, no logging of
     records, no credentials in code;
   - a run in a separate process, which must be deterministic and print nothing;
   - FHIR R4 schema validation and the estate's conventions;
   - reconciliation of record counts against the source.

   Failed checks go back to the Generate agent, with data values scrubbed from the error text. Code that fails
   a static check is never executed.
5. **Evidence.** The contract cannot catch a mapping that is valid FHIR but wrong, such as day and month
   swapped. Because the estate is synthetic, every value the adapter emits is compared with the known answer.
   The reviewer sees the result at the promotion gate. The agents never do.
6. **Promote and operate.** Promoted adapters are served by the gateway, and its console shows one patient's
   record assembled from all four systems. `python -m fabric check` re-probes every source, compares its
   structure with the fingerprint stored at promotion, and re-runs the contract. This catches loud breakage
   (the adapter crashes) and silent breakage (it runs but drops a field). `--heal` sends drifted systems back
   through the pipeline, gates included.

Every model call, tool run, validation result, evaluation and gate decision is written to
`runs/<run_id>/audit.jsonl`, and each run produces an HTML report (`runs/<run_id>/report.html`).

## Quick start

Python 3.10 or later.

```bash
pip install -r requirements.txt

# 1. Smoke test without a model (about 5 seconds). Proves the pipeline, not the agents.
python -m fabric demo --stub --auto-approve
python -m pytest

# 2. Real run. Configure a model first:
cp .env.example .env            # then fill in LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
python -m fabric demo           # interactive: you approve or reject at each gate
#   --auto-approve skips the prompts; the audit trail then records that no human reviewed
#   python -m fabric run --system lab-sql     runs a single system

# 3. Console and unified FHIR API
python -m fabric gateway        # http://127.0.0.1:8000  (console)   /docs (API)
```

Any OpenAI-compatible chat-completions endpoint works. `.env.example` shows Azure AI Foundry, Google Gemini and
a local Ollama. Model responses are cached on disk by prompt hash, so a completed run replays offline and
re-runs are free. Pass `--no-cache` to force fresh calls.

`--stub` replaces the model with hand-written fixtures in `fabric/stub/fixtures/`. Two of them start
deliberately broken so the repair loop runs. Stub output is labelled STUB in the audit trail, the registry, the
console and the report. Do not present a stub run as agent output.

## Demonstrating drift and self-repair

```bash
python -m mock_systems.drift lab   # vendor upgrade renames LAB_RSLT.RSLT_VAL: the adapter crashes (loud)
python -m mock_systems.drift ehr   # API renames dob to dateOfBirth: still valid FHIR, birth dates gone (silent)
python -m fabric check             # detects both, or press "Check estate now" in the console
python -m fabric check --heal      # re-integrates the drifted systems (needs a real model)
python -m mock_systems.drift reset # put everything back
```

The silent case is the one that matters. The contract still passes, so only the structure fingerprint and the
data check reveal it.

## Fault tolerance in the gateway

The gateway is a scatter-gather service over independent systems that fail independently. It is built so that
one slow or broken source cannot take down or stall the unified API, and so that a partial answer says so:

- **Parallel fan-out with per-source deadlines.** Sources are queried concurrently; each has a 10 s timeout.
- **Retries with exponential backoff.** Adapters are read-only, so a failed call is safely retried once.
- **Circuit breaker per source.** After 3 consecutive failures the gateway stops calling that source for 30 s,
  then lets one probe through (closed, open, half-open).
- **Stale-on-error cache.** Fresh copies are served for 30 s. If a source fails, a copy up to 5 minutes old is
  served and marked as stale.
- **Request coalescing.** Concurrent requests for the same data share one source call.
- **Explicit partial results.** A degraded answer carries a FHIR `OperationOutcome` (search mode `outcome`)
  naming each missing or stale source, plus the `X-Fabric-Partial` and `X-Fabric-Sources` headers. The console
  shows it as a banner.
- **Failover for patient demographics.** If the EHR is down, Patient records come from the lab and ADT systems,
  still one per MRN.
- **Observability.** Every request gets an `X-Request-ID` and one structured JSON log line with latency and
  per-source state; `/systems` shows each source's circuit state.

Try it: run the gateway, stop the mock EHR (`pkill -f mock_systems.ehr_rest`), and open a patient record.
Tests: `tests/test_gateway_resilience.py`.

## Measuring the hand-built baseline

The challenge asks for time and consistency gained against hand-built integrations. Do not assume the
hand-built figure; measure it. A team member writes one adapter by hand, without AI help, against exactly the
same contract:

```bash
python -m fabric handbuilt start pharmacy-csv --dev "Your name"   # starts the clock, writes BRIEF.md
#   edit baseline/pharmacy-csv/adapter.py
python -m fabric handbuilt check pharmacy-csv                     # repeat until it passes
python -m fabric report                                           # adds the side-by-side comparison
```

The report compares time to a validated adapter, validation runs needed, checks failed on the first run and
value accuracy. If you did not work in one sitting, pass `--minutes` with your active time; the report says
which kind of timing was used.

## Alerts and CI/CD

**Alerts** (`fabric/alerts.py`, console page *Alerts*). The fabric tells a person when something changes:

| Alert | Severity | Raised when | Clears when |
|---|---|---|---|
| circuit | critical | a source fails repeatedly and the gateway stops calling it | the source answers again |
| drift | warning | a source's structure no longer matches what was approved | the structure matches again |
| contract | critical | a live connector fails the 14-check contract | it passes again |

Each alert is raised once per incident and clears itself. The history is kept in `generated/alerts.jsonl`. Set
`FABRIC_ALERT_WEBHOOK` to a Slack or Microsoft Teams incoming-webhook URL to be notified there too.
`python -m fabric gateway --check-every 15` re-checks the whole estate every 15 minutes, so drift is caught
without anyone clicking *Check estate*.

**CI** (`.github/workflows/ci.yml`): every push and every pull request installs the project, builds the Synthea
estate and runs the full test suite, including the contract test of each promoted connector.

**CD** (`.github/workflows/cd.yml`, run manually): runs the tests, builds the container image (`Dockerfile`),
pushes it to GitHub Container Registry and, optionally, deploys it to Azure Container Apps (needs the
`AZURE_CREDENTIALS` secret and the `AZURE_RESOURCE_GROUP` / `AZURE_CONTAINERAPP` variables). Locally:
`docker build -t fabric . && docker run -p 8000:8000 fabric`.

## Synthea data

The four mock systems are seeded from **Synthea** (https://synthea.mitre.org): 40 synthetic patients
(seed 20261113, Massachusetts, reference date 2026-11-22). The loaded estate is committed as
`mock_systems/synthea_estate.json`, so it rebuilds with no Java and no 200 MB export:

```bash
python -m fabric seed                                         # the Synthea estate (default)
python -m fabric seed --synthea path/to/output/fhir           # from a fresh Synthea export
python -m fabric seed --synthea DIR --save-estate             # ...and make it the new default
```

The loader (`mock_systems/synthea.py`) writes each Synthea patient into all four legacy systems in their own
formats. It keeps disorders (not Synthea's social "findings") on the problem list, stores one unit per lab
test as the lab's reference table does, and writes postcodes as text ("MA 01450"), as the hospital systems store them.

The connectors were built and approved by the agent pipeline in run 20261113-175528, on an earlier estate from a
Python generator (since replaced by Synthea).
Re-seeded with Synthea, the same connectors, unchanged, pass the 14-check contract with no drift and
12,914 / 12,914 values correct against Synthea's answer key (`python -m fabric check`).

## Responsible AI safeguards

- **Synthetic data only**, and the pipeline still treats it as if it were real.
- **PHI guard on everything a model sees** (`fabric/privacy.py`): identifiers and high-cardinality values are
  replaced by keyed, shape-preserving pseudonyms, dates are shifted, and only low-cardinality value domains
  (codes, flags) pass through. Field statistics are computed locally and shared as aggregates.
  `tests/test_pipeline.py` asserts that no real MRN, family name, phone number, file path or credential appears
  in any prompt.
- **Scrubbed repair loop**: error messages are stripped of data values before they go back to the model.
- **Credentials** are injected at runtime. Agents see a placeholder, and an adapter containing a credential
  literal fails validation.
- **Generated code is not trusted**: static checks before execution, a separate process with a timeout,
  read-only access, and a human promotion gate.
- **Independent evidence**: correctness is measured against ground truth that the agents never see, so they
  cannot optimise for the test.
- **Human gates** after Discover, Design and Validate. A rejection sends the reviewer's feedback back to the
  agent. Automatic approvals are recorded as such.
- **Monitoring after go-live**: drift detection catches silent data loss that the contract alone would miss.
- **Audit trail** of every decision, with model name, prompt hash, token counts and latency.
- **Agents state their doubts**: open questions and review points are carried into the report.

## Limitations

- The four systems are mocks with 40 patients. Nothing here has run against a production system.
- The masking is a heuristic first line of defence, not a de-identification guarantee. A free-text field with
  few distinct values would pass through unmasked.
- The separate process is not a security boundary. A production fabric would run adapters in a container with
  no network route except to the source.
- Ground-truth evaluation exists only because the data is synthetic. In production its place is taken by a small
  set of golden records agreed with each system owner.
- Adapters do full reads on every call. There is no incremental sync, messaging layer or alerting channel yet.
- Timestamps without a timezone are treated as UTC, and this is flagged for human review in each design.
- Azure and .NET Core are the challenge's preferred stack. This prototype is Python, with the model reached
  through any OpenAI-compatible endpoint, including Azure AI Foundry.

## Layout

```
systems.yaml          the estate: where each system lives (all a human provides)
mock_systems/         Synthea loader + committed Synthea estate, builders for the four systems, mock EHR API, drift scenarios
fabric/
  probes.py           discovery tools (read-only) and the independent record counter
  privacy.py          PHI guard: masking, date shifting, error scrubbing
  agents.py           Discover, Design, Generate and repair prompts
  standards.py        pattern catalogue, FHIR conventions, adapter contract
  validate.py         the 14-check contract;  sandbox_runner.py runs an adapter in a child process
  evaluate.py         field-level comparison with ground truth (evidence, never shown to agents)
  operate.py          structure fingerprints, drift detection, health checks
  handbuilt.py        timed hand-built baseline against the same contract
  gates.py audit.py   human gates and the audit trail
  pipeline.py         orchestrator;  emit.py renders OpenAPI and a contract test per system
  gateway.py          unified FHIR API and console (console.html);  report.py builds the run report
  resilience.py       circuit breaker, request coalescing, TTL cache with stale fallback
  llm.py              OpenAI-compatible client with a replay cache;  stub/ is the no-model stand-in
generated/            promoted adapters, designs, profiles, OpenAPI, fingerprints and contract tests (created by a run)
baseline/             hand-built adapters and their timing logs (created by `handbuilt start`)
runs/                 one folder per run: artefacts, audit.jsonl, summary.json, report.html
tests/                pytest suite (runs without a model)
```

## AI tools used

Declared as the hackathon rules require.

- **Code:** written with Claude (Anthropic) as a coding assistant, directed and reviewed by the team
  (Tarun Teja Magendran and Vishal Krish).
- **Runtime agents:** the Discover, Design and Generate agents call Google Gemini (`gemini-3.6-flash`) through its
  OpenAI-compatible API. The model used for each run is recorded in `runs/<run_id>/summary.json` and at the top of
  its report. The connectors in `generated/` come from run `20261123-000225`, on the Synthea estate.
- **No AI at runtime:** the gateway, the upload validation and quarantine, drift detection and the 14-check contract
  are deterministic code. No model is called on the data path.
- **Baseline:** no hand-built baseline has been measured yet. The 40-hour figure in the reports is labelled as an
  assumption; `python -m fabric handbuilt` is the harness for measuring one.
- **Data:** Synthea synthetic patients only; no real or personal data.
- **Existing code:** none reused. Open-source dependencies are listed in `requirements.txt`.
