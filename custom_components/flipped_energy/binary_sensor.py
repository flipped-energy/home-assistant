from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FlippedEnergyConfigEntry
from .entity import FlippedEnergyEntity, SpotLinkedEntities, current_period
from .signals import Signals

if TYPE_CHECKING:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
else:
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class FlippedEnergyBinarySensorDescription(BinarySensorEntityDescription):
    value: Callable[[Signals], bool | None]
    attributes: Callable[[Signals], Mapping[str, Any]] | None = None


def wholesale_linked(signals: Signals) -> bool | None:
    period = current_period(signals)
    return None if period is None else period["wholesaleLinked"]


BINARY_SENSORS: Final = (
    FlippedEnergyBinarySensorDescription(
        key="wholesale_price_negative", value=lambda s: s["price"]["negative"]
    ),
    FlippedEnergyBinarySensorDescription(
        key="token_expiring",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s["account"]["tokenExpiringSoon"],
        attributes=lambda s: {
            "expires_at": s["account"]["tokenExpiresAt"],
            "scope": s["account"]["tokenScope"],
        },
    ),
)
SPOT_LINKED_BINARY_SENSORS: Final = (
    FlippedEnergyBinarySensorDescription(key="rate_wholesale_linked", value=wholesale_linked),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FlippedEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        FlippedEnergyBinarySensor(entry, d)
        for d in BINARY_SENSORS
        if not d.key.startswith("wholesale_")
    )
    wholesale = [d for d in BINARY_SENSORS if d.key.startswith("wholesale_")]
    SpotLinkedEntities(
        hass,
        entry,
        Platform.BINARY_SENSOR,
        [d.key for d in wholesale],
        lambda: [FlippedEnergyBinarySensor(entry, d) for d in wholesale],
        async_add_entities,
        configurable=True,
    ).start()
    SpotLinkedEntities(
        hass,
        entry,
        Platform.BINARY_SENSOR,
        [d.key for d in SPOT_LINKED_BINARY_SENSORS],
        lambda: [FlippedEnergyBinarySensor(entry, d) for d in SPOT_LINKED_BINARY_SENSORS],
        async_add_entities,
    ).start()


class FlippedEnergyBinarySensor(FlippedEnergyEntity, BinarySensorEntity):
    entity_description: FlippedEnergyBinarySensorDescription

    def __init__(
        self, entry: FlippedEnergyConfigEntry, description: FlippedEnergyBinarySensorDescription
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self.entity_description.value(self.signals) is not None

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.signals)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        attributes = self.entity_description.attributes
        return None if attributes is None else attributes(self.signals)
