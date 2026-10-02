from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .signals.model import UsageEntry
from .signals.timeprim import MICROS_PER_MINUTE, Instant, format_instant, parse_instant

MICROS_PER_HOUR: Final = 60 * MICROS_PER_MINUTE


class Statistic(StrEnum):
    GRID_IMPORT = "grid_import"
    SOLAR_EXPORT = "solar_export"
    CONTROLLED_LOAD = "controlled_load"
    USAGE_COST = "usage_cost"
    FEED_IN_CREDIT = "feed_in_credit"


@dataclass(slots=True)
class Hour:
    grid_import_kwh: float = 0.0
    solar_export_kwh: float = 0.0
    controlled_load_kwh: float = 0.0
    cost_aud: float = 0.0
    cost_unknown: bool = False
    cost_with_energy: bool = False
    feed_in_aud: float = 0.0
    feed_in_unknown: bool = False
    feed_in_with_export: bool = False

    def value(self, statistic: Statistic) -> float | None:
        match statistic:
            case Statistic.GRID_IMPORT:
                return self.grid_import_kwh
            case Statistic.SOLAR_EXPORT:
                return self.solar_export_kwh
            case Statistic.CONTROLLED_LOAD:
                return self.controlled_load_kwh
            case Statistic.USAGE_COST:
                return None if self.cost_unknown else self.cost_aud
            case Statistic.FEED_IN_CREDIT:
                return None if self.feed_in_unknown else self.feed_in_aud


@dataclass(frozen=True, slots=True)
class ImportReport:
    cost_unknown_hours: list[str]
    feed_in_unknown_hours: list[str]
    missing_hours: list[str]
    statistics: list[str]

    @property
    def has_gaps(self) -> bool:
        return bool(self.cost_unknown_hours or self.feed_in_unknown_hours or self.missing_hours)


def first_hour(window_start: Instant) -> Instant:
    return -(-window_start // MICROS_PER_HOUR) * MICROS_PER_HOUR


def add_interval(hours: dict[Instant, Hour], interval: UsageEntry, first: Instant) -> None:
    start = parse_instant(interval["start"])
    duration = interval["durationMinutes"] * MICROS_PER_MINUTE
    end = start + duration
    energy = interval["gridImportKwh"] + interval["controlledLoadKwh"]
    export = interval["solarExportKwh"]
    cost = interval["costAud"]
    feed_in = interval["feedInCreditAud"]
    hour_start = start - start % MICROS_PER_HOUR
    while hour_start < end:
        if hour_start >= first:
            overlap = min(end, hour_start + MICROS_PER_HOUR) - max(start, hour_start)
            share = overlap / duration
            hour = hours.setdefault(hour_start, Hour())
            hour.grid_import_kwh += interval["gridImportKwh"] * share
            hour.solar_export_kwh += export * share
            hour.controlled_load_kwh += interval["controlledLoadKwh"] * share
            if cost is not None:
                hour.cost_aud += cost * share
                if energy > 0:
                    hour.cost_with_energy = True
            elif energy > 0:
                hour.cost_unknown = True
            if feed_in is not None:
                hour.feed_in_aud += feed_in * share
                if export > 0:
                    hour.feed_in_with_export = True
            elif export > 0:
                hour.feed_in_unknown = True
        hour_start += MICROS_PER_HOUR


def hourly(intervals: list[UsageEntry], first: Instant) -> dict[Instant, Hour]:
    hours: dict[Instant, Hour] = {}
    for interval in intervals:
        add_interval(hours, interval, first)
    return dict(sorted(hours.items()))


def startable(hours: dict[Instant, Hour]) -> set[Statistic]:
    found: set[Statistic] = set()
    for hour in hours.values():
        if hour.grid_import_kwh > 0:
            found.add(Statistic.GRID_IMPORT)
        if hour.solar_export_kwh > 0:
            found.add(Statistic.SOLAR_EXPORT)
        if hour.controlled_load_kwh > 0:
            found.add(Statistic.CONTROLLED_LOAD)
        if hour.cost_with_energy:
            found.add(Statistic.USAGE_COST)
        if hour.feed_in_with_export:
            found.add(Statistic.FEED_IN_CREDIT)
    return found


def cost_unknown_hours(hours: dict[Instant, Hour]) -> list[str]:
    return [format_instant(start) for start, hour in hours.items() if hour.cost_unknown]


def feed_in_unknown_hours(hours: dict[Instant, Hour]) -> list[str]:
    return [format_instant(start) for start, hour in hours.items() if hour.feed_in_unknown]
