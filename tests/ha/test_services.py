import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.json import JsonObjectType

from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.const import DOMAIN

from ..unit.fakes import answer
from .conftest import (
    START,
    ApiFactory,
    make_entry,
    setup_entry,
    startup,
    vector_body,
)

BEFORE_NEXT_INTERVAL = "2026-10-01T02:29:00Z"
USAGE = "energy/usage-mapping.json"


async def call(hass: HomeAssistant, action: str, entry_id: str) -> JsonObjectType:
    response = await hass.services.async_call(
        DOMAIN,
        action,
        {"config_entry_id": entry_id},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    return response


async def test_the_three_responses(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(BEFORE_NEXT_INTERVAL)
    entry = make_entry()
    await setup_entry(
        hass,
        apis,
        entry,
        startup(
            account=vector_body(USAGE, "account"),
            meters=vector_body(USAGE, "meters"),
            outlook=vector_body("price/forecast-next-hour.json", "outlook"),
            half_hourly=vector_body(USAGE, "usageHalfHourly"),
            daily=vector_body(USAGE, "usageDaily"),
        ),
    )
    signals = entry.runtime_data.signals

    forecast = await call(hass, "get_wholesale_price_forecast", entry.entry_id)
    assert set(forecast) == {"next_hour", "ahead"}
    next_hour = forecast["next_hour"]
    assert isinstance(next_hour, dict)
    assert {key: next_hour[key] for key in next_hour if key != "points"} == {
        "from": "2026-10-01T02:30:00Z",
        "to": "2026-10-01T03:30:00Z",
        "published_at": "2026-10-01T02:30:00Z",
        "min_cents_per_kwh": 9.1,
        "max_cents_per_kwh": 34.9,
        "level": "elevated",
    }
    points = next_hour["points"]
    assert isinstance(points, list)
    assert len(points) == 12
    assert points[:2] == [
        {"start": "2026-10-01T02:30:00Z", "cents_per_kwh": 9.1},
        {"start": "2026-10-01T02:35:00Z", "cents_per_kwh": 9.8},
    ]
    price_forecast = signals["price"]["forecast"]
    assert price_forecast is not None
    assert (forecast["ahead"] is None) == (price_forecast["ahead"] is None)

    schedule = await call(hass, "get_rate_schedule", entry.entry_id)
    tariff = signals["tariff"]
    assert schedule == {
        "structure": tariff["structure"],
        "spot_linked": tariff["spotLinked"],
        "schedule": tariff["schedule"],
    }
    assert isinstance(schedule["schedule"], list) and schedule["schedule"]

    history = await call(hass, "get_usage_history", entry.entry_id)
    energy = signals["energy"]
    assert history == {
        "nmi": "4102000001",
        "latest_interval_end": "2026-09-29T01:30:00Z",
        "intervals": energy["intervals"],
        "days": energy["days"],
    }
    assert isinstance(history["intervals"], list) and history["intervals"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_http_fault_carries_the_raw_body(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    body = 'Service Unavailable {"retry": "later"} <b>_now_</b>'
    await setup_entry(
        hass,
        apis,
        entry,
        [*startup()[:3], answer(Endpoint.OUTLOOK, 503, body), *startup()[4:]],
    )
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "get_wholesale_price_forecast", entry.entry_id)
    assert raised.value.translation_key == "group_faulted_http"
    assert str(raised.value) == f"price group is faulted (http_error): HTTP 503: {body}"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_message_fault_carries_the_raw_message(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    message = "ClientConnectorError: scripted {x} <b>_raw_</b>"
    await setup_entry(
        hass,
        apis,
        entry,
        [*startup()[:4], answer(Endpoint.USAGE_HALF_HOURLY, network=message), startup()[5]],
    )
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "get_usage_history", entry.entry_id)
    assert raised.value.translation_key == "group_faulted_message"
    assert message in str(raised.value)
    assert str(raised.value).startswith("energy group is faulted (network_error): ")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_fault_without_status_or_message_is_not_empty(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    await setup_entry(hass, apis, entry, startup()[:4])
    fault = entry.runtime_data.signals["energy"]["fault"]
    assert fault == {"code": "not_loaded"}
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, "get_usage_history", entry.entry_id)
    assert raised.value.translation_key == "group_faulted"
    assert str(raised.value) == "energy group is faulted (not_loaded)"
    assert await hass.config_entries.async_unload(entry.entry_id)
