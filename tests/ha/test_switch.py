import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.flipped_energy.api import Endpoint

from ..unit.fakes import Obj, answer
from .conftest import (
    START,
    ApiFactory,
    account_with,
    make_entry,
    outlook,
    setup_entry,
    startup,
    vector_body,
    wait_until,
)

SWITCHES = (
    "switch.flipped_energy_peak_rate",
    "switch.flipped_energy_off_peak_rate",
    "switch.flipped_energy_shoulder_rate",
    "switch.flipped_energy_wholesale_price_high",
    "switch.flipped_energy_wholesale_price_low",
)
OFF_PEAK_AT = "2026-10-01T02:30:00Z"


async def test_virtual_devices_can_be_removed_and_restored(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    await setup_entry(hass, apis, entry, startup())
    assert hass.states.get(SWITCHES[0]) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={"virtualDevices": False})
    apis.script(*startup())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_until(entry.runtime_data, lambda: not apis.last.remaining)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert all(hass.states.get(entity_id) is None for entity_id in SWITCHES)
    assert all(registry.async_get(entity_id) is None for entity_id in SWITCHES)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={"virtualDevices": True})
    apis.script(*startup())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_until(entry.runtime_data, lambda: not apis.last.remaining)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCHES[0]) is not None
    assert hass.states.get(SWITCHES[3]) is None
    assert hass.states.get(SWITCHES[4]) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


def off_peak_low() -> list[Obj]:
    return startup(
        account=vector_body("price/forecast-next-hour.json", "account"),
        outlook=outlook("2026-10-01T12:30:00+10:00", tier="UnusuallyLow", cents=-3.0),
    )


SCENARIOS: dict[str, tuple[str, list[Obj], tuple[str, str, str, str, str]]] = {
    "peak_and_spike": (
        "2026-10-01T07:00:00Z",
        startup(
            account=vector_body("tariff/tou-two-rate-peak.json", "account"),
            outlook=outlook("2026-10-01T17:00:00+10:00", tier="Spike", cents=250.0),
        ),
        ("on", "off", "off", "on", "off"),
    ),
    "off_peak_and_unusually_low": (OFF_PEAK_AT, off_peak_low(), ("off", "on", "off", "off", "on")),
    "shoulder_and_normal_price": (
        START,
        startup(account=vector_body("tariff/tou-three-rate-shoulder.json", "account")),
        ("off", "off", "on", "off", "off"),
    ),
    "flat_plan_normal_price": (START, startup(), ("off", "off", "off", "off", "off")),
    "price_group_faulted": (
        START,
        [
            *startup()[:3],
            answer(Endpoint.OUTLOOK, 503, "Service Unavailable"),
            *startup()[4:],
        ],
        ("off", "off", "off", "unavailable", "unavailable"),
    ),
    "tariff_group_faulted": (
        START,
        startup(account=account_with(accountState="TERMINATED")),
        ("unavailable", "unavailable", "unavailable", "off", "off"),
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_state_mapping(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory, scenario: str
) -> None:
    when, responses, expected = SCENARIOS[scenario]
    freezer.move_to(when)
    entry = make_entry()
    await setup_entry(hass, apis, entry, responses)
    states = tuple(hass.states.get(entity_id) for entity_id in SWITCHES)
    assert tuple(state.state if state else None for state in states) == expected
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_toggle_is_refused(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory, service: str
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    await setup_entry(hass, apis, entry, startup())
    entity_id = "switch.flipped_energy_peak_rate"
    before = hass.states.get(entity_id)
    assert before is not None
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
    assert str(raised.value) == (
        "Flipped Energy Peak Rate is a read-only signal and cannot be switched"
    )
    await hass.async_block_till_done()
    after = hass.states.get(entity_id)
    assert after is not None
    assert after.state == before.state == "off"
    assert after.last_updated == before.last_updated
    assert [e for e in events if e.data["entity_id"] == entity_id] == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_during_on_period_goes_unavailable_then_on(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(OFF_PEAK_AT)
    entry = make_entry()
    await setup_entry(hass, apis, entry, off_peak_low())
    entity_id = "switch.flipped_energy_off_peak_rate"
    seen: list[str] = []

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        state = "removed" if new_state is None else new_state.state
        if not seen or seen[-1] != state:
            seen.append(state)

    remove = async_track_state_change_event(hass, entity_id, record)
    apis.script(*off_peak_low())
    assert await hass.config_entries.async_reload(entry.entry_id)
    api = apis.last
    await wait_until(entry.runtime_data, lambda: not api.remaining)
    await hass.async_block_till_done()
    remove()
    assert seen == ["unavailable", "on"]
    assert await hass.config_entries.async_unload(entry.entry_id)
