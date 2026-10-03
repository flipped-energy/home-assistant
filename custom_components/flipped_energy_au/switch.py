from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import ATTR_FRIENDLY_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FlippedEnergyConfigEntry
from .const import CONF_VIRTUAL_DEVICES, DOMAIN
from .entity import FlippedEnergyEntity, SpotLinkedEntities, current_period, signal_unique_id
from .signals import Signals

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class FlippedEnergySwitchDescription(SwitchEntityDescription):
    value: Callable[[Signals], bool | None]


def shoulder(signals: Signals) -> bool | None:
    period = current_period(signals)
    return None if period is None else period["band"] == "shoulder"


SWITCHES: Final = (
    FlippedEnergySwitchDescription(key="peak_rate", value=lambda s: s["tariff"]["peak"]),
    FlippedEnergySwitchDescription(key="off_peak_rate", value=lambda s: s["tariff"]["offPeak"]),
    FlippedEnergySwitchDescription(key="shoulder_rate", value=shoulder),
    FlippedEnergySwitchDescription(
        key="wholesale_price_high", value=lambda s: s["price"]["priceHigh"]
    ),
    FlippedEnergySwitchDescription(
        key="wholesale_price_low", value=lambda s: s["price"]["priceLow"]
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FlippedEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.options.get(CONF_VIRTUAL_DEVICES, True):
        registry = er.async_get(hass)
        for description in SWITCHES:
            entity_id = registry.async_get_entity_id(
                Platform.SWITCH, DOMAIN, signal_unique_id(entry, description.key)
            )
            if entity_id is not None:
                registry.async_remove(entity_id)
        return
    async_add_entities(
        FlippedEnergySwitch(entry, description)
        for description in SWITCHES
        if not description.key.startswith("wholesale_")
    )
    wholesale = [d for d in SWITCHES if d.key.startswith("wholesale_")]
    SpotLinkedEntities(
        hass,
        entry,
        Platform.SWITCH,
        [d.key for d in wholesale],
        lambda: [FlippedEnergySwitch(entry, d) for d in wholesale],
        async_add_entities,
        configurable=True,
    ).start()


class FlippedEnergySwitch(FlippedEnergyEntity, SwitchEntity):
    entity_description: FlippedEnergySwitchDescription

    def __init__(
        self, entry: FlippedEnergyConfigEntry, description: FlippedEnergySwitchDescription
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.entity_description.value(self.signals) is not None

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.signals)

    async def async_turn_on(self, **kwargs: Any) -> None:
        raise self._read_only()

    async def async_turn_off(self, **kwargs: Any) -> None:
        raise self._read_only()

    def _read_only(self) -> HomeAssistantError:
        state = self.hass.states.get(self.entity_id)
        assert state is not None
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="read_only",
            translation_placeholders={"name": str(state.attributes[ATTR_FRIENDLY_NAME])},
        )
