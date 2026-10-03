import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Final

from .api import LIMIT_HEADERS, ApiNetworkError, ApiResponse, BodyInvalid, Endpoint
from .signals.model import Json, SnapshotError
from .timers import Cancel, Scheduler, TimerKind, arm_at

RETRY_AFTER_PATTERN: Final = re.compile(r"[0-9]+", re.ASCII)
NOT_FOUND_RETRIED: Final = frozenset({Endpoint.USAGE_HALF_HOURLY, Endpoint.USAGE_DAILY})


class Outcome(Enum):
    OK = "ok"
    RETRY = "retry"
    REFUSED = "refused"
    HALT = "halt"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class Refusal:
    status: int
    body: str
    gateway: bool


@dataclass(frozen=True, slots=True)
class Exchange:
    outcome: Outcome
    response: ApiResponse | None = None
    error: SnapshotError | None = None
    refusal: Refusal | None = None
    retry_after_s: int | None = None


STOPPED: Final = Exchange(Outcome.STOPPED)


class RequestGate:
    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler
        self._open = asyncio.Event()
        self._open.set()
        self._cancel: Cancel | None = None
        self.closed_until: datetime | None = None
        self.limits: dict[str, str] = {}

    @property
    def is_open(self) -> bool:
        return self._open.is_set()

    async def wait_open(self) -> None:
        await self._open.wait()

    def close_until(self, target: datetime) -> None:
        if self.closed_until is not None and target <= self.closed_until:
            return
        if self._cancel is not None:
            self._cancel()
        self.closed_until = target
        self._open.clear()
        self._cancel = arm_at(self._scheduler, TimerKind.RETRY_AFTER, target, self._reopen)

    def observe(self, response: ApiResponse) -> None:
        for name in LIMIT_HEADERS:
            value = response.header(name)
            if value is not None:
                self.limits[name] = value

    def close(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    def _reopen(self) -> None:
        self._cancel = None
        self.closed_until = None
        self._open.set()


class GateRegistry:
    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler
        self._gates: dict[str, tuple[RequestGate, int]] = {}

    def acquire(self, token: str) -> RequestGate:
        gate, count = self._gates.get(token, (RequestGate(self._scheduler), 0))
        self._gates[token] = (gate, count + 1)
        return gate

    def release(self, token: str) -> None:
        gate, count = self._gates[token]
        if count > 1:
            self._gates[token] = (gate, count - 1)
            return
        del self._gates[token]
        gate.close()


def retry_after_s(response: ApiResponse) -> int:
    value = response.header("Retry-After")
    if value is None:
        raise BodyInvalid(f"{response.endpoint.value} answered 429 without Retry-After")
    if RETRY_AFTER_PATTERN.fullmatch(value) is None:
        raise BodyInvalid(
            f"{response.endpoint.value} answered 429 with Retry-After {value!r}, not an integer"
        )
    return int(value)


def is_gateway_unauthorized(response: ApiResponse) -> bool:
    try:
        value: Json = json.loads(response.body.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return False
    return isinstance(value, dict) and value.get("error") == "unauthorized"


def classify(response: ApiResponse) -> Exchange:
    status = response.status
    if response.ok:
        return Exchange(Outcome.OK, response)
    error = response.http_error()
    if status == 429:
        try:
            seconds = retry_after_s(response)
        except BodyInvalid as exc:
            return Exchange(Outcome.FAILED, response, {"kind": "invalid", "message": str(exc)})
        return Exchange(Outcome.RETRY, response, error, retry_after_s=seconds)
    if status in (401, 403):
        gateway = status == 401 and is_gateway_unauthorized(response)
        refusal = Refusal(status, error["body"], gateway)
        return Exchange(Outcome.REFUSED, response, error, refusal)
    if status == 400 or (status == 404 and response.endpoint not in NOT_FOUND_RETRIED):
        return Exchange(Outcome.HALT, response, error)
    return Exchange(Outcome.FAILED, response, error)


async def exchange(
    scheduler: Scheduler,
    gate: RequestGate,
    lock: asyncio.Lock,
    call: Callable[[], Awaitable[ApiResponse]],
    allowed: Callable[[], bool],
) -> Exchange:
    while True:
        await gate.wait_open()
        async with lock:
            if not gate.is_open:
                continue
            if not allowed():
                return STOPPED
            try:
                response = await call()
            except ApiNetworkError as exc:
                return Exchange(Outcome.FAILED, error={"kind": "network", "message": str(exc)})
        gate.observe(response)
        result = classify(response)
        if result.retry_after_s is not None:
            gate.close_until(scheduler.now() + timedelta(seconds=result.retry_after_s))
        return result
