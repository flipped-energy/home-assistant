import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from custom_components.flipped_energy.signals import Config, Json, Snapshot, compute_signals

VECTORS = Path(__file__).resolve().parent.parent / "vectors"
INDEX = cast(list[dict[str, str]], json.loads((VECTORS / "index.json").read_text("utf-8")))
TOLERANCE = 1e-6
FAULT_COMPARED = ("httpStatus", "body", "bodyBytes")


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_fault(expected: dict[str, Json], actual: object, path: str, out: list[str]) -> None:
    if not isinstance(actual, dict):
        out.append(f"{path}: expected a fault {expected!r}, got {actual!r}")
        return
    if actual.get("code") != expected["code"]:
        out.append(f"{path}.code: expected {expected['code']!r}, got {actual.get('code')!r}")
    for key in FAULT_COMPARED:
        if key in expected and actual.get(key) != expected[key]:
            out.append(f"{path}.{key}: expected {expected[key]!r}, got {actual.get(key)!r}")


def compare(expected: Json, actual: object, path: str, out: list[str]) -> None:
    if path.endswith(".fault") and isinstance(expected, dict):
        compare_fault(expected, actual, path, out)
        return
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            out.append(f"{path}: expected an object, got {actual!r}")
            return
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                out.append(f"{path}.{key}: missing")
            elif key not in expected:
                out.append(f"{path}.{key}: unexpected {actual[key]!r}")
            else:
                compare(expected[key], actual[key], f"{path}.{key}", out)
        return
    if isinstance(expected, list):
        if not isinstance(actual, list):
            out.append(f"{path}: expected an array, got {actual!r}")
            return
        if len(expected) != len(actual):
            out.append(f"{path}: expected {len(expected)} elements, got {len(actual)}")
            return
        for index, (want, got) in enumerate(zip(expected, actual, strict=True)):
            compare(want, got, f"{path}[{index}]", out)
        return
    if is_number(expected):
        if not is_number(actual):
            out.append(f"{path}: expected {expected!r}, got {actual!r}")
            return
        if abs(cast(float, expected) - cast(float, actual)) > TOLERANCE:
            out.append(f"{path}: expected {expected!r}, got {actual!r}")
        return
    if type(expected) is not type(actual) or expected != actual:
        out.append(f"{path}: expected {expected!r}, got {actual!r}")


@pytest.mark.parametrize("entry", INDEX, ids=[entry["path"] for entry in INDEX])
def test_vector(entry: dict[str, str]) -> None:
    raw = (VECTORS / entry["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    vector = json.loads(raw)
    assert vector["name"] == Path(entry["path"]).stem
    given = vector["input"]
    instant = None if given["instant"] is None else datetime.fromisoformat(given["instant"])
    actual = compute_signals(
        instant,
        cast(Config, given["config"]),
        cast(Snapshot, given["account"]),
        cast(Snapshot, given["meters"]),
        cast(Snapshot, given["tokens"]),
        cast(Snapshot, given["outlook"]),
        cast(Snapshot, given["usageHalfHourly"]),
        cast(Snapshot, given["usageDaily"]),
    )
    mismatches: list[str] = []
    compare(vector["expected"], actual, "expected", mismatches)
    assert not mismatches, "\n".join(mismatches)


@pytest.mark.parametrize("assessment", ["nowAssessment", "nextHourPeak"])
def test_unknown_tier_is_invalid_response(assessment: str) -> None:
    vector = json.loads((VECTORS / "price" / "forecast-next-hour.json").read_text("utf-8"))
    given = vector["input"]
    given["outlook"]["body"][assessment]["tier"] = "Extreme"
    actual = compute_signals(
        datetime.fromisoformat(given["instant"]),
        cast(Config, given["config"]),
        cast(Snapshot, given["account"]),
        cast(Snapshot, given["meters"]),
        cast(Snapshot, given["tokens"]),
        cast(Snapshot, given["outlook"]),
        cast(Snapshot, given["usageHalfHourly"]),
        cast(Snapshot, given["usageDaily"]),
    )
    assert actual["price"]["status"] == "faulted"
    assert actual["price"]["fault"] == {
        "code": "invalid_response",
        "message": f"outlook.{assessment}.tier is not one of "
        "UnusuallyLow, Normal, Elevated, Spike: 'Extreme'",
    }
