import json

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.flipped_energy.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    ACCOUNT_NUMBER,
    TOKEN,
    ApiFactory,
    make_entry,
    setup_entry,
    startup,
    vector_body,
)

USAGE = "energy/usage-mapping.json"


async def test_no_token_in_the_output(
    hass: HomeAssistant,
    apis: ApiFactory,
    freezer: FrozenDateTimeFactory,
    snapshot: SnapshotAssertion,
) -> None:
    freezer.move_to("2026-10-01T02:29:00Z")
    entry = make_entry(nmi="4102000001")
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
    await async_wait_recording_done(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diagnostics)
    assert TOKEN not in text
    assert entry.runtime_data.settings.token_preview not in text
    assert ACCOUNT_NUMBER not in text
    assert "4102000001" not in text
    assert diagnostics["statistics"] is not None
    assert diagnostics == snapshot
    assert await hass.config_entries.async_unload(entry.entry_id)
