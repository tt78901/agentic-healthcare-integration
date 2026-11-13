"""PHI guard: nothing an agent sees is a real source value unless it is a shared,
non-identifying category.

The data in this project is synthetic, but the pipeline is built as if it were
not. Before any sample reaches the LLM:

  * dates are shifted by a per-run offset (format preserved);
  * identifying fields (by name) and high-cardinality fields are replaced with
    a keyed, shape-preserving pseudonym (7381763 -> 4092518, Walsh -> Qkdpe);
  * only low-cardinality values (codes such as M/F, status flags, test names)
    pass through, because the agent needs the value domain to design a mapping.

Field statistics (counts, distinct counts, formats) are computed locally on the
real values and only the aggregates are shared.

This is a heuristic first line of defence, not a de-identification guarantee:
free-text fields with few distinct values would pass through unmasked.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets

PHI_NAME = re.compile(r"(mrn|ssn|pps|name|dob|birth|phone|tel|mobile|email|addr|street|line\d?$|city|zip|post"
                      r"|pt_?id|patient_?id)", re.I)
# HL7 v2: whole segments that describe a person, and clinician fields in PV1
HL7_PHI = re.compile(r"^(PID|NK1|GT1|IN1)\.|^PV1\.(7|8|9|17)(\.|$)")
HL7_SAFE = {"PID.1", "PID.8"}

_DATE_FORMATS = [
    ("date YYYY-MM-DD", re.compile(r"^\d{4}-\d{2}-\d{2}$"), "%Y-%m-%d"),
    ("datetime YYYY-MM-DDThh:mm:ss (no timezone)", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"), "%Y-%m-%dT%H:%M:%S"),
    ("datetime YYYY-MM-DDThh:mm:ssZ", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"), "%Y-%m-%dT%H:%M:%SZ"),
    ("datetime YYYYMMDDHHMMSS (no timezone)", re.compile(r"^(19|20)\d{12}$"), "%Y%m%d%H%M%S"),
    ("date YYYYMMDD", re.compile(r"^(19|20)\d{6}$"), "%Y%m%d"),
]
_SLASH = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def infer_format(values: list) -> tuple[str, str | None]:
    """Return (human-readable format hint, strptime pattern or None) for a field's values."""
    vals = [v for v in values if v is not None and v != ""]
    if not vals:
        return "always empty", None
    if all(isinstance(v, bool) for v in vals):
        return "boolean", None
    strs = [str(v) for v in vals]
    for label, rx, fmt in _DATE_FORMATS:
        if all(rx.match(s) for s in strs) and _parses(strs[:50], fmt):
            stored = " stored as integer" if all(isinstance(v, int) for v in vals) else ""
            return label + stored, fmt
    if all(_SLASH.match(s) for s in strs):
        first_gt12 = any(int(_SLASH.match(s).group(1)) > 12 for s in strs)
        second_gt12 = any(int(_SLASH.match(s).group(2)) > 12 for s in strs)
        if first_gt12 and not second_gt12:
            return "date DD/MM/YYYY", "%d/%m/%Y"
        if second_gt12 and not first_gt12:
            return "date MM/DD/YYYY", "%m/%d/%Y"
        return "date NN/NN/YYYY (day/month order cannot be determined from the data)", None
    if all(isinstance(v, int) for v in vals):
        return "integer", None
    if all(isinstance(v, (int, float)) for v in vals):
        return "decimal", None
    if all(re.fullmatch(r"-?\d+", s) for s in strs):
        return "integer stored as text", None
    if all(re.fullmatch(r"-?\d+(\.\d+)?", s) for s in strs):
        return "decimal stored as text", None
    return f"text (max length {max(len(s) for s in strs)})", None


def _parses(strs: list[str], fmt: str) -> bool:
    try:
        for s in strs:
            dt.datetime.strptime(s, fmt)
        return True
    except ValueError:
        return False


def field_stats(records: list[dict]) -> dict[str, dict]:
    """Aggregate statistics per field over flat records. Computed on real values, locally."""
    stats: dict[str, dict] = {}
    keys: list[str] = []
    for r in records:
        for k in r:
            if k not in stats:
                stats[k] = {}
                keys.append(k)
    n = len(records)
    for k in keys:
        vals = [r.get(k) for r in records if r.get(k) not in (None, "")]
        distinct = len({str(v) for v in vals})
        label, fmt = infer_format(vals)
        max_len = max((len(str(v)) for v in vals), default=0)
        is_date = label.startswith("date")
        constant = distinct == 1 and len(vals) >= 20  # same value in every record: not identifying
        identifying = (bool(PHI_NAME.search(k)) or (bool(HL7_PHI.search(k)) and k not in HL7_SAFE)) and not constant
        categorical = (not identifying and not is_date and not label.startswith("boolean")
                       and (distinct <= 3 or (len(vals) >= 20 and distinct / len(vals) <= 0.5 and max_len <= 120)))
        entry = {"present_in": len(vals), "of": n, "distinct": distinct, "format": label,
                 "_fmt": fmt, "_mask": not categorical and not label.startswith("boolean")}
        if categorical and distinct <= 15:
            entry["values"] = sorted({str(v) for v in vals})
        elif not categorical:
            entry["sample_values"] = "date-shifted" if is_date else "masked"
        stats[k] = entry
    return stats


def public_stats(stats: dict[str, dict]) -> dict[str, dict]:
    return {k: {a: b for a, b in v.items() if not a.startswith("_")} for k, v in stats.items()}


class Masker:
    def __init__(self, salt: str | None = None):
        self.salt = (salt or secrets.token_hex(16)).encode()
        self.shift = dt.timedelta(days=37 + int.from_bytes(self._h("shift")[:2], "big") % 300)

    def _h(self, text: str) -> bytes:
        return hmac.new(self.salt, text.encode("utf-8"), hashlib.sha256).digest()

    def pseudonym(self, value):
        """Keyed, shape-preserving replacement. Same input -> same output within a run."""
        s = str(value)
        stream = b""
        counter = 0
        while len(stream) < len(s):
            stream += self._h(f"{counter}:{s}")
            counter += 1
        out = []
        for i, ch in enumerate(s):
            b = stream[i]
            if ch.isdigit():
                out.append(str(1 + b % 9) if i == 0 else str(b % 10))
            elif ch.isalpha():
                out.append(chr((65 if ch.isupper() else 97) + b % 26))
            else:
                out.append(ch)
        masked = "".join(out)
        if isinstance(value, int) and not isinstance(value, bool):
            return int(masked)
        if isinstance(value, float):
            return float(masked)
        return masked

    def mask_value(self, value, stat: dict):
        if value in (None, "") or isinstance(value, bool):
            return value
        if stat.get("_fmt"):
            try:
                shifted = dt.datetime.strptime(str(value), stat["_fmt"]) - self.shift
                text = shifted.strftime(stat["_fmt"])
                return int(text) if isinstance(value, int) else text
            except ValueError:
                return self.pseudonym(value)
        if stat.get("format", "").startswith("date"):  # ambiguous slash dates
            return self.pseudonym(value)
        return self.pseudonym(value) if stat.get("_mask", True) else value

    def mask_flat(self, record: dict, stats: dict[str, dict]) -> dict:
        return {k: self.mask_value(v, stats.get(k, {})) for k, v in record.items()}


def flatten(obj, prefix: str = "") -> dict:
    """{'name': {'first': 'A'}} -> {'name.first': 'A'}. Lists of scalars are joined."""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            for x in obj[:1]:
                out.update(flatten(x, f"{prefix[:-1]}[]."))
        else:
            out[prefix[:-1]] = ", ".join(str(x) for x in obj) if obj else None
    else:
        out[prefix[:-1]] = obj
    return out


def unflatten_masked(obj, stats: dict, masker: Masker, prefix: str = ""):
    """Mask a nested JSON object in place of its original structure."""
    if isinstance(obj, dict):
        return {k: unflatten_masked(v, stats, masker, f"{prefix}{k}.") for k, v in obj.items()}
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return [unflatten_masked(x, stats, masker, f"{prefix[:-1]}[].") for x in obj[:2]]
        return [masker.pseudonym(x) for x in obj]
    return masker.mask_value(obj, stats.get(prefix[:-1], {}))


# ---------------------------------------------------------------- HL7 v2 helpers

def hl7_flatten(message: str) -> dict:
    """ADT message -> {'MSH.9.1': 'ADT', 'PID.5.1': 'WALSH', ...} (first repetition only)."""
    out: dict = {}
    for seg in filter(None, re.split(r"[\r\n]+", message)):
        fields = seg.split("|")
        name = fields[0]
        offset = 1 if name == "MSH" else 0  # MSH.1 is the field separator itself
        for idx, field in enumerate(fields[1:], start=1):
            if name == "MSH" and idx == 1:
                continue  # encoding characters
            if field == "":
                continue
            comps = field.split("~")[0].split("^")
            if len(comps) == 1:
                out[f"{name}.{idx + offset}"] = comps[0]
            else:
                for c_idx, comp in enumerate(comps, start=1):
                    if comp != "":
                        out[f"{name}.{idx + offset}.{c_idx}"] = comp
    return out


def hl7_mask(message: str, stats: dict, masker: Masker) -> str:
    segs = []
    for seg in filter(None, re.split(r"[\r\n]+", message)):
        fields = seg.split("|")
        name = fields[0]
        offset = 1 if name == "MSH" else 0
        for idx in range(1, len(fields)):
            if (name == "MSH" and idx == 1) or fields[idx] == "":
                continue
            comps = fields[idx].split("^")
            for c_idx in range(len(comps)):
                if comps[c_idx] == "":
                    continue
                path = f"{name}.{idx + offset}" if len(comps) == 1 else f"{name}.{idx + offset}.{c_idx + 1}"
                comps[c_idx] = str(masker.mask_value(comps[c_idx], stats.get(path, {})))
            fields[idx] = "^".join(comps)
        segs.append("|".join(fields))
    return "\n".join(segs)


# ------------------------------------------------- scrubbing errors before repair

def scrub_error(text: str, source_code: str = "") -> str:
    """Remove data values from an error message before it is sent to the LLM for
    repair. Quoted strings and long digit runs are kept only if they appear in the
    adapter's own source (field names do; patient data does not)."""
    def shape(s: str) -> str:
        return re.sub(r"[A-Za-z]", "x", re.sub(r"\d", "9", s))

    def quoted(m: re.Match) -> str:
        inner = m.group(2)
        return m.group(0) if inner and inner in source_code else f"{m.group(1)}{shape(inner)}{m.group(1)}"

    text = re.sub(r"(['\"])(.*?)\1", quoted, text)
    text = re.sub(r"\d{4,}", lambda m: m.group(0) if m.group(0) in source_code else "9" * len(m.group(0)), text)
    return text[:1500]
