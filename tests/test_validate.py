"""The static half of the validation contract: unsafe code never runs."""
import pytest

from fabric.stub import FIXTURES
from fabric.validate import static_checks

SYSTEM = {"id": "lab-sql", "kind": "sqlite", "headers": {"X-API-Key": "s3cr3t-value"}}
GOOD = (FIXTURES / "lab-sql" / "adapter.py").read_text(encoding="utf-8")


def failed(code):
    return {c.id for c in static_checks(code, SYSTEM) if c.status == "fail"}


def test_reference_adapter_passes_every_static_check():
    assert failed(GOOD) == set()


@pytest.mark.parametrize("snippet, check", [
    ("import subprocess\n", "static.imports"),
    ("import socket\n", "static.imports"),
    ("from . import secrets\n", "static.imports"),
    ("eval('1')\n", "static.no_dangerous_calls"),
    ("import os\nos.system('ls')\n", "static.no_dangerous_calls"),
    ("open('out.txt', 'w')\n", "static.no_dangerous_calls"),
    ("from pathlib import Path\nPath('x').write_text('y')\n", "static.no_dangerous_calls"),
    ("SQL = 'DELETE FROM LAB_RSLT'\n", "static.read_only_sql"),
    ("SQL = 'update PT_MSTR set LNAME = 1'\n", "static.read_only_sql"),
    ("print('row')\n", "static.no_data_logging"),
    ("KEY = 's3cr3t-value'\n", "static.no_hardcoded_secrets"),
    ("api_key = 'abcd1234'\n", "static.no_hardcoded_secrets"),
])
def test_unsafe_code_is_rejected(snippet, check):
    assert check in failed(GOOD + "\n" + snippet)


def test_interface_is_enforced():
    assert "contract.interface" in failed(GOOD.replace('SYSTEM_ID = "lab-sql"', 'SYSTEM_ID = "other"'))
    assert "contract.interface" in failed(GOOD.replace("def fetch(resource_type, config):", "def fetch(kind):"))
    assert "contract.interface" in failed(GOOD.replace('["Patient", "Observation"]', '["Patient", "Invoice"]'))


def test_syntax_error_is_reported_not_raised():
    assert failed("def fetch(:\n") == {"static.syntax"}
