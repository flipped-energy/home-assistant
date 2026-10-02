from typing import Final

from homeassistant.const import Platform
from homeassistant.util.hass_dict import HassKey

from .gate import GateRegistry
from .price_loop import PriceLoopRegistry

DOMAIN: Final = "flipped_energy"
PLATFORMS: Final = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

CONF_VIRTUAL_DEVICES: Final = "virtualDevices"
CONF_SPOT_PRICES: Final = "spotPrices"

CONF_TOKEN: Final = "token"
CONF_ACCOUNT_NUMBER: Final = "accountNumber"
CONF_NMI: Final = "nmi"
CONF_PRICE_HIGH_THRESHOLD: Final = "priceHighThresholdCentsPerKwh"
CONF_PRICE_LOW_THRESHOLD: Final = "priceLowThresholdCentsPerKwh"

TOKEN_PREFIX: Final = "fdk_"
DEVICE_NAME: Final = "Flipped Energy"
MANUFACTURER: Final = "Flipped Energy"
USER_AGENT_PRODUCT: Final = "flipped-energy-home-assistant"
UNIT_CENTS_PER_KWH: Final = "c/kWh"
MAX_STATE_LENGTH: Final = 255
TIER_STATES: Final = {
    "UnusuallyLow": "unusually_low",
    "Normal": "normal",
    "Elevated": "elevated",
    "Spike": "spike",
}

AUTH_ERROR_STATUS: Final = "auth_error_status"
AUTH_ERROR_BODY: Final = "auth_error_body"
ISSUE_API_REFUSED: Final = "api_refused"

DATA_PRICE_LOOPS: HassKey[PriceLoopRegistry] = HassKey("flipped_energy_price_loops")
DATA_REQUEST_GATES: HassKey[GateRegistry] = HassKey("flipped_energy_request_gates")


def instance_key(account_number: str, nmi: str | None) -> str:
    return account_number if nmi is None else f"{account_number}:{nmi}"


def device_title(account_number: str, nmi: str | None, first: bool) -> str:
    if first:
        return DEVICE_NAME
    title = f"{DEVICE_NAME} {account_number[-4:]}"
    return title if nmi is None else f"{title} {nmi[-4:]}"


def api_refused_issue_id(entry_id: str) -> str:
    return f"{ISSUE_API_REFUSED}_{entry_id}"
