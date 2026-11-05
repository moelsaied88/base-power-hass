# Base Power - Home Assistant Integration

Read-only Home Assistant integration for [Base Power Company](https://basepowercompany.com/) customers (Texas electricity + home battery). Exposes live battery state, grid-outage detection, energy flow, and billing as HA entities.

> **Unofficial.** Base Power does not publish a public API. This integration uses the same private API as the Base mobile app at `dashboard.baseapis.net`. If Base changes that API, entities can stop updating.

## Entities

Grouped under a single HA device.

### Sensors

| Entity | Unit | Source |
|---|---|---|
| Battery State of Energy | % | `MobileGetServiceContext.stateOfEnergyRaw` |
| Grid Voltage | V | `gridVoltage`, then `MobileGetGridStatus`, then `GetRecentGridVoltage` |
| Home Power | W | `powerFlow.toHome` |
| Power From Grid | W | `powerFlow.fromGrid` |
| Battery Net Power | W | `powerFlow.fromStorage`. Negative means charging |
| Power To Battery, Power From Battery | W | The charge and discharge parts of Battery Net Power |
| Power From Solar | W | `powerFlow.fromSolar` |
| Home From Grid, Home From Battery, Home From Solar | W | Home load split across its sources, solar first, excluding arbitrage |
| Backup Runtime (at current usage) | h | `availableBackup` |
| Backup Runtime at 750 W | h | `availableBackupAt750W` (disabled by default) |
| Usable Battery Energy (remaining) | kWh | Backup hours at 750 W x 0.75 |
| Battery Capacity (at 100%) | kWh | Usable energy / state of energy |
| Energy From Grid, Solar, Battery (window) | kWh | `MobileGetRecentUsage` rolling totals (disabled by default) |
| Battery State | - | `GetSnapshot`: on grid, outage, no home power, overcurrent, overcurrent standby, or telemetry unavailable |
| Grid Support Events (24 h) | - | `GetDailyOverview`, with the latest start and end |
| Energy Cost Today | USD | `GetDailyOverview`, with the 24-hour energy mix in its attributes |
| Bill to Date, Projected Bill, Solar Buyback Credit | USD | `GetCurrentBillingCycle` |
| Newer API Status | - | `ok`, `degraded`, or `failing`, with the last result of each call and where readings came from |

Bill and cost sensors are unavailable on accounts not billed by Base.

### Binary sensors

| Entity | Meaning |
|---|---|
| On Battery | The home is in an outage. See [outage detection](#outage-detection) |
| Grid Connected | The inverse of On Battery |
| Home Has Power | `homeHasPower` |
| Gateway Connected | `gatewayConnection` |
| Overcurrent Protection Active | `activeOutage.activeOvercurrent` |
| Overcurrent Protection Standby | `activeOutage.activeOvercurrentStandby` (disabled by default) |
| Grid Support | On during a grid support event. Can lag by up to 15 minutes |

### Events

The integration fires these on the HA event bus. Both carry `{ "entry_id": "<config_entry_id>" }`.

- `base_power_outage_started`: no outage to outage
- `base_power_outage_ended`: outage to no outage

A change to or from unknown doesn't fire either event.

## Outage detection

On Battery and Grid Connected can turn off real loads through automations, so the rules favor Base's own signals:

1. Base's outage flag on means outage.
2. A battery state of outage, no home power, overcurrent, or overcurrent standby means outage.
3. A voltage under 50 V means outage if the reading is dated within the last 10 minutes, was seen on 2 polls in a row, and a battery snapshot from the same poll doesn't report on grid.
4. Otherwise, a service context response with telemetry means no outage. Proto3 JSON leaves out false values, so a missing flag in that response is a real false.
5. Without that response, a battery state of on grid or a voltage of at least 100 V means no outage.
6. Anything else is unknown, and both sensors show as unavailable. Missing data never reads as "grid is up."

The `decided_by` attribute shows which signal decided, next to the outage flag and the battery state.

## Polling strategy

| Call | Schedule |
|---|---|
| `MobileGetServiceContext` | The grid interval, 5 s by default and configurable from 5 to 600 s. The outage interval while in an outage, also 5 s by default |
| `MobileGetGridStatus` | Same as above, skipped when the service context includes voltage |
| `MobileGetRecentUsage` | The usage interval, 5 minutes by default |
| `GetSnapshot` | Every 10 minutes. Every 30 s while something points to an outage or the service context isn't answering |
| `GetDailyOverview` | Every 15 minutes |
| `GetCurrentBillingCycle` | Every 6 hours |
| `GetRecentGridVoltage` | Only when the older calls have no voltage, at most every 30 s |

## Newer API

Base's current app uses `dashboard.mobile.v2`. These gRPC-Web calls use the same host and JWT as the older JSON endpoints, and take the address id rather than the numeric service location id.

- **A v2 failure can't hurt the older path.** It is logged at debug, shows in Newer API Status, and never fails a poll or opens a re-auth prompt.
- **The older API can go away.** If `MobileGetServiceContext` fails, the snapshot fills in power and backup readings so entities keep working. In this mode, Battery State of Energy is unavailable while the grid is up, because the snapshot reports percent only while off grid.
- **Parsing is defensive.** Unknown fields are skipped, and malformed data ends the parse without an error.

Field numbers come from [jerrit/ha-base-power](https://github.com/jerrit/ha-base-power), which read them from the server's gRPC reflection.

## Authentication

Base Power uses [Clerk](https://clerk.com) for identity, with a passwordless email code:

1. You enter your email, and Base emails you a 6-digit code.
2. The integration exchanges the code for a Clerk session, and stores the session and client ids in the config entry.
3. It mints a fresh 60-second JWT from `/v1/client/sessions/<sid>/tokens` as needed, and retries once on a rejected token.

When Base rejects the stored session, HA opens a re-auth prompt and Base emails a new code. You can answer the prompt automatically with the `base_power.submit_code` service. See the [top-level README](../../README.md#automatic-re-authentication-v073).

## Installation

### Via HACS (recommended)

1. In HACS -> Integrations -> menu -> Custom repositories, add this repo URL with category "Integration".
2. Install "Base Power".
3. Restart Home Assistant.
4. Settings -> Devices & Services -> Add Integration -> "Base Power".
5. Enter your Base Power account email, then the emailed code.

### Manual

1. Copy `custom_components/base_power/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Settings -> Devices & Services -> Add Integration -> "Base Power".

## How it works under the hood

- **Older API:** JSON at `dashboard.baseapis.net/<Method>`. Some replies are parsed into message types built at runtime from the bundled `file_descriptors.bin`, so no generated `_pb2.py` files are needed.
- **Newer API:** gRPC-Web at `dashboard.baseapis.net/<service>/<method>`, read field by field in `api_v2.py`.

The web dashboard's old ConnectRPC proxy at `account.basepowercompany.com/api/connect` was retired in September 2026.

## Diagnostics

Settings -> Devices & Services -> Base Power -> "Download diagnostics" produces a JSON dump. Email, session and client ids, address ids, service location ids, and Wi-Fi SSID are redacted.

## Limitations

- Read-only: battery dispatch commands (`SendOverCurrentCommand`, `SendManualBackupCommand`) are in the schema but aren't exposed as services.
- The window energy sensors are rolling totals, not lifetime counters, so they don't suit the Energy Dashboard. Use Integration helpers over the Home From sensors instead. See the [top-level README](../../README.md#energy-dashboard-setup).
- One service location per config entry.

## License

MIT. "Base Power" is a trademark of Base Power Company, and this integration is not affiliated with or endorsed by them. See [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md) for adapted code.
