from typing import Final

BASE_URL: Final = "https://mcp-api.flipped.energy/developer/v1"
WAIT_TIMEOUT_S: Final = 55
WAIT_HTTP_TIMEOUT_S: Final = 70
HTTP_TIMEOUT_S: Final = 60
DISPATCH_INTERVAL_S: Final = 300
WAIT_HOLDS_PER_INTERVAL: Final = 3
PRICE_STALE_AFTER_S: Final = 900
ACCOUNT_MAX_AGE_S: Final = 172800
SCAN_LIMIT_MIN: Final = 1500
PLAN_CHANGE_HORIZON_DAYS: Final = 31
KWH_UNLIMITED: Final = 999999999
TIMER_MAX_AHEAD_S: Final = 86400
USAGE_LOOKBACK_DAYS: Final = 7
ACCOUNT_SYNC_LOCAL_TIME: Final = "00:01:00"
USAGE_SYNC_LOCAL_TIME: Final = "12:01:00"
TOKEN_EXPIRY_WARNING_DAYS: Final = 14
FIRMWARE_ERROR_BODY_MAX_BYTES: Final = 2048
RATE_KEY_SCALE: Final = 1e9
RATE_KEY_OUTPUT_DIVISOR: Final = 1e7
MINUTES_PER_DAY: Final = 1440
SUPPLIED_ACCOUNT_STATES: Final = frozenset({"ACTIVE", "CLOSING"})
FIXED_UNIT_TYPE: Final = "FixedBillingUnit"
SPOT_UNIT_TYPE: Final = "SpotBillingUnit"
SPOT_WITH_CAP_UNIT_TYPE: Final = "SpotWithCapBillingUnit"
SPOT_UNIT_TYPES: Final = frozenset({SPOT_UNIT_TYPE, SPOT_WITH_CAP_UNIT_TYPE})
KNOWN_UNIT_TYPES: Final = frozenset(
    {
        "CertificateBillingUnit",
        "ControlledLoadBillingUnit",
        "FeedInTariff",
        FIXED_UNIT_TYPE,
        "NetworkTariffBillingUnit",
        "PeriodicBillingUnit",
        "PrepaidBillingUnit",
        SPOT_UNIT_TYPE,
        SPOT_WITH_CAP_UNIT_TYPE,
    }
)
PRICE_TIERS: Final = ("UnusuallyLow", "Normal", "Elevated", "Spike")
HIGH_TIERS: Final = frozenset({"Elevated", "Spike"})
LOW_TIER: Final = "UnusuallyLow"
USAGE_CONSUMPTION: Final = "Export"
USAGE_FEED_IN: Final = "Import"
