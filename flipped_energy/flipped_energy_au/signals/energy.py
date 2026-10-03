from dataclasses import dataclass, field
from datetime import tzinfo

from .account import Selection, select_account, selected_zone, site_address
from .constants import USAGE_CONSUMPTION, USAGE_FEED_IN
from .model import (
    Config,
    EnergySignals,
    Fault,
    GroupFault,
    Json,
    Snapshot,
    UsageEntry,
    as_bool,
    as_list,
    as_number,
    as_object,
    as_optional_number,
    as_optional_str,
    as_str,
    fault_error,
    invalid,
    make_fault,
    member,
    missing_body_fault,
)
from .timeprim import (
    MICROS_PER_MINUTE,
    Instant,
    format_instant,
    is_wall,
    local_occurrences,
    local_to_instant,
    next_date,
)

HALF_HOUR_MINUTES = 30

type CostKey = tuple[str, bool]


@dataclass(slots=True)
class Bucket:
    time: str
    grid_import_kwh: float = 0
    controlled_load_kwh: float = 0
    solar_export_kwh: float = 0
    costs: dict[CostKey, float | None] = field(default_factory=dict)

    def cost_aud(self) -> float | None:
        keys = [key for key in self.costs if key[0] == USAGE_CONSUMPTION]
        if not keys:
            return None
        total = 0.0
        for key in ((USAGE_CONSUMPTION, False), (USAGE_CONSUMPTION, True)):
            if key not in self.costs:
                continue
            cost = self.costs[key]
            if cost is None:
                return None
            total += cost
        return total

    def feed_in_credit_aud(self) -> float | None:
        key = (USAGE_FEED_IN, False)
        if key not in self.costs:
            return None
        cost = self.costs[key]
        if cost is None:
            return None
        return -cost


def faulted_energy(fault: Fault) -> EnergySignals:
    return {
        "status": "faulted",
        "fault": fault,
        "nmi": None,
        "intervals": None,
        "days": None,
        "latestIntervalEnd": None,
    }


def require_body(snapshot: Snapshot) -> Json:
    missing = missing_body_fault(snapshot)
    if missing is not None:
        raise GroupFault(missing)
    return snapshot["body"]


def check_zone_agreement(selection: Selection) -> None:
    zones: list[str | None] = []
    for path, product in selection.eligible:
        zone_name = as_optional_str(member(product, "timeZone", path), f"{path}.timeZone")
        if zone_name not in zones:
            zones.append(zone_name)
    if len(zones) > 1:
        raise fault_error(
            "usage_timezone_ambiguous",
            f"eligible accounts have time zones {', '.join(str(z) for z in zones)}",
        )


def select_nmi(selection: Selection, config: Config, meters: Snapshot) -> str:
    body = as_object(require_body(meters), "meters")
    meters_value = member(body, "meters", "meters")
    items = [] if meters_value is None else as_list(meters_value, "meters.meters")
    address = site_address(selection)
    every: list[str] = []
    candidates: list[str] = []
    for index, item in enumerate(items):
        path = f"meters.meters[{index}]"
        meter = as_object(item, path)
        nmi = as_str(member(meter, "nmi", path), f"{path}.nmi")
        meter_address = as_optional_str(member(meter, "address", path), f"{path}.address")
        if nmi not in every:
            every.append(nmi)
        if meter_address == address and nmi not in candidates:
            candidates.append(nmi)
    configured = config["nmi"]
    if configured is not None:
        if configured in every:
            return configured
        raise fault_error("nmi_not_found", f"nmi {configured} is not among {', '.join(every)}")
    if len(candidates) == 1:
        return candidates[0]
    listed = candidates if candidates else every
    raise fault_error("nmi_selection_required", ", ".join(listed))


def bucket_rows(value: Json, nmi: str, name: str) -> list[Bucket]:
    buckets: dict[str, Bucket] = {}
    for index, item in enumerate(as_list(value, name)):
        path = f"{name}[{index}]"
        row = as_object(item, path)
        if as_str(member(row, "nmi", path), f"{path}.nmi") != nmi:
            continue
        time = as_str(member(row, "time", path), f"{path}.time")
        if not is_wall(time):
            raise invalid(f"{path}.time is not a YYYY-MM-DDTHH:mm:ss text: {time!r}")
        bucket = buckets.get(time)
        if bucket is None:
            bucket = Bucket(time=time)
            buckets[time] = bucket
        usage_type = as_str(member(row, "usageType", path), f"{path}.usageType")
        if usage_type not in (USAGE_CONSUMPTION, USAGE_FEED_IN):
            continue
        kwh = as_number(member(row, "value", path), f"{path}.value")
        controlled = as_bool(member(row, "controlledLoad", path), f"{path}.controlledLoad")
        cost = as_optional_number(member(row, "cost", path), f"{path}.cost")
        if usage_type == USAGE_FEED_IN:
            bucket.solar_export_kwh += kwh
        elif controlled:
            bucket.controlled_load_kwh += kwh
        else:
            bucket.grid_import_kwh += kwh
        key = (usage_type, controlled)
        if key in bucket.costs:
            if bucket.costs[key] != cost:
                raise invalid(
                    f"{path}.cost {cost!r} differs from {bucket.costs[key]!r}"
                    f" on another {usage_type} controlledLoad={controlled} row at {time}"
                )
        else:
            bucket.costs[key] = cost
    return list(buckets.values())


def entry(bucket: Bucket, start: Instant, minutes: int) -> UsageEntry:
    return {
        "local": bucket.time[:19],
        "start": format_instant(start),
        "durationMinutes": minutes,
        "gridImportKwh": bucket.grid_import_kwh,
        "controlledLoadKwh": bucket.controlled_load_kwh,
        "solarExportKwh": bucket.solar_export_kwh,
        "costAud": bucket.cost_aud(),
        "feedInCreditAud": bucket.feed_in_credit_aud(),
    }


def merged_entry(group: list[tuple[Bucket, list[Instant]]]) -> tuple[Instant, UsageEntry]:
    ordered = sorted(group, key=lambda item: item[0].time)
    first_bucket, first_occurrences = ordered[0]
    start = first_occurrences[0]
    end = ordered[-1][1][1] + HALF_HOUR_MINUTES * MICROS_PER_MINUTE
    buckets = [bucket for bucket, _ in ordered]
    costs = [bucket.cost_aud() for bucket in buckets]
    credits = [bucket.feed_in_credit_aud() for bucket in buckets]
    cost_total: float | None = None
    if all(cost is not None for cost in costs):
        cost_total = sum(cost for cost in costs if cost is not None)
    credit_total: float | None = None
    if all(credit is not None for credit in credits):
        credit_total = sum(credit for credit in credits if credit is not None)
    return start, {
        "local": first_bucket.time[:19],
        "start": format_instant(start),
        "durationMinutes": (end - start) // MICROS_PER_MINUTE,
        "gridImportKwh": sum(bucket.grid_import_kwh for bucket in buckets),
        "controlledLoadKwh": sum(bucket.controlled_load_kwh for bucket in buckets),
        "solarExportKwh": sum(bucket.solar_export_kwh for bucket in buckets),
        "costAud": cost_total,
        "feedInCreditAud": credit_total,
    }


def map_intervals(buckets: list[Bucket], zone: tzinfo) -> list[tuple[Instant, UsageEntry]]:
    entries: list[tuple[Instant, UsageEntry]] = []
    repeated: dict[str, list[tuple[Bucket, list[Instant]]]] = {}
    for bucket in buckets:
        occurrences = local_occurrences(bucket.time, zone)
        if not occurrences:
            raise fault_error(
                "local_time_nonexistent", f"usage bucket {bucket.time} does not exist in {zone}"
            )
        if len(occurrences) == 1:
            entries.append((occurrences[0], entry(bucket, occurrences[0], HALF_HOUR_MINUTES)))
            continue
        repeated.setdefault(bucket.time[:10], []).append((bucket, occurrences))
    for group in repeated.values():
        entries.append(merged_entry(group))
    entries.sort(key=lambda item: item[0])
    return entries


def map_days(buckets: list[Bucket], zone: tzinfo) -> list[UsageEntry]:
    days: list[tuple[Instant, UsageEntry]] = []
    for bucket in buckets:
        date = bucket.time[:10]
        start = local_to_instant(f"{date}T00:00:00", zone)
        end = local_to_instant(f"{next_date(date)}T00:00:00", zone)
        days.append((start, entry(bucket, start, (end - start) // MICROS_PER_MINUTE)))
    days.sort(key=lambda item: item[0])
    return [day for _, day in days]


def compute_energy(
    instant: Instant | None,
    config: Config,
    account: Snapshot,
    meters: Snapshot,
    usage_half_hourly: Snapshot,
    usage_daily: Snapshot,
) -> EnergySignals:
    if instant is None:
        return faulted_energy(make_fault("clock_unsynced"))
    try:
        selection = select_account(account, config)
        _, zone = selected_zone(selection)
        check_zone_agreement(selection)
        nmi = select_nmi(selection, config, meters)
        half_hourly_body = require_body(usage_half_hourly)
        daily_body = require_body(usage_daily)
        intervals = map_intervals(bucket_rows(half_hourly_body, nmi, "usageHalfHourly"), zone)
        days = map_days(bucket_rows(daily_body, nmi, "usageDaily"), zone)
    except GroupFault as exc:
        return faulted_energy(exc.fault)
    latest: str | None = None
    if intervals:
        last_start, last = intervals[-1]
        latest = format_instant(last_start + last["durationMinutes"] * MICROS_PER_MINUTE)
    return {
        "status": "ok",
        "fault": None,
        "nmi": nmi,
        "intervals": [item for _, item in intervals],
        "days": days,
        "latestIntervalEnd": latest,
    }
