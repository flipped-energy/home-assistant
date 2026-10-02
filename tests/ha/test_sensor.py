import copy
import logging
from typing import cast

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.signals import Json

from ..unit.fakes import Obj, answer
from .conftest import (
    BODIES,
    START,
    ApiFactory,
    make_entry,
    setup_entry,
    startup,
    vector_body,
)

BEFORE_NEXT_INTERVAL = "2026-10-01T02:29:00Z"
PRICE_SENSORS = (
    "sensor.flipped_energy_wholesale_price",
    "sensor.flipped_energy_wholesale_price_level",
    "sensor.flipped_energy_wholesale_price_forecast",
)


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


def named_unit(name: Json) -> Obj:
    body = copy.deepcopy(BODIES.account)
    account = cast(list[Obj], body["accounts"])[0]
    plan = cast(Obj, cast(Obj, account["product"])["currentPlan"])
    cast(list[Obj], plan["billingUnits"])[0]["name"] = name
    return body


async def test_ok_unknown_and_faulted(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    await setup_entry(hass, apis, entry, startup())
    assert {
        entity_id.removeprefix("sensor.flipped_energy_"): state_of(hass, entity_id)
        for entity_id in sorted(
            s.entity_id
            for s in hass.states.async_all("sensor")
            if s.entity_id.startswith("sensor.flipped_energy_")
        )
    } == {
        "account_status": "ok",
        "current_rate": "32.197",
        "next_rate_change": "unknown",
        "rate_after_allowance": "unknown",
        "rate_allowance": "unknown",
        "rate_period": "anytime",
        "rate_period_name": "Anytime Rate",
        "rates_status": "ok",
        "usage_history_status": "ok",
        "usage_history_up_to": "unknown",
        "wholesale_price": "9.6",
        "wholesale_price_forecast": "unknown",
        "wholesale_price_level": "normal",
        "wholesale_price_status": "ok",
    }
    price = hass.states.get("sensor.flipped_energy_wholesale_price")
    assert price is not None
    assert price.attributes["interval_start"] == "2026-10-01T02:20:00Z"
    assert price.attributes["unit_of_measurement"] == "c/kWh"
    assert price.attributes["state_class"] == "measurement"
    period = hass.states.get("sensor.flipped_energy_rate_period")
    assert period is not None
    assert {key: period.attributes[key] for key in ("name", "structure", "spot_linked")} == {
        "name": "Anytime Rate",
        "structure": "flat",
        "spot_linked": False,
    }
    status = hass.states.get("sensor.flipped_energy_wholesale_price_status")
    assert status is not None
    assert {k: status.attributes[k] for k in ("http_status", "body", "body_bytes", "message")} == {
        "http_status": None,
        "body": None,
        "body_bytes": None,
        "message": None,
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_faulted_group_is_unavailable_with_fault_on_status_sensor(
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
    assert [state_of(hass, entity_id) for entity_id in PRICE_SENSORS] == ["unavailable"] * 3
    assert state_of(hass, "sensor.flipped_energy_current_rate") == "32.197"
    status = hass.states.get("sensor.flipped_energy_wholesale_price_status")
    assert status is not None
    assert status.state == "http_error"
    assert {k: status.attributes[k] for k in ("http_status", "body", "body_bytes", "message")} == {
        "http_status": 503,
        "body": body,
        "body_bytes": len(body.encode("utf-8")),
        "message": None,
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_timestamps_and_forecast(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(BEFORE_NEXT_INTERVAL)
    entry = make_entry()
    await setup_entry(
        hass,
        apis,
        entry,
        startup(
            account=vector_body("energy/usage-mapping.json", "account"),
            meters=vector_body("energy/usage-mapping.json", "meters"),
            outlook=vector_body("price/forecast-next-hour.json", "outlook"),
            half_hourly=vector_body("energy/usage-mapping.json", "usageHalfHourly"),
            daily=vector_body("energy/usage-mapping.json", "usageDaily"),
        ),
    )
    assert state_of(hass, "sensor.flipped_energy_next_rate_change") == "2026-10-01T04:00:00+00:00"
    assert state_of(hass, "sensor.flipped_energy_usage_history_up_to") == (
        "2026-09-29T01:30:00+00:00"
    )
    status = hass.states.get("sensor.flipped_energy_usage_history_status")
    assert status is not None
    assert (status.state, status.attributes["nmi"]) == ("ok", "4102000001")
    forecast = hass.states.get("sensor.flipped_energy_wholesale_price_forecast")
    assert forecast is not None
    assert forecast.state == "34.9"
    assert {
        key: forecast.attributes[key]
        for key in ("min_cents_per_kwh", "level", "from", "to", "published_at")
    } == {
        "min_cents_per_kwh": 9.1,
        "level": "elevated",
        "from": "2026-10-01T02:30:00Z",
        "to": "2026-10-01T03:30:00Z",
        "published_at": "2026-10-01T02:30:00Z",
    }
    assert forecast.attributes["points"][:2] == [
        {"start": "2026-10-01T02:30:00Z", "cents_per_kwh": 9.1},
        {"start": "2026-10-01T02:35:00Z", "cents_per_kwh": 9.8},
    ]
    assert len(forecast.attributes["points"]) == 12
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("N" * 255, "N" * 255), ("N" * 256, "unavailable"), (None, "")],
)
async def test_rate_period_name(
    hass: HomeAssistant,
    apis: ApiFactory,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    name: str | None,
    expected: str,
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    caplog.set_level(logging.ERROR, logger="custom_components.flipped_energy")
    await setup_entry(hass, apis, entry, startup(account=named_unit(name)))
    entry.runtime_data.recompute()
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.flipped_energy_rate_period_name") == expected
    period = hass.states.get("sensor.flipped_energy_rate_period")
    assert period is not None
    assert period.attributes["name"] == ("" if name is None else name)
    records = [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.flipped_energy" and "rate period name" in record.msg
    ]
    if expected == "unavailable":
        assert len(records) == 1
        assert "256 characters" in records[0]
        assert records[0].endswith("N" * 256)
    else:
        assert records == []
    assert await hass.config_entries.async_unload(entry.entry_id)
