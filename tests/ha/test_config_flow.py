import copy
from collections.abc import Generator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.flipped_energy.api import Endpoint
from custom_components.flipped_energy.const import DOMAIN
from custom_components.flipped_energy.signals import Json

from ..unit.fakes import Obj, answer
from .conftest import ACCOUNT_NUMBER, BODIES, TOKEN, ApiFactory, make_entry

NEW_TOKEN = "fdk_REPLACEMENT0000000000000000000000AbCd"
SECOND_ACCOUNT = "36200000000002"
SITE = "1 Example St, Sampletown NSW 2000"
HTML_401 = '<html><body>401 {"error": "unauthorized"} _x_ *y*</body></html>'
NETWORK = (
    "ClientConnectorDNSError: Cannot connect to host mcp-api.flipped.energy:443 "
    "ssl:default [nodename nor servname provided, or not known]"
)


@pytest.fixture(autouse=True)
def entry_setup() -> Generator[None]:
    with (
        patch("custom_components.flipped_energy.async_setup_entry", AsyncMock(return_value=True)),
        patch("custom_components.flipped_energy.async_unload_entry", AsyncMock(return_value=True)),
    ):
        yield


def accounts_body() -> Obj:
    body = copy.deepcopy(BODIES.account)
    accounts = cast(list[Obj], body["accounts"])
    second = copy.deepcopy(accounts[0])
    second["accountNumber"] = SECOND_ACCOUNT
    second["siteAddress"] = "2 Other St, Sampletown NSW 2000"
    second["accountState"] = "CLOSING"
    third = copy.deepcopy(accounts[0])
    third["accountNumber"] = "36200000000003"
    third["product"] = None
    accounts.extend([second, third])
    return body


def meters(*pairs: tuple[str, str]) -> Json:
    return {"meters": [{"nmi": nmi, "address": address} for nmi, address in pairs]}


def selector_options(result: ConfigFlowResult, field: str) -> list[Any]:
    schema = result["data_schema"]
    assert schema is not None
    return list(schema.schema[field].config["options"])


def shown(result: ConfigFlowResult) -> dict[str, str]:
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    return dict(placeholders)


async def submit_token(hass: HomeAssistant, token: str = TOKEN) -> ConfigFlowResult:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"token": token})


async def test_single_account(hass: HomeAssistant, apis: ApiFactory) -> None:
    apis.script(
        answer(Endpoint.ACCOUNT_DATA, body=BODIES.account),
        answer(Endpoint.METERS, body=BODIES.meters),
    )
    result = await submit_token(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Flipped Energy"
    assert result["data"] == {"token": TOKEN, "accountNumber": ACCOUNT_NUMBER}
    assert result["result"].unique_id == ACCOUNT_NUMBER
    assert [r["path"] for r in apis.last.transcript] == [
        Endpoint.ACCOUNT_DATA.value,
        Endpoint.METERS.value,
    ]


async def test_account_step_then_every_nmi_when_none_matches(
    hass: HomeAssistant, apis: ApiFactory
) -> None:
    apis.script(
        answer(Endpoint.ACCOUNT_DATA, body=accounts_body()),
        answer(Endpoint.METERS, body=meters(("4102000000", SITE), ("4102000009", "Elsewhere"))),
    )
    result = await submit_token(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account"
    assert selector_options(result, "accountNumber") == [
        {"value": ACCOUNT_NUMBER, "label": f"{ACCOUNT_NUMBER} · {SITE} · ACTIVE"},
        {
            "value": SECOND_ACCOUNT,
            "label": f"{SECOND_ACCOUNT} · 2 Other St, Sampletown NSW 2000 · CLOSING",
        },
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accountNumber": SECOND_ACCOUNT}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "nmi"
    assert selector_options(result, "nmi") == ["4102000000", "4102000009"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"nmi": "4102000009"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"token": TOKEN, "accountNumber": SECOND_ACCOUNT, "nmi": "4102000009"}
    assert result["result"].unique_id == f"{SECOND_ACCOUNT}:4102000009"
    assert result["title"] == "Flipped Energy"


async def test_nmi_step_offers_site_candidates_and_names_further_device(
    hass: HomeAssistant, apis: ApiFactory
) -> None:
    make_entry(nmi="4102000000").add_to_hass(hass)
    apis.script(
        answer(Endpoint.ACCOUNT_DATA, body=BODIES.account),
        answer(
            Endpoint.METERS,
            body=meters(("4102000000", SITE), ("4102000001", SITE), ("4102000009", "Elsewhere")),
        ),
    )
    result = await submit_token(hass)
    assert result["step_id"] == "nmi"
    assert selector_options(result, "nmi") == ["4102000000", "4102000001"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"nmi": "4102000001"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Flipped Energy 0001 0001"
    assert result["result"].unique_id == f"{ACCOUNT_NUMBER}:4102000001"


async def test_no_meter_creates_entry_without_nmi(hass: HomeAssistant, apis: ApiFactory) -> None:
    apis.script(
        answer(Endpoint.ACCOUNT_DATA, body=BODIES.account),
        answer(Endpoint.METERS, body=meters()),
    )
    result = await submit_token(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"token": TOKEN, "accountNumber": ACCOUNT_NUMBER}


async def test_already_configured(hass: HomeAssistant, apis: ApiFactory) -> None:
    make_entry().add_to_hass(hass)
    apis.script(
        answer(Endpoint.ACCOUNT_DATA, body=BODIES.account),
        answer(Endpoint.METERS, body=BODIES.meters),
    )
    result = await submit_token(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_token_format_makes_no_call(hass: HomeAssistant, apis: ApiFactory) -> None:
    result = await submit_token(hass, "abc_" + TOKEN[4:])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"token": "token_format"}
    assert apis.created == []


@pytest.mark.parametrize(
    ("responses", "error", "placeholders"),
    [
        (
            [answer(Endpoint.ACCOUNT_DATA, 401, HTML_401)],
            "http_error",
            {"status": "401", "body": HTML_401},
        ),
        (
            [answer(Endpoint.ACCOUNT_DATA, network=NETWORK)],
            "network_error",
            {"message": NETWORK},
        ),
        (
            [answer(Endpoint.ACCOUNT_DATA, body=[])],
            "invalid_response",
            {
                "message": "/api/MyAccount/GetAccountData answered 200 with a JSON array, "
                "expected an object"
            },
        ),
        (
            [
                answer(Endpoint.ACCOUNT_DATA, body=BODIES.account),
                answer(Endpoint.METERS, 503, "upstream timed out\n"),
            ],
            "http_error",
            {"status": "503", "body": "upstream timed out\n"},
        ),
        (
            [
                answer(Endpoint.ACCOUNT_DATA, body={"accounts": []}),
                answer(Endpoint.METERS, body=BODIES.meters),
            ],
            "account_none",
            {},
        ),
        (
            [
                answer(Endpoint.ACCOUNT_DATA, body={"accounts": [{"accountNumber": 7}]}),
                answer(Endpoint.METERS, body=BODIES.meters),
            ],
            "invalid_response",
            {"message": "account.accounts[0].accountNumber is not a string: 7"},
        ),
    ],
)
async def test_errors_shown_verbatim(
    hass: HomeAssistant,
    apis: ApiFactory,
    responses: list[Obj],
    error: str,
    placeholders: dict[str, str],
) -> None:
    apis.script(*responses)
    result = await submit_token(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"] == placeholders


@pytest.mark.parametrize("loaded", [True, False])
async def test_reauth_shows_stored_error_on_first_display(
    hass: HomeAssistant, apis: ApiFactory, loaded: bool
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    if loaded:
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
    assert not hasattr(entry, "runtime_data")
    result = await entry.start_reauth_flow(
        hass, data={"auth_error_status": "401", "auth_error_body": HTML_401}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "http_error"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert (placeholders["status"], placeholders["body"]) == ("401", HTML_401)


async def test_reauth_replaces_token(hass: HomeAssistant, apis: ApiFactory) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(
        hass, data={"auth_error_status": "401", "auth_error_body": HTML_401}
    )
    apis.script(answer(Endpoint.ACCOUNT_DATA, 401, "still refused"))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": NEW_TOKEN})
    assert result["errors"] == {"base": "http_error"}
    assert shown(result) == {"name": "Flipped Energy", "status": "401", "body": "still refused"}
    apis.script(answer(Endpoint.ACCOUNT_DATA, body=accounts_body()))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": NEW_TOKEN})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {"token": NEW_TOKEN, "accountNumber": ACCOUNT_NUMBER}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert apis.tokens == [NEW_TOKEN, NEW_TOKEN]


async def test_reauth_refuses_token_without_the_account(
    hass: HomeAssistant, apis: ApiFactory
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(
        hass, data={"auth_error_status": "401", "auth_error_body": HTML_401}
    )
    other = copy.deepcopy(BODIES.account)
    cast(list[Obj], other["accounts"])[0]["accountNumber"] = SECOND_ACCOUNT
    apis.script(answer(Endpoint.ACCOUNT_DATA, body=other))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": NEW_TOKEN})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "account_not_found"}
    assert shown(result) == {
        "name": "Flipped Energy",
        "account": ACCOUNT_NUMBER,
        "accounts": SECOND_ACCOUNT,
    }
    assert entry.data["token"] == TOKEN


async def test_reconfigure_replaces_token(hass: HomeAssistant, apis: ApiFactory) -> None:
    entry = make_entry(nmi="4102000000")
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "x"})
    assert result["errors"] == {"token": "token_format"}
    apis.script(answer(Endpoint.ACCOUNT_DATA, body=BODIES.account))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": NEW_TOKEN})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {"token": NEW_TOKEN, "accountNumber": ACCOUNT_NUMBER, "nmi": "4102000000"}
    assert entry.unique_id == f"{ACCOUNT_NUMBER}:4102000000"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_options_validation(hass: HomeAssistant) -> None:
    entry = make_entry(options={"priceHighThresholdCentsPerKwh": 30.0})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    for low, high in ((20, 10), (10, 10)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"priceHighThresholdCentsPerKwh": high, "priceLowThresholdCentsPerKwh": low},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "threshold_order"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"priceLowThresholdCentsPerKwh": -5.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"priceLowThresholdCentsPerKwh": -5.5, "virtualDevices": True}
