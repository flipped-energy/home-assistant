import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import async_create_api
from .api import ApiNetworkError, ApiResponse, BodyInvalid
from .const import (
    AUTH_ERROR_BODY,
    AUTH_ERROR_STATUS,
    CONF_ACCOUNT_NUMBER,
    CONF_NMI,
    CONF_PRICE_HIGH_THRESHOLD,
    CONF_PRICE_LOW_THRESHOLD,
    CONF_SPOT_PRICES,
    CONF_TOKEN,
    CONF_VIRTUAL_DEVICES,
    DOMAIN,
    TOKEN_PREFIX,
    UNIT_AUD_PER_KWH,
    device_title,
    instance_key,
)
from .signals import Config, Json, Snapshot
from .signals.account import Selection, select_account, site_address
from .signals.energy import select_nmi
from .signals.model import GroupFault, as_list, as_object, as_str, member

if TYPE_CHECKING:
    import probatio as vol
else:
    import voluptuous as vol

_LOGGER = logging.getLogger(__package__)

TOKEN_SCHEMA: Final = vol.Schema(
    {vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
THRESHOLD_SELECTOR: Final = NumberSelector(
    NumberSelectorConfig(
        mode=NumberSelectorMode.BOX, step="any", unit_of_measurement=UNIT_AUD_PER_KWH
    )
)
OPTIONS_SCHEMA: Final = vol.Schema(
    {
        vol.Optional(CONF_VIRTUAL_DEVICES, default=True): bool,
        vol.Optional(CONF_SPOT_PRICES, default=True): bool,
        vol.Optional(CONF_PRICE_HIGH_THRESHOLD): THRESHOLD_SELECTOR,
        vol.Optional(CONF_PRICE_LOW_THRESHOLD): THRESHOLD_SELECTOR,
    }
)
SELECTION_REQUIRED: Final = "account_selection_required"


class FlowError(Exception):
    def __init__(self, key: str, placeholders: dict[str, str]) -> None:
        super().__init__(key, placeholders)
        self.key = key
        self.placeholders = placeholders


def flow_config(account_number: str | None) -> Config:
    return {
        "accountNumber": account_number,
        "nmi": None,
        "tokenPreview": None,
        "priceHighThresholdCentsPerKwh": None,
        "priceLowThresholdCentsPerKwh": None,
    }


def received(body: Json) -> Snapshot:
    return {"fetchedAt": None, "error": None, "body": body}


async def read_body(call: Callable[[], Awaitable[ApiResponse]]) -> Json:
    try:
        response = await call()
    except ApiNetworkError as exc:
        raise FlowError("network_error", {"message": str(exc)}) from exc
    if response.status != 200:
        if response.ok:
            _LOGGER.error(
                "GET %s answered %s: %s", response.endpoint.value, response.status, response.text()
            )
        raise FlowError("http_error", {"status": str(response.status), "body": response.text()})
    try:
        return response.json_body()
    except BodyInvalid as exc:
        _LOGGER.error("%s", exc)
        raise FlowError("invalid_response", {"message": str(exc)}) from exc


def invalid_response(fault: GroupFault) -> FlowError:
    if fault.fault["code"] != "invalid_response":
        raise fault
    message = fault.fault["message"]
    _LOGGER.error("%s", message)
    return FlowError("invalid_response", {"message": message})


def eligible_accounts(body: Json) -> list[dict[str, Json]]:
    accounts = member(as_object(body, "account"), "accounts", "account")
    eligible: list[dict[str, Json]] = []
    for index, item in enumerate(as_list(accounts, "account.accounts")):
        account = as_object(item, f"account.accounts[{index}]")
        number = account.get("accountNumber")
        if isinstance(number, str) and number != "" and account.get("product") is not None:
            eligible.append(account)
    return eligible


def account_label(account: dict[str, Json]) -> str:
    parts = [account.get(key) for key in ("accountNumber", "siteAddress", "accountState")]
    return " · ".join(str(part) for part in parts if part is not None)


def select(body: Json, account_number: str | None) -> Selection:
    try:
        return select_account(received(body), flow_config(account_number))
    except GroupFault as fault:
        code = fault.fault["code"]
        if code == "account_none":
            raise FlowError(code, {}) from fault
        if code == "account_not_found" and account_number is not None:
            numbers = [str(account["accountNumber"]) for account in eligible_accounts(body)]
            raise FlowError(
                code, {"account": account_number, "accounts": ", ".join(numbers)}
            ) from fault
        if code == SELECTION_REQUIRED:
            raise
        raise invalid_response(fault) from fault


def nmi_choices(selection: Selection, meters: Json) -> list[str]:
    try:
        select_nmi(selection, flow_config(None), received(meters))
    except GroupFault as fault:
        if fault.fault["code"] != "nmi_selection_required":
            raise invalid_response(fault) from fault
    else:
        return []
    address = site_address(selection)
    every: list[str] = []
    candidates: list[str] = []
    items = member(as_object(meters, "meters"), "meters", "meters")
    for index, item in enumerate([] if items is None else as_list(items, "meters.meters")):
        meter = as_object(item, f"meters.meters[{index}]")
        nmi = as_str(meter.get("nmi"), f"meters.meters[{index}].nmi")
        if nmi not in every:
            every.append(nmi)
        if meter.get("address") == address and nmi not in candidates:
            candidates.append(nmi)
    return candidates or every


class FlippedEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._token = ""
        self._account_body: Json = None
        self._meters_body: Json = None
        self._account_number = ""
        self._nmi_choices: list[str] = []
        self._auth_error: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FlippedEnergyOptionsFlow:
        return FlippedEnergyOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=TOKEN_SCHEMA)
        token = str(user_input[CONF_TOKEN])
        if not token.startswith(TOKEN_PREFIX):
            return self._async_token_form("user", {CONF_TOKEN: "token_format"}, {})
        try:
            api = await async_create_api(self.hass, token)
            self._account_body = await read_body(api.account_data)
            self._meters_body = await read_body(api.meters)
            self._token = token
            selection = select(self._account_body, None)
        except FlowError as error:
            return self._async_token_form("user", {"base": error.key}, error.placeholders)
        except GroupFault as fault:
            if fault.fault["code"] != SELECTION_REQUIRED:
                raise
            return await self.async_step_account()
        return await self._async_resolve_nmi(selection)

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            selection = select(self._account_body, str(user_input[CONF_ACCOUNT_NUMBER]))
            return await self._async_resolve_nmi(selection)
        options = [
            SelectOptionDict(value=str(account["accountNumber"]), label=account_label(account))
            for account in eligible_accounts(self._account_body)
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT_NUMBER): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                )
            }
        )
        return self.async_show_form(step_id="account", data_schema=schema)

    async def async_step_nmi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_finish(str(user_input[CONF_NMI]))
        schema = vol.Schema(
            {
                vol.Required(CONF_NMI): SelectSelector(
                    SelectSelectorConfig(options=self._nmi_choices)
                )
            }
        )
        return self.async_show_form(step_id="nmi", data_schema=schema)

    async def _async_resolve_nmi(self, selection: Selection) -> ConfigFlowResult:
        self._account_number = str(selection.account["accountNumber"])
        try:
            choices = nmi_choices(selection, self._meters_body)
        except FlowError as error:
            return self._async_token_form("user", {"base": error.key}, error.placeholders)
        if not choices:
            return await self._async_finish(None)
        self._nmi_choices = choices
        return await self.async_step_nmi()

    async def _async_finish(self, nmi: str | None) -> ConfigFlowResult:
        await self.async_set_unique_id(instance_key(self._account_number, nmi))
        self._abort_if_unique_id_configured()
        first = not self._async_current_entries(include_ignore=False)
        data = {CONF_TOKEN: self._token, CONF_ACCOUNT_NUMBER: self._account_number}
        if nmi is not None:
            data[CONF_NMI] = nmi
        return self.async_create_entry(
            title=device_title(self._account_number, nmi, first), data=data
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._auth_error = {
            "status": str(entry_data[AUTH_ERROR_STATUS]),
            "body": str(entry_data[AUTH_ERROR_BODY]),
        }
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if user_input is None:
            return self._async_token_form(
                "reauth_confirm", {"base": "http_error"}, self._auth_error
            )
        return await self._async_replace_token("reauth_confirm", entry, user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=TOKEN_SCHEMA)
        return await self._async_replace_token("reconfigure", entry, user_input)

    async def _async_replace_token(
        self, step_id: str, entry: ConfigEntry, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        token = str(user_input[CONF_TOKEN])
        if not token.startswith(TOKEN_PREFIX):
            return self._async_token_form(step_id, {CONF_TOKEN: "token_format"}, {})
        try:
            api = await async_create_api(self.hass, token)
            body = await read_body(api.account_data)
            select(body, str(entry.data[CONF_ACCOUNT_NUMBER]))
        except FlowError as error:
            return self._async_token_form(step_id, {"base": error.key}, error.placeholders)
        return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})

    def _async_token_form(
        self, step_id: str, errors: dict[str, str], placeholders: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )


class FlippedEnergyOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options = {key: value for key, value in user_input.items() if value is not None}
            high = options.get(CONF_PRICE_HIGH_THRESHOLD)
            low = options.get(CONF_PRICE_LOW_THRESHOLD)
            if high is not None and low is not None and float(low) >= float(high):
                errors["base"] = "threshold_order"
            else:
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options if user_input is None else user_input
            ),
            errors=errors,
        )
