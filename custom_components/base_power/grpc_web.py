"""gRPC-Web framing and hand-written parsers for Base's current API.

Base retired the mobile REST/JSON routes (``/MobileGetServiceContext`` and
friends) on 2026-11-24; they now answer HTTP 415. The same host still serves
``dashboard.DashboardAPI`` and the newer ``dashboard.mobile.v2`` services over
gRPC-Web with binary protobuf bodies.

The v2 messages are not in ``file_descriptors.bin``, so they are decoded
here by field number. The field layouts and the on-grid battery-percent
estimate are adapted from jerrit/ha-base-power v1.15.0 (MIT License,
Copyright (c) 2025 Contributors), which mapped them from the server's gRPC
reflection.

This module has no Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import struct
from typing import Any, Iterator
from urllib.parse import unquote

CONTENT_TYPE = "application/grpc-web+proto"
DASHBOARD_SERVICE = "dashboard.DashboardAPI"
V2_PACKAGE = "dashboard.mobile.v2"
V2_BATTERY = f"{V2_PACKAGE}.BatteryService"
V2_USAGE = f"{V2_PACKAGE}.UsageService"
V2_BILLING = f"{V2_PACKAGE}.BillingService"
V2_LOCATIONS = f"{V2_PACKAGE}.LocationsService"

GRPC_OK = "0"
GRPC_UNIMPLEMENTED = "12"
GRPC_UNAUTHENTICATED = "16"

# ---- framing ---------------------------------------------------------------


def build_frame(payload: bytes) -> bytes:
    """Wrap a protobuf message in a gRPC-Web data frame."""
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def read_response(
    body: bytes, header_status: str = "", header_message: str = ""
) -> tuple[bytes, str, str]:
    """Split a gRPC-Web body into (message, grpc-status, grpc-message).

    The status can arrive in the HTTP headers or in the trailer frame
    (flag 0x80); a non-zero value from either wins.
    """
    message = b""
    trailer_status = ""
    trailer_message = ""
    offset = 0
    while offset + 5 <= len(body):
        flag = body[offset]
        length = struct.unpack(">I", body[offset + 1 : offset + 5])[0]
        offset += 5
        if offset + length > len(body):
            break
        frame = body[offset : offset + length]
        offset += length
        if flag & 0x80:
            for line in frame.decode("utf-8", "replace").splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                if key == "grpc-status":
                    trailer_status = value.strip()
                elif key == "grpc-message":
                    trailer_message = unquote(value.strip())
        elif not message:
            message = frame
    status = (header_status or "").strip()
    detail = unquote((header_message or "").strip())
    if trailer_status and trailer_status != GRPC_OK:
        return message, trailer_status, trailer_message or detail
    if not status:
        status = trailer_status
        detail = detail or trailer_message
    return message, status or GRPC_OK, detail


# ---- protobuf wire helpers ---------------------------------------------------


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        shift += 7
        offset += 1
        if not byte & 0x80:
            break
    return value, offset


def iter_fields(data: bytes) -> Iterator[tuple[int, str, Any]]:
    """Yield (field_number, kind, value) for each field of a message."""
    offset = 0
    while offset < len(data):
        tag, new_offset = _decode_varint(data, offset)
        if new_offset == offset:
            return
        offset = new_offset
        field_num, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
            yield field_num, "varint", value
        elif wire_type == 2:
            length, offset = _decode_varint(data, offset)
            if offset + length > len(data):
                return
            yield field_num, "bytes", data[offset : offset + length]
            offset += length
        elif wire_type == 1:
            if offset + 8 > len(data):
                return
            yield field_num, "double", struct.unpack("<d", data[offset : offset + 8])[0]
            offset += 8
        elif wire_type == 5:
            if offset + 4 > len(data):
                return
            yield field_num, "float", struct.unpack("<f", data[offset : offset + 4])[0]
            offset += 4
        else:
            return


def _first_bytes(data: bytes, field: int) -> bytes | None:
    return next((v for f, k, v in iter_fields(data) if f == field and k == "bytes"), None)


def _text(value: bytes) -> str | None:
    try:
        return value.decode("utf-8").strip() or None
    except UnicodeDecodeError:
        return None


def _int64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _timestamp_seconds(message: bytes) -> int | None:
    return next((v for f, k, v in iter_fields(message) if f == 1 and k == "varint"), None)


def encode_string_field1(value: str) -> bytes:
    """Request whose field 1 is a string (address id)."""
    raw = value.encode("utf-8")
    return b"\x0a" + encode_varint(len(raw)) + raw


def encode_int_field1(value: int) -> bytes:
    """Request whose field 1 is an int32 (numeric service location id)."""
    return b"\x08" + encode_varint(value)


def encode_account_request(address_id: str, account_type: int) -> bytes:
    """GetAccountRequest: field 1 AccountReference {1 address_id, 2 type}."""
    ref = encode_string_field1(address_id) + b"\x10" + encode_varint(account_type)
    return b"\x0a" + encode_varint(len(ref)) + ref


ACCOUNT_TYPE_BATTERY = 2

# ---- DashboardAPI ------------------------------------------------------------

GATEWAY_WIFI_STATES = {0: "connected", 1: "connecting", 2: "not_connected"}


def parse_service_status(data: bytes) -> dict[str, Any] | None:
    """DashboardAPI.GetServiceStatus.

    1 gridVoltage (int volts), 3 hasGateway, 4 gatewayConnection,
    5 stateOfEnergy (0-5 bars), 6 gatewayWifi {1 ssid, 2 state},
    9 activeOutage. Proto3 omits false/zero, so missing bools are False.
    Returns None for an empty reply.
    """
    if not data:
        return None
    out: dict[str, Any] = {
        "grid_voltage": None,
        "has_gateway": False,
        "gateway_connected": False,
        "energy_bars": 0,
        "active_outage": False,
        "wifi_ssid": None,
        "wifi_state": None,
    }
    for num, kind, value in iter_fields(data):
        if num == 1 and kind == "varint" and value:
            out["grid_voltage"] = float(value)
        elif num == 3 and kind == "varint":
            out["has_gateway"] = bool(value)
        elif num == 4 and kind == "varint":
            out["gateway_connected"] = bool(value)
        elif num == 5 and kind == "varint":
            out["energy_bars"] = value
        elif num == 9 and kind == "varint":
            out["active_outage"] = bool(value)
        elif num == 6 and kind == "bytes":
            state = 0
            for sub, sub_kind, sub_value in iter_fields(value):
                if sub == 1 and sub_kind == "bytes":
                    out["wifi_ssid"] = _text(sub_value)
                elif sub == 2 and sub_kind == "varint":
                    state = sub_value
            out["wifi_state"] = GATEWAY_WIFI_STATES.get(state)
    return out


def parse_dashboard_root(data: bytes) -> dict[str, Any]:
    """DashboardAPI.GetDashboardRoot: dashboardRoot (1) →
    serviceLocation (1) {1 id, 6 hasSolar}, address (5) {9 timezone},
    battery (10) {3 hasSolar}."""
    out: dict[str, Any] = {"service_location_id": None, "has_solar": False, "timezone": ""}
    root = _first_bytes(data, 1) or b""
    for num, kind, value in iter_fields(root):
        if kind != "bytes":
            continue
        for sub, sub_kind, sub_value in iter_fields(value):
            if (num, sub, sub_kind) == (1, 1, "varint") and sub_value > 0:
                out["service_location_id"] = int(sub_value)
            elif (num, sub) in ((1, 6), (10, 3)) and sub_kind == "varint" and sub_value:
                out["has_solar"] = True
            elif (num, sub, sub_kind) == (5, 9, "bytes"):
                out["timezone"] = _text(sub_value) or ""
    return out


def parse_available_locations(data: bytes) -> list[str]:
    """DashboardAPI.GetAvailableLocations: locations (1) → addressId (1)."""
    ids: list[str] = []
    for num, kind, loc in iter_fields(data):
        if num == 1 and kind == "bytes":
            first = _first_bytes(loc, 1)
            if first and (text := _text(first)):
                ids.append(text)
    return ids


# ---- dashboard.mobile.v2 -------------------------------------------------------


def parse_locations(data: bytes) -> list[dict[str, str]]:
    """LocationsService.ListLocations: locations (1) {1 address_id,
    2 DisplayAddress {1 line_1, 3 city}}."""
    out: list[dict[str, str]] = []
    for num, kind, summary in iter_fields(data):
        if num != 1 or kind != "bytes":
            continue
        address_id = ""
        parts: dict[int, str] = {}
        for sub, sub_kind, value in iter_fields(summary):
            if sub == 1 and sub_kind == "bytes":
                address_id = _text(value) or ""
            elif sub == 2 and sub_kind == "bytes":
                for addr, addr_kind, text in iter_fields(value):
                    if addr in (1, 3) and addr_kind == "bytes":
                        parts[addr] = _text(text) or ""
        if address_id:
            label = ", ".join(p for p in (parts.get(1), parts.get(3)) if p)
            out.append({"address_id": address_id, "label": label or address_id})
    return out


# BatterySnapshot oneof field → state
BATTERY_STATES = {
    2: "telemetry_unavailable",
    3: "on_grid",
    4: "outage",
    5: "no_home_power",
    6: "overcurrent",
    7: "overcurrent_standby",
}
OFF_GRID_STATES = frozenset({"outage", "no_home_power", "overcurrent", "overcurrent_standby"})
_SNAPSHOT_WIFI_STATES = {2: "not_connected", 3: "connecting", 4: "connected"}
_STATE_FIELDS: dict[str, dict[int, str]] = {
    "on_grid": {1: "observed_at", 2: "power_flow", 3: "backup_hours", 4: "backup_hours_750w"},
    "outage": {
        1: "observed_at", 2: "soe_pct", 3: "power_flow", 4: "backup_hours", 5: "backup_hours_750w",
    },
    "no_home_power": {1: "observed_at", 2: "soe_pct", 3: "backup_hours", 4: "backup_hours_750w"},
    "overcurrent": {
        1: "observed_at", 2: "soe_pct", 3: "power_flow", 4: "backup_hours",
        5: "backup_hours_750w", 6: "overcurrent_limit_kw",
    },
    "overcurrent_standby": {
        1: "observed_at", 2: "soe_pct", 3: "backup_hours", 4: "backup_hours_750w",
        5: "overcurrent_limit_kw",
    },
    "telemetry_unavailable": {},
}
_POWER_FLOW = {1: "from_grid_kw", 2: "from_storage_kw", 3: "from_solar_kw", 5: "to_home_kw"}
_DOUBLE_KEYS = ("backup_hours", "backup_hours_750w", "overcurrent_limit_kw")


def parse_battery_snapshot(data: bytes) -> dict[str, Any] | None:
    """BatteryService.GetSnapshot. The battery state is whichever oneof
    field is set; fields inside a present state message default to 0.
    Returns None for an empty reply."""
    snapshot = _first_bytes(data, 1)
    if snapshot is None:
        return None
    out: dict[str, Any] = {
        "state": None,
        "observed_at": None,
        "soe_pct": None,
        "backup_hours": None,
        "backup_hours_750w": None,
        "overcurrent_limit_kw": None,
        "power_flow": None,
        "wifi_ssid": None,
        "wifi_state": None,
    }
    for num, kind, value in iter_fields(snapshot):
        if num == 1 and kind == "bytes":
            status = 0
            for sub, sub_kind, sub_value in iter_fields(value):
                if sub == 1 and sub_kind == "varint":
                    status = sub_value
                elif sub == 2 and sub_kind == "bytes":
                    out["wifi_ssid"] = _text(sub_value)
            out["wifi_state"] = _SNAPSHOT_WIFI_STATES.get(status)
            continue
        state = BATTERY_STATES.get(num)
        if state is None or kind != "bytes":
            continue
        layout = _STATE_FIELDS[state]
        out["state"] = state
        for key in _DOUBLE_KEYS:
            if key in layout.values():
                out[key] = 0.0
        if "soe_pct" in layout.values():
            out["soe_pct"] = 0
        if "power_flow" in layout.values():
            out["power_flow"] = dict.fromkeys(_POWER_FLOW.values(), 0.0)
        for sub, sub_kind, sub_value in iter_fields(value):
            key = layout.get(sub)
            if key == "observed_at" and sub_kind == "bytes":
                out["observed_at"] = _timestamp_seconds(sub_value)
            elif key == "soe_pct" and sub_kind == "varint":
                out["soe_pct"] = _int64(sub_value)
            elif key in _DOUBLE_KEYS and sub_kind == "double":
                out[key] = round(sub_value, 3)
            elif key == "power_flow" and sub_kind == "bytes":
                for flow, flow_kind, flow_value in iter_fields(sub_value):
                    name = _POWER_FLOW.get(flow)
                    if name and flow_kind == "double":
                        out["power_flow"][name] = round(flow_value, 4)
    return out


def parse_recent_grid_voltage(data: bytes) -> tuple[float | None, int | None]:
    """UsageService.GetRecentGridVoltage: repeated (1) {1 Timestamp,
    2 voltage double}. Returns the newest (volts, epoch seconds)."""
    samples: list[tuple[int, float]] = []
    for num, kind, value in iter_fields(data):
        if num != 1 or kind != "bytes":
            continue
        stamp = None
        volts = 0.0
        for sub, sub_kind, sub_value in iter_fields(value):
            if sub == 1 and sub_kind == "bytes":
                stamp = _timestamp_seconds(sub_value)
            elif sub == 2 and sub_kind == "double":
                volts = sub_value
        if stamp is not None:
            samples.append((stamp, volts))
    if not samples:
        return None, None
    stamp, volts = max(samples)
    return float(volts), stamp


def parse_battery_sku(data: bytes) -> str | None:
    """BillingService.GetAccount (battery): account (1) → plan (2) →
    battery (3) → equipment (1) → hardware_sku (2)."""
    account = _first_bytes(data, 1)
    plan = _first_bytes(account, 2) if account else None
    battery = _first_bytes(plan, 3) if plan else None
    if battery is None:
        return None
    for num, kind, equipment in iter_fields(battery):
        if num == 1 and kind == "bytes":
            sku = _first_bytes(equipment, 2)
            if sku and (text := _text(sku)):
                return text
    return None


# ---- battery percent estimate ------------------------------------------------

# Nameplate kWh by hardware SKU (Base's web checkout sizes).
SKU_CAPACITY_KWH: dict[str, float] = {
    "GRO_SINGLE": 25.0,
    "GRO_DUAL": 50.0,
    "GRO_WALL": 20.0,
    "BASE1_SINGLE": 39.2,
    "BASE1_DUAL": 78.4,
}
BACKUP_RESERVE_PERCENT = 10.0
ESTIMATE_LOAD_KW = 0.75


def estimate_soe_pct(backup_hours_750w: float | None, capacity_kwh: float | None) -> float | None:
    """Battery percent from Base's 750 W backup time.

    Base no longer sends the percent while on grid. hours x 0.75 kW is the
    energy left above Base's 10 % reserve, so percent = 10 + that / capacity.
    """
    if backup_hours_750w is None or not capacity_kwh or backup_hours_750w < 0:
        return None
    pct = BACKUP_RESERVE_PERCENT + backup_hours_750w * ESTIMATE_LOAD_KW / capacity_kwh * 100
    return round(min(100.0, max(0.0, pct)), 1)
