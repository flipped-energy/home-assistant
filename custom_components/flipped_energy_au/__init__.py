import asyncio
import logging
from collections.abc import Coroutine
from datetime import datetime

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .api import FlippedApi
from .const import (
    AUTH_ERROR_BODY,
    AUTH_ERROR_STATUS,
    CONF_ACCOUNT_NUMBER,
    CONF_NMI,
    CONF_PRICE_HIGH_THRESHOLD,
    CONF_PRICE_LOW_THRESHOLD,
    CONF_TOKEN,
    DATA_PRICE_LOOPS,
    DATA_REQUEST_GATES,
    DOMAIN,
    ISSUE_API_REFUSED,
    PLATFORMS,
    USER_AGENT_PRODUCT,
    api_refused_issue_id,
    instance_key,
    to_cents,
)
from .gate import GateRegistry
from .price_loop import PriceLoopRegistry
from .runtime import InstanceRuntime, Settings
from .services import async_setup_services
from .signals.model import EnergySignals
from .signals.timeprim import Instant
from .statistics import StatisticsImporter
from .timers import Action, Cancel, TimerKind

_LOGGER = logging.getLogger(__package__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type FlippedEnergyConfigEntry = ConfigEntry[InstanceRuntime]


class HassScheduler:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def now(self) -> datetime:
        return dt_util.utcnow()

    def call_at(self, target: datetime, action: Action) -> Cancel:
        @callback
        def fire(_: datetime) -> None:
            action()

        return async_track_point_in_utc_time(self._hass, fire, target)

    def armed(self, kind: TimerKind, target: datetime, delay_s: float) -> None:
        _LOGGER.debug("%s timer armed for %s in %s s", kind.value, target.isoformat(), delay_s)


class EntryHooks:
    def __init__(self, hass: HomeAssistant, entry: FlippedEnergyConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    def auth_refused(self, status: int, body: str) -> None:
        self._entry.async_start_reauth(
            self._hass, data={AUTH_ERROR_STATUS: str(status), AUTH_ERROR_BODY: body}
        )

    def auth_restored(self) -> None:
        for flow in list(self._entry.async_get_active_flows(self._hass, {SOURCE_REAUTH})):
            self._hass.config_entries.flow.async_abort(flow["flow_id"])

    def access_refused(self, status: int, body: str, since: str) -> None:
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            api_refused_issue_id(self._entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_API_REFUSED,
            translation_placeholders={
                "name": self._entry.title,
                "status": str(status),
                "body": body,
                "since": since,
            },
        )

    def access_restored(self) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, api_refused_issue_id(self._entry.entry_id))

    def account_pinned(self, account_number: str) -> None:
        self._hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_ACCOUNT_NUMBER: account_number}
        )


async def async_create_api(hass: HomeAssistant, token: str) -> FlippedApi:
    integration = await async_get_integration(hass, DOMAIN)
    return FlippedApi(
        async_get_clientsession(hass), token, f"{USER_AGENT_PRODUCT}/{integration.version}"
    )


def entry_settings(entry: ConfigEntry) -> Settings:
    high = entry.options.get(CONF_PRICE_HIGH_THRESHOLD)
    low = entry.options.get(CONF_PRICE_LOW_THRESHOLD)
    return Settings(
        token=entry.data[CONF_TOKEN],
        account_number=entry.data[CONF_ACCOUNT_NUMBER],
        nmi=entry.data.get(CONF_NMI),
        price_high_threshold=None if high is None else to_cents(float(high)),
        price_low_threshold=None if low is None else to_cents(float(low)),
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    scheduler = HassScheduler(hass)

    def create_task(coro: Coroutine[object, object, None], name: str) -> asyncio.Task[None]:
        return hass.async_create_background_task(coro, name, eager_start=False)

    hass.data[DATA_PRICE_LOOPS] = PriceLoopRegistry(scheduler, create_task)
    hass.data[DATA_REQUEST_GATES] = GateRegistry(scheduler)
    async_setup_services(hass)
    return True


def track_statistics(hass: HomeAssistant, entry: FlippedEnergyConfigEntry) -> None:
    runtime = entry.runtime_data
    importer = StatisticsImporter(
        hass, instance_key(entry.data[CONF_ACCOUNT_NUMBER], entry.data.get(CONF_NMI)), entry.title
    )

    async def import_usage(energy: EnergySignals, window_start: Instant) -> None:
        report = await importer.async_import(energy, window_start)
        if report is not None:
            runtime.statistics_imported(report)

    def usage_synced(window_start: Instant) -> None:
        entry.async_create_task(
            hass,
            import_usage(runtime.signals["energy"], window_start),
            "flipped_energy_au statistics",
            eager_start=False,
        )

    entry.async_on_unload(runtime.add_usage_listener(usage_synced))


async def async_setup_entry(hass: HomeAssistant, entry: FlippedEnergyConfigEntry) -> bool:
    runtime = InstanceRuntime(
        settings=entry_settings(entry),
        api=await async_create_api(hass, entry.data[CONF_TOKEN]),
        scheduler=HassScheduler(hass),
        load_zone=dt_util.async_get_time_zone,
        hooks=EntryHooks(hass, entry),
        loops=hass.data[DATA_PRICE_LOOPS],
        gates=hass.data[DATA_REQUEST_GATES],
    )
    entry.runtime_data = runtime
    issue_id = api_refused_issue_id(entry.entry_id)
    entry.async_on_unload(lambda: ir.async_delete_issue(hass, DOMAIN, issue_id))
    entry.async_on_unload(runtime.close)
    track_statistics(hass, entry)
    entry.async_create_background_task(
        hass, runtime.run(), "flipped_energy_au sync", eager_start=False
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FlippedEnergyConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
