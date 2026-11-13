"""The three LLM agents: Discover, Design and Generate (which also repairs).

Each agent is one focused prompt with a strict output format. Agents never touch
a source system: they read probe output (masked) and write artefacts that the
deterministic Validate stage and a human reviewer then check.
"""
from __future__ import annotations

import json

from .config import public_view
from .llm import extract_code, extract_json
from .standards import COUNT_BASIS_HELP, PATTERNS, RESOURCE_TYPES, adapter_contract, fhir_conventions

MASKING_NOTE = """How to read the discovery output:
- Structure (tables, endpoints, message types, columns, paths) and field statistics are exact. They were
  computed locally on the full data.
- Sample records are privacy-masked. Identifiers, names and other high-cardinality values are scrambled with
  their shape preserved, and dates are shifted. Never treat a sample value as real.
- Lists under "values" are the real, complete value domain of low-cardinality fields (codes and flags)."""

DISCOVER_SYSTEM = f"""You are the Discover agent of an integration fabric for a hospital group. A deterministic
probe has inspected one source system. From its output you write the system profile an integration engineer
would otherwise write by hand: what the system is, what business records it holds, what each field means,
how records relate, and what looks risky.

{MASKING_NOTE}

Reply with ONE JSON object and nothing else, in exactly this shape:
{{
  "system_id": "<id>",
  "summary": "<2-3 sentences: what this system is and what it holds>",
  "access": {{"mechanism": "rest_api | sql_database | hl7v2_message_files | delimited_file_export", "notes": "<auth, paging, read constraints>"}},
  "entities": [
    {{
      "name": "<business record, e.g. Patient, Lab result>",
      "source": "<table | endpoint path | message type(s) | file pattern>",
      "description": "<what one record is; how repeated or split records must be merged>",
      "primary_key": "<field that identifies one record>",
      "patient_key": "<field that carries the MRN or links to the patient, or null>",
      "fields": [{{"name": "<exact source name or path>", "format": "<format>", "meaning": "<plain English>",
                  "phi": true, "fhir_candidate": "<FHIR R4 element, e.g. Patient.birthDate, or null>"}}]
    }}
  ],
  "relationships": ["<A.x -> B.y>"],
  "cross_system_keys": [{{"field": "<name>", "meaning": "<what it shares with other systems>"}}],
  "code_systems": [{{"field": "<name>", "system": "<LOINC | SNOMED CT | RxNorm | local>", "value_map": {{"<code>": "<meaning>"}}}}],
  "data_quality_notes": ["<duplicates, missing values, ambiguous formats, timezone gaps>"],
  "open_questions": ["<what a human must confirm before go-live>"]
}}
List every field that matters for clinical or demographic meaning. Use exact source names."""

DESIGN_SYSTEM = """You are the Design agent of an integration fabric for a hospital group. Given the approved
profile of one source system, you choose the integration pattern and specify the mapping to FHIR R4. A
reviewer approves your design before any code is generated, so state your reasoning and your doubts.

Reply with ONE JSON object and nothing else, in exactly this shape:
{
  "system_id": "<id>",
  "pattern": "<one key from the pattern catalogue>",
  "pattern_rationale": "<why this pattern fits this system>",
  "alternatives_considered": [{"pattern": "<key>", "why_not": "<reason>"}],
  "sync": {"mode": "on_demand | scheduled_poll | event_driven", "detail": "<frequency or trigger>"},
  "resources": [
    {
      "resource_type": "<FHIR resource type>",
      "source_entity": "<entity from the profile>",
      "id_source_field": "<source field used to build the resource id>",
      "count_basis": {"entity": "...", "distinct_key": "..."},
      "field_map": [{"fhir": "<FHIR element>", "source": "<exact source field or path>", "transform": "<rule, including every code mapping>"}],
      "merge_rule": "<how repeated source records collapse into one resource, or 'none'>"
    }
  ],
  "risks": ["<what could go wrong in production>"],
  "human_review_points": ["<decisions a human should check>"]
}"""

GENERATE_SYSTEM = """You are the Generate agent of an integration fabric for a hospital group. You write the
adapter module for one source system from its approved design. Your code is checked automatically (static
safety checks, a sandboxed run, FHIR R4 schema validation, estate conventions and reconciliation of record
counts against the source) and then reviewed by a human before it serves any data.

Reply with the complete module in ONE ```python block and nothing else."""


class AgentError(RuntimeError):
    pass


def _ask_json(llm, system_prompt: str, user: str, tag: str, check) -> dict:
    """Ask for JSON; if the reply is unusable, tell the model why and ask once more."""
    problem = None
    for attempt in range(2):
        prompt = user if problem is None else f"{user}\n\nYour previous reply was rejected: {problem}\nReply again with the corrected JSON object only."
        reply = llm.complete(system=system_prompt, user=prompt, tag=f"{tag}:try{attempt + 1}" if attempt else tag, json_mode=True)
        try:
            data = extract_json(reply.text)
        except ValueError as exc:
            problem = f"it was not valid JSON ({exc})"
            continue
        problem = check(data)
        if problem is None:
            return data
    raise AgentError(f"{tag}: no usable answer after two attempts ({problem})")


def _feedback(feedback: str | None, previous: dict | None) -> str:
    if not feedback:
        return ""
    return (f"\n\nA human reviewer rejected your previous answer. Their feedback:\n{feedback}\n\n"
            f"Your previous answer:\n{json.dumps(previous, indent=1)}\n\nProduce a corrected answer.")


def discover(llm, system: dict, probe_out: dict, feedback: str | None = None, previous: dict | None = None) -> dict:
    user = (f"SYSTEM\n{json.dumps(public_view(system), indent=1)}\n\n"
            f"DISCOVERY OUTPUT\n{json.dumps(probe_out, indent=1, default=str)}{_feedback(feedback, previous)}")

    def check(p: dict) -> str | None:
        if not isinstance(p.get("entities"), list) or not p["entities"]:
            return "'entities' must be a non-empty list"
        for e in p["entities"]:
            if not isinstance(e, dict) or not e.get("name") or not isinstance(e.get("fields"), list):
                return "every entity needs 'name' and a 'fields' list"
        return None

    profile = _ask_json(llm, DISCOVER_SYSTEM, user, f"discover:{system['id']}", check)
    profile["system_id"] = system["id"]
    return profile


def design(llm, system: dict, profile: dict, mrn_system: str, feedback: str | None = None,
           previous: dict | None = None) -> dict:
    catalogue = "\n".join(f"- {k}: {v}" for k, v in PATTERNS.items())
    user = (f"SYSTEM\n{json.dumps(public_view(system), indent=1)}\n\n"
            f"PATTERN CATALOGUE (choose exactly one key)\n{catalogue}\n\n"
            f"TARGET RESOURCE TYPES\n{', '.join(RESOURCE_TYPES)}. Map only what this system actually holds. "
            f"A system that holds patient demographics publishes Patient as well as its clinical records.\n\n"
            f"{fhir_conventions(mrn_system)}\n\n"
            f"COUNT BASIS\nFor each resource, count_basis tells the Validate stage how to count, independently of "
            f"your code, the distinct source records that become one resource each. For this kind of system use: "
            f"{COUNT_BASIS_HELP[system['kind']]}\n\n"
            f"APPROVED PROFILE\n{json.dumps(profile, indent=1)}{_feedback(feedback, previous)}")

    def check(d: dict) -> str | None:
        if d.get("pattern") not in PATTERNS:
            return f"'pattern' must be one of {list(PATTERNS)}"
        if not isinstance(d.get("resources"), list) or not d["resources"]:
            return "'resources' must be a non-empty list"
        seen = set()
        for r in d["resources"]:
            if not isinstance(r, dict) or r.get("resource_type") not in RESOURCE_TYPES:
                return f"every resource_type must be one of {RESOURCE_TYPES}"
            if r["resource_type"] in seen:
                return f"resource_type {r['resource_type']} appears twice; use one entry per type"
            seen.add(r["resource_type"])
            basis = r.get("count_basis")
            if not isinstance(basis, dict) or not basis.get("distinct_key") or "entity" not in basis:
                return f"{r['resource_type']} needs count_basis with 'entity' and 'distinct_key'"
            if not isinstance(r.get("field_map"), list) or not r["field_map"]:
                return f"{r['resource_type']} needs a non-empty field_map"
        return None

    result = _ask_json(llm, DESIGN_SYSTEM, user, f"design:{system['id']}", check)
    result["system_id"] = system["id"]
    return result


def _generation_context(system: dict, profile: dict, design_doc: dict, probe_out: dict, mrn_system: str) -> str:
    return (f"{adapter_contract(system, mrn_system)}\n\n"
            f"SYSTEM\n{json.dumps(public_view(system), indent=1)}\n\n"
            f"APPROVED DESIGN\n{json.dumps(design_doc, indent=1)}\n\n"
            f"SYSTEM PROFILE\n{json.dumps(profile, indent=1)}\n\n"
            f"{MASKING_NOTE}\n\nDISCOVERY OUTPUT\n{json.dumps(probe_out, indent=1, default=str)}")


def generate(llm, system: dict, profile: dict, design_doc: dict, probe_out: dict, mrn_system: str) -> str:
    user = _generation_context(system, profile, design_doc, probe_out, mrn_system)
    return _ask_code(llm, user, f"generate:{system['id']}")


def repair(llm, system: dict, profile: dict, design_doc: dict, probe_out: dict, mrn_system: str,
           code: str, failures: list[dict], round_no: int) -> str:
    """Self-healing: feed the failed checks back. Error text has been scrubbed of data values."""
    failed = "\n".join(f"- [{f['id']}] {f['title']}: {f['detail']}" for f in failures)
    user = (f"The adapter below failed validation. Fix every failing check and return the complete corrected "
            f"module. Change only what the failures require.\n\nFAILING CHECKS\n{failed}\n\n"
            f"CURRENT MODULE\n```python\n{code}```\n\n"
            f"{_generation_context(system, profile, design_doc, probe_out, mrn_system)}")
    return _ask_code(llm, user, f"repair{round_no}:{system['id']}")


def _ask_code(llm, user: str, tag: str) -> str:
    problem = None
    for attempt in range(2):
        prompt = user if problem is None else f"{user}\n\nYour previous reply was rejected: {problem}"
        reply = llm.complete(system=GENERATE_SYSTEM, user=prompt, tag=f"{tag}:try{attempt + 1}" if attempt else tag)
        try:
            return extract_code(reply.text)
        except ValueError as exc:
            problem = str(exc)
    raise AgentError(f"{tag}: no usable module after two attempts ({problem})")
