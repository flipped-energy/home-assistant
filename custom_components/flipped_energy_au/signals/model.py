from typing import Literal, NotRequired, TypedDict

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Status = Literal["ok", "faulted"]


class HttpError(TypedDict):
    kind: Literal["http"]
    status: int
    body: str
    bodyBytes: int


class NetworkError(TypedDict):
    kind: Literal["network"]
    message: str


class InvalidError(TypedDict):
    kind: Literal["invalid"]
    message: str


type SnapshotError = HttpError | NetworkError | InvalidError


class Snapshot(TypedDict):
    fetchedAt: str | None
    error: SnapshotError | None
    body: Json


class Config(TypedDict):
    accountNumber: str | None
    nmi: str | None
    tokenPreview: str | None
    priceHighThresholdCentsPerKwh: float | None
    priceLowThresholdCentsPerKwh: float | None


class Fault(TypedDict):
    code: str
    httpStatus: NotRequired[int]
    body: NotRequired[str]
    bodyBytes: NotRequired[int]
    message: NotRequired[str]


class AccountSignals(TypedDict):
    status: Status
    fault: Fault | None
    accountNumber: str | None
    accountState: str | None
    productName: str | None
    region: str | None
    timeZone: str | None
    tokenExpiresAt: str | None
    tokenScope: str | None
    tokenExpiringSoon: bool | None


class Block(TypedDict):
    fromKwh: float
    toKwh: float | None
    rateCentsPerKwh: float


class Segment(TypedDict):
    band: str
    name: str
    rateCentsPerKwh: float
    kwhLimit: float | None
    rateAfterLimitCentsPerKwh: float | None
    blocks: list[Block]
    wholesaleLinked: bool
    wholesaleCapCentsPerKwh: float | None


class Period(Segment):
    start: str | None
    end: str | None


class ScheduleEntry(Segment):
    startMinute: int
    endMinute: int


class TariffSignals(TypedDict):
    status: Status
    fault: Fault | None
    structure: str | None
    spotLinked: bool | None
    peak: bool | None
    offPeak: bool | None
    period: Period | None
    nextChange: str | None
    schedule: list[ScheduleEntry] | None


class ForecastPoint(TypedDict):
    start: str
    centsPerKwh: float


ForecastWindow = TypedDict(
    "ForecastWindow",
    {
        "from": str,
        "to": str,
        "publishedAt": str,
        "minCentsPerKwh": float,
        "maxCentsPerKwh": float,
        "tier": str,
        "points": list[ForecastPoint],
    },
)


class Forecast(TypedDict):
    nextHour: ForecastWindow | None
    ahead: ForecastWindow | None


class PriceSignals(TypedDict):
    status: Status
    fault: Fault | None
    centsPerKwh: float | None
    intervalStart: str | None
    tier: str | None
    priceHigh: bool | None
    priceLow: bool | None
    negative: bool | None
    forecast: Forecast | None


class UsageEntry(TypedDict):
    local: str
    start: str
    durationMinutes: int
    gridImportKwh: float
    controlledLoadKwh: float
    solarExportKwh: float
    costAud: float | None
    feedInCreditAud: float | None


class EnergySignals(TypedDict):
    status: Status
    fault: Fault | None
    nmi: str | None
    intervals: list[UsageEntry] | None
    days: list[UsageEntry] | None
    latestIntervalEnd: str | None


class Signals(TypedDict):
    account: AccountSignals
    tariff: TariffSignals
    price: PriceSignals
    energy: EnergySignals
    nextEvaluation: str | None


class GroupFault(Exception):
    def __init__(self, fault: Fault) -> None:
        super().__init__(fault["code"], fault.get("message"))
        self.fault = fault


def make_fault(code: str, message: str | None = None) -> Fault:
    if message is None:
        return {"code": code}
    return {"code": code, "message": message}


def fault_error(code: str, message: str | None = None) -> GroupFault:
    return GroupFault(make_fault(code, message))


def invalid(message: str) -> GroupFault:
    return fault_error("invalid_response", message)


def error_fault(error: SnapshotError) -> Fault:
    if error["kind"] == "http":
        return {
            "code": "http_error",
            "httpStatus": error["status"],
            "body": error["body"],
            "bodyBytes": error["bodyBytes"],
        }
    if error["kind"] == "network":
        return {"code": "network_error", "message": error["message"]}
    return {"code": "invalid_response", "message": error["message"]}


def missing_body_fault(snapshot: Snapshot) -> Fault | None:
    if snapshot["body"] is not None:
        return None
    error = snapshot["error"]
    if error is not None:
        return error_fault(error)
    return make_fault("not_loaded")


def member(obj: dict[str, Json], key: str, path: str) -> Json:
    return obj.get(key)


def as_object(value: Json, path: str) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise invalid(f"{path} is not an object: {value!r}")
    return value


def as_list(value: Json, path: str) -> list[Json]:
    if not isinstance(value, list):
        raise invalid(f"{path} is not an array: {value!r}")
    return value


def as_str(value: Json, path: str) -> str:
    if not isinstance(value, str):
        raise invalid(f"{path} is not a string: {value!r}")
    return value


def as_optional_str(value: Json, path: str) -> str | None:
    if value is None:
        return None
    return as_str(value, path)


def as_number(value: Json, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid(f"{path} is not a number: {value!r}")
    return value


def as_optional_number(value: Json, path: str) -> float | None:
    if value is None:
        return None
    return as_number(value, path)


def as_bool(value: Json, path: str) -> bool:
    if not isinstance(value, bool):
        raise invalid(f"{path} is not a boolean: {value!r}")
    return value
