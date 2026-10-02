import pytest

from custom_components.flipped_energy.hourly import (
    Hour,
    Statistic,
    cost_unknown_hours,
    feed_in_unknown_hours,
    first_hour,
    hourly,
    startable,
)
from custom_components.flipped_energy.signals.model import UsageEntry
from custom_components.flipped_energy.signals.timeprim import (
    Instant,
    format_instant,
    local_to_instant,
    parse_instant,
    zone_for,
)


def interval(
    start: str,
    *,
    minutes: int = 30,
    grid: float = 0.0,
    controlled: float = 0.0,
    export: float = 0.0,
    cost: float | None = 0.0,
    feed_in: float | None = None,
) -> UsageEntry:
    return {
        "local": "",
        "start": start,
        "durationMinutes": minutes,
        "gridImportKwh": grid,
        "controlledLoadKwh": controlled,
        "solarExportKwh": export,
        "costAud": cost,
        "feedInCreditAud": feed_in,
    }


def window_start(date: str, zone: str) -> Instant:
    return local_to_instant(f"{date}T00:00:00", zone_for(zone))


def by_text(hours: dict[Instant, Hour]) -> dict[str, Hour]:
    return {format_instant(start): hour for start, hour in hours.items()}


def test_sydney_half_hours_fill_one_utc_hour() -> None:
    first = first_hour(window_start("2026-09-24", "Australia/Sydney"))
    assert format_instant(first) == "2026-09-23T14:00:00Z"
    hours = by_text(
        hourly(
            [
                interval("2026-09-23T14:00:00Z", grid=0.25, cost=0.08),
                interval("2026-09-23T14:30:00Z", grid=0.5, cost=0.16),
                interval("2026-09-23T15:00:00Z", grid=1.0, cost=0.3),
            ],
            first,
        )
    )
    assert list(hours) == ["2026-09-23T14:00:00Z", "2026-09-23T15:00:00Z"]
    assert hours["2026-09-23T14:00:00Z"].grid_import_kwh == 0.75
    assert hours["2026-09-23T14:00:00Z"].value(Statistic.USAGE_COST) == pytest.approx(0.24)
    assert hours["2026-09-23T15:00:00Z"].grid_import_kwh == 1.0


def test_brisbane_half_hours() -> None:
    first = first_hour(window_start("2026-01-10", "Australia/Brisbane"))
    assert format_instant(first) == "2026-01-09T14:00:00Z"
    hours = by_text(
        hourly(
            [
                interval("2026-01-09T14:00:00Z", controlled=0.4, cost=0.1),
                interval("2026-01-09T14:30:00Z", controlled=0.6, cost=0.2),
            ],
            first,
        )
    )
    assert list(hours) == ["2026-01-09T14:00:00Z"]
    assert hours["2026-01-09T14:00:00Z"].controlled_load_kwh == 1.0


def test_adelaide_alignment_skips_the_first_half_hour() -> None:
    start = window_start("2026-06-10", "Australia/Adelaide")
    assert format_instant(start) == "2026-06-09T14:30:00Z"
    first = first_hour(start)
    assert format_instant(first) == "2026-06-09T15:00:00Z"
    hours = by_text(
        hourly(
            [
                interval("2026-06-09T14:30:00Z", grid=9.0, cost=9.0),
                interval("2026-06-09T15:00:00Z", grid=0.5, cost=0.1),
                interval("2026-06-09T15:30:00Z", grid=0.25, cost=0.05),
            ],
            first,
        )
    )
    assert list(hours) == ["2026-06-09T15:00:00Z"]
    assert hours["2026-06-09T15:00:00Z"].grid_import_kwh == 0.75


def test_first_hour_on_the_hour_is_itself() -> None:
    start = parse_instant("2026-09-23T14:00:00Z")
    assert first_hour(start) == start


def test_merged_interval_sydney_spreads_over_two_hours() -> None:
    first = parse_instant("2027-04-03T13:00:00Z")
    hours = by_text(
        hourly([interval("2027-04-03T15:00:00Z", minutes=120, grid=0.9, cost=0.2)], first)
    )
    assert list(hours) == ["2027-04-03T15:00:00Z", "2027-04-03T16:00:00Z"]
    assert hours["2027-04-03T15:00:00Z"].grid_import_kwh == 0.45
    assert hours["2027-04-03T16:00:00Z"].grid_import_kwh == 0.45
    assert hours["2027-04-03T15:00:00Z"].cost_aud == 0.1


def test_merged_interval_adelaide_spreads_over_three_hours() -> None:
    first = parse_instant("2027-04-03T14:00:00Z")
    hours = by_text(
        hourly(
            [interval("2027-04-03T15:30:00Z", minutes=120, grid=2.0, export=1.0, feed_in=0.4)],
            first,
        )
    )
    assert list(hours) == [
        "2027-04-03T15:00:00Z",
        "2027-04-03T16:00:00Z",
        "2027-04-03T17:00:00Z",
    ]
    assert [hour.grid_import_kwh for hour in hours.values()] == [0.5, 1.0, 0.5]
    assert [hour.solar_export_kwh for hour in hours.values()] == [0.25, 0.5, 0.25]
    assert [hour.feed_in_aud for hour in hours.values()] == [0.1, 0.2, 0.1]


def test_cost_null_with_energy_is_unknown() -> None:
    first = parse_instant("2026-09-23T14:00:00Z")
    hours = hourly([interval("2026-09-23T14:00:00Z", grid=0.3, cost=None)], first)
    hour = by_text(hours)["2026-09-23T14:00:00Z"]
    assert hour.cost_unknown
    assert hour.value(Statistic.USAGE_COST) is None
    assert cost_unknown_hours(hours) == ["2026-09-23T14:00:00Z"]


def test_cost_null_with_no_energy_is_not_unknown() -> None:
    first = parse_instant("2026-09-23T14:00:00Z")
    hours = hourly([interval("2026-09-23T14:00:00Z", export=0.2, cost=None, feed_in=0.02)], first)
    hour = by_text(hours)["2026-09-23T14:00:00Z"]
    assert not hour.cost_unknown
    assert hour.value(Statistic.USAGE_COST) == 0.0
    assert cost_unknown_hours(hours) == []


def test_feed_in_null_with_no_export_is_not_unknown() -> None:
    first = parse_instant("2026-09-23T14:00:00Z")
    hours = hourly([interval("2026-09-23T14:00:00Z", grid=0.3, cost=0.1, feed_in=None)], first)
    hour = by_text(hours)["2026-09-23T14:00:00Z"]
    assert not hour.feed_in_unknown
    assert hour.value(Statistic.FEED_IN_CREDIT) == 0.0
    assert feed_in_unknown_hours(hours) == []


def test_feed_in_null_with_export_is_unknown() -> None:
    first = parse_instant("2026-09-23T14:00:00Z")
    hours = hourly([interval("2026-09-23T14:30:00Z", export=0.4, feed_in=None)], first)
    hour = by_text(hours)["2026-09-23T14:00:00Z"]
    assert hour.feed_in_unknown
    assert hour.value(Statistic.FEED_IN_CREDIT) is None
    assert feed_in_unknown_hours(hours) == ["2026-09-23T14:00:00Z"]


def test_one_known_and_one_unknown_half_hour_make_the_hour_unknown() -> None:
    first = parse_instant("2026-09-23T14:00:00Z")
    hours = hourly(
        [
            interval("2026-09-23T14:00:00Z", grid=0.3, cost=0.1),
            interval("2026-09-23T14:30:00Z", grid=0.3, cost=None),
        ],
        first,
    )
    hour = by_text(hours)["2026-09-23T14:00:00Z"]
    assert hour.cost_unknown
    assert hour.value(Statistic.USAGE_COST) is None
    assert hour.value(Statistic.GRID_IMPORT) == 0.6


def test_startable_statistics() -> None:
    first = parse_instant("2026-09-23T14:00:00Z")
    assert startable(hourly([interval("2026-09-23T14:00:00Z", grid=0.3, cost=0.1)], first)) == {
        Statistic.GRID_IMPORT,
        Statistic.USAGE_COST,
    }
    assert startable(hourly([interval("2026-09-23T14:00:00Z", grid=0.3, cost=None)], first)) == {
        Statistic.GRID_IMPORT
    }
    assert startable(
        hourly([interval("2026-09-23T14:00:00Z", export=0.5, cost=None, feed_in=0.03)], first)
    ) == {Statistic.SOLAR_EXPORT, Statistic.FEED_IN_CREDIT}
    assert startable(
        hourly([interval("2026-09-23T14:00:00Z", export=0.5, cost=None, feed_in=None)], first)
    ) == {Statistic.SOLAR_EXPORT}
    assert startable(
        hourly([interval("2026-09-23T14:00:00Z", controlled=0.7, cost=0.2)], first)
    ) == {Statistic.CONTROLLED_LOAD, Statistic.USAGE_COST}
    assert startable(hourly([interval("2026-09-23T14:00:00Z", cost=0.0)], first)) == set()
    assert startable(hourly([interval("2026-09-23T13:30:00Z", grid=1.0, cost=1.0)], first)) == set()
