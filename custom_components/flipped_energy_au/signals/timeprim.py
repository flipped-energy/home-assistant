import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import fault_error

type Instant = int

MICROS_PER_SECOND = 1_000_000
MICROS_PER_MINUTE = 60 * MICROS_PER_SECOND
MICROS_PER_DAY = 86_400 * MICROS_PER_SECOND
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
WALL_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}", re.ASCII)
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


@dataclass(frozen=True, slots=True)
class LocalTime:
    wall: str
    date: str
    minute_of_day: int
    offset_seconds: int


class ZoneUnsupported(Exception):
    pass


def zone_for(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ZoneUnsupported(str(exc.args[0])) from exc
    except ValueError as exc:
        raise ZoneUnsupported(str(exc)) from exc


def instant_from_datetime(value: datetime) -> Instant:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime {value.isoformat()} is not an instant")
    return (value - EPOCH) // timedelta(microseconds=1)


def datetime_from_instant(instant: Instant) -> datetime:
    return EPOCH + timedelta(microseconds=instant)


def parse_instant(text: str) -> Instant:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError(f"{text} has no UTC offset")
    return instant_from_datetime(value)


def format_instant(instant: Instant) -> str:
    whole = instant - instant % MICROS_PER_SECOND
    value = datetime_from_instant(whole)
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}Z"
    )


def is_wall(text: str) -> bool:
    return WALL_PATTERN.match(text) is not None


def is_date(text: str) -> bool:
    return DATE_PATTERN.fullmatch(text) is not None


def to_local(instant: Instant, zone: tzinfo) -> LocalTime:
    local = datetime_from_instant(instant).astimezone(zone)
    offset = local.utcoffset()
    if offset is None:
        raise ValueError(f"zone {zone} gives no UTC offset")
    date = f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    wall = f"{date}T{local.hour:02d}:{local.minute:02d}:{local.second:02d}"
    return LocalTime(
        wall=wall,
        date=date,
        minute_of_day=local.hour * 60 + local.minute,
        offset_seconds=offset // timedelta(seconds=1),
    )


def wall_as_utc(wall: str) -> Instant:
    head = wall[:19]
    if not is_wall(head):
        raise ValueError(f"{wall} is not a wall-clock date-time")
    return instant_from_datetime(datetime.fromisoformat(head).replace(tzinfo=UTC))


def local_occurrences(wall: str, zone: tzinfo) -> list[Instant]:
    head = wall[:19]
    u = wall_as_utc(head)
    offsets = {
        to_local(u - MICROS_PER_DAY, zone).offset_seconds,
        to_local(u + MICROS_PER_DAY, zone).offset_seconds,
    }
    candidates = [u - offset * MICROS_PER_SECOND for offset in offsets]
    return sorted(c for c in candidates if to_local(c, zone).wall == head)


def local_to_instant(wall: str, zone: tzinfo) -> Instant:
    occurrences = local_occurrences(wall, zone)
    if not occurrences:
        raise fault_error("local_time_nonexistent", f"{wall[:19]} does not exist in {zone}")
    return occurrences[0]


def is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def month_length(year: int, month: int) -> int:
    if month == 2:
        return 29 if is_leap_year(year) else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


def next_date(date: str) -> str:
    if not is_date(date):
        raise ValueError(f"{date} is not a YYYY-MM-DD date")
    year, month, day = int(date[0:4]), int(date[5:7]), int(date[8:10])
    if day < month_length(year, month):
        day += 1
    elif month < 12:
        month += 1
        day = 1
    else:
        year += 1
        month = 1
        day = 1
    return f"{year:04d}-{month:02d}-{day:02d}"
