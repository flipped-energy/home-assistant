import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.const import DOMAIN
from custom_components.flipped_energy.signals import Json

from ..unit.fakes import Obj, answer
from .conftest import (
    ACCOUNT_NUMBER,
    BODIES,
    START,
    ApiFactory,
    make_entry,
    move_to,
    outlook,
    setup_entry,
    startup,
    vector_body,
    wait_until,
)

NEGATIVE = "binary_sensor.flipped_energy_wholesale_price_negative"
TOKEN_EXPIRING = "binary_sensor.flipped_energy_token_expiring_soon"
LINKED = "binary_sensor.flipped_energy_wholesale_linked_rate"
FIXED = "sensor.flipped_energy_fixed_rate_component"
CAP = "sensor.flipped_energy_wholesale_rate_cap"
PREVIEW = "fdk_SEQU…wXyZ"
SPOT_AT = "2026-10-01T02:30:00Z"
NEXT_ACCOUNT_SYNC = "2026-10-01T14:01:00Z"


def tokens(expires_at: str, preview: str = PREVIEW) -> Json:
    return {"tokens": [{"tokenPreview": preview, "expiresAt": expires_at, "scope": "read"}]}


SCENARIOS: dict[str, tuple[list[Obj], str, str]] = {
    "negative_price_expiring_token": (
        startup(
            outlook=outlook("2026-10-01T12:20:00+10:00", cents=-3.0),
            tokens=tokens("2026-10-08T00:00:00Z"),
        ),
        "on",
        "on",
    ),
    "positive_price_distant_expiry": (
        startup(tokens=tokens("2027-09-30T00:00:00Z")),
        "off",
        "off",
    ),
    "price_faulted_no_matching_token": (
        [
            *startup(tokens=tokens("2026-10-08T00:00:00Z", "fdk_OTHE…AbCd"))[:3],
            answer(Endpoint.OUTLOOK, 503, "Service Unavailable"),
            *startup()[4:],
        ],
        "unavailable",
        "unavailable",
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_state_mapping(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory, scenario: str
) -> None:
    responses, negative, expiring = SCENARIOS[scenario]
    freezer.move_to(START)
    entry = make_entry()
    await setup_entry(hass, apis, entry, responses)
    assert hass.states.get("sensor.flipped_energy_account_status").state == "ok"
    negative_state = hass.states.get(NEGATIVE)
    expiring_state = hass.states.get(TOKEN_EXPIRING)
    assert (negative_state.state, expiring_state.state) == (negative, expiring)
    if expiring != "unavailable":
        assert expiring_state.attributes["device_class"] == "problem"
        assert expiring_state.attributes["scope"] == "read"
        assert expiring_state.attributes["expires_at"] in (
            "2026-10-08T00:00:00Z",
            "2027-09-30T00:00:00Z",
        )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_wholesale_linked_entities_added_and_removed(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(SPOT_AT)
    entry = make_entry()
    api = await setup_entry(
        hass,
        apis,
        entry,
        startup(
            account=vector_body("tariff/spot-stacked.json", "account"),
            outlook=outlook("2026-10-01T12:30:00+10:00"),
        ),
    )
    assert hass.states.get(LINKED).state == "on"
    assert hass.states.get(FIXED).state == "14.982"
    assert hass.states.get(CAP).state == "unknown"
    assert hass.states.get("sensor.flipped_energy_current_rate").state == "unknown"
    registry = er.async_get(hass)
    unique_id = f"flipped:{ACCOUNT_NUMBER}:rate_wholesale_linked"
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id) == LINKED
    api.remaining.extend(
        [
            answer(Endpoint.WAIT, network="ClientConnectorError: scripted"),
            answer(Endpoint.ACCOUNT_DATA, body=BODIES.account),
            answer(Endpoint.METERS, body=BODIES.meters),
            answer(Endpoint.TOKENS, body=BODIES.tokens),
            answer(Endpoint.USAGE_HALF_HOURLY, body=[]),
            answer(Endpoint.USAGE_DAILY, body=[]),
        ]
    )
    await move_to(hass, freezer, NEXT_ACCOUNT_SYNC)
    await wait_until(entry.runtime_data, lambda: not api.remaining)
    await hass.async_block_till_done()
    assert entry.runtime_data.signals["tariff"]["spotLinked"] is False
    for entity_id in (LINKED, FIXED, CAP):
        assert hass.states.get(entity_id) is None
        assert registry.async_get(entity_id) is None
    assert hass.states.get("sensor.flipped_energy_current_rate").state == "32.197"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_goes_unavailable_then_on(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    negative = startup(outlook=outlook("2026-10-01T12:20:00+10:00", cents=-3.0))
    await setup_entry(hass, apis, entry, negative)
    assert hass.states.get(NEGATIVE).state == "on"
    seen: list[str] = []

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        state = "removed" if new_state is None else new_state.state
        if not seen or seen[-1] != state:
            seen.append(state)

    remove = async_track_state_change_event(hass, NEGATIVE, record)
    apis.script(*startup(outlook=outlook("2026-10-01T12:20:00+10:00", cents=-3.0)))
    assert await hass.config_entries.async_reload(entry.entry_id)
    api = apis.last
    await wait_until(entry.runtime_data, lambda: not api.remaining)
    await hass.async_block_till_done()
    remove()
    assert seen == ["unavailable", "on"]
    assert await hass.config_entries.async_unload(entry.entry_id)
