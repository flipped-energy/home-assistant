from datetime import datetime

from .account import TokenInfo, compute_account
from .constants import ACCOUNT_MAX_AGE_S, PRICE_STALE_AFTER_S, TOKEN_EXPIRY_WARNING_DAYS
from .energy import compute_energy
from .model import Config, Signals, Snapshot
from .price import PriceResult, compute_price
from .tariff import TariffResult, compute_tariff
from .timeprim import (
    MICROS_PER_DAY,
    MICROS_PER_SECOND,
    Instant,
    format_instant,
    instant_from_datetime,
    parse_instant,
)


def next_evaluation(
    instant: Instant,
    account: Snapshot,
    tariff: TariffResult,
    price: PriceResult,
    token: TokenInfo | None,
) -> Instant | None:
    candidates: list[Instant] = []
    if tariff.next_change is not None:
        candidates.append(tariff.next_change)
    if tariff.plan_change is not None:
        candidates.append(tariff.plan_change)
    if account["body"] is not None:
        fetched_at = account["fetchedAt"]
        if fetched_at is None:
            raise ValueError("account snapshot has a body and no fetchedAt")
        candidates.append(parse_instant(fetched_at) + ACCOUNT_MAX_AGE_S * MICROS_PER_SECOND)
    if price.interval_start is not None:
        candidates.append(price.interval_start + PRICE_STALE_AFTER_S * MICROS_PER_SECOND)
    if token is not None:
        candidates.append(token.expires_at - TOKEN_EXPIRY_WARNING_DAYS * MICROS_PER_DAY)
    later = [candidate for candidate in candidates if candidate > instant]
    return min(later) if later else None


def compute_signals(
    instant: datetime | None,
    config: Config,
    account: Snapshot,
    meters: Snapshot,
    tokens: Snapshot,
    outlook: Snapshot,
    usage_half_hourly: Snapshot,
    usage_daily: Snapshot,
) -> Signals:
    now = None if instant is None else instant_from_datetime(instant)
    account_signals, token = compute_account(now, config, account, tokens)
    tariff = compute_tariff(now, config, account)
    price = compute_price(now, config, account, outlook)
    energy = compute_energy(now, config, account, meters, usage_half_hourly, usage_daily)
    evaluation: str | None = None
    if now is not None:
        target = next_evaluation(now, account, tariff, price, token)
        evaluation = None if target is None else format_instant(target)
    return {
        "account": account_signals,
        "tariff": tariff.signals,
        "price": price.signals,
        "energy": energy,
        "nextEvaluation": evaluation,
    }
