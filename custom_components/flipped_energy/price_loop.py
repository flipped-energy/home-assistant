import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from .api import ApiResponse, BodyInvalid
from .gate import Exchange, Outcome, Refusal, RequestGate, exchange
from .signals.constants import DISPATCH_INTERVAL_S, WAIT_HOLDS_PER_INTERVAL
from .signals.model import Json, Snapshot, SnapshotError
from .signals.timeprim import format_instant, instant_from_datetime
from .timers import Scheduler, TimerKind, boundary, next_boundary, sleep_until

_LOGGER = logging.getLogger(__package__)

NEM_TIME: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+10:00", re.ASCII
)
NEM_OFFSET: Final = timedelta(hours=10)
INTERVAL: Final = timedelta(seconds=DISPATCH_INTERVAL_S)

type TaskFactory = Callable[[Coroutine[object, object, None], str], asyncio.Task[None]]


class PriceApi(Protocol):
    async def outlook(self, region: str) -> ApiResponse: ...

    async def wait(self, region: str, since: str) -> ApiResponse: ...


class Subscriber(Protocol):
    def outlook_changed(self) -> None: ...

    def loop_refused(self, refusal: Refusal) -> None: ...


class LoopState(StrEnum):
    BOOTSTRAP = "bootstrap"
    ARM = "arm"
    HOLD = "hold"
    OUTLOOK = "outlook"
    STOPPED = "stopped"


def nem_instant(value: Json, path: str) -> datetime:
    if not isinstance(value, str) or NEM_TIME.fullmatch(value) is None:
        raise BodyInvalid(f"{path} is {value!r}, not a NEM time ending in +10:00")
    return datetime.fromisoformat(value).astimezone(UTC)


def nem_since(instant: datetime) -> str:
    return (instant.astimezone(UTC) + NEM_OFFSET).strftime("%Y-%m-%dT%H:%M:%S")


def outlook_time(body: Json) -> datetime:
    now = body.get("now") if isinstance(body, dict) else None
    if not isinstance(now, dict) or "time" not in now:
        raise BodyInvalid("outlook.now.time is missing")
    return nem_instant(now["time"], "outlook.now.time")


def newest_after(body: Json, after: datetime) -> datetime | None:
    if not isinstance(body, list):
        raise BodyInvalid("wait body is not an array")
    newest: datetime | None = None
    for index, point in enumerate(body):
        if not isinstance(point, dict) or "time" not in point:
            raise BodyInvalid(f"wait[{index}].time is missing")
        at = nem_instant(point["time"], f"wait[{index}].time")
        if at > after and (newest is None or at > newest):
            newest = at
    return newest


def empty_snapshot() -> Snapshot:
    return {"fetchedAt": None, "error": None, "body": None}


class PriceLoop:
    def __init__(
        self,
        *,
        key: tuple[str, str],
        api: PriceApi,
        scheduler: Scheduler,
        gate: RequestGate,
        create_task: TaskFactory,
    ) -> None:
        self.key = key
        self.region = key[1]
        self.lock = asyncio.Lock()
        self.outlook = empty_snapshot()
        self.state = LoopState.STOPPED
        self.t: datetime | None = None
        self._api = api
        self._scheduler = scheduler
        self._gate = gate
        self._create_task = create_task
        self._subscribers: list[Subscriber] = []
        self._task: asyncio.Task[None] | None = None
        self._first_attempt = asyncio.Event()
        self._holds = 0
        self._holds_interval: datetime | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def holds(self) -> int:
        if self._holds_interval != boundary(self._scheduler.now()):
            return 0
        return self._holds

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def add(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def remove(self, subscriber: Subscriber) -> bool:
        self._subscribers.remove(subscriber)
        return not self._subscribers

    def start(self) -> None:
        if self.running:
            return
        self._first_attempt.clear()
        self.state = LoopState.BOOTSTRAP
        self._task = self._create_task(self._run(), f"flipped_energy price loop {self.region}")

    async def first_attempt(self) -> None:
        await self._first_attempt.wait()

    def stop(self) -> None:
        self.state = LoopState.STOPPED
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _run(self) -> None:
        state = LoopState.BOOTSTRAP
        while True:
            self.state = state
            if state is LoopState.BOOTSTRAP:
                result = await self._exchange(self._call_outlook)
                if result.outcome is Outcome.OK and result.response is not None:
                    published = self._publish(result.response)
                    self._first_attempt.set()
                    if published is not None:
                        self.t = published
                        state = LoopState.ARM
                    else:
                        await self._sleep_to_next_boundary()
                    continue
                self._record_error(result)
                self._first_attempt.set()
                if not self._continues(result):
                    return
                if result.outcome is Outcome.FAILED:
                    await self._sleep_to_next_boundary()
            elif state is LoopState.ARM:
                target = self._held() + INTERVAL
                if target > self._scheduler.now():
                    await sleep_until(self._scheduler, TimerKind.PRICE, target)
                state = LoopState.HOLD
            elif state is LoopState.HOLD:
                if self.holds >= WAIT_HOLDS_PER_INTERVAL:
                    await self._sleep_to_next_boundary()
                    continue
                result = await self._exchange(self._call_wait)
                if result.outcome is Outcome.OK and result.response is not None:
                    state = await self._released(result.response)
                    continue
                self._record_error(result)
                if not self._continues(result):
                    return
                if result.outcome is Outcome.FAILED:
                    await self._sleep_to_next_boundary()
            elif state is LoopState.OUTLOOK:
                result = await self._exchange(self._call_outlook)
                if result.outcome is Outcome.OK and result.response is not None:
                    published = self._publish(result.response)
                    if published is not None:
                        self.t = max(self._held(), published)
                    state = LoopState.ARM
                    continue
                self._record_error(result)
                if not self._continues(result):
                    return
                if result.outcome is Outcome.FAILED:
                    state = LoopState.ARM
            else:
                return

    async def _released(self, response: ApiResponse) -> LoopState:
        if response.status == 204:
            return LoopState.HOLD
        held = self._held()
        try:
            newest = newest_after(response.json_body(), held)
        except BodyInvalid as exc:
            self._record({"kind": "invalid", "message": str(exc)})
            await self._sleep_to_next_boundary()
            return LoopState.HOLD
        if newest is None:
            message = (
                f"wait_no_progress: {response.endpoint.value} region={self.region} "
                f"since={nem_since(held)} answered {response.status} with no point after since"
            )
            _LOGGER.error(message)
            self._record({"kind": "invalid", "message": message})
            self.t = held + INTERVAL
            return LoopState.ARM
        self.t = newest
        return LoopState.OUTLOOK

    def _continues(self, result: Exchange) -> bool:
        if result.outcome is Outcome.REFUSED and result.refusal is not None:
            self.state = LoopState.STOPPED
            for subscriber in list(self._subscribers):
                subscriber.loop_refused(result.refusal)
            return False
        if result.outcome in (Outcome.HALT, Outcome.STOPPED):
            self.state = LoopState.STOPPED
            return False
        return True

    def _held(self) -> datetime:
        if self.t is None:
            raise RuntimeError(f"price loop {self.region} has no price instant in {self.state}")
        return self.t

    async def _sleep_to_next_boundary(self) -> None:
        target = next_boundary(self._scheduler.now())
        await sleep_until(self._scheduler, TimerKind.PRICE, target)

    async def _exchange(
        self, call: Callable[[], Coroutine[object, object, ApiResponse]]
    ) -> Exchange:
        return await exchange(self._scheduler, self._gate, self.lock, call, self._allowed)

    def _allowed(self) -> bool:
        return self.state is not LoopState.STOPPED

    async def _call_outlook(self) -> ApiResponse:
        return await self._api.outlook(self.region)

    async def _call_wait(self) -> ApiResponse:
        now = self._scheduler.now()
        interval = boundary(now)
        if self._holds_interval != interval:
            self._holds_interval = interval
            self._holds = 0
        self._holds += 1
        return await self._api.wait(self.region, nem_since(self._held()))

    def _publish(self, response: ApiResponse) -> datetime | None:
        try:
            body = response.json_body()
        except BodyInvalid as exc:
            self._record({"kind": "invalid", "message": str(exc)})
            return None
        now = instant_from_datetime(self._scheduler.now())
        self.outlook = {"fetchedAt": format_instant(now), "error": None, "body": body}
        try:
            published = outlook_time(body)
        except BodyInvalid as exc:
            self._record({"kind": "invalid", "message": str(exc)})
            return None
        self._notify()
        return published

    def _record_error(self, result: Exchange) -> None:
        if result.error is not None:
            self._record(result.error)

    def _record(self, error: SnapshotError) -> None:
        self.outlook = {**self.outlook, "error": error}
        self._notify()

    def _notify(self) -> None:
        for subscriber in list(self._subscribers):
            subscriber.outlook_changed()


class PriceLoopRegistry:
    def __init__(self, scheduler: Scheduler, create_task: TaskFactory) -> None:
        self._scheduler = scheduler
        self._create_task = create_task
        self._loops: dict[tuple[str, str], PriceLoop] = {}

    def get(self, token: str, region: str) -> PriceLoop | None:
        return self._loops.get((token, region))

    def subscribe(
        self,
        token: str,
        region: str,
        api: PriceApi,
        gate: RequestGate,
        subscriber: Subscriber,
    ) -> PriceLoop:
        key = (token, region)
        loop = self._loops.get(key)
        if loop is None:
            loop = PriceLoop(
                key=key,
                api=api,
                scheduler=self._scheduler,
                gate=gate,
                create_task=self._create_task,
            )
            self._loops[key] = loop
        loop.add(subscriber)
        loop.start()
        return loop

    def unsubscribe(self, loop: PriceLoop, subscriber: Subscriber) -> None:
        if loop.remove(subscriber):
            loop.stop()
            del self._loops[loop.key]
