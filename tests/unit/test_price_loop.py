from datetime import UTC, datetime, timedelta

import pytest

from custom_components.flipped_energy.api import BodyInvalid, Endpoint
from custom_components.flipped_energy.price_loop import (
    LoopState,
    nem_instant,
    nem_since,
    newest_after,
)
from custom_components.flipped_energy.timers import (
    TimerKind,
    arm_at,
    boundary,
    next_boundary,
    next_utc_time,
)

from .fakes import Bodies, FakeScheduler, Harness, RecordingHooks, ScriptedApi, answer, parse_time

UNAUTHORIZED = '{"error":"unauthorized","message":"Token is invalid, expired or revoked."}'


def at(text: str) -> datetime:
    return parse_time(text)


def test_boundaries() -> None:
    assert boundary(at("2026-10-01T02:24:59Z")) == at("2026-10-01T02:20:00Z")
    assert boundary(at("2026-10-01T02:25:00Z")) == at("2026-10-01T02:25:00Z")
    assert next_boundary(at("2026-10-01T02:25:00Z")) == at("2026-10-01T02:30:00Z")
    assert next_boundary(at("2026-10-01T23:58:30Z")) == at("2026-10-02T00:00:00Z")
    assert next_utc_time(at("2026-10-01T00:01:00Z"), "00:01:00") == at("2026-10-02T00:01:00Z")
    assert next_utc_time(at("2026-10-01T00:00:59Z"), "00:01:00") == at("2026-10-01T00:01:00Z")


def test_nem_time() -> None:
    assert nem_since(at("2026-10-01T02:25:00Z")) == "2026-10-01T12:25:00"
    assert nem_since(at("2026-10-01T14:00:00Z")) == "2026-10-02T00:00:00"
    assert nem_instant("2026-10-02T00:00:00+10:00", "p") == at("2026-10-01T14:00:00Z")
    with pytest.raises(BodyInvalid):
        nem_instant("2026-10-01T02:25:00Z", "p")
    with pytest.raises(BodyInvalid):
        nem_instant("2026-10-01T12:25:00+11:00", "p")


def test_newest_after() -> None:
    held = at("2026-10-01T02:25:00Z")
    body = Bodies.points(
        "2026-10-01T12:25:00+10:00", "2026-10-01T12:35:00+10:00", "2026-10-01T12:30:00+10:00"
    )
    assert newest_after(body, held) == at("2026-10-01T02:35:00Z")
    assert newest_after(Bodies.points("2026-10-01T12:25:00+10:00"), held) is None
    assert newest_after([], held) is None
    with pytest.raises(BodyInvalid):
        newest_after([{"region": "NSW1"}], held)
    with pytest.raises(BodyInvalid):
        newest_after({"time": "2026-10-01T12:30:00+10:00"}, held)


async def test_arm_at_caps_and_rearms() -> None:
    scheduler = FakeScheduler(at("2026-10-01T00:00:00Z"))
    fired: list[datetime] = []
    arm_at(
        scheduler,
        TimerKind.EVALUATION,
        at("2026-10-02T06:00:00Z"),
        lambda: fired.append(scheduler.now()),
    )
    await scheduler.advance_to(at("2026-10-02T05:59:59Z"))
    assert fired == []
    await scheduler.advance_to(at("2026-10-03T00:00:00Z"))
    assert fired == [at("2026-10-02T06:00:00Z")]
    assert [(t["armedAt"], t["delaySeconds"]) for t in scheduler.timers] == [
        ("2026-10-01T00:00:00Z", 86400),
        ("2026-10-02T00:00:00Z", 21600),
    ]


async def test_arm_at_early_wake_rearms_and_cancel_stops() -> None:
    scheduler = FakeScheduler(at("2026-10-01T00:00:00Z"))
    fired: list[datetime] = []
    target = at("2026-10-01T00:10:00Z")
    cancel = arm_at(scheduler, TimerKind.PRICE, target, lambda: fired.append(scheduler.now()))
    early = scheduler._entries[0]
    early.target = at("2026-10-01T00:05:00Z")
    await scheduler.advance_to(at("2026-10-01T00:06:00Z"))
    assert fired == []
    assert scheduler.timers[-1]["armedAt"] == "2026-10-01T00:05:00Z"
    cancel()
    await scheduler.advance_to(at("2026-10-01T01:00:00Z"))
    assert fired == []


async def test_shared_loop_for_two_instances() -> None:
    bodies = Bodies()
    harness = Harness(at("2026-10-01T02:21:10Z"))
    first = ScriptedApi(
        harness.scheduler,
        [
            *bodies.startup("2026-10-01T12:20:00+10:00"),
            answer(
                Endpoint.WAIT,
                body=Bodies.points("2026-10-01T12:25:00+10:00"),
                query={"region": "Ausgrid", "since": "2026-10-01T12:20:00", "timeoutSeconds": "55"},
                hold=25,
            ),
            answer(Endpoint.OUTLOOK, body=bodies.outlook("2026-10-01T12:25:00+10:00")),
        ],
        strict=False,
    )
    second_script = [r for r in bodies.startup("") if r["path"] != Endpoint.OUTLOOK.value]
    second = ScriptedApi(harness.scheduler, second_script, strict=False)
    try:
        a = harness.launch(bodies.settings(), first, RecordingHooks())
        await harness.scheduler.advance(0)
        b = harness.launch(bodies.settings(), second, RecordingHooks())
        await harness.scheduler.advance_to(at("2026-10-01T02:26:00Z"))
        assert first.problems == second.problems == []
        assert [r["path"] for r in second.transcript] == [
            Endpoint.ACCOUNT_DATA.value,
            Endpoint.METERS.value,
            Endpoint.TOKENS.value,
            Endpoint.USAGE_HALF_HOURLY.value,
            Endpoint.USAGE_DAILY.value,
        ]
        assert first.sent(Endpoint.OUTLOOK) == ["2026-10-01T02:21:10Z", "2026-10-01T02:25:25Z"]
        assert first.sent(Endpoint.WAIT) == ["2026-10-01T02:25:00Z"]
        assert a.loop is b.loop
        assert a.loop is not None and a.loop.subscriber_count == 2
        for runtime in (a, b):
            assert runtime.signals["price"]["status"] == "ok"
            assert runtime.signals["price"]["intervalStart"] == "2026-10-01T02:25:00Z"
        await harness.stop(a)
        assert b.loop is not None and b.loop.subscriber_count == 1 and b.loop.running
        await harness.stop(b)
        assert harness.loops.get(bodies.settings().token, "Ausgrid") is None
    finally:
        await harness.close()


async def test_sync_during_hold_waits_for_the_hold() -> None:
    bodies = Bodies()
    harness = Harness(at("2026-10-01T13:58:30Z"))
    api = ScriptedApi(
        harness.scheduler,
        [
            *bodies.startup("2026-10-01T23:55:00+10:00"),
            answer(Endpoint.WAIT, 204, hold=55),
            answer(Endpoint.WAIT, body=Bodies.points("2026-10-02T00:00:00+10:00"), hold=50),
            answer(Endpoint.OUTLOOK, body=bodies.outlook("2026-10-02T00:00:00+10:00")),
            *[r for r in bodies.startup("") if r["path"] != Endpoint.OUTLOOK.value],
        ],
        strict=False,
    )
    try:
        harness.launch(bodies.settings(), api, RecordingHooks())
        await harness.scheduler.advance_to(at("2026-10-01T14:03:00Z"))
        assert api.problems == []
        assert api.sent(Endpoint.WAIT) == ["2026-10-01T14:00:00Z", "2026-10-01T14:00:55Z"]
        assert api.sent(Endpoint.ACCOUNT_DATA) == ["2026-10-01T13:58:30Z", "2026-10-01T14:01:45Z"]
        assert api.sent_while_busy == []
        timers = harness.scheduler.timers
        assert {
            "armedAt": "2026-10-01T13:58:30Z",
            "timer": "accountSync",
            "target": "2026-10-01T14:01:00Z",
            "delaySeconds": 150,
        } in timers
        assert any(
            t["timer"] == "accountSync" and t["armedAt"] == "2026-10-01T14:01:00Z" for t in timers
        )
        assert api.sent(Endpoint.USAGE_DAILY)[-1] == "2026-10-01T14:01:45Z"
    finally:
        await harness.close()


def two_region_harness(
    first_wait: list[dict[str, object]], second_wait: list[dict[str, object]]
) -> tuple[Harness, ScriptedApi, ScriptedApi, Bodies]:
    bodies = Bodies()
    harness = Harness(at("2026-10-01T13:58:30Z"))
    energex = bodies.accounts({"gridType": "Energex", "timeZone": "Australia/Brisbane"})
    first = ScriptedApi(
        harness.scheduler, [*bodies.startup("2026-10-01T23:55:00+10:00"), *first_wait], strict=False
    )
    second = ScriptedApi(
        harness.scheduler,
        [*bodies.startup("2026-10-01T23:55:00+10:00", energex), *second_wait],
        strict=False,
    )
    return harness, first, second, bodies


async def test_two_regions_one_token_hold_together() -> None:
    point = Bodies.points("2026-10-02T00:00:00+10:00")
    harness, first, second, bodies = two_region_harness(
        [answer(Endpoint.WAIT, body=point, hold=50), answer(Endpoint.OUTLOOK, body=None)],
        [answer(Endpoint.WAIT, body=point, hold=50), answer(Endpoint.OUTLOOK, body=None)],
    )
    try:
        a = harness.launch(bodies.settings(), first, RecordingHooks())
        b = harness.launch(bodies.settings(), second, RecordingHooks())
        await harness.scheduler.advance_to(at("2026-10-01T14:00:49Z"))
        assert first.in_flight == 1 and second.in_flight == 1
        await harness.scheduler.advance_to(at("2026-10-01T14:00:50Z"))
        assert first.problems == second.problems == []
        assert first.sent(Endpoint.WAIT) == ["2026-10-01T14:00:00Z"]
        assert second.sent(Endpoint.WAIT) == ["2026-10-01T14:00:00Z"]
        assert first.sent(Endpoint.OUTLOOK)[-1] == "2026-10-01T14:00:50Z"
        assert second.sent(Endpoint.OUTLOOK)[-1] == "2026-10-01T14:00:50Z"
        assert a.loop is not None and b.loop is not None and a.loop is not b.loop
        assert (a.loop.region, b.loop.region) == ("Ausgrid", "Energex")
    finally:
        await harness.close()


async def test_429_closes_the_gate_for_the_other_loop_of_the_token() -> None:
    point = Bodies.points("2026-10-02T00:00:00+10:00")
    limited = answer(
        Endpoint.WAIT,
        429,
        '{"error":"rate_limited","message":"Over 60 calls this minute."}',
        headers={"Retry-After": "30", "X-DailyLimit-Remaining": "4870"},
    )
    harness, first, second, bodies = two_region_harness(
        [limited, answer(Endpoint.WAIT, body=point, hold=20), answer(Endpoint.OUTLOOK, body=None)],
        [answer(Endpoint.WAIT, body=point, hold=20), answer(Endpoint.OUTLOOK, body=None)],
    )
    try:
        a = harness.launch(bodies.settings(), first, RecordingHooks())
        b = harness.launch(bodies.settings(), second, RecordingHooks())
        await harness.scheduler.advance_to(at("2026-10-01T14:00:59Z"))
        assert first.problems == second.problems == []
        assert first.sent(Endpoint.WAIT) == ["2026-10-01T14:00:00Z", "2026-10-01T14:00:30Z"]
        assert second.sent(Endpoint.WAIT) == ["2026-10-01T14:00:30Z"]
        assert {
            "armedAt": "2026-10-01T14:00:00Z",
            "timer": "retryAfter",
            "target": "2026-10-01T14:00:30Z",
            "delaySeconds": 30,
        } in harness.scheduler.timers
        assert a.daily_limit_remaining == b.daily_limit_remaining == "4870"
    finally:
        await harness.close()


async def test_gateway_401_stops_the_loop() -> None:
    bodies = Bodies()
    harness = Harness(at("2026-10-01T02:21:10Z"))
    api = ScriptedApi(
        harness.scheduler,
        [*bodies.startup("2026-10-01T12:20:00+10:00"), answer(Endpoint.WAIT, 401, UNAUTHORIZED)],
        strict=False,
    )
    try:
        runtime = harness.launch(bodies.settings(), api, RecordingHooks())
        await harness.scheduler.advance_to(at("2026-10-01T03:00:00Z"))
        assert runtime.loop is not None
        assert runtime.loop.state is LoopState.STOPPED and not runtime.loop.running
        assert runtime.loop.outlook["error"] == {
            "kind": "http",
            "status": 401,
            "body": UNAUTHORIZED,
            "bodyBytes": len(UNAUTHORIZED),
        }
        assert api.sent(Endpoint.WAIT) == ["2026-10-01T02:25:00Z"]
        assert harness.scheduler.now() - at("2026-10-01T02:25:00Z") == timedelta(minutes=35)
        assert harness.scheduler.now().tzinfo is UTC
    finally:
        await harness.close()
