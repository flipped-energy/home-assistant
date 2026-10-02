from collections.abc import Callable, Iterable
from typing import Literal

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FlippedEnergyConfigEntry
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_NMI,
    CONF_SPOT_PRICES,
    DOMAIN,
    MANUFACTURER,
    instance_key,
)
from .runtime import InstanceRuntime
from .signals import Signals
from .signals.model import AccountSignals, EnergySignals, Period, PriceSignals, TariffSignals

type Group = Literal["account", "tariff", "price", "energy"]
type GroupSignals = AccountSignals | TariffSignals | PriceSignals | EnergySignals


def entry_instance_key(entry: FlippedEnergyConfigEntry) -> str:
    return instance_key(entry.data[CONF_ACCOUNT_NUMBER], entry.data.get(CONF_NMI))


def signal_unique_id(entry: FlippedEnergyConfigEntry, key: str) -> str:
    return f"flipped:{entry_instance_key(entry)}:{key}"


def group_signals(signals: Signals, group: Group) -> GroupSignals:
    return signals[group]


def current_period(signals: Signals) -> Period | None:
    return signals["tariff"]["period"]


class FlippedEnergyEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: FlippedEnergyConfigEntry, key: str) -> None:
        self.runtime: InstanceRuntime = entry.runtime_data
        self._attr_translation_key = key
        self._attr_unique_id = signal_unique_id(entry, key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_instance_key(entry))},
            name=entry.title,
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def signals(self) -> Signals:
        return self.runtime.signals

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.add_listener(self.async_write_ha_state))


class SpotLinkedEntities:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: FlippedEnergyConfigEntry,
        platform: Platform,
        keys: Iterable[str],
        create: Callable[[], Iterable[Entity]],
        add: AddConfigEntryEntitiesCallback,
        configurable: bool = False,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._platform = platform
        self._unique_ids = [signal_unique_id(entry, key) for key in keys]
        self._create = create
        self._add = add
        self._added = False
        self._configurable = configurable

    def start(self) -> None:
        self._entry.async_on_unload(self._entry.runtime_data.add_listener(self.update))
        self.update()

    @callback
    def update(self) -> None:
        tariff = self._entry.runtime_data.signals["tariff"]
        configured = self._entry.options.get(CONF_SPOT_PRICES) if self._configurable else None
        if configured is None and tariff["status"] != "ok":
            return
        if configured if configured is not None else tariff["spotLinked"]:
            if not self._added:
                self._added = True
                self._add(self._create())
            return
        self._added = False
        registry = er.async_get(self._hass)
        for unique_id in self._unique_ids:
            entity_id = registry.async_get_entity_id(self._platform, DOMAIN, unique_id)
            if entity_id is not None:
                registry.async_remove(entity_id)
