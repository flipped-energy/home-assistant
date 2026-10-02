import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN
from .hourly import (
    Hour,
    ImportReport,
    Statistic,
    cost_unknown_hours,
    feed_in_unknown_hours,
    first_hour,
    hourly,
    startable,
)
from .signals.model import EnergySignals
from .signals.timeprim import Instant, datetime_from_instant, format_instant, instant_from_datetime

_LOGGER = logging.getLogger(__package__)

CURRENCY: Final = "AUD"


@dataclass(frozen=True, slots=True)
class StatisticSpec:
    statistic: Statistic
    name: str
    unit: str
    unit_class: str | None


SPECS: Final = (
    StatisticSpec(
        Statistic.GRID_IMPORT,
        "Grid Import",
        UnitOfEnergy.KILO_WATT_HOUR,
        EnergyConverter.UNIT_CLASS,
    ),
    StatisticSpec(
        Statistic.SOLAR_EXPORT,
        "Solar Export",
        UnitOfEnergy.KILO_WATT_HOUR,
        EnergyConverter.UNIT_CLASS,
    ),
    StatisticSpec(
        Statistic.CONTROLLED_LOAD,
        "Controlled Load",
        UnitOfEnergy.KILO_WATT_HOUR,
        EnergyConverter.UNIT_CLASS,
    ),
    StatisticSpec(Statistic.USAGE_COST, "Usage Cost", CURRENCY, None),
    StatisticSpec(Statistic.FEED_IN_CREDIT, "Feed-in Credit", CURRENCY, None),
)


def statistic_slug(instance_key: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", instance_key.lower())


def row_instant(row: StatisticsRow) -> Instant:
    return instant_from_datetime(datetime.fromtimestamp(row["start"], UTC))


def row_sum(row: StatisticsRow, statistic_id: str) -> float:
    value = row.get("sum")
    if value is None:
        raise ValueError(f"{statistic_id} has a row without sum: {row}")
    return value


def build_rows(
    statistic: Statistic,
    hours: dict[Instant, Hour],
    stored: dict[Instant, StatisticsRow],
    anchor: float,
) -> list[StatisticData]:
    running = anchor
    rows: list[StatisticData] = []
    for start in sorted(hours.keys() | stored.keys()):
        when = datetime_from_instant(start)
        hour = hours.get(start)
        if hour is not None:
            value = hour.value(statistic)
            if value is not None:
                running += value
                rows.append({"start": when, "state": value, "sum": running})
            elif start in stored:
                rows.append({"start": when, "sum": running})
            continue
        state = stored[start].get("state")
        if state is None:
            rows.append({"start": when, "sum": running})
        else:
            running += state
            rows.append({"start": when, "state": state, "sum": running})
    return rows


class StatisticsImporter:
    def __init__(self, hass: HomeAssistant, instance_key: str, device_name: str) -> None:
        self._hass = hass
        self._slug = statistic_slug(instance_key)
        self._device_name = device_name
        self._lock = asyncio.Lock()

    def statistic_id(self, statistic: Statistic) -> str:
        return f"{DOMAIN}:{self._slug}_{statistic.value}"

    def metadata(self, spec: StatisticSpec) -> StatisticMetaData:
        return {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": f"{self._device_name} {spec.name}",
            "source": DOMAIN,
            "statistic_id": self.statistic_id(spec.statistic),
            "unit_class": spec.unit_class,
            "unit_of_measurement": spec.unit,
        }

    async def async_import(
        self, energy: EnergySignals, window_start: Instant
    ) -> ImportReport | None:
        if energy["status"] != "ok":
            return None
        intervals = energy["intervals"]
        if intervals is None:
            raise ValueError("energy group is ok without intervals")
        first = first_hour(window_start)
        async with self._lock:
            return await self._import(hourly(intervals, first), first)

    async def _import(self, hours: dict[Instant, Hour], first: Instant) -> ImportReport:
        starts = startable(hours)
        recorder = get_instance(self._hass)
        await recorder.async_block_till_done()
        existing: list[str] = []
        stored_hours: set[Instant] = set()
        for spec in SPECS:
            statistic_id = self.statistic_id(spec.statistic)
            last = await recorder.async_add_executor_job(
                get_last_statistics, self._hass, 1, statistic_id, False, {"sum"}
            )
            older = last.get(statistic_id, [])
            if not older and spec.statistic not in starts:
                continue
            existing.append(spec.statistic.value)
            window = await recorder.async_add_executor_job(
                statistics_during_period,
                self._hass,
                datetime_from_instant(first),
                None,
                {statistic_id},
                "hour",
                None,
                {"state", "sum"},
            )
            in_window = window.get(statistic_id, [])
            stored = {row_instant(row): row for row in in_window}
            stored_hours.update(stored)
            if not hours:
                continue
            if in_window:
                head = in_window[0]
                anchor = row_sum(head, statistic_id) - (head.get("state") or 0)
            elif older:
                anchor = row_sum(older[0], statistic_id)
            else:
                anchor = 0.0
            rows = build_rows(spec.statistic, hours, stored, anchor)
            if rows:
                async_add_external_statistics(self._hass, self.metadata(spec), rows)
        report = ImportReport(
            cost_unknown_hours=cost_unknown_hours(hours),
            feed_in_unknown_hours=feed_in_unknown_hours(hours),
            missing_hours=[format_instant(start) for start in sorted(stored_hours - hours.keys())],
            statistics=existing,
        )
        if report.has_gaps:
            _LOGGER.error(
                "%s usage history import: cost_unknown_hours %s; "
                "feed_in_unknown_hours %s; missing_hours %s",
                self._device_name,
                report.cost_unknown_hours,
                report.feed_in_unknown_hours,
                report.missing_hours,
            )
        return report
