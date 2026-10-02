import asyncio
import copy
import json
import threading
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.const import DOMAIN
from custom_components.flipped_energy.runtime import InstanceRuntime
from custom_components.flipped_energy.signals import Json
from custom_components.flipped_energy.timers import Action, Cancel

from ..unit.fakes import Bodies, FakeScheduler, Obj, ScriptedApi, answer

VECTORS = Path(__file__).resolve().parent.parent / "vectors"
TOKEN = "fdk_SEQUENCEFIXTURE000000000000000000wXyZ"
ACCOUNT_NUMBER = "36200000000001"
START = "2026-10-01T02:21:10Z"
NEM_NOW = "2026-10-01T12:20:00+10:00"
GATEWAY_401 = {"error": "unauthorized", "message": "token is unknown, expired or revoked"}


@pytest.fixture(autouse=True)
def ha_environment(recorder_mock: Recorder, enable_custom_integrations: None) -> None:
    return None


class HassClock(FakeScheduler):
    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(dt_util.utcnow())
        self._hass = hass

    def now(self) -> datetime:
        return dt_util.utcnow()

    def call_at(self, target: datetime, action: Action) -> Cancel:
        @callback
        def fire(_: datetime) -> None:
            action()

        return async_track_point_in_utc_time(self._hass, fire, target)


class ApiFactory:
    def __init__(self, hass: HomeAssistant) -> None:
        self._clock = HassClock(hass)
        self._scripts: list[list[Obj]] = []
        self.created: list[ScriptedApi] = []
        self.tokens: list[str] = []
        self.user_agents: list[str] = []

    def script(self, *responses: Obj) -> None:
        self._scripts.append(list(responses))

    def __call__(self, session: object, token: str, user_agent: str) -> ScriptedApi:
        api = ScriptedApi(self._clock, self._scripts.pop(0), strict=False)
        self.created.append(api)
        self.tokens.append(token)
        self.user_agents.append(user_agent)
        return api

    @property
    def last(self) -> ScriptedApi:
        return self.created[-1]


@pytest.fixture
def apis(hass: HomeAssistant) -> Generator[ApiFactory]:
    factory = ApiFactory(hass)
    with patch("custom_components.flipped_energy.FlippedApi", factory):
        yield factory


def vector_input(path: str) -> Obj:
    vector = cast(Obj, json.loads((VECTORS / path).read_text("utf-8")))
    return cast(Obj, vector["input"])


def vector_body(path: str, slot: str) -> Json:
    return copy.deepcopy(cast(Obj, vector_input(path)[slot])["body"])


BODIES = Bodies()


def outlook(nem_time: str, *, tier: str = "Normal", cents: float = 9.6) -> Obj:
    body = BODIES.outlook(nem_time)
    now = cast(Obj, body["now"])
    for key in ("averageCentsPerKwh", "minCentsPerKwh", "maxCentsPerKwh"):
        now[key] = cents
    assessment = cast(Obj, body["nowAssessment"])
    assessment["tier"] = tier
    assessment["centsPerKwh"] = cents
    return body


def account_with(**fields: Json) -> Obj:
    body = copy.deepcopy(BODIES.account)
    cast(list[Obj], body["accounts"])[0].update(fields)
    return body


def startup(
    *,
    account: Json = None,
    meters: Json = None,
    tokens: Json = None,
    outlook: Json = None,
    half_hourly: Json = None,
    daily: Json = None,
) -> list[Obj]:
    return [
        answer(
            Endpoint.ACCOUNT_DATA,
            body=copy.deepcopy(BODIES.account) if account is None else account,
        ),
        answer(Endpoint.METERS, body=BODIES.meters if meters is None else meters),
        answer(Endpoint.TOKENS, body=BODIES.tokens if tokens is None else tokens),
        answer(Endpoint.OUTLOOK, body=BODIES.outlook(NEM_NOW) if outlook is None else outlook),
        answer(Endpoint.USAGE_HALF_HOURLY, body=[] if half_hourly is None else half_hourly),
        answer(Endpoint.USAGE_DAILY, body=[] if daily is None else daily),
    ]


def make_entry(
    *,
    nmi: str | None = None,
    options: dict[str, float] | None = None,
    title: str = "Flipped Energy",
) -> MockConfigEntry:
    data: dict[str, str] = {"token": TOKEN, "accountNumber": ACCOUNT_NUMBER}
    unique_id = ACCOUNT_NUMBER
    if nmi is not None:
        data["nmi"] = nmi
        unique_id = f"{ACCOUNT_NUMBER}:{nmi}"
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data=data,
        options={"spotPrices": True, **(options or {})},
        unique_id=unique_id,
        version=1,
        minor_version=1,
    )


async def wait_until(runtime: InstanceRuntime, done: Callable[[], bool]) -> None:
    if done():
        return
    reached: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def check() -> None:
        if done() and not reached.done():
            reached.set_result(None)

    remove = runtime.add_listener(check)
    stop = threading.Event()
    watchdog = asyncio.get_running_loop().run_in_executor(None, stop.wait, 10)
    try:
        await asyncio.wait({reached, watchdog}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        remove()
        stop.set()
        await watchdog
    if not reached.done():
        raise AssertionError("the runtime did not reach the expected state within 10 s")


async def setup_entry(
    hass: HomeAssistant, apis: ApiFactory, entry: MockConfigEntry, responses: list[Obj]
) -> ScriptedApi:
    entry.add_to_hass(hass)
    apis.script(*responses)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    api = apis.last
    try:
        await wait_until(entry.runtime_data, lambda: not api.remaining)
    except AssertionError as exc:
        unanswered = [entry["path"] for entry in api.remaining]
        raise AssertionError(f"{exc}; unanswered {unanswered}; {api.problems}") from exc
    await hass.async_block_till_done()
    return api


async def move_to(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: str) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(HomeAssistantSnapshotExtension)
