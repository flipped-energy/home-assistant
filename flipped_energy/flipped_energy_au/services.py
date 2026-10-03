from collections.abc import Callable
from typing import TYPE_CHECKING, Final, cast

from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import service
from homeassistant.helpers.selector import ConfigEntrySelector
from homeassistant.util.json import JsonObjectType, JsonValueType

from .const import DOMAIN, TIER_STATES, to_dollars
from .runtime import InstanceRuntime
from .signals.model import Fault, ForecastWindow

if TYPE_CHECKING:
    import probatio as vol
else:
    import voluptuous as vol

SERVICE_GET_WHOLESALE_PRICE_FORECAST: Final = "get_wholesale_price_forecast"
SERVICE_GET_RATE_SCHEDULE: Final = "get_rate_schedule"
SERVICE_GET_USAGE_HISTORY: Final = "get_usage_history"

SERVICE_SCHEMA: Final = vol.Schema(
    {vol.Required(ATTR_CONFIG_ENTRY_ID): ConfigEntrySelector({"integration": DOMAIN})}
)


def group_fault_error(group: str, fault: Fault) -> HomeAssistantError:
    code = fault["code"]
    if "httpStatus" in fault:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="group_faulted_http",
            translation_placeholders={
                "group": group,
                "code": code,
                "status": str(fault["httpStatus"]),
                "body": fault["body"],
            },
        )
    if "message" in fault:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="group_faulted_message",
            translation_placeholders={"group": group, "code": code, "message": fault["message"]},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="group_faulted",
        translation_placeholders={"group": group, "code": code},
    )


def raise_fault(group: str, fault: Fault | None) -> None:
    if fault is not None:
        raise group_fault_error(group, fault)


def forecast_window(window: ForecastWindow | None) -> JsonValueType:
    if window is None:
        return None
    return {
        "from": window["from"],
        "to": window["to"],
        "published_at": window["publishedAt"],
        "min_aud_per_kwh": to_dollars(window["minCentsPerKwh"]),
        "max_aud_per_kwh": to_dollars(window["maxCentsPerKwh"]),
        "level": TIER_STATES[window["tier"]],
        "points": [
            {"start": point["start"], "aud_per_kwh": to_dollars(point["centsPerKwh"])}
            for point in window["points"]
        ],
    }


def wholesale_price_forecast(runtime: InstanceRuntime) -> JsonObjectType:
    price = runtime.signals["price"]
    raise_fault("price", price["fault"])
    forecast = price["forecast"]
    return {
        "next_hour": forecast_window(None if forecast is None else forecast["nextHour"]),
        "ahead": forecast_window(None if forecast is None else forecast["ahead"]),
    }


def rate_schedule(runtime: InstanceRuntime) -> JsonObjectType:
    tariff = runtime.signals["tariff"]
    raise_fault("tariff", tariff["fault"])
    return {
        "structure": tariff["structure"],
        "spot_linked": tariff["spotLinked"],
        "schedule": cast(JsonValueType, tariff["schedule"]),
    }


def usage_history(runtime: InstanceRuntime) -> JsonObjectType:
    energy = runtime.signals["energy"]
    raise_fault("energy", energy["fault"])
    return {
        "nmi": energy["nmi"],
        "latest_interval_end": energy["latestIntervalEnd"],
        "intervals": cast(JsonValueType, energy["intervals"]),
        "days": cast(JsonValueType, energy["days"]),
    }


type Respond = Callable[[InstanceRuntime], JsonObjectType]

ACTIONS: Final[dict[str, Respond]] = {
    SERVICE_GET_WHOLESALE_PRICE_FORECAST: wholesale_price_forecast,
    SERVICE_GET_RATE_SCHEDULE: rate_schedule,
    SERVICE_GET_USAGE_HISTORY: usage_history,
}


def action_handler(
    hass: HomeAssistant, respond: Respond
) -> Callable[[ServiceCall], ServiceResponse]:
    @callback
    def handler(call: ServiceCall) -> ServiceResponse:
        entry = service.async_get_config_entry(hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY_ID])
        runtime: InstanceRuntime = entry.runtime_data
        return respond(runtime)

    return handler


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    for name, respond in ACTIONS.items():
        hass.services.async_register(
            DOMAIN,
            name,
            action_handler(hass, respond),
            SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
