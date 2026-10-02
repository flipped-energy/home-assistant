from datetime import datetime

import pytest

from custom_components.flipped_energy import runtime as runtime_module
from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.runtime import InstanceRuntime, zone_names
from custom_components.flipped_energy.signals import Config, Signals, Snapshot

from .fakes import (
    Bodies,
    Harness,
    RecordingHooks,
    ScriptedApi,
    answer,
    group_differences,
    load_sequence,
    parse_time,
    replay,
    sequence_index,
    sha256_of,
    timer_key,
)


def at(text: str) -> datetime:
    return parse_time(text)


INDEX = sequence_index()


def test_ten_sequences_indexed() -> None:
    assert len(INDEX) == 10


@pytest.mark.parametrize("entry", INDEX, ids=[e["path"].removesuffix(".json") for e in INDEX])
async def test_sequence(entry: dict[str, str]) -> None:
    assert sha256_of(entry["path"]) == entry["sha256"]
    sequence = load_sequence(entry["path"])
    expected = sequence["expected"]
    assert isinstance(expected, dict)
    result = await replay(sequence)
    assert result.problems == []
    want_requests = [
        {"sendAt": r["sendAt"], "path": r["path"], "query": r["query"]}
        for r in expected["requests"]
    ]
    assert result.transcript == want_requests
    assert result.unused == []
    assert sorted(map(timer_key, result.timers)) == sorted(map(timer_key, expected["timers"]))
    assert group_differences(expected["groups"], result.signals) == []
    assert result.stored == expected["storedAccountNumber"]


UNAUTHORIZED = '{"error":"unauthorized","message":"Token is invalid, expired or revoked."}'
LOCKED_OUT = '{"success":false,"message":"User 4711 is locked out."}'
DISABLED = '{"error":"developer_mode_disabled","message":"APIs and MCPs are turned off."}'


async def test_gateway_401_hooks_once_and_no_further_request() -> None:
    sequence = load_sequence("price-loop-basics.json")
    result = await replay(sequence)
    assert result.hooks.calls == [("auth_refused", 401, UNAUTHORIZED)]
    bodies = Bodies()
    harness = Harness(at("2026-10-01T02:21:10Z"))
    api = ScriptedApi(
        harness.scheduler,
        [*bodies.startup("2026-10-01T12:20:00+10:00"), answer(Endpoint.WAIT, 401, UNAUTHORIZED)],
        strict=False,
    )
    hooks = RecordingHooks()
    try:
        runtime = harness.launch(bodies.settings(), api, hooks)
        await harness.scheduler.advance_to(at("2026-10-04T03:00:00Z"))
        assert api.problems == []
        assert len(api.transcript) == 7
        assert hooks.calls == [("auth_refused", 401, UNAUTHORIZED)]
        assert runtime.halted
        after = [t for t in harness.scheduler.timers if t["armedAt"] >= "2026-10-01T02:25:00Z"]
        assert {t["timer"] for t in after} == {"evaluation"}
        assert runtime.signals["price"]["fault"] == {
            "code": "http_error",
            "httpStatus": 401,
            "body": UNAUTHORIZED,
            "bodyBytes": len(UNAUTHORIZED),
        }
    finally:
        await harness.close()


def refused_then_restored(status: int, body: str) -> tuple[Harness, ScriptedApi, Bodies]:
    bodies = Bodies()
    harness = Harness(at("2026-10-01T13:51:10Z"))
    restart = [
        answer(Endpoint.ACCOUNT_DATA, body=bodies.account),
        answer(Endpoint.METERS, body=bodies.meters),
        answer(Endpoint.TOKENS, body=bodies.tokens),
        answer(Endpoint.OUTLOOK, body=bodies.outlook("2026-10-03T00:00:00+10:00")),
        answer(Endpoint.USAGE_HALF_HOURLY, body=[]),
        answer(Endpoint.USAGE_DAILY, body=[]),
    ]
    api = ScriptedApi(
        harness.scheduler,
        [
            *bodies.startup("2026-10-01T23:50:00+10:00"),
            answer(Endpoint.WAIT, status, body),
            answer(Endpoint.ACCOUNT_DATA, status, body),
            *restart,
        ],
    )
    return harness, api, bodies


async def check_daily_probe(status: int, body: str, hooks: RecordingHooks) -> ScriptedApi:
    harness, api, bodies = refused_then_restored(status, body)
    try:
        runtime = harness.launch(bodies.settings(), api, hooks)
        await harness.scheduler.advance_to(at("2026-10-02T14:02:00Z"))
        assert api.problems == []
        assert api.remaining == []
        assert api.sent(Endpoint.ACCOUNT_DATA) == [
            "2026-10-01T13:51:10Z",
            "2026-10-01T14:01:00Z",
            "2026-10-02T14:01:00Z",
        ]
        assert [r["sendAt"] for r in api.transcript[6:]] == [
            "2026-10-01T13:55:00Z",
            "2026-10-01T14:01:00Z",
            *["2026-10-02T14:01:00Z"] * 6,
        ]
        assert [r["path"] for r in api.transcript[-6:]] == [
            Endpoint.ACCOUNT_DATA.value,
            Endpoint.METERS.value,
            Endpoint.TOKENS.value,
            Endpoint.OUTLOOK.value,
            Endpoint.USAGE_HALF_HOURLY.value,
            Endpoint.USAGE_DAILY.value,
        ]
        assert runtime.refusal is None
        assert runtime.loop is not None and runtime.loop.running
        assert runtime.signals["price"]["status"] == "ok"
        assert any(
            t["timer"] == "usageSync" and t["armedAt"] == "2026-10-02T02:01:00Z"
            for t in harness.scheduler.timers
        )
        return api
    finally:
        await harness.close()


async def test_non_gateway_401_one_e1_per_account_sync() -> None:
    hooks = RecordingHooks()
    await check_daily_probe(401, LOCKED_OUT, hooks)
    assert hooks.calls == [
        ("auth_refused", 401, LOCKED_OUT),
        ("auth_refused", 401, LOCKED_OUT),
        ("auth_restored",),
    ]


async def test_non_gateway_401_sequence_hooks() -> None:
    result = await replay(load_sequence("non-gateway-401-account-sync.json"))
    assert result.hooks.calls == [("auth_refused", 401, LOCKED_OUT), ("auth_restored",)]


async def test_403_repeated_daily_with_the_first_since() -> None:
    hooks = RecordingHooks()
    await check_daily_probe(403, DISABLED, hooks)
    assert hooks.calls == [
        ("access_refused", 403, DISABLED, "2026-10-01T13:55:00Z"),
        ("access_refused", 403, DISABLED, "2026-10-01T13:55:00Z"),
        ("access_restored",),
    ]


async def test_403_at_startup_retries_at_utc_0001() -> None:
    bodies = Bodies()
    harness = Harness(at("2026-10-01T02:21:10Z"))
    api = ScriptedApi(
        harness.scheduler,
        [
            answer(Endpoint.ACCOUNT_DATA, 403, DISABLED),
            *bodies.startup("2026-10-02T10:00:00+10:00"),
        ],
    )
    hooks = RecordingHooks()
    try:
        runtime = harness.launch(bodies.settings(), api, hooks)
        await harness.scheduler.advance_to(at("2026-10-02T00:02:00Z"))
        assert api.problems == [] and api.remaining == []
        assert api.sent(Endpoint.ACCOUNT_DATA) == ["2026-10-01T02:21:10Z", "2026-10-02T00:01:00Z"]
        assert {
            "armedAt": "2026-10-01T02:21:10Z",
            "timer": "startup",
            "target": "2026-10-02T00:01:00Z",
            "delaySeconds": 77990,
        } in harness.scheduler.timers
        assert hooks.calls == [
            ("access_refused", 403, DISABLED, "2026-10-01T02:21:10Z"),
            ("access_restored",),
        ]
        assert runtime.signals["account"]["status"] == "ok"
    finally:
        await harness.close()


async def test_zones_loaded_before_each_computation(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = Bodies()
    account = bodies.accounts(
        {},
        {"accountNumber": "36200000000002", "timeZone": "Australia/Perth"},
        {"accountNumber": "36200000000003", "timeZone": "Mars/Olympus_Mons"},
    )
    harness = Harness(at("2026-10-01T02:21:10Z"))
    api = ScriptedApi(
        harness.scheduler,
        [
            *bodies.startup("2026-10-01T12:20:00+10:00", account),
            answer(Endpoint.ACCOUNT_DATA, body=account),
        ],
        strict=False,
    )
    holder: list[InstanceRuntime] = []
    checked: list[list[str]] = []
    real = runtime_module.compute_signals

    def checking(
        instant: datetime | None, config: Config, account: Snapshot, *rest: Snapshot
    ) -> Signals:
        if account["body"] is not None:
            names = zone_names(account["body"])
            for name in names:
                assert name in harness.zones.awaited
                assert name in holder[0].zones or name == "Mars/Olympus_Mons"
            checked.append(names)
        return real(instant, config, account, *rest)

    monkeypatch.setattr(runtime_module, "compute_signals", checking)
    try:
        holder.append(harness.launch(bodies.settings(), api, RecordingHooks()))
        await harness.scheduler.advance_to(at("2026-10-01T02:22:00Z"))
        runtime = holder[0]
        assert api.problems == []
        assert checked and all(len(names) == 3 for names in checked)
        assert harness.zones.awaited == ["Australia/Sydney", "Australia/Perth", "Mars/Olympus_Mons"]
        assert sorted(runtime.zones) == ["Australia/Perth", "Australia/Sydney"]
        assert runtime.signals["account"]["status"] == "ok"
        assert runtime.signals["energy"]["fault"]["code"] == "usage_timezone_ambiguous"
    finally:
        await harness.close()


@pytest.mark.parametrize(
    ("zone", "code", "message"),
    [
        (None, "timezone_missing", "product.timeZone is null or absent"),
        ("", "timezone_unsupported", "ZoneInfo keys must be normalized relative paths, got: "),
    ],
)
async def test_price_loop_runs_without_a_usable_zone(
    zone: str | None, code: str, message: str
) -> None:
    bodies = Bodies()
    account = bodies.accounts({"timeZone": zone})
    harness = Harness(at("2026-10-01T02:21:10Z"))
    api = ScriptedApi(harness.scheduler, bodies.startup("2026-10-01T12:20:00+10:00", account)[:4])
    try:
        runtime = harness.launch(bodies.settings(), api, RecordingHooks())
        await harness.scheduler.advance_to(at("2026-10-01T02:22:00Z"))
        assert api.problems == [] and api.remaining == []
        assert runtime.loop is not None and runtime.loop.running
        assert runtime.signals["price"]["status"] == "ok"
        for group in ("tariff", "energy"):
            fault = runtime.signals[group]["fault"]
            assert fault is not None and fault["code"] == code
            assert message in str(fault.get("message"))
        assert {
            "armedAt": "2026-10-01T02:21:10Z",
            "timer": "startup",
            "target": "2026-10-02T00:01:00Z",
            "delaySeconds": 77990,
        } in harness.scheduler.timers
    finally:
        await harness.close()


NOT_FOUND = '{"error":"unknown_operation","message":"GET /tokens is not an operation."}'
BAD_REQUEST = "region is required"


async def test_400_and_404_block_only_their_own_request() -> None:
    bodies = Bodies()
    startup = bodies.startup("2026-10-01T12:20:00+10:00")
    startup[2] = answer(Endpoint.TOKENS, 404, NOT_FOUND)
    startup[3] = answer(Endpoint.OUTLOOK, 400, BAD_REQUEST)
    harness = Harness(at("2026-10-01T02:21:10Z"))
    api = ScriptedApi(
        harness.scheduler,
        [
            *startup,
            answer(Endpoint.ACCOUNT_DATA, body=bodies.account),
            answer(Endpoint.METERS, body=bodies.meters),
            answer(Endpoint.USAGE_HALF_HOURLY, body=[]),
            answer(Endpoint.USAGE_DAILY, body=[]),
            answer(Endpoint.USAGE_HALF_HOURLY, body=[]),
            answer(Endpoint.USAGE_DAILY, body=[]),
        ],
    )
    try:
        runtime = harness.launch(bodies.settings(), api, RecordingHooks())
        await harness.scheduler.advance_to(at("2026-10-02T02:02:00Z"))
        assert api.problems == [] and api.remaining == []
        assert not runtime.halted
        assert api.sent(Endpoint.ACCOUNT_DATA) == ["2026-10-01T02:21:10Z", "2026-10-01T14:01:00Z"]
        assert api.sent(Endpoint.TOKENS) == ["2026-10-01T02:21:10Z"]
        assert api.sent(Endpoint.OUTLOOK) == ["2026-10-01T02:21:10Z"]
        assert api.sent(Endpoint.USAGE_DAILY) == [
            "2026-10-01T02:21:10Z",
            "2026-10-01T14:01:00Z",
            "2026-10-02T02:01:00Z",
        ]
        assert runtime.signals["tariff"]["status"] == "ok"
        assert runtime.signals["price"]["fault"] == {
            "code": "http_error",
            "httpStatus": 400,
            "body": BAD_REQUEST,
            "bodyBytes": len(BAD_REQUEST),
        }
    finally:
        await harness.close()
