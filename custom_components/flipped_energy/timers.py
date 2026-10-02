import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Protocol

from .signals.constants import DISPATCH_INTERVAL_S, TIMER_MAX_AHEAD_S
from .signals.timeprim import (
    datetime_from_instant,
    instant_from_datetime,
    local_occurrences,
    next_date,
    to_local,
)

type Action = Callable[[], None]
type Cancel = Callable[[], None]


class TimerKind(StrEnum):
    STARTUP = "startup"
    ACCOUNT_SYNC = "accountSync"
    USAGE_SYNC = "usageSync"
    EVALUATION = "evaluation"
    PRICE = "price"
    RETRY_AFTER = "retryAfter"


class Scheduler(Protocol):
    def now(self) -> datetime: ...

    def call_at(self, target: datetime, action: Action) -> Cancel: ...

    def armed(self, kind: TimerKind, target: datetime, delay_s: float) -> None: ...


class _Chain:
    __slots__ = ("cancelled", "platform")

    def __init__(self) -> None:
        self.cancelled = False
        self.platform: Cancel | None = None


def arm_at(scheduler: Scheduler, kind: TimerKind, target: datetime, action: Action) -> Cancel:
    chain = _Chain()

    def arm() -> None:
        now = scheduler.now()
        delay = min(max(0.0, (target - now).total_seconds()), float(TIMER_MAX_AHEAD_S))
        scheduler.armed(kind, target, delay)
        chain.platform = scheduler.call_at(now + timedelta(seconds=delay), fire)

    def fire() -> None:
        chain.platform = None
        if chain.cancelled:
            return
        if scheduler.now() >= target:
            action()
        else:
            arm()

    def cancel() -> None:
        chain.cancelled = True
        platform = chain.platform
        chain.platform = None
        if platform is not None:
            platform()

    arm()
    return cancel


async def sleep_until(scheduler: Scheduler, kind: TimerKind, target: datetime) -> None:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def wake() -> None:
        if not future.done():
            future.set_result(None)

    cancel = arm_at(scheduler, kind, target, wake)
    try:
        await future
    finally:
        cancel()


def boundary(value: datetime) -> datetime:
    utc = value.astimezone(UTC).replace(second=0, microsecond=0)
    return utc - timedelta(minutes=utc.minute % (DISPATCH_INTERVAL_S // 60))


def next_boundary(value: datetime) -> datetime:
    return boundary(value) + timedelta(seconds=DISPATCH_INTERVAL_S)


def next_utc_time(value: datetime, clock: str) -> datetime:
    utc = value.astimezone(UTC)
    candidate = datetime.fromisoformat(f"{utc.date().isoformat()}T{clock}").replace(tzinfo=UTC)
    if candidate <= utc:
        candidate += timedelta(days=1)
    return candidate


def next_local_time(value: datetime, zone: tzinfo, clock: str) -> datetime:
    now = instant_from_datetime(value)
    day = to_local(now, zone).date
    while True:
        occurrences = local_occurrences(f"{day}T{clock}", zone)
        if occurrences and occurrences[0] > now:
            return datetime_from_instant(occurrences[0])
        day = next_date(day)


def local_date_minus(day: str, days: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=days)).isoformat()
