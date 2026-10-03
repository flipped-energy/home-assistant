from typing import Any, Final

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import FlippedEnergyConfigEntry
from .const import CONF_ACCOUNT_NUMBER, CONF_NMI, CONF_TOKEN
from .runtime import InstanceRuntime, Slot
from .signals import Snapshot
from .signals.timeprim import format_instant, instant_from_datetime

TO_REDACT_ENTRY: Final = {CONF_TOKEN, CONF_ACCOUNT_NUMBER, CONF_NMI, "unique_id", "title"}
LIMIT_HEADERS: Final = ("X-RateLimit-Remaining", "X-DailyLimit-Remaining")


def snapshot_diagnostics(snapshot: Snapshot, with_body: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"fetchedAt": snapshot["fetchedAt"], "error": snapshot["error"]}
    if with_body:
        result["body"] = snapshot["body"]
    return result


def signals_diagnostics(runtime: InstanceRuntime) -> dict[str, Any]:
    signals = runtime.signals
    energy = signals["energy"]
    intervals = energy["intervals"]
    days = energy["days"]
    return {
        "account": async_redact_data(signals["account"], {CONF_ACCOUNT_NUMBER}),
        "tariff": signals["tariff"],
        "price": signals["price"],
        "energy": async_redact_data(
            {
                "status": energy["status"],
                "fault": energy["fault"],
                "nmi": energy["nmi"],
                "intervalCount": None if intervals is None else len(intervals),
                "dayCount": None if days is None else len(days),
                "latestIntervalEnd": energy["latestIntervalEnd"],
            },
            {CONF_NMI},
        ),
    }


def loop_diagnostics(runtime: InstanceRuntime) -> dict[str, Any] | None:
    loop = runtime.loop
    if loop is None:
        return None
    return {
        "state": loop.state.value,
        "T": None if loop.t is None else format_instant(instant_from_datetime(loop.t)),
        "holds": loop.holds,
        "subscriberCount": loop.subscriber_count,
    }


def statistics_diagnostics(runtime: InstanceRuntime) -> dict[str, Any] | None:
    report = runtime.statistics
    if report is None:
        return None
    return {
        "cost_unknown_hours": report.cost_unknown_hours,
        "feed_in_unknown_hours": report.feed_in_unknown_hours,
        "missing_hours": report.missing_hours,
        "statistics": report.statistics,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FlippedEnergyConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    limits = runtime.limits
    snapshots = {
        slot.name.lower(): snapshot_diagnostics(runtime.snapshots[slot], False) for slot in Slot
    }
    snapshots["outlook"] = snapshot_diagnostics(runtime.outlook, True)
    return {
        "entry": async_redact_data(
            {
                "data": dict(entry.data),
                "options": dict(entry.options),
                "unique_id": entry.unique_id,
                "title": entry.title,
            },
            TO_REDACT_ENTRY,
        ),
        "signals": signals_diagnostics(runtime),
        "snapshots": snapshots,
        "loop": loop_diagnostics(runtime),
        "limits": {name: limits.get(name) for name in LIMIT_HEADERS},
        "statistics": statistics_diagnostics(runtime),
        "zones": sorted(runtime.zones),
    }
