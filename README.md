# Base Power - Home Assistant Integration

Unofficial Home Assistant integration for [Base Power](https://basepowercompany.com/) residential battery / grid-backup service.

Reverse-engineers the Base Power mobile dashboard's ConnectRPC/Protobuf API to expose:

- **Battery state of energy** (`%`)
- **Home power draw** (`kW`)
- **Grid voltage** (`V`)
- **On-battery / grid-connected / gateway-connected** binary sensors
- **Energy totals** (grid/solar/storage → home, kWh)
- **Outage transition events** fired on HA event bus for fast automations

Adaptive polling: 60s normal, 10s while on battery.

## Installation via HACS

1. HACS → Integrations → top-right menu → **Custom repositories**
2. Repository: `moelsaied88/base-power-hass`  •  Category: `Integration`
3. Install "Base Power"
4. Restart Home Assistant
5. Settings → Devices & Services → Add Integration → search "Base Power"
6. Enter your Base Power account email + password

## How it works

See [`custom_components/base_power/README.md`](custom_components/base_power/README.md) for architecture details, entity list, diagnostics/privacy notes, and limitations.

## Disclaimer

This is an **unofficial** integration. Base Power doesn't publish a public API; this integration talks to the private ConnectRPC endpoints used by their web/mobile dashboard. Expect occasional breakage when Base updates their backend. No warranty.

## License

MIT
