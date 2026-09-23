"""Shared fixtures, built from the 63 real EPD records."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SEED = ROOT / "seed" / "epds.json"


@pytest.fixture(scope="session")
def all_records() -> list[dict]:
    return json.loads(SEED.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def records_by_id(all_records) -> dict[int, dict]:
    return {r["product_id"]: r for r in all_records}


@pytest.fixture
def record(records_by_id) -> dict:
    """Product 6, SmartRoof Base: full GWP breakdown, five materials, a ratio."""
    import copy

    return copy.deepcopy(records_by_id[6])
