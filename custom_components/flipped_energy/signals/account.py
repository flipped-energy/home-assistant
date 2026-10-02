from dataclasses import dataclass
from datetime import tzinfo

from .constants import TOKEN_EXPIRY_WARNING_DAYS
from .model import (
    AccountSignals,
    Config,
    GroupFault,
    Json,
    Snapshot,
    as_list,
    as_object,
    as_optional_str,
    as_str,
    fault_error,
    invalid,
    make_fault,
    member,
    missing_body_fault,
)
from .timeprim import (
    MICROS_PER_DAY,
    Instant,
    ZoneUnsupported,
    format_instant,
    parse_instant,
    zone_for,
)


@dataclass(frozen=True, slots=True)
class Selection:
    account: dict[str, Json]
    product: dict[str, Json]
    eligible: list[tuple[str, dict[str, Json]]]
    path: str


@dataclass(frozen=True, slots=True)
class TokenInfo:
    expires_at: Instant
    scope: str | None


def select_account(snapshot: Snapshot, config: Config) -> Selection:
    missing = missing_body_fault(snapshot)
    if missing is not None:
        raise GroupFault(missing)
    body = as_object(snapshot["body"], "account")
    accounts_value = member(body, "accounts", "account")
    if accounts_value is None:
        raise fault_error("account_none", "accounts is null")
    eligible: list[tuple[int, dict[str, Json], dict[str, Json]]] = []
    for index, item in enumerate(as_list(accounts_value, "account.accounts")):
        path = f"account.accounts[{index}]"
        account = as_object(item, path)
        number = as_optional_str(member(account, "accountNumber", path), f"{path}.accountNumber")
        product = member(account, "product", path)
        if number is None or number == "" or product is None:
            continue
        eligible.append((index, account, as_object(product, f"{path}.product")))
    if not eligible:
        raise fault_error("account_none", "no account has an account number and a product")
    numbers = [str(account["accountNumber"]) for _, account, _ in eligible]
    configured = config["accountNumber"]
    if configured is not None:
        for index, account, product in eligible:
            if account["accountNumber"] == configured:
                return _selection(index, account, product, eligible)
        raise fault_error(
            "account_not_found",
            f"account {configured} is not among {', '.join(numbers)}",
        )
    if len(eligible) == 1:
        index, account, product = eligible[0]
        return _selection(index, account, product, eligible)
    raise fault_error("account_selection_required", ", ".join(numbers))


def _selection(
    index: int,
    account: dict[str, Json],
    product: dict[str, Json],
    eligible: list[tuple[int, dict[str, Json], dict[str, Json]]],
) -> Selection:
    return Selection(
        account=account,
        product=product,
        eligible=[(f"account.accounts[{i}].product", p) for i, _, p in eligible],
        path=f"account.accounts[{index}]",
    )


def account_state(selection: Selection) -> str | None:
    path = selection.path
    return as_optional_str(member(selection.account, "accountState", path), f"{path}.accountState")


def time_zone(selection: Selection) -> str | None:
    path = f"{selection.path}.product"
    return as_optional_str(member(selection.product, "timeZone", path), f"{path}.timeZone")


def selected_zone(selection: Selection) -> tuple[str, tzinfo]:
    name = time_zone(selection)
    if name is None:
        raise fault_error(
            "timezone_missing", f"{selection.path}.product.timeZone is null or absent"
        )
    try:
        return name, zone_for(name)
    except ZoneUnsupported as exc:
        raise fault_error("timezone_unsupported", f"{name}: {exc}") from exc


def grid_type(selection: Selection) -> str | None:
    path = f"{selection.path}.product"
    return as_optional_str(member(selection.product, "gridType", path), f"{path}.gridType")


def site_address(selection: Selection) -> str | None:
    path = selection.path
    return as_optional_str(member(selection.account, "siteAddress", path), f"{path}.siteAddress")


def find_token(snapshot: Snapshot, config: Config) -> TokenInfo | None:
    preview = config["tokenPreview"]
    if preview is None or snapshot["body"] is None:
        return None
    body = as_object(snapshot["body"], "tokens")
    for index, item in enumerate(as_list(member(body, "tokens", "tokens"), "tokens.tokens")):
        path = f"tokens.tokens[{index}]"
        token = as_object(item, path)
        if as_str(member(token, "tokenPreview", path), f"{path}.tokenPreview") != preview:
            continue
        expires_text = as_str(member(token, "expiresAt", path), f"{path}.expiresAt")
        try:
            expires_at = parse_instant(expires_text)
        except ValueError as exc:
            raise invalid(f"{path}.expiresAt: {exc}") from exc
        scope = as_optional_str(member(token, "scope", path), f"{path}.scope")
        return TokenInfo(expires_at=expires_at, scope=scope)
    return None


def faulted_account(fault_value: GroupFault) -> AccountSignals:
    return {
        "status": "faulted",
        "fault": fault_value.fault,
        "accountNumber": None,
        "accountState": None,
        "productName": None,
        "region": None,
        "timeZone": None,
        "tokenExpiresAt": None,
        "tokenScope": None,
        "tokenExpiringSoon": None,
    }


def compute_account(
    instant: Instant | None,
    config: Config,
    account_snapshot: Snapshot,
    tokens_snapshot: Snapshot,
) -> tuple[AccountSignals, TokenInfo | None]:
    if instant is None:
        return faulted_account(GroupFault(make_fault("clock_unsynced"))), None
    try:
        selection = select_account(account_snapshot, config)
        path = selection.path
        number = as_str(member(selection.account, "accountNumber", path), f"{path}.accountNumber")
        state = account_state(selection)
        product_name = as_optional_str(
            member(selection.account, "productName", path), f"{path}.productName"
        )
        region = grid_type(selection)
        zone_name = time_zone(selection)
        token = find_token(tokens_snapshot, config)
    except GroupFault as exc:
        return faulted_account(exc), None
    signals: AccountSignals = {
        "status": "ok",
        "fault": None,
        "accountNumber": number,
        "accountState": state,
        "productName": product_name,
        "region": region,
        "timeZone": zone_name,
        "tokenExpiresAt": None,
        "tokenScope": None,
        "tokenExpiringSoon": None,
    }
    if token is not None:
        signals["tokenExpiresAt"] = format_instant(token.expires_at)
        signals["tokenScope"] = token.scope
        warning = TOKEN_EXPIRY_WARNING_DAYS * MICROS_PER_DAY
        signals["tokenExpiringSoon"] = token.expires_at - instant < warning
    return signals, token
