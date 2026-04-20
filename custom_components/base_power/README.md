# Base Power - Home Assistant Integration

Read-only Home Assistant integration for [Base Power Company](https://basepowercompany.com/) customers (Texas electricity + home battery). Exposes live battery state, grid-outage detection, and energy flow as HA entities.

> **Unofficial.** Base Power does not publish a public API. This integration speaks to the same ConnectRPC endpoints their web dashboard uses at `account.basepowercompany.com`. If Base changes those endpoints it will break; rerun the schema capture tool (`tools/base_power_capture/`) to rebuild.

## Entities

Grouped under a single HA Device ("Base Battery"):

### Sensors

| Entity | Unit | Source field |
|---|---|---|
| Battery State of Charge | % | `GetServiceStatus.stateOfEnergy` |
| Grid Voltage | V | `GetServiceStatus.gridVoltage` |
| Inverter Synthetic Voltage | V | `GetServiceStatus.synVoltage` (disabled by default) |
| Home Power | W | `MobileGetRecentUsage.power_level_data[-1].power_to_home_kw` × 1000 |
| Energy From Grid (recent) | kWh | `energy_usage_source.grid_to_home_kwh` |
| Energy From Solar (recent) | kWh | `energy_usage_source.solar_to_home_kwh` |
| Energy From Battery (recent) | kWh | `energy_usage_source.storage_to_home_kwh` |
| Backup Runtime Estimate | min | last `DurationSOEDataPoint.duration` |
| Backup Runtime Estimate at 750W | min | last `DurationSOEDataPoint.duration_at_750w` (disabled by default) |

### Binary sensors

| Entity | Source field |
|---|---|
| On Battery | `activeOutage` (ON = grid is down, you're on battery) |
| Grid Connected | inverse of `activeOutage` |
| Gateway Connected | `gatewayConnection` |
| Overcurrent Protection Active | `activeOvercurrent` |
| Overcurrent Protection Standby | `activeOvercurrentStandby` (disabled by default) |

### Events

The integration fires the following on the HA event bus on outage transitions:

- `base_power_outage_started` - `activeOutage` flipped false → true
- `base_power_outage_ended`   - `activeOutage` flipped true → false

Both events carry `{ "entry_id": "<config_entry_id>" }` in their data.

## Polling strategy

- **30 s** on grid (configurable in Options, 5-600 s range).
- **5 s**  during an active outage (configurable, 5-600 s range).
- Primary poll is `GetServiceStatus` - a cheap protobuf call. The heavier time-series endpoint `MobileGetRecentUsage` runs at most once every 60 s regardless of primary interval.

## Authentication

Base Power uses [Clerk](https://clerk.com) for identity. The integration:

1. Signs in with email/password using Clerk's `/v1/client/sign_ins` endpoint.
2. Stores the resulting Clerk `session_id` in memory only.
3. Mints a fresh 60-second JWT from `/v1/client/sessions/<sid>/tokens` for every API request window.
4. Passes the JWT as `Authorization: Bearer <jwt>` on ConnectRPC calls.

Credentials are stored encrypted in Home Assistant's config entry store. The integration never writes credentials to logs.

> **2FA / MFA is not currently supported.** If you have two-factor auth enabled on your Base account, the integration will refuse to sign in. Workaround: disable 2FA, or file an issue requesting MFA support.

## Installation

### Via HACS (recommended)

1. In HACS → Integrations → ⋮ → Custom repositories, add this repo URL with category "Integration".
2. Install "Base Power".
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → "Base Power".
5. Enter your Base Power account email and password.

### Manual

1. Copy `custom_components/base_power/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "Base Power".

## How it works under the hood

Base Power's web dashboard is a Next.js SPA that talks to a ConnectRPC (protobuf-over-HTTP) backend. The `GetServiceStatus` endpoint is the single source of live battery / outage state and is polled frequently by the web UI itself.

This integration:

1. Bundles a `file_descriptors.bin` FileDescriptorSet extracted from the SPA's JS bundle (see `tools/base_power_capture/extract_schema.py`).
2. Builds a runtime `DescriptorPool` + `MessageFactory` on first load, so we can encode/decode protobuf messages without protoc-generated `_pb2.py` files.
3. Sends `application/proto` requests with the required `connect-protocol-version: 1` header.

If Base Power ever changes their schema, rerun the capture tool and copy the regenerated `file_descriptors.bin` back into this directory - no code changes needed as long as the field names stay stable.

## Diagnostics

HA's Settings → Devices & Services → Base Power → "Download diagnostics" produces a JSON dump. Email, password, address IDs, service location IDs, and Wi-Fi SSID are automatically redacted before the dump is written.

## Limitations

- Read-only: battery dispatch commands (`SendOverCurrentCommand`, `SendManualBackupCommand`) are documented in the proto schema but not exposed as services. Open an issue if you'd use them.
- Energy entities are "recent" totals (rolling window from `MobileGetRecentUsage`) rather than lifetime counters. The HA Energy dashboard expects monotonic counters, so treat these as informational rather than wiring them up as a utility meter.
- Single service location per config entry. If your account has multiple locations you'll need to add the integration multiple times once that's supported (currently defaults to the first location).

## License

MIT. Trademarks belong to their respective owners; "Base Power" is a trademark of Base Power Company and this integration is not affiliated with or endorsed by them.
