import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import Enum
from typing import Final, Protocol

from .api import ApiResponse, BodyInvalid, Endpoint
from .gate import STOPPED, Exchange, GateRegistry, Outcome, Refusal, exchange
from .hourly import ImportReport
from .price_loop import PriceLoop, PriceLoopRegistry, empty_snapshot
from .signals import Config, Json, Signals, Snapshot, SnapshotError, compute_signals
from .signals.account import Selection, grid_type, select_account, selected_zone
from .signals.constants import (
    ACCOUNT_SYNC_LOCAL_TIME,
    USAGE_LOOKBACK_DAYS,
    USAGE_SYNC_LOCAL_TIME,
)
from .signals.energy import select_nmi
from .signals.model import GroupFault
from .signals.timeprim import (
    Instant,
    datetime_from_instant,
    format_instant,
    instant_from_datetime,
    local_to_instant,
    next_date,
    parse_instant,
    to_local,
)
from .timers import (
    Cancel,
    Scheduler,
    TimerKind,
    arm_at,
    local_date_minus,
    next_boundary,
    next_local_time,
    next_utc_time,
    sleep_until,
)

_LOGGER = logging.getLogger(__package__)

STARTUP_REFUSED_RETRY_UTC: Final = "00:01:00"

type ZoneLoader = Callable[[str], Awaitable[tzinfo | None]]
type Listener = Callable[[], None]
type UsageListener = Callable[[Instant], None]


class Api(Protocol):
    async def account_data(self) -> ApiResponse: ...

    async def outlook(self, region: str) -> ApiResponse: ...

    async def wait(self, region: str, since: str) -> ApiResponse: ...

    async def usage_half_hourly(self, start: str, end: str, nmi: str) -> ApiResponse: ...

    async def usage_daily(self, start: str, end: str, nmi: str) -> ApiResponse: ...

    async def meters(self) -> ApiResponse: ...

    async def tokens(self) -> ApiResponse: ...


class RuntimeHooks(Protocol):
    def auth_refused(self, status: int, body: str) -> None: ...

    def auth_restored(self) -> None: ...

    def access_refused(self, status: int, body: str, since: str) -> None: ...

    def access_restored(self) -> None: ...

    def account_pinned(self, account_number: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    account_number: str | None
    nmi: str | None
    price_high_threshold: float | None
    price_low_threshold: float | None

    @property
    def token_preview(self) -> str:
        return f"{self.token[:8]}…{self.token[-4:]}"


class SyncPoint(Enum):
    ACCOUNT = TimerKind.ACCOUNT_SYNC
    USAGE = TimerKind.USAGE_SYNC


class Slot(Enum):
    ACCOUNT = Endpoint.ACCOUNT_DATA
    METERS = Endpoint.METERS
    TOKENS = Endpoint.TOKENS
    USAGE_HALF_HOURLY = Endpoint.USAGE_HALF_HOURLY
    USAGE_DAILY = Endpoint.USAGE_DAILY


SYNC_CLOCK: Final = {
    SyncPoint.ACCOUNT: ACCOUNT_SYNC_LOCAL_TIME,
    SyncPoint.USAGE: USAGE_SYNC_LOCAL_TIME,
}
ACCOUNT_PART: Final = (Slot.ACCOUNT, Slot.METERS, Slot.TOKENS)


def zone_names(body: Json) -> list[str]:
    names: list[str] = []
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(accounts, list):
        return names
    for account in accounts:
        product = account.get("product") if isinstance(account, dict) else None
        name = product.get("timeZone") if isinstance(product, dict) else None
        if isinstance(name, str) and name not in names:
            names.append(name)
    return names


class InstanceRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        api: Api,
        scheduler: Scheduler,
        load_zone: ZoneLoader,
        hooks: RuntimeHooks,
        loops: PriceLoopRegistry,
        gates: GateRegistry,
    ) -> None:
        self.settings = settings
        self.account_number = settings.account_number
        self.snapshots: dict[Slot, Snapshot] = {slot: empty_snapshot() for slot in Slot}
        self.zones: dict[str, tzinfo] = {}
        self.loop: PriceLoop | None = None
        self.refusal: Refusal | None = None
        self.refused_since: str | None = None
        self.halted = False
        self.blocked: set[Slot] = set()
        self._api = api
        self._scheduler = scheduler
        self._load_zone = load_zone
        self._hooks = hooks
        self._loops = loops
        self._gates = gates
        self._gate = gates.acquire(settings.token)
        self._lock = asyncio.Lock()
        self._events: asyncio.Queue[SyncPoint] = asyncio.Queue()
        self._sync_timers: dict[SyncPoint, Cancel] = {}
        self._sync_zone: tzinfo | None = None
        self._evaluation: tuple[str, Cancel] | None = None
        self._listeners: list[Listener] = []
        self._usage_listeners: list[UsageListener] = []
        self.statistics: ImportReport | None = None
        self._closed = False
        self.signals: Signals = self._compute()

    @property
    def config(self) -> Config:
        return {
            "accountNumber": self.account_number,
            "nmi": self.settings.nmi,
            "tokenPreview": self.settings.token_preview,
            "priceHighThresholdCentsPerKwh": self.settings.price_high_threshold,
            "priceLowThresholdCentsPerKwh": self.settings.price_low_threshold,
        }

    @property
    def outlook(self) -> Snapshot:
        return empty_snapshot() if self.loop is None else self.loop.outlook

    @property
    def limits(self) -> dict[str, str]:
        return dict(self._gate.limits)

    @property
    def daily_limit_remaining(self) -> str | None:
        return self._gate.limits.get("X-DailyLimit-Remaining")

    def add_listener(self, listener: Listener) -> Cancel:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def add_usage_listener(self, listener: UsageListener) -> Cancel:
        self._usage_listeners.append(listener)

        def remove() -> None:
            if listener in self._usage_listeners:
                self._usage_listeners.remove(listener)

        return remove

    def statistics_imported(self, report: ImportReport) -> None:
        if self._closed:
            return
        self.statistics = report
        for listener in list(self._listeners):
            listener()

    def recompute(self) -> None:
        if self._closed:
            return
        self.signals = self._compute()
        account = self.signals["account"]
        number = account["accountNumber"]
        if self.account_number is None and account["status"] == "ok" and number is not None:
            self.account_number = number
            self._hooks.account_pinned(number)
        self._arm_evaluation(self.signals["nextEvaluation"])
        for listener in list(self._listeners):
            listener()

    def outlook_changed(self) -> None:
        self.recompute()

    def loop_refused(self, refusal: Refusal) -> None:
        self._refused(refusal)

    async def run(self) -> None:
        try:
            if await self._startup():
                while True:
                    point = await self._events.get()
                    await self._sync(point)
            else:
                await asyncio.Event().wait()
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_sync_timers()
        if self._evaluation is not None:
            self._evaluation[1]()
            self._evaluation = None
        loop = self.loop
        if loop is not None:
            self._loops.unsubscribe(loop, self)
        self._gates.release(self.settings.token)
        self._listeners.clear()
        self._usage_listeners.clear()

    def _compute(self) -> Signals:
        return compute_signals(
            self._scheduler.now(),
            self.config,
            self.snapshots[Slot.ACCOUNT],
            self.snapshots[Slot.METERS],
            self.snapshots[Slot.TOKENS],
            self.outlook,
            self.snapshots[Slot.USAGE_HALF_HOURLY],
            self.snapshots[Slot.USAGE_DAILY],
        )

    async def _startup(self) -> bool:
        while True:
            result = await self._fetch(Slot.ACCOUNT, self._api.account_data, probe=True)
            if result.outcome is Outcome.OK:
                restored = self.refusal is not None
                if restored:
                    self._restored()
                zone = self._zone()
                if zone is not None:
                    self._sync_zone = zone
                    break
                await self._fetch(Slot.METERS, self._api.meters)
                await self._fetch(Slot.TOKENS, self._api.tokens)
                if restored or self._region_changed():
                    await self._resume_price_loop()
                await self._sleep(TimerKind.STARTUP, self._next_utc_retry())
                continue
            if self.halted or Slot.ACCOUNT in self.blocked:
                return False
            if result.outcome is Outcome.REFUSED or self._account_held():
                await self._sleep(TimerKind.STARTUP, self._next_utc_retry())
                continue
            await self._sleep(TimerKind.STARTUP, next_boundary(self._scheduler.now()))
        self._arm_sync(SyncPoint.ACCOUNT)
        self._arm_sync(SyncPoint.USAGE)
        await self._fetch(Slot.METERS, self._api.meters)
        await self._fetch(Slot.TOKENS, self._api.tokens)
        await self._resume_price_loop()
        await self._usage_part()
        return True

    async def _sync(self, point: SyncPoint) -> None:
        if self.halted:
            return
        if self.refusal is not None:
            if point is not SyncPoint.ACCOUNT:
                return
            result = await self._fetch(Slot.ACCOUNT, self._api.account_data, probe=True)
            if result.outcome is not Outcome.OK:
                return
            self._restored()
            await self._fetch(Slot.METERS, self._api.meters)
            await self._fetch(Slot.TOKENS, self._api.tokens)
            await self._resume_price_loop()
            await self._usage_part()
            return
        retry = any(self._failed(slot) for slot in ACCOUNT_PART if slot not in self.blocked)
        if point is SyncPoint.ACCOUNT or retry:
            await self._fetch(Slot.ACCOUNT, self._api.account_data)
            await self._fetch(Slot.METERS, self._api.meters)
            await self._fetch(Slot.TOKENS, self._api.tokens)
            if self._region_changed():
                await self._resume_price_loop()
        await self._usage_part()

    async def _usage_part(self) -> None:
        window = self._usage_window()
        if window is None:
            return
        start, end, nmi, window_start = window

        async def half_hourly() -> ApiResponse:
            return await self._api.usage_half_hourly(start, end, nmi)

        async def daily() -> ApiResponse:
            return await self._api.usage_daily(start, end, nmi)

        intervals = await self._fetch(Slot.USAGE_HALF_HOURLY, half_hourly)
        days = await self._fetch(Slot.USAGE_DAILY, daily)
        if intervals.outcome is Outcome.OK and days.outcome is Outcome.OK and not self._closed:
            for listener in list(self._usage_listeners):
                listener(window_start)

    async def _resume_price_loop(self) -> None:
        if self.refusal is not None or self.halted:
            return
        region = self._region()
        loop = self.loop
        if loop is not None and loop.region != region:
            self._loops.unsubscribe(loop, self)
            self.loop = None
            loop = None
            self.recompute()
        if region is None:
            return
        if loop is None:
            loop = self._loops.subscribe(self.settings.token, region, self._api, self._gate, self)
            self.loop = loop
        else:
            loop.start()
        await loop.first_attempt()

    async def _fetch(
        self,
        slot: Slot,
        call: Callable[[], Coroutine[object, object, ApiResponse]],
        *,
        probe: bool = False,
    ) -> Exchange:
        if slot in self.blocked:
            return STOPPED
        while True:
            result = await exchange(
                self._scheduler,
                self._gate,
                self._flight_lock(),
                call,
                lambda: self._allowed(probe),
            )
            if result.outcome is Outcome.STOPPED:
                return result
            if result.outcome is Outcome.OK and result.response is not None:
                try:
                    body = result.response.json_body()
                except BodyInvalid as exc:
                    self._record(slot, {"kind": "invalid", "message": str(exc)})
                    return Exchange(Outcome.FAILED, result.response)
                self.snapshots[slot] = {
                    "fetchedAt": format_instant(instant_from_datetime(self._scheduler.now())),
                    "error": None,
                    "body": body,
                }
                if slot is Slot.ACCOUNT:
                    await self._load_zones(body)
                    zone = self._zone()
                    if zone is not None:
                        self._sync_zone = zone
                self.recompute()
                return result
            if result.error is not None:
                self._record(slot, result.error)
            if result.outcome is Outcome.RETRY:
                continue
            if result.outcome is Outcome.REFUSED and result.refusal is not None:
                self._refused(result.refusal)
            elif result.outcome is Outcome.HALT:
                self._halt(slot)
            return result

    def _allowed(self, probe: bool) -> bool:
        return not self._closed and not self.halted and (probe or self.refusal is None)

    def _flight_lock(self) -> asyncio.Lock:
        return self._lock if self.loop is None else self.loop.lock

    def _failed(self, slot: Slot) -> bool:
        snapshot = self.snapshots[slot]
        return snapshot["error"] is not None or snapshot["body"] is None

    def _account_held(self) -> bool:
        return self.snapshots[Slot.ACCOUNT]["body"] is not None

    def _record(self, slot: Slot, error: SnapshotError) -> None:
        self.snapshots[slot] = {**self.snapshots[slot], "error": error}
        self.recompute()

    async def _load_zones(self, body: Json) -> None:
        for name in zone_names(body):
            if name in self.zones:
                continue
            try:
                zone = await self._load_zone(name)
            except ValueError as exc:
                _LOGGER.debug("time zone %r not loadable: %s", name, exc)
                continue
            if zone is not None:
                self.zones[name] = zone

    def _selection(self) -> Selection | None:
        try:
            return select_account(self.snapshots[Slot.ACCOUNT], self.config)
        except GroupFault:
            return None

    def _zone(self) -> tzinfo | None:
        selection = self._selection()
        if selection is None:
            return None
        try:
            return selected_zone(selection)[1]
        except GroupFault:
            return None

    def _region_changed(self) -> bool:
        return self._region() != (None if self.loop is None else self.loop.region)

    def _region(self) -> str | None:
        selection = self._selection()
        if selection is None:
            return None
        try:
            return grid_type(selection)
        except GroupFault:
            return None

    def _usage_window(self) -> tuple[str, str, str, Instant] | None:
        selection = self._selection()
        if selection is None:
            return None
        try:
            zone = selected_zone(selection)[1]
            nmi = select_nmi(selection, self.config, self.snapshots[Slot.METERS])
            today = to_local(instant_from_datetime(self._scheduler.now()), zone).date
            start = f"{local_date_minus(today, USAGE_LOOKBACK_DAYS)}T00:00:00"
            window_start = local_to_instant(start, zone)
        except GroupFault:
            return None
        return start, f"{next_date(today)}T00:00:00", nmi, window_start

    def _next_utc_retry(self) -> datetime:
        return next_utc_time(self._scheduler.now(), STARTUP_REFUSED_RETRY_UTC)

    async def _sleep(self, kind: TimerKind, target: datetime) -> None:
        await sleep_until(self._scheduler, kind, target)

    def _arm_sync(self, point: SyncPoint) -> None:
        zone = self._sync_zone
        if zone is None:
            raise RuntimeError(f"{point.value.value} armed before a time zone was read")
        target = next_local_time(self._scheduler.now(), zone, SYNC_CLOCK[point])

        def fired() -> None:
            self._sync_timers.pop(point, None)
            self._arm_sync(point)
            self._events.put_nowait(point)

        self._sync_timers[point] = arm_at(self._scheduler, point.value, target, fired)

    def _cancel_sync_timers(self) -> None:
        for cancel in self._sync_timers.values():
            cancel()
        self._sync_timers.clear()

    def _arm_evaluation(self, target: str | None) -> None:
        current = self._evaluation
        if current is not None and current[0] == target:
            return
        if current is not None:
            current[1]()
            self._evaluation = None
        if target is None:
            return
        at = datetime_from_instant(parse_instant(target))
        self._evaluation = (
            target,
            arm_at(self._scheduler, TimerKind.EVALUATION, at, self._evaluate),
        )

    def _evaluate(self) -> None:
        self._evaluation = None
        self.recompute()

    def _refused(self, refusal: Refusal) -> None:
        if self.halted or self._closed:
            return
        self.refusal = refusal
        if refusal.gateway:
            self.halted = True
            self._cancel_sync_timers()
        loop = self.loop
        if loop is not None:
            loop.stop()
        if refusal.status == 403:
            if self.refused_since is None:
                self.refused_since = format_instant(instant_from_datetime(self._scheduler.now()))
            self._hooks.access_refused(refusal.status, refusal.body, self.refused_since)
        else:
            self._hooks.auth_refused(refusal.status, refusal.body)

    def _restored(self) -> None:
        refusal = self.refusal
        self.refusal = None
        if refusal is None:
            return
        if refusal.status == 403:
            self.refused_since = None
            self._hooks.access_restored()
        else:
            self._hooks.auth_restored()

    def _halt(self, slot: Slot) -> None:
        if slot is not Slot.ACCOUNT or self._account_held():
            self.blocked.add(slot)
            return
        self.halted = True
        self._cancel_sync_timers()
