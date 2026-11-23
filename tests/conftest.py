import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def cfg():
    """Config with the mock estate built and the mock EHR API running."""
    from fabric.__main__ import ensure_mocks
    from fabric.config import load_config

    if not (ROOT / "mock_systems" / "_data").exists():
        from mock_systems.build import build_all
        from mock_systems.synthea import load_estate

        build_all(load_estate())
    config = load_config()
    ensure_mocks(config)
    return config
