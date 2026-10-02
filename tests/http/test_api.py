import json
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict, CIMultiDictProxy

from custom_components.flipped_energy import api
from custom_components.flipped_energy.api import (
    LIMIT_HEADERS,
    ApiNetworkError,
    ApiResponse,
    BodyInvalid,
    Endpoint,
    FlippedApi,
)

from .fake_server import PREFIX, FakeServer, Scripted

TOKEN = "fdk_HTTPTESTFIXTURE0000000000000000000wXyZ"
USER_AGENT = "flipped-energy-home-assistant/0.1.0"
type ServerFactory = Callable[[aiohttp.web.Application], Awaitable[TestServer]]

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.fixture
async def fake(aiohttp_server: ServerFactory) -> AsyncIterator[tuple[FakeServer, FlippedApi]]:
    server = FakeServer()
    test_server = await aiohttp_server(server.app)
    base = str(test_server.make_url(PREFIX))
    async with aiohttp.ClientSession() as session:
        yield server, FlippedApi(session, TOKEN, USER_AGENT, base_url=base)


def ok_json(value: object) -> Scripted:
    return Scripted(200, json.dumps(value).encode(), {"Content-Type": "application/json"})


async def test_headers_sent(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    server.script(f"{PREFIX}{Endpoint.ACCOUNT_DATA.value}", ok_json({"accounts": []}))
    response = await client.account_data()
    assert response.status == 200
    headers = {key.lower(): value for key, value in server.received[0].headers.items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["accept"] == "*/*"
    assert headers["user-agent"] == USER_AGENT
    assert "origin" not in headers
    assert server.received[0].query == {}


async def test_paths_and_queries(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    for endpoint in Endpoint:
        body = [] if endpoint in api.ARRAY_ENDPOINTS else {}
        server.script(f"{PREFIX}{endpoint.value}", ok_json(body))
    await client.account_data()
    await client.meters()
    await client.tokens()
    await client.outlook("Ausgrid")
    await client.wait("Ausgrid", "2026-10-01T12:25:00")
    await client.usage_half_hourly("2026-09-24T00:00:00", "2026-10-02T00:00:00", "4102000000")
    await client.usage_daily("2026-09-24T00:00:00", "2026-10-02T00:00:00", "4102000000")
    seen = [(r.path.removeprefix(PREFIX), r.query) for r in server.received]
    window = {"start": "2026-09-24T00:00:00", "end": "2026-10-02T00:00:00", "nmi": "4102000000"}
    assert seen == [
        (Endpoint.ACCOUNT_DATA.value, {}),
        (Endpoint.METERS.value, {}),
        (Endpoint.TOKENS.value, {}),
        (Endpoint.OUTLOOK.value, {"region": "Ausgrid"}),
        (
            Endpoint.WAIT.value,
            {"region": "Ausgrid", "since": "2026-10-01T12:25:00", "timeoutSeconds": "55"},
        ),
        (Endpoint.USAGE_HALF_HOURLY.value, window),
        (Endpoint.USAGE_DAILY.value, window),
    ]


async def test_redirect_not_followed(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    target = f"{PREFIX}/elsewhere"
    server.script(
        f"{PREFIX}{Endpoint.OUTLOOK.value}",
        Scripted(302, b"", {"Location": str(target)}),
    )
    server.script(target, ok_json({"leak": True}))
    response = await client.outlook("Ausgrid")
    assert response.status == 302
    assert response.header("Location") == target
    assert response.body == b""
    assert [r.path for r in server.received] == [f"{PREFIX}{Endpoint.OUTLOOK.value}"]
    assert response.http_error() == {"kind": "http", "status": 302, "body": "", "bodyBytes": 0}


async def test_text_error_body_verbatim(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    body = (
        "Live price feed is Failed: System.Net.WebSockets.WebSocketException (0x80004005): "
        "The remote party closed the WebSocket connection\r\n"
        "   at Flipped.Home.Services.NemPriceFeed.ListenAsync(CancellationToken ct)\n"
        "   at Flipped.Home.Services.NemPriceFeed.RunAsync() — end\n"
    ).encode()
    server.script(
        f"{PREFIX}{Endpoint.WAIT.value}",
        Scripted(503, body, {"Content-Type": "text/plain; charset=utf-8"}),
    )
    response = await client.wait("Ausgrid", "2026-10-01T12:25:00")
    assert response.status == 503
    assert response.body == body
    assert response.http_error() == {
        "kind": "http",
        "status": 503,
        "body": body.decode("utf-8"),
        "bodyBytes": len(body),
    }


async def test_json_error_body_verbatim(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    body = b'{"error":"unauthorized","message":"Token is invalid, expired or revoked."}'
    server.script(
        f"{PREFIX}{Endpoint.ACCOUNT_DATA.value}",
        Scripted(401, body, {"Content-Type": "application/json; charset=utf-8"}),
    )
    response = await client.account_data()
    assert response.status == 401
    assert response.text() == body.decode()
    assert response.http_error()["bodyBytes"] == 74


async def test_no_content(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    server.script(f"{PREFIX}{Endpoint.WAIT.value}", Scripted(204, b""))
    response = await client.wait("Ausgrid", "2026-10-01T12:25:00")
    assert response.status == 204
    assert response.ok
    assert response.body == b""


async def test_rate_limit_headers(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    headers = {
        "Retry-After": "17",
        "X-RateLimit-Limit": "60",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "17",
        "X-DailyLimit-Limit": "5000",
        "X-DailyLimit-Remaining": "4870",
    }
    body = b'{"error":"rate_limited","message":"Over 60 calls this minute."}'
    server.script(f"{PREFIX}{Endpoint.METERS.value}", Scripted(429, body, headers))
    response = await client.meters()
    assert response.status == 429
    assert response.header("retry-after") == "17"
    assert {name: response.header(name) for name in LIMIT_HEADERS} == {
        name: headers[name] for name in LIMIT_HEADERS
    }
    assert response.text() == body.decode()


async def test_limit_headers_on_success(fake: tuple[FakeServer, FlippedApi]) -> None:
    server, client = fake
    server.script(
        f"{PREFIX}{Endpoint.TOKENS.value}",
        Scripted(200, b'{"tokens":[]}', {"X-DailyLimit-Remaining": "4999"}),
    )
    response = await client.tokens()
    assert response.header("X-DailyLimit-Remaining") == "4999"
    assert response.header("X-RateLimit-Remaining") is None
    assert response.json_body() == {"tokens": []}


async def test_timeout_raises_raw_text(
    fake: tuple[FakeServer, FlippedApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, client = fake
    monkeypatch.setattr(api, "HTTP_TIMEOUT_S", 0.2)
    server.script(f"{PREFIX}{Endpoint.METERS.value}", Scripted(200, b"{}", delay_s=2))
    with pytest.raises(ApiNetworkError) as caught:
        await client.meters()
    cause = caught.value.__cause__
    assert isinstance(cause, TimeoutError)
    assert str(caught.value) == f"{type(cause).__name__}: {cause}"


async def test_connection_refused_raises_raw_text(unused_tcp_port: int) -> None:
    async with aiohttp.ClientSession() as session:
        client = FlippedApi(
            session, TOKEN, USER_AGENT, base_url=f"http://127.0.0.1:{unused_tcp_port}"
        )
        with pytest.raises(ApiNetworkError) as caught:
            await client.tokens()
    cause = caught.value.__cause__
    assert isinstance(cause, aiohttp.ClientError)
    assert str(caught.value) == f"{type(cause).__name__}: {cause}"


def response(endpoint: Endpoint, body: bytes) -> ApiResponse:
    return ApiResponse(endpoint, 200, CIMultiDictProxy(CIMultiDict()), body)


def test_body_not_json_is_invalid() -> None:
    with pytest.raises(BodyInvalid) as caught:
        response(Endpoint.OUTLOOK, b"<html>busy</html>").json_body()
    assert str(caught.value).startswith(
        "/api/Live/nempricing/outlook answered 200: JSONDecodeError"
    )


def test_body_of_wrong_top_level_type_is_invalid() -> None:
    with pytest.raises(BodyInvalid) as caught:
        response(Endpoint.WAIT, b"{}").json_body()
    assert str(caught.value) == (
        "/api/Live/nempricing/wait answered 200 with a JSON object, expected an array"
    )
    with pytest.raises(BodyInvalid):
        response(Endpoint.ACCOUNT_DATA, b"[]").json_body()


def test_account_body_projected_at_receipt() -> None:
    unit = {
        "billingUnitId": "u1",
        "name": None,
        "billingUnitType": "FixedBillingUnit",
        "chargePerKwhIncludingGst": 0.3,
        "timeOfDayStartMinutes": 0,
        "timeOfDayEndMinutes": 0,
        "kwhStart": 0,
        "kwhEnd": None,
        "chargePerKwh": 0.27,
    }
    plan = {"planId": "p", "code": "C", "start": "2026-01-01T00:00:00", "billingUnits": [unit]}
    account = {
        "accountNumber": "1",
        "siteAddress": "1 Example St",
        "accountState": "ACTIVE",
        "productName": "Flat",
        "dateOfBirth": "1970-01-01",
        "phone": "0400000000",
        "product": {"gridType": "Ausgrid", "timeZone": None, "currentPlan": plan, "extra": 1},
    }
    body = json.dumps({"accounts": [account, "not an object"], "bpayReference": "123"})
    projected = response(Endpoint.ACCOUNT_DATA, body.encode()).json_body()
    kept_unit = {key: value for key, value in unit.items() if key != "chargePerKwh"}
    assert projected == {
        "accounts": [
            {
                "accountNumber": "1",
                "siteAddress": "1 Example St",
                "accountState": "ACTIVE",
                "productName": "Flat",
                "product": {
                    "gridType": "Ausgrid",
                    "timeZone": None,
                    "currentPlan": {
                        **{k: v for k, v in plan.items() if k != "billingUnits"},
                        "billingUnits": [kept_unit],
                    },
                },
            },
            "not an object",
        ]
    }
