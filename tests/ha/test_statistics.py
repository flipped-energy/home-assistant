import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.energy import data as energy_data
from homeassistant.components.energy import validate as energy_validate
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.hourly import MICROS_PER_HOUR, ImportReport, Statistic
from custom_components.flipped_energy.signals.model import EnergySignals, UsageEntry
from custom_components.flipped_energy.signals.timeprim import (
    MICROS_PER_DAY,
    Instant,
    format_instant,
    instant_from_datetime,
    parse_instant,
)
from custom_components.flipped_energy.statistics import StatisticsImporter

from ..unit.fakes import answer
from .conftest import (
    ACCOUNT_NUMBER,
    START,
    ApiFactory,
    make_entry,
    setup_entry,
    startup,
    vector_body,
)

WINDOW = parse_instant("2026-09-23T14:00:00Z")
NOW = "2026-10-01T02:29:00Z"
LOGGER = "custom_components.flipped_energy"
USAGE = "energy/usage-mapping.json"

type Row = tuple[str, float | None, float | None]
type Period = Literal["hour", "day"]


def at(hour: int, base: Instant = WINDOW) -> str:
    return format_instant(base + hour * MICROS_PER_HOUR)


def usage(
    hour: int,
    *,
    grid: float = 0.0,
    controlled: float = 0.0,
    export: float = 0.0,
    cost: float | None = 0.0,
    feed_in: float | None = None,
    base: Instant = WINDOW,
) -> UsageEntry:
    return {
        "local": "",
        "start": at(hour, base),
        "durationMinutes": 30,
        "gridImportKwh": grid,
        "controlledLoadKwh": controlled,
        "solarExportKwh": export,
        "costAud": cost,
        "feedInCreditAud": feed_in,
    }


def energy(intervals: list[UsageEntry]) -> EnergySignals:
    return {
        "status": "ok",
        "fault": None,
        "nmi": "4102000001",
        "intervals": intervals,
        "days": [],
        "latestIntervalEnd": None,
    }


def statistic_id(statistic: Statistic) -> str:
    return f"flipped_energy:{ACCOUNT_NUMBER}_{statistic.value}"


@pytest.fixture
async def importer(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> StatisticsImporter:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Australia/Sydney")
    return StatisticsImporter(hass, ACCOUNT_NUMBER, "Flipped Energy")


async def run(
    hass: HomeAssistant,
    importer: StatisticsImporter,
    intervals: list[UsageEntry],
    window: Instant = WINDOW,
) -> ImportReport:
    report = await importer.async_import(energy(intervals), window)
    await async_wait_recording_done(hass)
    assert report is not None
    return report


async def query(
    hass: HomeAssistant, statistic: Statistic, period: Period = "hour"
) -> list[dict[str, Any]]:
    sid = statistic_id(statistic)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 9, 1, tzinfo=UTC),
        None,
        {sid},
        period,
        None,
        {"state", "sum", "change"},
    )
    return [dict(row) for row in result.get(sid, [])]


def start_of(row: dict[str, Any]) -> str:
    return format_instant(instant_from_datetime(datetime.fromtimestamp(row["start"], UTC)))


async def rows(hass: HomeAssistant, statistic: Statistic) -> list[Row]:
    return [
        (start_of(row), row.get("state"), row.get("sum")) for row in await query(hass, statistic)
    ]


async def changes(
    hass: HomeAssistant, statistic: Statistic, period: Period = "hour"
) -> list[tuple[str, float | None]]:
    return [(start_of(row), row.get("change")) for row in await query(hass, statistic, period)]


async def exists(hass: HomeAssistant, statistic: Statistic) -> bool:
    sid = statistic_id(statistic)
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, sid, False, {"sum"}
    )
    return bool(last.get(sid))


def errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno >= logging.ERROR
    ]


def three(grid: Callable[[int], float], cost: Callable[[int], float | None]) -> list[UsageEntry]:
    return [usage(hour, grid=grid(hour), cost=cost(hour)) for hour in range(3)]


async def test_first_import_from_an_empty_database(
    hass: HomeAssistant, importer: StatisticsImporter, caplog: pytest.LogCaptureFixture
) -> None:
    report = await run(
        hass,
        importer,
        [
            usage(0, grid=1.0, cost=0.25),
            usage(1, grid=0.5, cost=0.125),
            usage(2, grid=2.0, cost=0.5),
        ],
    )
    assert await rows(hass, Statistic.GRID_IMPORT) == [
        (at(0), 1.0, 1.0),
        (at(1), 0.5, 1.5),
        (at(2), 2.0, 3.5),
    ]
    assert await rows(hass, Statistic.USAGE_COST) == [
        (at(0), 0.25, 0.25),
        (at(1), 0.125, 0.375),
        (at(2), 0.5, 0.875),
    ]
    for statistic in (Statistic.SOLAR_EXPORT, Statistic.CONTROLLED_LOAD, Statistic.FEED_IN_CREDIT):
        assert not await exists(hass, statistic)
    assert report == ImportReport([], [], [], ["grid_import", "usage_cost"])
    metadata = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_source="flipped_energy")
    )
    assert {
        sid: (meta["name"], meta["unit_of_measurement"], meta["unit_class"], meta["has_sum"])
        for sid, (_, meta) in metadata.items()
    } == {
        statistic_id(Statistic.GRID_IMPORT): ("Flipped Energy Grid Import", "kWh", "energy", True),
        statistic_id(Statistic.USAGE_COST): ("Flipped Energy Usage Cost", "AUD", None, True),
    }
    assert errors(caplog) == []


async def test_identical_reimport_leaves_identical_rows(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    intervals = three(lambda hour: 1.0 + hour, lambda hour: 0.25 * (hour + 1))
    await run(hass, importer, intervals)
    before = (await rows(hass, Statistic.GRID_IMPORT), await rows(hass, Statistic.USAGE_COST))
    await run(hass, importer, intervals)
    after = (await rows(hass, Statistic.GRID_IMPORT), await rows(hass, Statistic.USAGE_COST))
    assert after == before


async def test_revised_middle_hour_changes_it_and_every_later_sum(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, three(lambda hour: [1.0, 0.5, 2.0][hour], lambda hour: 0.25))
    await run(hass, importer, three(lambda hour: [1.0, 1.5, 2.0][hour], lambda hour: 0.25))
    assert await rows(hass, Statistic.GRID_IMPORT) == [
        (at(0), 1.0, 1.0),
        (at(1), 1.5, 2.5),
        (at(2), 2.0, 4.5),
    ]


async def test_late_data_for_an_earlier_hour(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, [usage(0, grid=1.0), usage(2, grid=2.0)])
    assert await rows(hass, Statistic.GRID_IMPORT) == [(at(0), 1.0, 1.0), (at(2), 2.0, 3.0)]
    await run(hass, importer, [usage(0, grid=1.0), usage(1, grid=0.5), usage(2, grid=2.0)])
    assert await rows(hass, Statistic.GRID_IMPORT) == [
        (at(0), 1.0, 1.0),
        (at(1), 0.5, 1.5),
        (at(2), 2.0, 3.5),
    ]


async def test_sliding_window_anchors_on_the_first_row_inside_it(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, [usage(hour, grid=1.0) for hour in range(4)])
    moved = WINDOW + 2 * MICROS_PER_HOUR
    await run(hass, importer, [usage(hour, grid=1.0) for hour in range(2, 6)], moved)
    assert await rows(hass, Statistic.GRID_IMPORT) == [
        (at(hour), 1.0, float(hour + 1)) for hour in range(6)
    ]


async def test_more_than_seven_days_offline_anchors_on_the_last_row(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, [usage(hour, grid=1.0) for hour in range(3)])
    later = WINDOW + 10 * MICROS_PER_DAY
    await run(hass, importer, [usage(hour, grid=1.0, base=later) for hour in range(2)], later)
    assert await rows(hass, Statistic.GRID_IMPORT) == [
        (at(0), 1.0, 1.0),
        (at(1), 1.0, 2.0),
        (at(2), 1.0, 3.0),
        (at(0, later), 1.0, 4.0),
        (at(1, later), 1.0, 5.0),
    ]


async def test_cost_unknown_hour_without_a_stored_row_gets_no_row(
    hass: HomeAssistant, importer: StatisticsImporter, caplog: pytest.LogCaptureFixture
) -> None:
    report = await run(
        hass, importer, three(lambda hour: 1.0, lambda hour: [0.25, None, 0.5][hour])
    )
    assert await rows(hass, Statistic.USAGE_COST) == [(at(0), 0.25, 0.25), (at(2), 0.5, 0.75)]
    assert await changes(hass, Statistic.USAGE_COST) == [(at(0), 0.25), (at(2), 0.5)]
    assert await changes(hass, Statistic.USAGE_COST, "day") == [(at(0), 0.75)]
    assert len(await rows(hass, Statistic.GRID_IMPORT)) == 3
    assert report.cost_unknown_hours == [at(1)]
    assert (report.feed_in_unknown_hours, report.missing_hours) == ([], [])
    assert errors(caplog) == [
        (
            f"Flipped Energy usage history import: cost_unknown_hours ['{at(1)}']; "
            "feed_in_unknown_hours []; missing_hours []"
        )
    ]


async def test_cost_withdrawn_for_an_hour_with_a_stored_amount(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, three(lambda hour: 1.0, lambda hour: [0.25, 0.125, 0.5][hour]))
    report = await run(
        hass, importer, three(lambda hour: 1.0, lambda hour: [0.25, None, 0.5][hour])
    )
    assert await rows(hass, Statistic.USAGE_COST) == [
        (at(0), 0.25, 0.25),
        (at(1), None, 0.25),
        (at(2), 0.5, 0.75),
    ]
    assert await changes(hass, Statistic.USAGE_COST) == [(at(0), 0.25), (at(1), 0.0), (at(2), 0.5)]
    assert report.cost_unknown_hours == [at(1)]


async def test_hour_missing_from_the_response_keeps_its_stored_state(
    hass: HomeAssistant, importer: StatisticsImporter, caplog: pytest.LogCaptureFixture
) -> None:
    await run(hass, importer, [usage(hour, grid=1.0) for hour in range(4)])
    report = await run(hass, importer, [usage(0, grid=2.0), usage(2, grid=1.0), usage(3, grid=1.0)])
    assert await rows(hass, Statistic.GRID_IMPORT) == [
        (at(0), 2.0, 2.0),
        (at(1), 1.0, 3.0),
        (at(2), 1.0, 4.0),
        (at(3), 1.0, 5.0),
    ]
    assert report.missing_hours == [at(1)]
    assert errors(caplog) == [
        (
            "Flipped Energy usage history import: cost_unknown_hours []; "
            f"feed_in_unknown_hours []; missing_hours ['{at(1)}']"
        )
    ]


async def test_empty_body_after_a_populated_import_changes_nothing(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, three(lambda hour: 1.0, lambda hour: 0.25))
    before = (await rows(hass, Statistic.GRID_IMPORT), await rows(hass, Statistic.USAGE_COST))
    report = await run(hass, importer, [])
    after = (await rows(hass, Statistic.GRID_IMPORT), await rows(hass, Statistic.USAGE_COST))
    assert after == before
    assert report == ImportReport([], [], [at(0), at(1), at(2)], ["grid_import", "usage_cost"])


async def test_site_without_solar_or_controlled_load(
    hass: HomeAssistant, importer: StatisticsImporter, caplog: pytest.LogCaptureFixture
) -> None:
    report = await run(
        hass,
        importer,
        [usage(hour, grid=0.5, cost=0.125, export=0.0, feed_in=None) for hour in range(3)],
    )
    assert [statistic for statistic in Statistic if await exists(hass, statistic)] == [
        Statistic.GRID_IMPORT,
        Statistic.USAGE_COST,
    ]
    assert report == ImportReport([], [], [], ["grid_import", "usage_cost"])
    assert errors(caplog) == []


async def test_exported_energy_with_a_null_feed_in_amount(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    report = await run(
        hass,
        importer,
        [usage(0, export=1.0, cost=None, feed_in=0.25), usage(1, export=0.5, cost=None)],
    )
    assert report.feed_in_unknown_hours == [at(1)]
    assert report.cost_unknown_hours == []
    assert await rows(hass, Statistic.SOLAR_EXPORT) == [(at(0), 1.0, 1.0), (at(1), 0.5, 1.5)]
    assert await rows(hass, Statistic.FEED_IN_CREDIT) == [(at(0), 0.25, 0.25)]


async def test_written_statistic_is_written_through_an_all_zero_window(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, [usage(0, grid=1.0, export=1.0, cost=0.25, feed_in=0.5)])
    later = WINDOW + MICROS_PER_DAY
    report = await run(hass, importer, [usage(0, base=later), usage(1, base=later)], later)
    assert report.statistics == ["grid_import", "solar_export", "usage_cost", "feed_in_credit"]
    assert await rows(hass, Statistic.SOLAR_EXPORT) == [
        (at(0), 1.0, 1.0),
        (at(0, later), 0.0, 1.0),
        (at(1, later), 0.0, 1.0),
    ]
    assert await rows(hass, Statistic.FEED_IN_CREDIT) == [
        (at(0), 0.5, 0.5),
        (at(0, later), 0.0, 0.5),
        (at(1, later), 0.0, 0.5),
    ]
    assert not await exists(hass, Statistic.CONTROLLED_LOAD)


async def test_faulted_energy_group_imports_nothing(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(hass, importer, three(lambda hour: 1.0, lambda hour: 0.25))
    before = await rows(hass, Statistic.GRID_IMPORT)
    faulted: EnergySignals = {
        **energy([usage(0, grid=5.0)]),
        "status": "faulted",
        "fault": {"code": "http_error", "httpStatus": 503, "body": "down", "bodyBytes": 4},
    }
    assert await importer.async_import(faulted, WINDOW) is None
    await async_wait_recording_done(hass)
    assert await rows(hass, Statistic.GRID_IMPORT) == before


async def test_entry_imports_after_the_usage_sync(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    await setup_entry(
        hass,
        apis,
        entry,
        startup(
            account=vector_body(USAGE, "account"),
            meters=vector_body(USAGE, "meters"),
            half_hourly=vector_body(USAGE, "usageHalfHourly"),
            daily=vector_body(USAGE, "usageDaily"),
        ),
    )
    await async_wait_recording_done(hass)
    assert await exists(hass, Statistic.GRID_IMPORT)
    report = entry.runtime_data.statistics
    assert report is not None
    status = hass.states.get("sensor.flipped_energy_usage_history_status")
    assert status is not None
    assert {
        key: status.attributes[key]
        for key in ("cost_unknown_hours", "feed_in_unknown_hours", "missing_hours")
    } == {
        "cost_unknown_hours": report.cost_unknown_hours,
        "feed_in_unknown_hours": report.feed_in_unknown_hours,
        "missing_hours": report.missing_hours,
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_entry_with_a_faulted_energy_group_imports_nothing(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    await setup_entry(
        hass,
        apis,
        entry,
        [
            *startup(account=vector_body(USAGE, "account"), meters=vector_body(USAGE, "meters"))[
                :4
            ],
            answer(Endpoint.USAGE_HALF_HOURLY, 503, "Service Unavailable"),
            answer(Endpoint.USAGE_DAILY, body=vector_body(USAGE, "usageDaily")),
        ],
    )
    await async_wait_recording_done(hass)
    assert entry.runtime_data.signals["energy"]["status"] == "faulted"
    assert entry.runtime_data.statistics is None
    for statistic in Statistic:
        assert not await exists(hass, statistic)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_energy_dashboard_validation_has_no_issue(
    hass: HomeAssistant, importer: StatisticsImporter
) -> None:
    await run(
        hass,
        importer,
        [
            usage(0, grid=1.0, controlled=0.5, cost=0.25),
            usage(1, export=1.0, cost=None, feed_in=0.125),
        ],
    )
    manager = await energy_data.async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": statistic_id(Statistic.GRID_IMPORT),
                    "stat_energy_to": statistic_id(Statistic.SOLAR_EXPORT),
                    "stat_cost": statistic_id(Statistic.USAGE_COST),
                    "stat_compensation": statistic_id(Statistic.FEED_IN_CREDIT),
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0,
                },
                {
                    "type": "grid",
                    "stat_energy_from": statistic_id(Statistic.CONTROLLED_LOAD),
                    "stat_energy_to": None,
                    "stat_cost": None,
                    "stat_compensation": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0,
                },
            ]
        }
    )
    result = await energy_validate.async_validate(hass)
    assert result.as_dict()["energy_sources"] == [[], []]
