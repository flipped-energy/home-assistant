from datetime import UTC, datetime

import pytest

from custom_components.flipped_energy.signals.model import GroupFault
from custom_components.flipped_energy.signals.timeprim import (
    ZoneUnsupported,
    format_instant,
    instant_from_datetime,
    local_occurrences,
    local_to_instant,
    next_date,
    parse_instant,
    to_local,
    zone_for,
)

SYDNEY = zone_for("Australia/Sydney")
BRISBANE = zone_for("Australia/Brisbane")


def at(text: str) -> int:
    return parse_instant(text)


@pytest.mark.parametrize(
    ("instant", "zone_name", "wall", "minute", "offset"),
    [
        ("2026-10-01T02:30:00Z", "Australia/Sydney", "2026-10-01T12:30:00", 750, 36000),
        ("2026-10-03T15:59:00Z", "Australia/Sydney", "2026-10-04T01:59:00", 119, 36000),
        ("2026-10-03T16:00:00Z", "Australia/Sydney", "2026-10-04T03:00:00", 180, 39600),
        ("2027-04-03T15:30:00Z", "Australia/Sydney", "2027-04-04T02:30:00", 150, 39600),
        ("2027-04-03T16:30:00Z", "Australia/Sydney", "2027-04-04T02:30:00", 150, 36000),
        ("2026-10-01T03:00:00Z", "Australia/Adelaide", "2026-10-01T12:30:00", 750, 34200),
        ("2026-10-03T16:30:00Z", "Australia/Adelaide", "2026-10-04T03:00:00", 180, 37800),
        ("2026-10-03T16:00:00Z", "Australia/Brisbane", "2026-10-04T02:00:00", 120, 36000),
        ("2026-10-01T03:59:59.999Z", "Australia/Sydney", "2026-10-01T13:59:59", 839, 36000),
    ],
)
def test_to_local(instant: str, zone_name: str, wall: str, minute: int, offset: int) -> None:
    local = to_local(at(instant), zone_for(zone_name))
    assert local.wall == wall
    assert local.date == wall[:10]
    assert local.minute_of_day == minute
    assert local.offset_seconds == offset


def test_local_occurrences_normal() -> None:
    assert local_occurrences("2026-10-01T12:30:00", SYDNEY) == [at("2026-10-01T02:30:00Z")]


def test_local_occurrences_gap() -> None:
    assert local_occurrences("2026-10-04T02:30:00", SYDNEY) == []
    with pytest.raises(GroupFault) as raised:
        local_to_instant("2026-10-04T02:30:00", SYDNEY)
    assert raised.value.fault["code"] == "local_time_nonexistent"


def test_local_occurrences_overlap() -> None:
    assert local_occurrences("2027-04-04T02:30:00", SYDNEY) == [
        at("2027-04-03T15:30:00Z"),
        at("2027-04-03T16:30:00Z"),
    ]
    assert local_to_instant("2027-04-04T02:30:00", SYDNEY) == at("2027-04-03T15:30:00Z")


def test_local_occurrences_ignores_fraction() -> None:
    assert local_occurrences("2026-10-01T12:30:00.9999999", SYDNEY) == [at("2026-10-01T02:30:00Z")]


def test_local_occurrences_without_daylight_saving() -> None:
    assert local_occurrences("2027-04-04T02:30:00", BRISBANE) == [at("2027-04-03T16:30:00Z")]


@pytest.mark.parametrize(
    ("date", "following"),
    [
        ("2026-10-01", "2026-10-02"),
        ("2026-09-30", "2026-10-01"),
        ("2026-12-31", "2027-01-01"),
        ("2027-02-28", "2027-03-01"),
        ("2028-02-28", "2028-02-29"),
        ("2028-02-29", "2028-03-01"),
        ("2100-02-28", "2100-03-01"),
        ("2000-02-28", "2000-02-29"),
        ("9999-12-31", "10000-01-01"),
        ("0001-01-01", "0001-01-02"),
    ],
)
def test_next_date(date: str, following: str) -> None:
    assert next_date(date) == following


@pytest.mark.parametrize("name", ["Not/AZone", "Australia", "", "../etc/passwd", "/etc/localtime"])
def test_unknown_zone(name: str) -> None:
    with pytest.raises(ZoneUnsupported):
        zone_for(name)


def test_format_instant_truncates_to_whole_second() -> None:
    assert format_instant(at("2026-10-15T02:35:00.999Z")) == "2026-10-15T02:35:00Z"
    assert format_instant(at("2026-10-01T12:25:00+10:00")) == "2026-10-01T02:25:00Z"


def test_instant_from_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError):
        instant_from_datetime(datetime.fromisoformat("2026-10-01T02:30:00"))
    assert instant_from_datetime(datetime(2026, 10, 1, 2, 30, tzinfo=UTC)) == at(
        "2026-10-01T02:30:00Z"
    )
