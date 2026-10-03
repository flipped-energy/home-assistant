import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import aiohttp
from multidict import CIMultiDictProxy

from .signals.constants import BASE_URL, HTTP_TIMEOUT_S, WAIT_HTTP_TIMEOUT_S, WAIT_TIMEOUT_S
from .signals.model import HttpError, Json

_LOGGER = logging.getLogger(__package__)


class Endpoint(StrEnum):
    ACCOUNT_DATA = "/api/MyAccount/GetAccountData"
    OUTLOOK = "/api/Live/nempricing/outlook"
    WAIT = "/api/Live/nempricing/wait"
    USAGE_HALF_HOURLY = "/api/Usage/usage/projectreads/halfhourly"
    USAGE_DAILY = "/api/Usage/usage/projectreads/daily"
    METERS = "/api/Billing/meters"
    TOKENS = "/tokens"


ARRAY_ENDPOINTS: Final = frozenset(
    {Endpoint.WAIT, Endpoint.USAGE_HALF_HOURLY, Endpoint.USAGE_DAILY}
)

LIMIT_HEADERS: Final = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "X-DailyLimit-Limit",
    "X-DailyLimit-Remaining",
)

type Schema = None | dict[str, Schema] | list[Schema]

UNIT_SCHEMA: Final[Schema] = {
    key: None
    for key in (
        "billingUnitId",
        "name",
        "billingUnitType",
        "chargePerKwhIncludingGst",
        "timeOfDayStartMinutes",
        "timeOfDayEndMinutes",
        "kwhStart",
        "kwhEnd",
        "capPerKwhIncludingGst",
    )
}
PLAN_SCHEMA: Final[Schema] = {
    "planId": None,
    "code": None,
    "start": None,
    "end": None,
    "billingUnits": [UNIT_SCHEMA],
}
ACCOUNT_SCHEMA: Final[Schema] = {
    "accounts": [
        {
            "accountNumber": None,
            "siteAddress": None,
            "accountState": None,
            "productName": None,
            "product": {
                "gridType": None,
                "timeZone": None,
                "currentPlan": PLAN_SCHEMA,
                "upcomingPlan": PLAN_SCHEMA,
            },
        }
    ]
}


class ApiNetworkError(Exception):
    pass


class BodyInvalid(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ApiResponse:
    endpoint: Endpoint
    status: int
    headers: CIMultiDictProxy[str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str) -> str | None:
        return self.headers.get(name)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def http_error(self) -> HttpError:
        return {
            "kind": "http",
            "status": self.status,
            "body": self.text(),
            "bodyBytes": len(self.body),
        }

    def json_body(self) -> Json:
        try:
            value: Json = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BodyInvalid(
                f"{self.endpoint.value} answered {self.status}: {type(exc).__name__}: {exc}"
            ) from exc
        expected = "array" if self.endpoint in ARRAY_ENDPOINTS else "object"
        actual = json_kind(value)
        if actual != expected:
            raise BodyInvalid(
                f"{self.endpoint.value} answered {self.status} with a JSON {actual}, "
                f"expected an {expected}"
            )
        if self.endpoint is Endpoint.ACCOUNT_DATA:
            return project(value, ACCOUNT_SCHEMA)
        return value


def json_kind(value: Json) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    return "number"


def project(value: Json, schema: Schema) -> Json:
    if schema is None:
        return value
    if isinstance(schema, list):
        if not isinstance(value, list):
            return value
        return [project(item, schema[0]) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: project(value[key], sub) for key, sub in schema.items() if key in value}


class FlippedApi:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        user_agent: str,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "*/*",
            "User-Agent": user_agent,
        }

    async def account_data(self) -> ApiResponse:
        return await self._get(Endpoint.ACCOUNT_DATA, {}, HTTP_TIMEOUT_S)

    async def outlook(self, region: str) -> ApiResponse:
        return await self._get(Endpoint.OUTLOOK, {"region": region}, HTTP_TIMEOUT_S)

    async def wait(self, region: str, since: str) -> ApiResponse:
        query = {"region": region, "since": since, "timeoutSeconds": str(WAIT_TIMEOUT_S)}
        return await self._get(Endpoint.WAIT, query, WAIT_HTTP_TIMEOUT_S)

    async def usage_half_hourly(self, start: str, end: str, nmi: str) -> ApiResponse:
        query = {"start": start, "end": end, "nmi": nmi}
        return await self._get(Endpoint.USAGE_HALF_HOURLY, query, HTTP_TIMEOUT_S)

    async def usage_daily(self, start: str, end: str, nmi: str) -> ApiResponse:
        query = {"start": start, "end": end, "nmi": nmi}
        return await self._get(Endpoint.USAGE_DAILY, query, HTTP_TIMEOUT_S)

    async def meters(self) -> ApiResponse:
        return await self._get(Endpoint.METERS, {}, HTTP_TIMEOUT_S)

    async def tokens(self) -> ApiResponse:
        return await self._get(Endpoint.TOKENS, {}, HTTP_TIMEOUT_S)

    async def _get(
        self, endpoint: Endpoint, query: Mapping[str, str], timeout_s: float
    ) -> ApiResponse:
        try:
            async with self._session.get(
                f"{self._base_url}{endpoint.value}",
                params=query,
                headers=self._headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                body = await response.read()
                result = ApiResponse(endpoint, response.status, response.headers, body)
        except (aiohttp.ClientError, TimeoutError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            _LOGGER.error("GET %s %s: %s", endpoint.value, dict(query), message)
            raise ApiNetworkError(message) from exc
        if not result.ok:
            location = result.header("Location")
            if location is None:
                _LOGGER.error(
                    "GET %s %s answered %s: %s",
                    endpoint.value,
                    dict(query),
                    result.status,
                    result.text(),
                )
            else:
                _LOGGER.error(
                    "GET %s %s answered %s with Location %s: %s",
                    endpoint.value,
                    dict(query),
                    result.status,
                    location,
                    result.text(),
                )
        return result
