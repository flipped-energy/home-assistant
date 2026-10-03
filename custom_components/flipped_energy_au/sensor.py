import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, Platform, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import FlippedEnergyConfigEntry
from .const import MAX_STATE_LENGTH, TIER_STATES, UNIT_AUD_PER_KWH, to_dollars
from .entity import (
    FlippedEnergyEntity,
    Group,
    SpotLinkedEntities,
    current_period,
    group_signals,
)
from .runtime import InstanceRuntime
from .signals.model import ForecastWindow, Period

_LOGGER = logging.getLogger(__package__)

PARALLEL_UPDATES = 0

BAND_STATES: Final = {
    "peak": "peak",
    "shoulder": "shoulder",
    "offPeak": "off_peak",
    "anytime": "anytime",
}
FAULT_CODES: Final = (
    "not_loaded",
    "clock_unsynced",
    "http_error",
    "network_error",
    "invalid_response",
    "account_none",
    "account_selection_required",
    "account_not_found",
    "account_not_supplied",
    "account_data_stale",
    "timezone_missing",
    "timezone_unsupported",
    "local_time_nonexistent",
    "plan_unavailable",
    "billing_unit_unsupported",
    "billing_unit_malformed",
    "billing_unit_overlap",
    "tariff_gap",
    "spot_direction_unknown",
    "region_missing",
    "price_stale",
    "nmi_selection_required",
    "nmi_not_found",
    "usage_timezone_ambiguous",
)
STATUS_OPTIONS: Final = ["ok", *FAULT_CODES]

type Value = StateType | datetime
type Attributes = Mapping[str, Any] | None


@dataclass(frozen=True, kw_only=True)
class FlippedEnergySensorDescription(SensorEntityDescription):
    group: Group | None
    value: Callable[[InstanceRuntime], Value]
    attributes: Callable[[InstanceRuntime], Attributes] = lambda runtime: None


def as_timestamp(value: str | None) -> datetime | None:
    return None if value is None else dt_util.parse_datetime(value, raise_on_error=True)


def period_value(read: Callable[[Period], float | None]) -> Callable[[InstanceRuntime], Value]:
    def value(runtime: InstanceRuntime) -> Value:
        period = current_period(runtime.signals)
        return None if period is None else read(period)

    return value


def current_rate(runtime: InstanceRuntime) -> Value:
    period = current_period(runtime.signals)
    if period is None or period["wholesaleLinked"]:
        return None
    return to_dollars(period["rateCentsPerKwh"])


def price_level(runtime: InstanceRuntime) -> Value:
    tier = runtime.signals["price"]["tier"]
    return None if tier is None else TIER_STATES[tier]


def next_hour(runtime: InstanceRuntime) -> ForecastWindow | None:
    forecast = runtime.signals["price"]["forecast"]
    return None if forecast is None else forecast["nextHour"]


def forecast_value(runtime: InstanceRuntime) -> Value:
    window = next_hour(runtime)
    return None if window is None else to_dollars(window["maxCentsPerKwh"])


def forecast_attributes(runtime: InstanceRuntime) -> Attributes:
    window = next_hour(runtime)
    if window is None:
        return None
    return {
        "min_aud_per_kwh": to_dollars(window["minCentsPerKwh"]),
        "level": TIER_STATES[window["tier"]],
        "from": window["from"],
        "to": window["to"],
        "published_at": window["publishedAt"],
        "points": [
            {"start": point["start"], "aud_per_kwh": to_dollars(point["centsPerKwh"])}
            for point in window["points"]
        ],
    }


def rate_period_value(runtime: InstanceRuntime) -> Value:
    period = current_period(runtime.signals)
    return None if period is None else BAND_STATES[period["band"]]


def rate_period_attributes(runtime: InstanceRuntime) -> Attributes:
    tariff = runtime.signals["tariff"]
    period = tariff["period"]
    if period is None:
        return None
    return {
        "name": period["name"],
        "structure": tariff["structure"],
        "spot_linked": tariff["spotLinked"],
        "period_start": period["start"],
        "period_end": period["end"],
        "blocks": period["blocks"],
        "schedule": tariff["schedule"],
    }


def rate_period_name(runtime: InstanceRuntime) -> str | None:
    period = current_period(runtime.signals)
    return None if period is None else period["name"]


def status_value(group: Group) -> Callable[[InstanceRuntime], Value]:
    def value(runtime: InstanceRuntime) -> Value:
        fault = group_signals(runtime.signals, group)["fault"]
        return "ok" if fault is None else fault["code"]

    return value


def status_attributes(group: Group) -> Callable[[InstanceRuntime], dict[str, Any]]:
    def attributes(runtime: InstanceRuntime) -> dict[str, Any]:
        fault = group_signals(runtime.signals, group)["fault"]
        if fault is None:
            return {"http_status": None, "body": None, "body_bytes": None, "message": None}
        return {
            "http_status": fault.get("httpStatus"),
            "body": fault.get("body"),
            "body_bytes": fault.get("bodyBytes"),
            "message": fault.get("message"),
        }

    return attributes


def usage_history_status_attributes(runtime: InstanceRuntime) -> Attributes:
    report = runtime.statistics
    return {
        **status_attributes("energy")(runtime),
        "nmi": runtime.signals["energy"]["nmi"],
        "cost_unknown_hours": None if report is None else report.cost_unknown_hours,
        "feed_in_unknown_hours": None if report is None else report.feed_in_unknown_hours,
        "missing_hours": None if report is None else report.missing_hours,
    }


def rate(key: str, read: Callable[[Period], float | None]) -> FlippedEnergySensorDescription:
    return FlippedEnergySensorDescription(
        key=key,
        group="tariff",
        native_unit_of_measurement=UNIT_AUD_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        value=period_value(lambda period: to_dollars(read(period))),
    )


def status(key: str, group: Group) -> FlippedEnergySensorDescription:
    return FlippedEnergySensorDescription(
        key=key,
        group=group,
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=status_value(group),
        attributes=status_attributes(group),
    )


SENSORS: Final = (
    FlippedEnergySensorDescription(
        key="wholesale_price",
        group="price",
        native_unit_of_measurement=UNIT_AUD_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda runtime: to_dollars(runtime.signals["price"]["centsPerKwh"]),
        attributes=lambda runtime: {"interval_start": runtime.signals["price"]["intervalStart"]},
    ),
    FlippedEnergySensorDescription(
        key="wholesale_price_level",
        group="price",
        device_class=SensorDeviceClass.ENUM,
        options=list(TIER_STATES.values()),
        value=price_level,
    ),
    FlippedEnergySensorDescription(
        key="current_rate",
        group="tariff",
        native_unit_of_measurement=UNIT_AUD_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        value=current_rate,
    ),
    FlippedEnergySensorDescription(
        key="rate_period_allowance",
        group="tariff",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value=period_value(lambda period: period["kwhLimit"]),
    ),
    rate("rate_after_allowance", lambda period: period["rateAfterLimitCentsPerKwh"]),
    FlippedEnergySensorDescription(
        key="next_rate_change",
        group="tariff",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda runtime: as_timestamp(runtime.signals["tariff"]["nextChange"]),
    ),
    FlippedEnergySensorDescription(
        key="usage_history_up_to",
        group="energy",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda runtime: as_timestamp(runtime.signals["energy"]["latestIntervalEnd"]),
    ),
    FlippedEnergySensorDescription(
        key="api_calls_remaining_today",
        group=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda runtime: runtime.daily_limit_remaining,
    ),
)
FORECAST: Final = FlippedEnergySensorDescription(
    key="wholesale_price_forecast",
    group="price",
    native_unit_of_measurement=UNIT_AUD_PER_KWH,
    suggested_display_precision=4,
    value=forecast_value,
    attributes=forecast_attributes,
)
RATE_PERIOD: Final = FlippedEnergySensorDescription(
    key="rate_period",
    group="tariff",
    device_class=SensorDeviceClass.ENUM,
    options=list(BAND_STATES.values()),
    value=rate_period_value,
    attributes=rate_period_attributes,
)
RATE_PERIOD_NAME: Final = FlippedEnergySensorDescription(
    key="rate_period_name", group="tariff", value=rate_period_name
)
STATUS_SENSORS: Final = (
    status("account_status", "account"),
    status("tariff_status", "tariff"),
    status("wholesale_price_status", "price"),
)
USAGE_HISTORY_STATUS: Final = FlippedEnergySensorDescription(
    key="usage_history_status",
    group="energy",
    device_class=SensorDeviceClass.ENUM,
    options=STATUS_OPTIONS,
    entity_category=EntityCategory.DIAGNOSTIC,
    value=status_value("energy"),
    attributes=usage_history_status_attributes,
)
SPOT_LINKED_SENSORS: Final = (
    rate("fixed_rate_component", lambda period: period["rateCentsPerKwh"]),
    rate("wholesale_rate_cap", lambda period: period["wholesaleCapCentsPerKwh"]),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FlippedEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            *(
                FlippedEnergySensor(entry, description)
                for description in SENSORS
                if not description.key.startswith("wholesale_")
            ),
            RatePeriodSensor(entry, RATE_PERIOD),
            RatePeriodNameSensor(entry, RATE_PERIOD_NAME),
            *(
                StatusSensor(entry, description)
                for description in STATUS_SENSORS
                if not description.key.startswith("wholesale_")
            ),
            UsageHistoryStatusSensor(entry, USAGE_HISTORY_STATUS),
        ]
    )
    wholesale = [d for d in SENSORS if d.key.startswith("wholesale_")]
    statuses = [d for d in STATUS_SENSORS if d.key.startswith("wholesale_")]
    SpotLinkedEntities(
        hass,
        entry,
        Platform.SENSOR,
        [d.key for d in wholesale] + [d.key for d in statuses] + [FORECAST.key],
        lambda: [
            *(FlippedEnergySensor(entry, d) for d in wholesale),
            *(StatusSensor(entry, d) for d in statuses),
            ForecastSensor(entry, FORECAST),
        ],
        async_add_entities,
        configurable=True,
    ).start()
    SpotLinkedEntities(
        hass,
        entry,
        Platform.SENSOR,
        [description.key for description in SPOT_LINKED_SENSORS],
        lambda: [FlippedEnergySensor(entry, description) for description in SPOT_LINKED_SENSORS],
        async_add_entities,
    ).start()


class FlippedEnergySensor(FlippedEnergyEntity, SensorEntity):
    entity_description: FlippedEnergySensorDescription

    def __init__(
        self, entry: FlippedEnergyConfigEntry, description: FlippedEnergySensorDescription
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        group = self.entity_description.group
        return group is None or group_signals(self.signals, group)["status"] == "ok"

    @property
    def native_value(self) -> Value:
        return self.entity_description.value(self.runtime)

    @property
    def extra_state_attributes(self) -> Attributes:
        return self.entity_description.attributes(self.runtime)


class ForecastSensor(FlippedEnergySensor):
    _unrecorded_attributes = frozenset({"points"})


class RatePeriodSensor(FlippedEnergySensor):
    _unrecorded_attributes = frozenset({"blocks", "schedule"})


class RatePeriodNameSensor(FlippedEnergySensor):
    def __init__(
        self, entry: FlippedEnergyConfigEntry, description: FlippedEnergySensorDescription
    ) -> None:
        super().__init__(entry, description)
        self._logged: set[str] = set()

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        name = rate_period_name(self.runtime)
        if name is None or len(name) <= MAX_STATE_LENGTH:
            return True
        if name not in self._logged:
            self._logged.add(name)
            _LOGGER.error(
                "%s: rate period name is %s characters long, more than a state holds: %s",
                self.entity_id,
                len(name),
                name,
            )
        return False


class StatusSensor(FlippedEnergySensor):
    _unrecorded_attributes = frozenset({"body", "message"})

    @property
    def available(self) -> bool:
        return True


class UsageHistoryStatusSensor(StatusSensor):
    _unrecorded_attributes = StatusSensor._unrecorded_attributes | frozenset(
        {"cost_unknown_hours", "feed_in_unknown_hours", "missing_hours"}
    )
