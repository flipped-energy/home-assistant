import json
from pathlib import Path
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.flipped_energy import runtime as runtime_module
from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.const import DATA_PRICE_LOOPS, DOMAIN, USER_AGENT_PRODUCT
from custom_components.flipped_energy.price_loop import LoopState
from custom_components.flipped_energy.runtime import Slot
from custom_components.flipped_energy.signals import Config, Signals, Snapshot

from ..unit.fakes import answer
from .conftest import (
    BODIES,
    START,
    TOKEN,
    ApiFactory,
    make_entry,
    move_to,
    setup_entry,
    startup,
    wait_until,
)

MANIFEST = Path(__file__).parents[2] / "custom_components" / "flipped_energy" / "manifest.json"
VERSION = json.loads(MANIFEST.read_text())["version"]
STATUS_SENSORS = {
    "sensor.flipped_energy_account_status",
    "sensor.flipped_energy_rates_status",
    "sensor.flipped_energy_wholesale_price_status",
    "sensor.flipped_energy_usage_history_status",
}
SIGNAL_ENTITIES = {
    "switch.flipped_energy_peak_rate",
    "switch.flipped_energy_off_peak_rate",
    "switch.flipped_energy_shoulder_rate",
    "switch.flipped_energy_wholesale_price_high",
    "switch.flipped_energy_wholesale_price_low",
    "binary_sensor.flipped_energy_wholesale_price_negative",
    "binary_sensor.flipped_energy_token_expiring_soon",
    "sensor.flipped_energy_wholesale_price",
    "sensor.flipped_energy_wholesale_price_level",
    "sensor.flipped_energy_wholesale_price_forecast",
    "sensor.flipped_energy_current_rate",
    "sensor.flipped_energy_rate_allowance",
    "sensor.flipped_energy_rate_after_allowance",
    "sensor.flipped_energy_rate_period",
    "sensor.flipped_energy_rate_period_name",
    "sensor.flipped_energy_next_rate_change",
    "sensor.flipped_energy_usage_history_up_to",
}
NEXT_DAILY_E1 = "2026-10-02T00:01:00Z"
FRESH_AT_NEXT_DAILY_E1 = "2026-10-02T10:00:00+10:00"
NON_GATEWAY_401 = (
    '{"error":"grant_refused","message":"Grant was revoked: the user\'s security stamp '
    'has changed since it was issued."}'
)
DEVELOPER_MODE_403 = '{"error":"developer_mode_disabled","message":"APIs are off <b>_now_</b>"}'


def own_states(hass: HomeAssistant) -> dict[str, str]:
    return {
        state.entity_id: state.state
        for state in hass.states.async_all()
        if state.entity_id.split(".", 1)[1].startswith("flipped_energy_")
    }


async def test_setup_creates_entities_all_unavailable(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    entry.add_to_hass(hass)
    apis.script()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    states = own_states(hass)
    assert set(states) == SIGNAL_ENTITIES | STATUS_SENSORS
    assert {entity_id: states[entity_id] for entity_id in SIGNAL_ENTITIES} == dict.fromkeys(
        SIGNAL_ENTITIES, "unavailable"
    )
    assert {entity_id: states[entity_id] for entity_id in STATUS_SENSORS} == dict.fromkeys(
        STATUS_SENSORS, "not_loaded"
    )
    calls = er.async_get(hass).async_get("sensor.flipped_energy_api_calls_remaining_today")
    assert calls is not None
    assert calls.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert calls.entity_category is EntityCategory.DIAGNOSTIC
    assert apis.tokens == [TOKEN]
    assert apis.user_agents == [f"{USER_AGENT_PRODUCT}/{VERSION}"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_cancels_tasks_and_timers(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    api = await setup_entry(hass, apis, entry, startup())
    loops = hass.data[DATA_PRICE_LOOPS]
    loop = loops.get(TOKEN, "Ausgrid")
    assert loop is not None
    assert loop.running
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert loops.get(TOKEN, "Ausgrid") is None
    assert loop.state is LoopState.STOPPED
    sent = len(api.transcript)
    await move_to(hass, freezer, "2026-10-03T02:21:10Z")
    assert len(api.transcript) == sent
    assert set(own_states(hass).values()) == {"unavailable"}


async def test_zone_awaited_before_first_computation_with_account_body(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    events: list[str] = []
    load_zone = dt_util.async_get_time_zone
    compute_signals = runtime_module.compute_signals

    async def wrapped_load_zone(name: str) -> object:
        events.append(f"zone {name}")
        return await load_zone(name)

    def wrapped_compute(
        instant: object, config: Config, account: Snapshot, *rest: Snapshot
    ) -> Signals:
        if account["body"] is not None:
            events.append("compute")
        return compute_signals(instant, config, account, *rest)

    entry = make_entry()
    with (
        patch.object(dt_util, "async_get_time_zone", wrapped_load_zone),
        patch.object(runtime_module, "compute_signals", wrapped_compute),
    ):
        await setup_entry(hass, apis, entry, startup())
    assert events[0] == "zone Australia/Sydney"
    assert "compute" in events
    assert list(entry.runtime_data.zones) == ["Australia/Sydney"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_non_gateway_401_opens_reauth_and_recovering_daily_e1_closes_it(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    entry.add_to_hass(hass)
    apis.script(
        answer(Endpoint.ACCOUNT_DATA, 401, NON_GATEWAY_401),
        *startup(outlook=BODIES.outlook(FRESH_AT_NEXT_DAILY_E1)),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    await wait_until(runtime, lambda: runtime.snapshots[Slot.ACCOUNT]["error"] is not None)
    await hass.async_block_till_done()
    flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert len(flows) == 1
    form = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    assert form["step_id"] == "reauth_confirm"
    assert form["errors"] == {"base": "http_error"}
    placeholders = form["description_placeholders"]
    assert placeholders is not None
    assert (placeholders["status"], placeholders["body"]) == ("401", NON_GATEWAY_401)
    assert [r["path"] for r in apis.last.transcript] == [Endpoint.ACCOUNT_DATA.value]
    api = apis.last
    await move_to(hass, freezer, NEXT_DAILY_E1)
    await wait_until(runtime, lambda: not api.remaining)
    await hass.async_block_till_done()
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert runtime.signals["account"]["status"] == "ok"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_403_creates_issue_until_success(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    entry.add_to_hass(hass)
    apis.script(
        answer(Endpoint.ACCOUNT_DATA, 403, DEVELOPER_MODE_403),
        *startup(outlook=BODIES.outlook(FRESH_AT_NEXT_DAILY_E1)),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    await wait_until(runtime, lambda: runtime.snapshots[Slot.ACCOUNT]["error"] is not None)
    issue_id = f"api_refused_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "api_refused"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {
        "name": "Flipped Energy",
        "status": "403",
        "body": DEVELOPER_MODE_403,
        "since": START,
    }
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    api = apis.last
    await move_to(hass, freezer, NEXT_DAILY_E1)
    await wait_until(runtime, lambda: not api.remaining)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_403_issue_removed_on_unload(
    hass: HomeAssistant, apis: ApiFactory, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    entry = make_entry()
    entry.add_to_hass(hass)
    apis.script(answer(Endpoint.ACCOUNT_DATA, 403, DEVELOPER_MODE_403))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    await wait_until(runtime, lambda: runtime.snapshots[Slot.ACCOUNT]["error"] is not None)
    issue_id = f"api_refused_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
