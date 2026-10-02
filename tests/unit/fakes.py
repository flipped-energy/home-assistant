import asyncio
import copy
import hashlib
import json
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from multidict import CIMultiDict, CIMultiDictProxy

from custom_components.flipped_energy.api import ApiNetworkError, ApiResponse, Endpoint
from custom_components.flipped_energy.gate import GateRegistry
from custom_components.flipped_energy.price_loop import PriceLoopRegistry
from custom_components.flipped_energy.runtime import InstanceRuntime, Settings
from custom_components.flipped_energy.signals import Json, Signals
from custom_components.flipped_energy.timers import Action, Cancel, TimerKind

SEQUENCES = Path(__file__).resolve().parent.parent / "sequences"
type Obj = dict[str, Json]


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_sequence(name: str) -> Obj:
    return cast(Obj, json.loads((SEQUENCES / name).read_text("utf-8")))


def sequence_index() -> list[dict[str, str]]:
    return cast(list[dict[str, str]], json.loads((SEQUENCES / "index.json").read_text("utf-8")))


def sha256_of(name: str) -> str:
    return hashlib.sha256((SEQUENCES / name).read_bytes()).hexdigest()


async def settle() -> None:
    loop = asyncio.get_running_loop()
    for _ in range(100_000):
        await asyncio.sleep(0)
        if not loop._ready:
            return
    raise AssertionError("the event loop did not settle")


@dataclass(order=True)
class _Entry:
    target: datetime
    seq: int
    action: Action = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class FakeScheduler:
    def __init__(self, start: datetime) -> None:
        self._now = start
        self._seq = 0
        self._entries: list[_Entry] = []
        self.timers: list[Obj] = []

    def now(self) -> datetime:
        return self._now

    def call_at(self, target: datetime, action: Action) -> Cancel:
        self._seq += 1
        entry = _Entry(target, self._seq, action)
        self._entries.append(entry)

        def cancel() -> None:
            entry.cancelled = True

        return cancel

    def armed(self, kind: TimerKind, target: datetime, delay_s: float) -> None:
        self.timers.append(
            {
                "armedAt": format_time(self._now),
                "timer": kind.value,
                "target": format_time(target),
                "delaySeconds": int(delay_s) if float(delay_s).is_integer() else delay_s,
            }
        )

    def _next(self, until: datetime, inclusive: bool) -> _Entry | None:
        live = [e for e in self._entries if not e.cancelled]
        self._entries = live
        due = [e for e in live if e.target < until or (inclusive and e.target == until)]
        return min(due) if due else None

    async def advance_to(self, until: datetime, inclusive: bool = True) -> None:
        await settle()
        while (entry := self._next(until, inclusive)) is not None:
            self._entries.remove(entry)
            self._now = max(self._now, entry.target)
            entry.action()
            await settle()
        self._now = max(self._now, until)

    async def advance(self, seconds: float) -> None:
        await self.advance_to(self._now + timedelta(seconds=seconds))


def body_bytes(body: Json) -> bytes:
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def answer(
    path: Endpoint,
    status: int = 200,
    body: Json = None,
    *,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    hold: float = 0,
    network: str | None = None,
) -> Obj:
    entry: Obj = {"method": "GET", "path": path.value}
    if query is not None:
        entry["query"] = cast(Json, query)
    if network is not None:
        entry["answer"] = {"network": network}
    else:
        entry["answer"] = {
            "status": status,
            "headers": cast(Json, headers or {}),
            "body": body,
        }
    if hold:
        entry["holdSeconds"] = hold
    return entry


class ScriptedApi:
    def __init__(self, scheduler: FakeScheduler, responses: list[Obj], strict: bool = True) -> None:
        self._scheduler = scheduler
        self._responses = list(responses)
        self._strict = strict
        self.transcript: list[Obj] = []
        self.problems: list[str] = []
        self.in_flight = 0
        self.sent_while_busy: list[str] = []

    @property
    def remaining(self) -> list[Obj]:
        return self._responses

    async def account_data(self) -> ApiResponse:
        return await self._answer(Endpoint.ACCOUNT_DATA, {})

    async def outlook(self, region: str) -> ApiResponse:
        return await self._answer(Endpoint.OUTLOOK, {"region": region})

    async def wait(self, region: str, since: str) -> ApiResponse:
        query = {"region": region, "since": since, "timeoutSeconds": "55"}
        return await self._answer(Endpoint.WAIT, query)

    async def usage_half_hourly(self, start: str, end: str, nmi: str) -> ApiResponse:
        query = {"start": start, "end": end, "nmi": nmi}
        return await self._answer(Endpoint.USAGE_HALF_HOURLY, query)

    async def usage_daily(self, start: str, end: str, nmi: str) -> ApiResponse:
        query = {"start": start, "end": end, "nmi": nmi}
        return await self._answer(Endpoint.USAGE_DAILY, query)

    async def meters(self) -> ApiResponse:
        return await self._answer(Endpoint.METERS, {})

    async def tokens(self) -> ApiResponse:
        return await self._answer(Endpoint.TOKENS, {})

    def _take(self, endpoint: Endpoint, query: dict[str, str]) -> Obj | None:
        for index, entry in enumerate(self._responses):
            if entry["path"] == endpoint.value:
                if self._strict and index != 0:
                    break
                self._responses.pop(index)
                expected = entry.get("query")
                if expected is not None and expected != query:
                    self.problems.append(
                        f"{format_time(self._scheduler.now())} {endpoint.value}: "
                        f"query {query!r}, scripted {expected!r}"
                    )
                    return None
                return entry
            if self._strict:
                break
        self.problems.append(
            f"{format_time(self._scheduler.now())} unexpected request {endpoint.value} {query!r}"
        )
        return None

    def sent(self, endpoint: Endpoint) -> list[str]:
        return [str(r["sendAt"]) for r in self.transcript if r["path"] == endpoint.value]

    async def _answer(self, endpoint: Endpoint, query: dict[str, str]) -> ApiResponse:
        if self.in_flight:
            self.sent_while_busy.append(f"{format_time(self._scheduler.now())} {endpoint.value}")
        self.transcript.append(
            {"sendAt": format_time(self._scheduler.now()), "path": endpoint.value, "query": query}
        )
        entry = self._take(endpoint, query)
        if entry is None:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        self.in_flight += 1
        try:
            hold = entry.get("holdSeconds")
            if isinstance(hold, (int, float)) and hold > 0:
                released: asyncio.Future[None] = asyncio.get_running_loop().create_future()

                def release() -> None:
                    if not released.done():
                        released.set_result(None)

                cancel = self._scheduler.call_at(
                    self._scheduler.now() + timedelta(seconds=hold), release
                )
                try:
                    await released
                finally:
                    cancel()
        finally:
            self.in_flight -= 1
        scripted = cast(Obj, entry["answer"])
        if "network" in scripted:
            raise ApiNetworkError(str(scripted["network"]))
        headers = cast(dict[str, str], scripted.get("headers") or {})
        return ApiResponse(
            endpoint,
            int(cast(int, scripted["status"])),
            CIMultiDictProxy(CIMultiDict(headers)),
            body_bytes(scripted.get("body")),
        )


class FakeZoneLoader:
    def __init__(self) -> None:
        self.awaited: list[str] = []

    async def __call__(self, name: str) -> tzinfo | None:
        self.awaited.append(name)
        await asyncio.sleep(0)
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return None


@dataclass
class RecordingHooks:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def auth_refused(self, status: int, body: str) -> None:
        self.calls.append(("auth_refused", status, body))

    def auth_restored(self) -> None:
        self.calls.append(("auth_restored",))

    def access_refused(self, status: int, body: str, since: str) -> None:
        self.calls.append(("access_refused", status, body, since))

    def access_restored(self) -> None:
        self.calls.append(("access_restored",))

    def account_pinned(self, account_number: str) -> None:
        self.calls.append(("account_pinned", account_number))


def create_task(coro: Coroutine[object, object, None], name: str) -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(coro, name=name)


def settings_from(config: Obj, account_number: str | None = None) -> Settings:
    high = config.get("priceHighThresholdCentsPerKwh")
    low = config.get("priceLowThresholdCentsPerKwh")
    stored = account_number if account_number is not None else config.get("accountNumber")
    nmi = config.get("nmi")
    return Settings(
        token=str(config["token"]),
        account_number=None if stored is None else str(stored),
        nmi=None if nmi is None else str(nmi),
        price_high_threshold=None if high is None else float(cast(float, high)),
        price_low_threshold=None if low is None else float(cast(float, low)),
    )


class Harness:
    def __init__(self, start: datetime) -> None:
        self.scheduler = FakeScheduler(start)
        self.loops = PriceLoopRegistry(self.scheduler, create_task)
        self.gates = GateRegistry(self.scheduler)
        self.zones = FakeZoneLoader()
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def launch(
        self, settings: Settings, api: ScriptedApi, hooks: RecordingHooks
    ) -> InstanceRuntime:
        runtime = InstanceRuntime(
            settings=settings,
            api=api,
            scheduler=self.scheduler,
            load_zone=self.zones,
            hooks=hooks,
            loops=self.loops,
            gates=self.gates,
        )
        self._tasks[id(runtime)] = create_task(runtime.run(), "runtime")
        return runtime

    async def stop(self, runtime: InstanceRuntime) -> None:
        task = self._tasks.pop(id(runtime))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await settle()

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        await settle()
        current = asyncio.current_task()
        others = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for task in others:
            task.cancel()
        await asyncio.gather(*others, return_exceptions=True)


@dataclass
class Replay:
    transcript: list[Obj]
    timers: list[Obj]
    signals: Signals
    stored: str | None
    problems: list[str]
    hooks: RecordingHooks
    unused: list[Obj]


async def replay(sequence: Obj) -> Replay:
    harness = Harness(parse_time(str(sequence["start"])))
    api = ScriptedApi(harness.scheduler, cast(list[Obj], sequence["responses"]))
    hooks = RecordingHooks()
    config = cast(Obj, sequence["config"])
    runtime = harness.launch(settings_from(config), api, hooks)
    try:
        for restart in cast(list[str], sequence["restarts"]):
            await harness.scheduler.advance_to(parse_time(restart), inclusive=False)
            stored = runtime.account_number
            await harness.stop(runtime)
            runtime = harness.launch(settings_from(config, stored), api, hooks)
        await harness.scheduler.advance_to(parse_time(str(sequence["end"])))
        return Replay(
            transcript=api.transcript,
            timers=harness.scheduler.timers,
            signals=runtime.signals,
            stored=runtime.account_number,
            problems=api.problems,
            hooks=hooks,
            unused=api.remaining,
        )
    finally:
        await harness.close()


def timer_key(timer: Obj) -> tuple[str, str, str, str]:
    return (
        str(timer["armedAt"]),
        str(timer["timer"]),
        str(timer["target"]),
        str(timer["delaySeconds"]),
    )


def group_differences(expected: Obj, signals: Signals) -> list[str]:
    out: list[str] = []
    for name, value in expected.items():
        group = cast(Obj, cast(Obj, signals)[name])
        want = cast(Obj, value)
        if group["status"] != want["status"]:
            out.append(f"{name}.status: expected {want['status']!r}, got {group['status']!r}")
        fault = want["fault"]
        actual = group["fault"]
        if fault is None:
            if actual is not None:
                out.append(f"{name}.fault: expected null, got {actual!r}")
            continue
        if not isinstance(actual, dict):
            out.append(f"{name}.fault: expected {fault!r}, got {actual!r}")
            continue
        for key, wanted in cast(Obj, fault).items():
            if key != "message" and actual.get(key) != wanted:
                out.append(f"{name}.fault.{key}: expected {wanted!r}, got {actual.get(key)!r}")
    return out


class Bodies:
    def __init__(self) -> None:
        sequence = load_sequence("price-loop-basics.json")
        responses = cast(list[Obj], sequence["responses"])
        self.config = cast(Obj, sequence["config"])
        self.account = cast(Obj, cast(Obj, responses[0]["answer"])["body"])
        self.meters = cast(Obj, responses[1]["answer"])["body"]
        self.tokens = cast(Obj, responses[2]["answer"])["body"]
        self._outlook = cast(Obj, cast(Obj, responses[3]["answer"])["body"])

    def settings(self) -> Settings:
        return settings_from(self.config)

    def outlook(self, nem_time: str) -> Obj:
        value = copy.deepcopy(self._outlook)
        cast(Obj, value["now"])["time"] = nem_time
        return value

    def accounts(self, *variants: dict[str, Json]) -> Obj:
        template = cast(list[Obj], self.account["accounts"])[0]
        accounts: list[Json] = []
        for variant in variants:
            account = copy.deepcopy(template)
            product = cast(Obj, account["product"])
            for key, value in variant.items():
                if key == "accountNumber":
                    account[key] = value
                else:
                    product[key] = value
            accounts.append(account)
        return {"accounts": accounts}

    @staticmethod
    def points(*times: str) -> list[Json]:
        return [
            {
                "time": time,
                "region": "NSW1",
                "averageCentsPerKwh": 9.1,
                "minCentsPerKwh": 9.1,
                "maxCentsPerKwh": 9.1,
                "intervals": 1,
            }
            for time in times
        ]

    def startup(self, nem_time: str, account: Obj | None = None) -> list[Obj]:
        return [
            answer(Endpoint.ACCOUNT_DATA, body=self.account if account is None else account),
            answer(Endpoint.METERS, body=self.meters),
            answer(Endpoint.TOKENS, body=self.tokens),
            answer(Endpoint.OUTLOOK, body=self.outlook(nem_time)),
            answer(Endpoint.USAGE_HALF_HOURLY, body=[]),
            answer(Endpoint.USAGE_DAILY, body=[]),
        ]
