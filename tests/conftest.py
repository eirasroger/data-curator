"""Shared test setup.

The seed fixtures load the real 63 extracted EPDs rather than hand-written
stand-ins. Rules that only ever meet invented data tend to be rules that only
work on invented data.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SEED = ROOT / "seed" / "epds.json"


@pytest.fixture(scope="session")
def all_records() -> list[dict]:
    return json.loads(SEED.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def records_by_id(all_records) -> dict[int, dict]:
    return {r["product_id"]: r for r in all_records}


@pytest.fixture
def record(records_by_id) -> dict:
    """Product 6, SmartRoof Base (Knauf Insulation).

    Chosen because it exercises most of the schema: a full GWP breakdown with a
    negative biogenic value, five materials, a conversion ratio, and an expiry
    date inside the next year.
    """
    import copy

    return copy.deepcopy(records_by_id[6])
