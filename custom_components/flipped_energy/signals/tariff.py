import math
from dataclasses import dataclass
from datetime import tzinfo
from itertools import pairwise

from .account import account_state, select_account, selected_zone
from .constants import (
    ACCOUNT_MAX_AGE_S,
    FIXED_UNIT_TYPE,
    KNOWN_UNIT_TYPES,
    KWH_UNLIMITED,
    MINUTES_PER_DAY,
    PLAN_CHANGE_HORIZON_DAYS,
    RATE_KEY_OUTPUT_DIVISOR,
    RATE_KEY_SCALE,
    SCAN_LIMIT_MIN,
    SPOT_UNIT_TYPES,
    SPOT_WITH_CAP_UNIT_TYPE,
    SUPPLIED_ACCOUNT_STATES,
)
from .model import (
    Block,
    Config,
    Fault,
    GroupFault,
    Json,
    Period,
    ScheduleEntry,
    Segment,
    Snapshot,
    TariffSignals,
    as_list,
    as_object,
    as_optional_str,
    as_str,
    error_fault,
    fault_error,
    invalid,
    make_fault,
    member,
    missing_body_fault,
)
from .timeprim import (
    MICROS_PER_DAY,
    MICROS_PER_MINUTE,
    MICROS_PER_SECOND,
    Instant,
    format_instant,
    is_wall,
    local_to_instant,
    next_date,
    parse_instant,
    to_local,
)


@dataclass(frozen=True, slots=True)
class FixedUnit:
    unit_id: str
    name: str
    start: int
    end: int
    from_kwh: float
    to_kwh: float | None
    key: int


@dataclass(frozen=True, slots=True)
class SpotUnit:
    unit_id: str
    start: int
    end: int
    with_cap: bool
    cap_key: int | None


type BlockKey = tuple[float, float | None, int]


@dataclass(frozen=True, slots=True)
class SegmentKey:
    band: str
    name: str
    blocks: tuple[BlockKey, ...]
    wholesale: bool
    cap_key: int | None


@dataclass(frozen=True, slots=True)
class TariffResult:
    signals: TariffSignals
    plan_change: Instant | None
    next_change: Instant | None


def rate_key(value: float) -> int:
    magnitude = math.floor(abs(value) * RATE_KEY_SCALE + 0.5)
    return -magnitude if value < 0 else magnitude


def rate_output(key: int) -> float:
    return key / RATE_KEY_OUTPUT_DIVISOR


def in_window(start: int, end: int, minute: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def faulted_tariff(fault: Fault) -> TariffSignals:
    return {
        "status": "faulted",
        "fault": fault,
        "structure": None,
        "spotLinked": None,
        "peak": None,
        "offPeak": None,
        "period": None,
        "nextChange": None,
        "schedule": None,
    }


def account_usable_fault(snapshot: Snapshot, instant: Instant) -> Fault | None:
    missing = missing_body_fault(snapshot)
    if missing is not None:
        return missing
    fetched_text = snapshot["fetchedAt"]
    if fetched_text is None:
        raise ValueError("account snapshot has a body and no fetchedAt")
    if instant - parse_instant(fetched_text) < ACCOUNT_MAX_AGE_S * MICROS_PER_SECOND:
        return None
    error = snapshot["error"]
    if error is not None:
        return error_fault(error)
    return make_fault("account_data_stale", f"account snapshot fetched at {fetched_text}")


def plan_object(product: dict[str, Json], key: str, path: str) -> dict[str, Json] | None:
    value = member(product, key, path)
    if value is None:
        return None
    plan = as_object(value, f"{path}.{key}")
    for date_key in ("start", "end"):
        text = member(plan, date_key, f"{path}.{key}")
        if not isinstance(text, str) or not is_wall(text):
            raise invalid(f"{path}.{key}.{date_key} is not a YYYY-MM-DDTHH:mm:ss text: {text!r}")
    return plan


def plan_text(plan: dict[str, Json], key: str) -> str:
    value = plan[key]
    if not isinstance(value, str):
        raise TypeError(f"plan {key} was not validated")
    return value


def in_effect(plan: dict[str, Json] | None, wall: str, date: str) -> bool:
    if plan is None:
        return False
    return plan_text(plan, "start")[:19] <= wall and date <= plan_text(plan, "end")[:10]


def plan_change_instant(
    instant: Instant,
    zone: tzinfo,
    plan: dict[str, Json] | None,
    upcoming: dict[str, Json] | None,
    wall: str,
) -> Instant | None:
    t0 = instant - instant % MICROS_PER_MINUTE
    horizon_date = to_local(t0 + PLAN_CHANGE_HORIZON_DAYS * MICROS_PER_DAY, zone).date
    candidates: list[str] = []
    if plan is not None:
        end_date = plan_text(plan, "end")[:10]
        if end_date < horizon_date:
            candidates.append(next_date(end_date) + "T00:00:00")
    if upcoming is not None and plan is not upcoming:
        upcoming_start = plan_text(upcoming, "start")
        if upcoming_start[:19] > wall and upcoming_start[:10] <= horizon_date:
            candidates.append(upcoming_start[:19])
    if not candidates:
        return None
    return local_to_instant(min(candidates), zone)


def window_minute(unit: dict[str, Json], key: str) -> int | None:
    value = unit[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not float(value).is_integer() or value < 0 or value > MINUTES_PER_DAY:
        return None
    return int(value)


def kwh_value(value: Json) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def parse_units(plan: dict[str, Json], path: str) -> tuple[list[FixedUnit], list[SpotUnit], bool]:
    fixed: list[FixedUnit] = []
    spots: list[SpotUnit] = []
    spot_linked = False
    units = as_list(member(plan, "billingUnits", path), f"{path}.billingUnits")
    for index, item in enumerate(units):
        unit_path = f"{path}.billingUnits[{index}]"
        unit = as_object(item, unit_path)
        unit_id = as_str(member(unit, "billingUnitId", unit_path), f"{unit_path}.billingUnitId")
        raw_type = member(unit, "billingUnitType", unit_path)
        unit_type = raw_type if isinstance(raw_type, str) else None
        if unit_type is None or unit_type not in KNOWN_UNIT_TYPES:
            priced = [
                key
                for key in (
                    "timeOfDayStartMinutes",
                    "timeOfDayEndMinutes",
                    "chargePerKwh",
                    "chargePerKwhIncludingGst",
                )
                if member(unit, key, unit_path) is not None
            ]
            if priced:
                raise fault_error(
                    "billing_unit_unsupported",
                    f"billing unit {unit_id} has type {raw_type!r} and non-null {', '.join(priced)}",
                )
            continue
        if unit_type != FIXED_UNIT_TYPE and unit_type not in SPOT_UNIT_TYPES:
            continue
        member(unit, "timeOfDayStartMinutes", unit_path)
        member(unit, "timeOfDayEndMinutes", unit_path)
        start = window_minute(unit, "timeOfDayStartMinutes")
        end = window_minute(unit, "timeOfDayEndMinutes")
        if start is None or end is None:
            raise fault_error(
                "billing_unit_malformed",
                f"billing unit {unit_id} has window {unit['timeOfDayStartMinutes']!r}"
                f" to {unit['timeOfDayEndMinutes']!r}",
            )
        if unit_type in SPOT_UNIT_TYPES:
            spot_linked = True
            cap_key: int | None = None
            if unit_type == SPOT_WITH_CAP_UNIT_TYPE:
                cap = member(unit, "capPerKwhIncludingGst", unit_path)
                if cap is not None:
                    cap_number = kwh_value(cap)
                    if cap_number is None:
                        raise fault_error(
                            "billing_unit_malformed",
                            f"billing unit {unit_id} has capPerKwhIncludingGst {cap!r}",
                        )
                    cap_key = rate_key(cap_number)
            spots.append(
                SpotUnit(
                    unit_id=unit_id,
                    start=start,
                    end=end,
                    with_cap=unit_type == SPOT_WITH_CAP_UNIT_TYPE,
                    cap_key=cap_key,
                )
            )
            continue
        charge = kwh_value(member(unit, "chargePerKwhIncludingGst", unit_path))
        if charge is None:
            raise fault_error(
                "billing_unit_malformed",
                f"billing unit {unit_id} has chargePerKwhIncludingGst"
                f" {unit['chargePerKwhIncludingGst']!r}",
            )
        kwh_start_raw = member(unit, "kwhStart", unit_path)
        kwh_end_raw = member(unit, "kwhEnd", unit_path)
        from_kwh = 0 if kwh_start_raw is None else kwh_value(kwh_start_raw)
        kwh_end = None if kwh_end_raw is None else kwh_value(kwh_end_raw)
        if from_kwh is None or (kwh_end_raw is not None and kwh_end is None):
            raise fault_error(
                "billing_unit_malformed",
                f"billing unit {unit_id} has kWh block {kwh_start_raw!r} to {kwh_end_raw!r}",
            )
        to_kwh = None if kwh_end is None or kwh_end == 0 or kwh_end >= KWH_UNLIMITED else kwh_end
        if (
            from_kwh < 0
            or (kwh_end is not None and kwh_end < 0)
            or (to_kwh is not None and to_kwh <= from_kwh)
        ):
            raise fault_error(
                "billing_unit_malformed",
                f"billing unit {unit_id} has kWh block {kwh_start_raw!r} to {kwh_end_raw!r}",
            )
        name = as_optional_str(member(unit, "name", unit_path), f"{unit_path}.name")
        fixed.append(
            FixedUnit(
                unit_id=unit_id,
                name="" if name is None else name,
                start=start,
                end=end,
                from_kwh=from_kwh,
                to_kwh=to_kwh,
                key=rate_key(charge),
            )
        )
    return fixed, spots, spot_linked


def kwh_text(value: float) -> str:
    return str(value)


def minute_blocks(
    minute: int, fixed: list[FixedUnit], spots: list[SpotUnit]
) -> tuple[list[FixedUnit], list[SpotUnit]]:
    covering = sorted(
        (u for u in fixed if in_window(u.start, u.end, minute)), key=lambda u: u.from_kwh
    )
    spot_cover = [s for s in spots if in_window(s.start, s.end, minute)]
    if not covering and spot_cover:
        raise fault_error(
            "spot_direction_unknown",
            f"minute {minute}: only wholesale-linked units {', '.join(s.unit_id for s in spot_cover)}",
        )
    if not covering:
        raise fault_error("tariff_gap", f"minute {minute}")
    if covering[0].from_kwh != 0:
        raise fault_error("tariff_gap", f"minute {minute}, kWh 0")
    for prev, cur in pairwise(covering):
        if prev.to_kwh is None or cur.from_kwh < prev.to_kwh:
            raise fault_error(
                "billing_unit_overlap",
                f"billing units {prev.unit_id} and {cur.unit_id} overlap at minute {minute}",
            )
        if cur.from_kwh > prev.to_kwh:
            raise fault_error("tariff_gap", f"minute {minute}, kWh {kwh_text(prev.to_kwh)}")
    last = covering[-1].to_kwh
    if last is not None:
        raise fault_error("tariff_gap", f"minute {minute}, kWh {kwh_text(last)}")
    return covering, spot_cover


def build_day(fixed: list[FixedUnit], spots: list[SpotUnit]) -> list[SegmentKey]:
    raw: list[tuple[str, tuple[BlockKey, ...], bool, int | None]] = []
    for minute in range(MINUTES_PER_DAY):
        covering, spot_cover = minute_blocks(minute, fixed, spots)
        blocks = tuple((u.from_kwh, u.to_kwh, u.key) for u in covering)
        cap_key: int | None = None
        if len(spot_cover) == 1 and spot_cover[0].with_cap:
            cap_key = spot_cover[0].cap_key
        raw.append((covering[0].name, blocks, bool(spot_cover), cap_key))
    tiers = sorted({blocks[0][2] for _, blocks, _, _ in raw})
    day: list[SegmentKey] = []
    for name, blocks, wholesale, cap_key in raw:
        key = blocks[0][2]
        if len(tiers) == 1:
            band = "anytime"
        elif key == tiers[0]:
            band = "offPeak"
        elif key == tiers[-1]:
            band = "peak"
        else:
            band = "shoulder"
        day.append(
            SegmentKey(band=band, name=name, blocks=blocks, wholesale=wholesale, cap_key=cap_key)
        )
    return day


def segment_members(segment: SegmentKey) -> Segment:
    blocks: list[Block] = [
        {"fromKwh": from_kwh, "toKwh": to_kwh, "rateCentsPerKwh": rate_output(key)}
        for from_kwh, to_kwh, key in segment.blocks
    ]
    return {
        "band": segment.band,
        "name": segment.name,
        "rateCentsPerKwh": rate_output(segment.blocks[0][2]),
        "kwhLimit": segment.blocks[0][1],
        "rateAfterLimitCentsPerKwh": (
            rate_output(segment.blocks[1][2]) if len(segment.blocks) > 1 else None
        ),
        "blocks": blocks,
        "wholesaleLinked": segment.wholesale,
        "wholesaleCapCentsPerKwh": (
            None if segment.cap_key is None else rate_output(segment.cap_key)
        ),
    }


def schedule_of(day: list[SegmentKey]) -> list[ScheduleEntry]:
    entries: list[ScheduleEntry] = []
    start = 0
    for minute in range(1, MINUTES_PER_DAY + 1):
        if minute < MINUTES_PER_DAY and day[minute] == day[start]:
            continue
        members = segment_members(day[start])
        entries.append(
            {
                "startMinute": start,
                "endMinute": minute,
                "band": members["band"],
                "name": members["name"],
                "rateCentsPerKwh": members["rateCentsPerKwh"],
                "kwhLimit": members["kwhLimit"],
                "rateAfterLimitCentsPerKwh": members["rateAfterLimitCentsPerKwh"],
                "blocks": members["blocks"],
                "wholesaleLinked": members["wholesaleLinked"],
                "wholesaleCapCentsPerKwh": members["wholesaleCapCentsPerKwh"],
            }
        )
        start = minute
    return entries


def period_bounds(
    instant: Instant,
    zone: tzinfo,
    day: list[SegmentKey],
    plan: dict[str, Json],
    plan_change: Instant | None,
) -> tuple[SegmentKey, Instant | None, Instant | None]:
    t0 = instant - instant % MICROS_PER_MINUTE
    current = day[to_local(t0, zone).minute_of_day]
    end: Instant | None = None
    for k in range(1, SCAN_LIMIT_MIN + 1):
        candidate = t0 + k * MICROS_PER_MINUTE
        if day[to_local(candidate, zone).minute_of_day] != current:
            end = candidate
            break
    if end is not None and plan_change is not None:
        end = min(end, plan_change)
    start: Instant | None = None
    for k in range(SCAN_LIMIT_MIN + 1):
        candidate = t0 - k * MICROS_PER_MINUTE
        if day[to_local(candidate - MICROS_PER_MINUTE, zone).minute_of_day] != current:
            start = candidate
            break
    if start is not None:
        plan_start = plan_text(plan, "start")
        earliest_date = to_local(t0 - SCAN_LIMIT_MIN * MICROS_PER_MINUTE, zone).date
        if plan_start[:10] >= earliest_date:
            start = max(start, local_to_instant(plan_start[:19], zone))
    return current, start, end


def compute_tariff(instant: Instant | None, config: Config, account: Snapshot) -> TariffResult:
    if instant is None:
        return TariffResult(faulted_tariff(make_fault("clock_unsynced")), None, None)
    unusable = account_usable_fault(account, instant)
    if unusable is not None:
        return TariffResult(faulted_tariff(unusable), None, None)
    plan_change: Instant | None = None
    try:
        selection = select_account(account, config)
        state = account_state(selection)
        if state not in SUPPLIED_ACCOUNT_STATES:
            raise fault_error("account_not_supplied", f"accountState {state}")
        _, zone = selected_zone(selection)
        product_path = f"{selection.path}.product"
        current_plan = plan_object(selection.product, "currentPlan", product_path)
        upcoming_plan = plan_object(selection.product, "upcomingPlan", product_path)
        local = to_local(instant, zone)
        plan: dict[str, Json] | None = None
        plan_path = product_path
        if in_effect(upcoming_plan, local.wall, local.date):
            plan = upcoming_plan
            plan_path = f"{product_path}.upcomingPlan"
        elif in_effect(current_plan, local.wall, local.date):
            plan = current_plan
            plan_path = f"{product_path}.currentPlan"
        plan_change = plan_change_instant(instant, zone, plan, upcoming_plan, local.wall)
        if plan is None:
            raise fault_error("plan_unavailable", f"no plan is in effect at {local.wall}")
        fixed, spots, spot_linked = parse_units(plan, plan_path)
        day = build_day(fixed, spots)
        current, start, end = period_bounds(instant, zone, day, plan, plan_change)
    except GroupFault as exc:
        return TariffResult(faulted_tariff(exc.fault), plan_change, None)
    tiers = {segment.blocks[0][2] for segment in day}
    members = segment_members(current)
    period: Period = {
        "band": members["band"],
        "name": members["name"],
        "rateCentsPerKwh": members["rateCentsPerKwh"],
        "kwhLimit": members["kwhLimit"],
        "rateAfterLimitCentsPerKwh": members["rateAfterLimitCentsPerKwh"],
        "blocks": members["blocks"],
        "wholesaleLinked": members["wholesaleLinked"],
        "wholesaleCapCentsPerKwh": members["wholesaleCapCentsPerKwh"],
        "start": None if start is None else format_instant(start),
        "end": None if end is None else format_instant(end),
    }
    next_candidates = [value for value in (end, plan_change) if value is not None]
    next_change = min(next_candidates) if next_candidates else None
    signals: TariffSignals = {
        "status": "ok",
        "fault": None,
        "structure": "flat" if len(tiers) == 1 else "timeOfUse",
        "spotLinked": spot_linked,
        "peak": current.band == "peak",
        "offPeak": current.band == "offPeak",
        "period": period,
        "nextChange": None if next_change is None else format_instant(next_change),
        "schedule": schedule_of(day),
    }
    return TariffResult(signals, plan_change, next_change)
