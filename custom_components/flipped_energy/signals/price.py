from dataclasses import dataclass

from .account import grid_type, select_account
from .constants import HIGH_TIERS, LOW_TIER, PRICE_STALE_AFTER_S, PRICE_TIERS
from .model import (
    Config,
    Fault,
    ForecastPoint,
    ForecastWindow,
    GroupFault,
    Json,
    PriceSignals,
    Snapshot,
    as_list,
    as_number,
    as_object,
    as_str,
    error_fault,
    fault_error,
    invalid,
    make_fault,
    member,
    missing_body_fault,
)
from .timeprim import MICROS_PER_SECOND, Instant, format_instant, parse_instant


@dataclass(frozen=True, slots=True)
class Outlook:
    cents_per_kwh: float
    interval_start: Instant
    tier: str
    next_hour: ForecastWindow | None
    ahead: ForecastWindow | None


@dataclass(frozen=True, slots=True)
class PriceResult:
    signals: PriceSignals
    interval_start: Instant | None


def faulted_price(fault: Fault) -> PriceSignals:
    return {
        "status": "faulted",
        "fault": fault,
        "centsPerKwh": None,
        "intervalStart": None,
        "tier": None,
        "priceHigh": None,
        "priceLow": None,
        "negative": None,
        "forecast": None,
    }


def offset_instant(value: Json, path: str) -> Instant:
    text = as_str(value, path)
    try:
        return parse_instant(text)
    except ValueError as exc:
        raise invalid(f"{path}: {exc}") from exc


def read_tier(value: Json, path: str) -> str:
    assessment = as_object(value, path)
    tier = as_str(member(assessment, "tier", path), f"{path}.tier")
    if tier not in PRICE_TIERS:
        raise invalid(f"{path}.tier is not one of {', '.join(PRICE_TIERS)}: {tier!r}")
    return tier


def forecast_window(body: dict[str, Json], key: str, peak_key: str) -> ForecastWindow | None:
    value = member(body, key, "outlook")
    if value is None:
        return None
    path = f"outlook.{key}"
    forecast = as_object(value, path)
    points = as_list(member(forecast, "points", path), f"{path}.points")
    if not points:
        return None
    parsed: list[ForecastPoint] = []
    for index, item in enumerate(points):
        point_path = f"{path}.points[{index}]"
        point = as_object(item, point_path)
        parsed.append(
            {
                "start": format_instant(
                    offset_instant(member(point, "time", point_path), f"{point_path}.time")
                ),
                "centsPerKwh": as_number(
                    member(point, "averageCentsPerKwh", point_path),
                    f"{point_path}.averageCentsPerKwh",
                ),
            }
        )
    peak_path = f"outlook.{peak_key}"
    return {
        "from": format_instant(offset_instant(member(forecast, "from", path), f"{path}.from")),
        "to": format_instant(offset_instant(member(forecast, "to", path), f"{path}.to")),
        "publishedAt": format_instant(
            offset_instant(member(forecast, "publishedAt", path), f"{path}.publishedAt")
        ),
        "minCentsPerKwh": as_number(
            member(forecast, "minCentsPerKwh", path), f"{path}.minCentsPerKwh"
        ),
        "maxCentsPerKwh": as_number(
            member(forecast, "maxCentsPerKwh", path), f"{path}.maxCentsPerKwh"
        ),
        "tier": read_tier(member(body, peak_key, "outlook"), peak_path),
        "points": parsed,
    }


def read_outlook(value: Json) -> Outlook:
    body = as_object(value, "outlook")
    now = as_object(member(body, "now", "outlook"), "outlook.now")
    return Outlook(
        cents_per_kwh=as_number(
            member(now, "averageCentsPerKwh", "outlook.now"), "outlook.now.averageCentsPerKwh"
        ),
        interval_start=offset_instant(member(now, "time", "outlook.now"), "outlook.now.time"),
        tier=read_tier(member(body, "nowAssessment", "outlook"), "outlook.nowAssessment"),
        next_hour=forecast_window(body, "nextHour", "nextHourPeak"),
        ahead=forecast_window(body, "ahead", "aheadPeak"),
    )


def usable_outlook(instant: Instant, snapshot: Snapshot) -> Outlook:
    missing = missing_body_fault(snapshot)
    if missing is not None:
        raise GroupFault(missing)
    error = snapshot["error"]
    try:
        outlook = read_outlook(snapshot["body"])
    except GroupFault as exc:
        if error is not None:
            raise GroupFault(error_fault(error)) from exc
        raise
    age = max(0, instant - outlook.interval_start)
    if age >= PRICE_STALE_AFTER_S * MICROS_PER_SECOND:
        if error is not None:
            raise GroupFault(error_fault(error))
        raise fault_error(
            "price_stale", f"newest price interval starts {format_instant(outlook.interval_start)}"
        )
    return outlook


def price_flags(config: Config, cents: float, tier: str) -> tuple[bool, bool]:
    high_threshold = config["priceHighThresholdCentsPerKwh"]
    low_threshold = config["priceLowThresholdCentsPerKwh"]
    high_t = None if high_threshold is None else cents >= high_threshold
    low_t = None if low_threshold is None else cents <= low_threshold
    high_tier = tier in HIGH_TIERS and cents >= 0
    low_tier = tier == LOW_TIER or cents < 0
    price_high = high_t if high_t is not None else (high_tier and low_t is not True)
    price_low = low_t if low_t is not None else (low_tier and high_t is not True)
    return price_high, price_low


def compute_price(
    instant: Instant | None, config: Config, account: Snapshot, outlook_snapshot: Snapshot
) -> PriceResult:
    if instant is None:
        return PriceResult(faulted_price(make_fault("clock_unsynced")), None)
    try:
        selection = select_account(account, config)
        if grid_type(selection) is None:
            raise fault_error("region_missing", f"{selection.path}.product.gridType is null")
        outlook = usable_outlook(instant, outlook_snapshot)
    except GroupFault as exc:
        return PriceResult(faulted_price(exc.fault), None)
    price_high, price_low = price_flags(config, outlook.cents_per_kwh, outlook.tier)
    signals: PriceSignals = {
        "status": "ok",
        "fault": None,
        "centsPerKwh": outlook.cents_per_kwh,
        "intervalStart": format_instant(outlook.interval_start),
        "tier": outlook.tier,
        "priceHigh": price_high,
        "priceLow": price_low,
        "negative": outlook.cents_per_kwh < 0,
        "forecast": {"nextHour": outlook.next_hour, "ahead": outlook.ahead},
    }
    return PriceResult(signals, outlook.interval_start)
