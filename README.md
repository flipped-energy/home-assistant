# Flipped Energy for Home Assistant

## What it is

`flipped_energy_au` brings your Flipped Energy plan, the regional wholesale electricity price and your meter's usage history into Home Assistant. It shows which rate of your plan applies now, as switches and sensors that automations can use, and imports your metered half-hourly usage and cost into the Energy dashboard. It only reads: it cannot change anything in your Flipped account, your plan or your meter.

Two things in this integration look alike and are not the same:

- **Your rate** (`Peak Rate`, `Off-Peak Rate`, `Current Rate`, `Rate Period` and the other "Rate" entities) comes from your own plan. It is what you pay per kWh, including GST.
- **Wholesale price** (every entity whose name starts with "Wholesale") is the price on the wholesale market in your region, every 5 minutes. It excludes GST, network charges, losses and retail margin. It is not what you pay. On a plan with fixed rates it does not change your bill; it tells you when the grid has cheap (often surplus solar) or expensive energy.

## Requirements

- Home Assistant 2026.9.4 or later.
- A Flipped Energy account with **APIs and MCPs** turned on in the Flipped portal.

## Install

**HACS:** in HACS open the menu, choose **Custom repositories**, add `https://github.com/flipped-energy/home-assistant` with type **Integration**, then install **Flipped Energy** and restart Home Assistant.

**Manual copy:** copy the folder `custom_components/flipped_energy_au` from this repository to `custom_components/flipped_energy_au` inside your Home Assistant configuration directory, then restart Home Assistant.

## Create a token

1. In the Flipped portal open **APIs and MCPs** and create a token.
2. Scope: **Read only**. The portal suggests "Read and write"; this integration only reads.
3. **Expires in:** the longest expiry the portal offers (365 days). The portal suggests 90 days.
4. Copy the token. It starts with `fdk_`.

Tokens expire. Before the token expires, create a new one and enter it with **Reconfigure** on the Flipped Energy entry (**Settings** > **Devices & services** > **Flipped Energy**). From 14 days before expiry `Token Expiring Soon` is on.

Changing your Flipped account password revokes every token. The integration then stops calling the API and Home Assistant asks for a new token (see "When something is wrong").

## Set up

1. **Settings** > **Devices & services** > **Add integration** > **Flipped Energy**.
2. **Token:** paste the token.
3. **Account:** shown only when the token can read more than one account. Choose the account this device serves.
4. **Meter:** shown only when the account does not map to exactly one meter. Choose the meter (NMI) whose usage history is read.

Each account (and meter) is one entry and one device. The first device is named `Flipped Energy`; further devices are named `Flipped Energy` followed by the last four digits of the account number, and the last four characters of the NMI when you chose a meter. The entity ids below are those of the first device.

## Device and price settings

Open **Settings > Devices & services > Flipped Energy > Configure**. **Expose virtual devices** controls the read-only virtual switches. **Show wholesale prices** controls whether the wholesale price switches and sensors appear; they are on for every account unless you turn them off. These settings reload the integration.

Create tokens and use the live API reference, tester and MCP setup in [APIs and MCPs](https://flipped.energy/accounts/developer).

## What you get

### Switches

| Entity | Entity id | On while |
|---|---|---|
| Peak Rate | `switch.flipped_energy_peak_rate` | your plan's most expensive rate applies |
| Off-Peak Rate | `switch.flipped_energy_off_peak_rate` | your plan's cheapest rate applies |
| Shoulder Rate | `switch.flipped_energy_shoulder_rate` | a rate between your plan's cheapest and most expensive applies |
| Wholesale Price High | `switch.flipped_energy_wholesale_price_high` | the wholesale price is high (see below) |
| Wholesale Price Low | `switch.flipped_energy_wholesale_price_low` | the wholesale price is low (see below) |

The five switches show a state and cannot be switched. Turning one on or off is refused with the message "<name> is a read-only signal and cannot be switched", and the toggle returns to the real state.

**Peak Rate, Off-Peak Rate and Shoulder Rate.** The integration ranks your plan's fixed general-usage rates (for a period with an allowance, the rate within the allowance). `Off-Peak Rate` is on while the cheapest of them applies, `Peak Rate` while the most expensive applies. With exactly two rates one of the two is always on and `Shoulder Rate` is always off. With three or more, the rates in between are "Shoulder": while one of them applies, `Shoulder Rate` is on and the other two are off. On a single-rate plan all three are always off. Controlled load and any wholesale-linked part of the plan play no part in the ranking.

**Wholesale Price High and Wholesale Price Low.** Without thresholds (see "Thresholds"):

- `Wholesale Price High` is on while `Wholesale Price Level` is Elevated or Spike and the price is not negative.
- `Wholesale Price Low` is on while `Wholesale Price Level` is Unusually low or the price is below 0 AUD/kWh.

A threshold you set replaces the rule for its own switch. The two are never on together. Both follow the current price and can change every 5 minutes.

### Binary sensors

| Entity | Entity id | On while | Present |
|---|---|---|---|
| Wholesale Price Negative | `binary_sensor.flipped_energy_wholesale_price_negative` | the wholesale price is below 0 AUD/kWh | unless wholesale prices are turned off |
| Wholesale-Linked Rate | `binary_sensor.flipped_energy_wholesale_linked_rate` | the current period of your plan follows the wholesale market | only on a wholesale-linked plan |
| Token Expiring Soon | `binary_sensor.flipped_energy_token_expiring_soon` | the token expires within 14 days; attributes `expires_at`, `scope` | always (diagnostic) |

### Sensors

| Entity | Entity id | Shows |
|---|---|---|
| Wholesale Price | `sensor.flipped_energy_wholesale_price` | the current 5-minute wholesale price in AUD/kWh, negative values included; attribute `interval_start` |
| Wholesale Price Level | `sensor.flipped_energy_wholesale_price_level` | Flipped's assessment of the current wholesale price against the region's last 7 days: Unusually low, Normal, Elevated or Spike |
| Wholesale Price Forecast | `sensor.flipped_energy_wholesale_price_forecast` | the highest forecast wholesale price of the next hour in AUD/kWh; attributes `min_aud_per_kwh`, `level`, `from`, `to`, `published_at`, `points` (5-minute steps) |
| Current Rate | `sensor.flipped_energy_current_rate` | the rate of your plan now, AUD/kWh including GST; unknown while the rate is wholesale-linked |
| Rate Allowance | `sensor.flipped_energy_rate_allowance` | the kWh per day charged at the Current Rate within this rate period; unknown when the period has no allowance |
| Rate After Allowance | `sensor.flipped_energy_rate_after_allowance` | the rate once the allowance is used, AUD/kWh including GST; unknown when there is no allowance |
| Rate Period | `sensor.flipped_energy_rate_period` | Peak, Shoulder, Off-peak or Anytime (Anytime on a single-rate plan); attributes `name`, `structure`, `spot_linked`, `period_start`, `period_end`, `blocks`, `schedule` |
| Rate Period Name | `sensor.flipped_energy_rate_period_name` | the name of the current rate period exactly as your plan states it |
| Next Rate Change | `sensor.flipped_energy_next_rate_change` | when the rate period or the plan changes next (the rate can be the same on both sides) |
| Fixed Rate Component | `sensor.flipped_energy_fixed_rate_component` | only on a wholesale-linked plan: the fixed part of the current rate, AUD/kWh including GST |
| Wholesale Rate Cap | `sensor.flipped_energy_wholesale_rate_cap` | only on a wholesale-linked plan: the cap on the wholesale-linked part, AUD/kWh including GST |
| Account Status, Rates Status, Wholesale Price Status, Usage History Status | `sensor.flipped_energy_account_status`, `..._rates_status`, `..._wholesale_price_status`, `..._usage_history_status` | diagnostic: `ok` or the fault code of that group (see "When something is wrong") |
| Usage History Up To | `sensor.flipped_energy_usage_history_up_to` | diagnostic: the end of the newest half-hour of usage history received |
| API Calls Remaining Today | `sensor.flipped_energy_api_calls_remaining_today` | diagnostic, disabled by default: the API calls left today, as last reported by the API |

A plan with an allowance (for example a free window with the first kWh of each day at one rate and the rest at another) always has `Current Rate`, `Rate Allowance` and `Rate After Allowance` together. `Rate Period Name` is unavailable when the name is longer than 255 characters (Home Assistant's limit for a state); the full name is then in the `name` attribute of `Rate Period` and in the log.

### Unavailable and unknown

- **Unavailable** means the value is not known: the integration has not had an answer yet, or the API or the data failed. The switches and binary sensors are never "off" in that case and never keep their last value. Every entity is unavailable for a short time after Home Assistant starts, after the entry is reloaded (options changed, token replaced) and until a failure is over.
- **Unknown** on a sensor means the value legitimately does not exist right now, for example `Current Rate` during a wholesale-linked period, `Rate Allowance` on a period without one, or `Wholesale Price Forecast` when no forecast is published.

The rates come from your plan and keep working without API calls. The wholesale price, the account and the usage history fail independently: a fault in one leaves the others working.

## Thresholds

On the Flipped Energy entry choose **Configure**:

- **Wholesale Price High threshold** (AUD/kWh): when set, `Wholesale Price High` is on while the wholesale price is at or above it.
- **Wholesale Price Low threshold** (AUD/kWh): when set, `Wholesale Price Low` is on while the wholesale price is at or below it. It can be negative.

Thresholds are wholesale prices, excluding GST. An empty field keeps the rule of "What you get". When both are set the low threshold must be less than the high one. A threshold you set also outranks the rule of the other switch, so the two are never on together. Saving reloads the entry: every entity is unavailable for a moment.

## Usage history in the Energy dashboard

The integration imports your metered usage as hourly statistics. Meter data reaches Flipped a day or more late, so the newest day or two are empty and are filled in later. The integration reads it at start-up and at 00:01 and 12:01 in your account's time zone. History starts 7 days before the first run; if the integration cannot read the usage for more than 7 days, those days stay empty.

| Statistic | Contains | Exists when |
|---|---|---|
| `Flipped Energy Grid Import` | energy bought from the grid (general usage), kWh | your meter has recorded grid usage |
| `Flipped Energy Solar Export` | energy exported to the grid, kWh | your meter has recorded export (a site with solar) |
| `Flipped Energy Controlled Load` | controlled-load energy (for example a hot water system on its own circuit), kWh | your site has a controlled load |
| `Flipped Energy Usage Cost` | usage charges for general usage and controlled load, AUD | cost data exists |
| `Flipped Energy Feed-in Credit` | feed-in credit for exported energy, AUD | export with a feed-in amount exists |

A site with no solar has no `Solar Export` and no `Feed-in Credit`, and a site with no controlled load has no `Controlled Load`; they are then not offered in the dashboard's pickers.

### Configuration

1. Go to **Settings** > **Dashboards** > **Energy**. Under **Electricity grid** select **Add grid connection**.
2. **Energy imported from grid**: `Flipped Energy Grid Import`.
3. **Cost tracking**: **Use an entity tracking the total costs** → `Flipped Energy Usage Cost`.
4. Only for a site with solar AND a solar production sensor from your inverter's own integration added under **Solar panels** > **Add solar production**: **Energy exported to grid**: `Flipped Energy Solar Export`; **Export compensation**: **Use an entity tracking the total compensation** → `Flipped Energy Feed-in Credit`.
5. Only for a site with a controlled load (`Flipped Energy Controlled Load` exists): **Add grid connection** again, **Energy imported from grid**: `Flipped Energy Controlled Load`, **Cost tracking**: **Do not track costs**.

Two configurations, depending on step 4:

- **Without step 4** (no solar, or no solar production sensor): the dashboard's consumption is the energy bought from the grid. On a solar site that is less than your home used, because solar used in the home never passes the meter. `Solar Export` and `Feed-in Credit` are then not in the dashboard; they remain available as statistics (**Settings** > **Tools** > **Statistics**, a statistics graph card) and through `flipped_energy_au.get_usage_history`.
- **With step 4 and a solar production sensor**: the dashboard shows consumption including solar. The last day or two show solar production with no grid data yet, because meter data arrives a day or more late.

Do not configure step 4 without a solar production sensor: the dashboard would then show consumption as import minus export, which is wrong.

### What the dashboard cannot show

- **`Usage Cost` excludes the daily supply charge.** It is usage charges only, so it is less than your bill.
- **Day and month cost totals leave out hours whose cost the API could not give.** The dashboard cannot show an unknown amount: such an hour counts as 0, so the total is understated by that hour's cost and nothing on the dashboard marks it. The hours are listed in the attributes `cost_unknown_hours` (for `Usage Cost`) and `feed_in_unknown_hours` (for `Feed-in Credit`) of `Usage History Status` and in the log. Hours missing from the API's answer are listed in `missing_hours`.
- The cost of a day is the sum of the hourly costs and can differ by rounding from the daily cost that `flipped_energy_au.get_usage_history` returns.
- `Usage Cost` covers general usage and controlled load together and is attached to the general grid connection; the dashboard cannot split it per connection.
- On a wholesale-linked plan `Usage Cost` is net of any wholesale-linked feed-in, and `Feed-in Credit` excludes it.
- In South Australia a dashboard day runs from 00:30 to 00:30 local time, as for every statistic in a half-hour time zone.
- `Current Rate` can be the price entity of a meter sensor from another integration (for example a CT clamp or your inverter's grid sensor): **Use an entity with current price** → `Current Rate`. It is unknown while the rate is wholesale-linked, and it does not know when an allowance is used up. Do not use `Wholesale Price` as a price: it is not what you pay. The dashboard does not accept a price entity for the Flipped Energy statistics themselves; they bring their own cost.

## Apple Home

Home Assistant's HomeKit Bridge integration can show some of these entities in Apple Home.

1. Add the **HomeKit Bridge** integration (**Settings** > **Devices & services** > **Add integration** > **HomeKit Bridge**). Its suggested domains include `switch`, so `Peak Rate`, `Off-Peak Rate`, `Shoulder Rate`, `Wholesale Price High` and `Wholesale Price Low` appear as five switches with no further configuration.
2. A tap on one of these switches in the Home app is refused: Home Assistant logs a warning and the tile returns to the real state.
3. `Wholesale Price Negative` is not included by default. Add `binary_sensor.flipped_energy_wholesale_price_negative` in the bridge's options (**Configure** on the HomeKit Bridge entry), then reload the bridge.
4. Its tile is an occupancy sensor by default ("Occupancy Detected" while the price is negative). To change the tile, open the entity's settings in Home Assistant and set **Show as** to Door, Window, Opening or Garage door (a contact sensor) or Motion (a motion sensor), then reload the bridge. Do not choose Smoke, Moisture, Gas or Carbon monoxide.
5. Switches and sensors can trigger Apple Home automations. Automations need a home hub.
6. After a Home Assistant restart, a reload of the Flipped Energy entry or a recovered failure, the tiles show "No Response" and then the real value. An Apple Home automation triggered by "turns on" can therefore run once more when this happens during an on-period.
7. Diagnostic entities (`Token Expiring Soon`, the status sensors) are not included unless you pick them.

The sensors of this integration (prices, rates, times, kWh), its statistics and the Energy dashboard data do not reach Apple Home through the HomeKit Bridge: the bridge does not support those sensor types, and Apple Home has no price or tariff concept. Matter is not a way either: Home Assistant does not expose its entities as Matter devices.

## Automation examples

The entities pass through `unavailable` at every restart, every reload and after every recovered failure. A state trigger with only `to: "on"` would fire again each time. Write every state trigger with both sides:

```yaml
triggers:
  - trigger: state
    entity_id: switch.flipped_energy_wholesale_price_high
    from: "off"
    to: "on"
```

Such a trigger does not fire for an on-period that begins while the entity is unavailable (it goes from `unavailable` to `on`). To ask "is it on now", use a state condition:

```yaml
conditions:
  - condition: state
    entity_id: switch.flipped_energy_off_peak_rate
    state: "on"
```

**Run something while Off-Peak Rate is on** (replace `switch.pool_pump` with your own device):

```yaml
alias: Pool pump in the off-peak period
triggers:
  - trigger: state
    entity_id: switch.flipped_energy_off_peak_rate
    from: "off"
    to: "on"
    id: start
  - trigger: state
    entity_id: switch.flipped_energy_off_peak_rate
    from: "on"
    to: "off"
    id: stop
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: start
        sequence:
          - action: switch.turn_on
            target:
              entity_id: switch.pool_pump
      - conditions:
          - condition: trigger
            id: stop
        sequence:
          - action: switch.turn_off
            target:
              entity_id: switch.pool_pump
```

**Notify on Wholesale Price High:**

```yaml
alias: Wholesale price high
triggers:
  - trigger: state
    entity_id: switch.flipped_energy_wholesale_price_high
    from: "off"
    to: "on"
actions:
  - action: persistent_notification.create
    data:
      title: Wholesale Price High
      message: "Wholesale price {{ states('sensor.flipped_energy_wholesale_price') }} AUD/kWh"
```

**A caution about allowances.** On a period with an allowance, for example "0 c for the first 24 kWh per day in this window, then 27.5 c", the integration shows the rate, `Rate Allowance` and `Rate After Allowance`, but it cannot tell when the allowance has been used: meter data arrives a day or more late. An automation that keeps loads running through the window pays `Rate After Allowance` for everything beyond the allowance while `Off-Peak Rate` is still on.

## Actions

Three actions answer from the values the integration already holds; they make no API call. Each takes the Flipped Energy entry (`config_entry_id`; in the action editor, the **Device** field) and returns a response. When the group behind an action has failed, the action fails with an error that contains the API's own status and response text.

**`flipped_energy_au.get_wholesale_price_forecast`**: the forecast for the next hour (5-minute steps) and the period ahead (30-minute steps). Each is empty (`null`) when no forecast is published.

```yaml
action: flipped_energy_au.get_wholesale_price_forecast
data:
  config_entry_id: 01K6EXAMPLEENTRYID
response_variable: forecast
```

```yaml
next_hour:
  from: "2026-10-01T02:30:00Z"
  to: "2026-10-01T03:30:00Z"
  published_at: "2026-10-01T02:30:00Z"
  min_aud_per_kwh: 0.091
  max_aud_per_kwh: 0.349
  level: elevated
  points:
    - start: "2026-10-01T02:30:00Z"
      aud_per_kwh: 0.091
    - start: "2026-10-01T02:35:00Z"
      aud_per_kwh: 0.098
ahead: null
```

**`flipped_energy_au.get_rate_schedule`**: the rate periods of the plan in effect, by minute of the day in your account's time zone.

```yaml
structure: timeOfUse
spot_linked: false
schedule:
  - startMinute: 0
    endMinute: 360
    band: shoulder
    name: Overnight
    rateCentsPerKwh: 22
    kwhLimit: null
    rateAfterLimitCentsPerKwh: null
    blocks:
      - fromKwh: 0
        toKwh: null
        rateCentsPerKwh: 22
    wholesaleLinked: false
    wholesaleCapCentsPerKwh: null
```

**`flipped_energy_au.get_usage_history`**: the half-hourly intervals and the daily totals of the last 7 days, as the API gives them. The daily cost here is the API's own daily figure.

```yaml
nmi: "4102000001"
latest_interval_end: "2026-10-03T14:00:00Z"
intervals:
  - local: "2026-10-03T00:00:00"
    start: "2026-10-02T14:00:00Z"
    durationMinutes: 30
    gridImportKwh: 0.412
    controlledLoadKwh: 0.3
    solarExportKwh: 0
    costAud: 0.21
    feedInCreditAud: 0
days:
  - local: "2026-10-03T00:00:00"
    start: "2026-10-02T14:00:00Z"
    durationMinutes: 1440
    gridImportKwh: 14.212
    controlledLoadKwh: 0
    solarExportKwh: 9.84
    costAud: 3.18
    feedInCreditAud: 0.2
```

## When something is wrong

**Status sensors.** `Account Status`, `Rates Status`, `Wholesale Price Status` and `Usage History Status` (diagnostic entities on the device) are `ok`, or the fault code of their group. Their attributes hold the failure exactly as received: `http_status` and `body` (the API's response text) for an HTTP error, `message` for a network error or a data error, and `body_bytes`. `Usage History Status` also has `nmi`, `cost_unknown_hours`, `feed_in_unknown_hours` and `missing_hours`.

| Code | Meaning |
|---|---|
| `not_loaded` | no answer yet |
| `http_error` | the API answered with an error status; see `http_status` and `body` |
| `network_error` | no answer from the API; see `message` |
| `invalid_response` | the answer lacks a field the integration needs; `message` names it |
| `account_none`, `account_selection_required`, `account_not_found` | the token reads no account, several accounts and none is chosen, or not the chosen one |
| `account_not_supplied` | the account is not being supplied; `message` carries its state |
| `account_data_stale` | the account data is too old |
| `timezone_missing`, `timezone_unsupported`, `local_time_nonexistent` | the account's time zone is missing or unusable |
| `plan_unavailable` | no plan is in effect now |
| `billing_unit_unsupported`, `billing_unit_malformed`, `billing_unit_overlap`, `tariff_gap`, `spot_direction_unknown` | the plan cannot be read as a schedule of rates; `message` names the part |
| `region_missing` | the account has no region |
| `price_stale` | the newest wholesale price is more than 15 minutes old |
| `nmi_selection_required`, `nmi_not_found` | the meter cannot be chosen, or the chosen meter is not on the account |
| `usage_timezone_ambiguous` | your accounts are not all in one time zone |

**Token refused (HTTP 401).** The integration stops calling the API and Home Assistant shows a reauthentication prompt for Flipped Energy (**Settings** > **Devices & services**, and under **Repairs**). The form shows the API's 401 status and response. Enter a new token (see "Create a token"). When the API refuses the token itself (expired, revoked in the portal, or unknown) nothing is requested until you enter a new one. For any other 401 the integration checks the account once a day at 00:01, and the prompt is removed when that check succeeds.

**Access refused (HTTP 403).** Repairs shows the issue "Flipped Energy: the Flipped Energy API answered HTTP 403" with the time it was first seen and the API's response. The integration then checks once a day at 00:01; the issue is removed when the API answers again.

**Log.** Every error answer from the API is logged at error level with its status and complete response; a network failure with its own text. The logger is `custom_components.flipped_energy_au` (**Settings** > **System** > **Logs**).

**Diagnostics.** On the Flipped Energy entry choose **Download diagnostics**. The file contains no token; account number, NMI and device name are redacted.

**API call budget.** The API allows 60 calls a minute and 5,000 a day (UTC), shared by every token of your account, so by this integration and any other tools you use with Flipped's API. One entry uses at most about 1,160 a day, plus 6 at each restart or reload. When a limit is reached the API answers 429; the integration logs it and waits the time the API gives. The rates keep working; the wholesale price and usage history are unavailable until then. `API Calls Remaining Today` shows the daily count left.

## Removing

1. **Settings** > **Devices & services** > **Flipped Energy**: on the entry, open the menu and choose **Delete**.
2. The usage statistics are not deleted with the entry. Delete them in **Settings** > **Tools** > **Statistics**: search for `flipped_energy_au`.
3. Revoke the token on the **APIs and MCPs** page of the Flipped portal if nothing else uses it.

## Privacy

- The integration calls only `mcp-api.flipped.energy`, over HTTPS, with your token, for your own account's data. There are no analytics or tracking calls.
- It reads your account, plan, meters, token list, the wholesale price of your region and your usage history.
- Stored in Home Assistant: the token, the account number and the NMI (when you chose a meter) in the config entry; the usage statistics in the recorder database; the entities' states in the recorder history like every entity.
- Never stored: the API's account response, which contains personal data (name, date of birth, phone, payment details). Only the fields the integration needs are taken from it, and a successful response is never logged.
