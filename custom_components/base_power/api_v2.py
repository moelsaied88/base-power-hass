"""Wire helpers and parsers for Base's ``dashboard.mobile.v2`` services.

Base's current app calls these gRPC-Web services on the same host as the older
``Mobile*`` endpoints, with the same Clerk JWT. No generated code or bundled
descriptors are involved: messages are read field by field, so an unknown field
is skipped and a new one can't break a poll.

Proto3 leaves out fields that hold zero. A number missing from a message that
is present is 0, not unknown.

Adapted from jerrit/ha-base-power (MIT). See THIRD_PARTY_NOTICES.md. That
project took the field numbers from the server's gRPC reflection.

This module imports nothing from Home Assistant or aiohttp.
"""

from __future__ import annotations

import logging
import math
import struct
from typing import Any, Iterator
from urllib.parse import unquote

_LOGGER = logging.getLogger(__name__)

V2_PACKAGE = "dashboard.mobile.v2"
BATTERY_SERVICE = f"{V2_PACKAGE}.BatteryService"
USAGE_SERVICE = f"{V2_PACKAGE}.UsageService"
BILLING_SERVICE = f"{V2_PACKAGE}.BillingService"

GRPC_STATUS_OK = "0"
GRPC_STATUS_UNAUTHENTICATED = "16"

# BatterySnapshot oneof: field number -> state name.
BATTERY_STATES: dict[int, str] = {
    2: "telemetry_unavailable",
    3: "on_grid",
    4: "outage",
    5: "no_home_power",
    6: "overcurrent",
    7: "overcurrent_standby",
}
OFF_GRID_STATES = frozenset(
    {"outage", "no_home_power", "overcurrent", "overcurrent_standby"}
)

# Field layout of each state message: field number -> key.
_STATE_FIELDS: dict[str, dict[int, str]] = {
    "on_grid": {
        1: "observed_at",
        2: "power_flow",
        3: "backup_hours",
        4: "backup_hours_750w",
    },
    "outage": {
        1: "observed_at",
        2: "state_of_energy_percent",
        3: "power_flow",
        4: "backup_hours",
        5: "backup_hours_750w",
    },
    "no_home_power": {
        1: "observed_at",
        2: "state_of_energy_percent",
        3: "backup_hours",
        4: "backup_hours_750w",
    },
    "overcurrent": {
        1: "observed_at",
        2: "state_of_energy_percent",
        3: "power_flow",
        4: "backup_hours",
        5: "backup_hours_750w",
        6: "overcurrent_limit_kw",
    },
    "overcurrent_standby": {
        1: "observed_at",
        2: "state_of_energy_percent",
        3: "backup_hours",
        4: "backup_hours_750w",
        5: "overcurrent_limit_kw",
    },
    "telemetry_unavailable": {},
}
_POWER_FLOW_FIELDS = {
    1: "from_grid_kw",
    2: "from_storage_kw",
    3: "from_solar_kw",
    4: "non_solar_to_home_kw",
    5: "to_home_kw",
}
_NUMBER_KEYS = ("backup_hours", "backup_hours_750w", "overcurrent_limit_kw")

ACCOUNT_TYPE_ENERGY = 1


# ---- Protobuf wire format ----------------------------------------------------


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
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
    """Return (value, new_offset). Raises ValueError on a truncated varint."""
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("truncated varint")


def iter_fields(data: bytes) -> Iterator[tuple[int, str, Any]]:
    """Yield (field_number, kind, value). Stops quietly at malformed data."""
    offset = 0
    try:
        while offset < len(data):
            tag, offset = _decode_varint(data, offset)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if wire_type == 0:
                value, offset = _decode_varint(data, offset)
                yield field_num, "varint", value
            elif wire_type == 1:
                if offset + 8 > len(data):
                    return
                yield field_num, "double", struct.unpack_from("<d", data, offset)[0]
                offset += 8
            elif wire_type == 2:
                length, offset = _decode_varint(data, offset)
                if offset + length > len(data):
                    return
                yield field_num, "bytes", data[offset : offset + length]
                offset += length
            elif wire_type == 5:
                if offset + 4 > len(data):
                    return
                yield field_num, "float", struct.unpack_from("<f", data, offset)[0]
                offset += 4
            else:
                return
    except ValueError:
        return


def _timestamp_seconds(message: bytes) -> int | None:
    """Read google.protobuf.Timestamp.seconds."""
    seconds: int | None = None
    for field_num, kind, value in iter_fields(message):
        if field_num == 1 and kind == "varint":
            seconds = value
    return seconds


def _int64(value: int) -> int:
    """A negative int64 arrives as a 64-bit two's-complement varint."""
    return value - (1 << 64) if value >= 1 << 63 else value


# ---- gRPC-Web framing --------------------------------------------------------


def build_grpc_frame(payload: bytes) -> bytes:
    """Wrap a protobuf message in a gRPC-Web data frame."""
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def read_grpc_web(
    body: bytes,
    header_status: str | None = None,
    header_message: str | None = None,
) -> tuple[bytes, str, str]:
    """Return (message, grpc_status, grpc_message) from a gRPC-Web response.

    Each frame is a flag byte, a 4-byte length, and that many bytes. Flag 0x80
    is the trailer. Status can arrive in the HTTP headers or the trailer. A
    non-zero status from either one wins.
    """
    message = b""
    trailer_status = ""
    trailer_message = ""
    offset = 0
    while offset + 5 <= len(body):
        flag = body[offset]
        length = struct.unpack_from(">I", body, offset + 1)[0]
        offset += 5
        if offset + length > len(body):
            break
        frame = body[offset : offset + length]
        offset += length
        if flag & 0x80:
            for line in frame.decode("utf-8", "replace").splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                key = key.strip().lower()
                if key == "grpc-status":
                    trailer_status = value.strip()
                elif key == "grpc-message":
                    trailer_message = unquote(value.strip())
        elif not message:
            message = frame

    header_status = (header_status or "").strip()
    header_message = (header_message or "").strip()
    if trailer_status and trailer_status != GRPC_STATUS_OK:
        return message, trailer_status, trailer_message or header_message
    if header_status:
        return message, header_status, header_message
    return message, trailer_status, trailer_message


# ---- Request encoders --------------------------------------------------------


def encode_address_request(address_id: str) -> bytes:
    """Field 1 is the address id string, not the numeric service location id."""
    encoded = address_id.encode("utf-8")
    return b"\x0a" + encode_varint(len(encoded)) + encoded


def encode_billing_cycle_request(
    address_id: str, account_type: int = ACCOUNT_TYPE_ENERGY
) -> bytes:
    """Field 1 is AccountReference {1 address, 2 type}."""
    reference = (
        encode_address_request(address_id) + b"\x10" + encode_varint(account_type)
    )
    return b"\x0a" + encode_varint(len(reference)) + reference


# ---- Response parsers --------------------------------------------------------


def empty_snapshot() -> dict[str, Any]:
    return {
        "available": False,
        "state": None,
        "observed_at": None,
        "state_of_energy_percent": None,
        "backup_hours": None,
        "backup_hours_750w": None,
        "overcurrent_limit_kw": None,
        "power_flow": None,
    }


def parse_battery_snapshot(data: bytes) -> dict[str, Any]:
    """Parse GetSnapshotResponse. The state is whichever oneof field is set."""
    result = empty_snapshot()
    snapshot = next(
        (v for f, k, v in iter_fields(data) if f == 1 and k == "bytes"), None
    )
    if snapshot is None:
        return result
    for field_num, kind, value in iter_fields(snapshot):
        state = BATTERY_STATES.get(field_num)
        if state is None or kind != "bytes":
            continue
        layout = _STATE_FIELDS[state]
        keys = set(layout.values())
        result["available"] = True
        result["state"] = state
        for key in _NUMBER_KEYS:
            if key in keys:
                result[key] = 0.0
        if "state_of_energy_percent" in keys:
            result["state_of_energy_percent"] = 0
        if "power_flow" in keys:
            result["power_flow"] = {key: 0.0 for key in _POWER_FLOW_FIELDS.values()}
        for sub_num, sub_kind, sub_value in iter_fields(value):
            key = layout.get(sub_num)
            if key == "observed_at" and sub_kind == "bytes":
                result["observed_at"] = _timestamp_seconds(sub_value)
            elif key == "state_of_energy_percent" and sub_kind == "varint":
                result[key] = _int64(sub_value)
            elif key in _NUMBER_KEYS and sub_kind == "double":
                if math.isfinite(sub_value):
                    result[key] = round(sub_value, 2)
            elif key == "power_flow" and sub_kind == "bytes":
                for flow_num, flow_kind, flow_value in iter_fields(sub_value):
                    name = _POWER_FLOW_FIELDS.get(flow_num)
                    if name and flow_kind == "double" and math.isfinite(flow_value):
                        result["power_flow"][name] = round(flow_value, 3)
    return result


def empty_overview() -> dict[str, Any]:
    return {
        "available": False,
        "energy_mix_24h": None,
        "grid_support": [],
        "cost_today_cents": None,
        "cost_currency": None,
    }


def parse_daily_overview(data: bytes, start: int, end: int) -> dict[str, Any]:
    """Parse GetDailyOverviewResponse.

    The reply is a rolling 24 hours. ``energy_mix_24h`` covers that whole
    window. ``cost_today_cents`` sums hourly grid cost within [start, end).
    """
    result = empty_overview()
    cost_total = 0.0
    cost_seen = False
    for field_num, kind, value in iter_fields(data):
        if kind != "bytes":
            continue
        result["available"] = True
        if field_num == 3:
            interval: dict[str, int | None] = {"start": None, "end": None}
            for sub_num, sub_kind, sub_value in iter_fields(value):
                if sub_kind == "bytes" and sub_num in (1, 2):
                    interval["start" if sub_num == 1 else "end"] = _timestamp_seconds(
                        sub_value
                    )
            if interval["start"] is not None:
                result["grid_support"].append(interval)
        elif field_num == 5:
            stamp: int | None = None
            cents = 0.0
            for sub_num, sub_kind, sub_value in iter_fields(value):
                if sub_num == 1 and sub_kind == "bytes":
                    stamp = _timestamp_seconds(sub_value)
                elif sub_num == 2 and sub_kind == "double":
                    cents = sub_value
            if stamp is not None and start <= stamp < end and math.isfinite(cents):
                cost_total += cents
                cost_seen = True
        elif field_num == 6:
            result["cost_currency"] = value.decode("utf-8", errors="replace") or None
        elif field_num == 7:
            mix = {
                "grid_to_home_kwh": 0.0,
                "solar_to_home_kwh": 0.0,
                "battery_to_home_kwh": 0.0,
            }
            names = {
                1: "grid_to_home_kwh",
                2: "solar_to_home_kwh",
                3: "battery_to_home_kwh",
            }
            for sub_num, sub_kind, sub_value in iter_fields(value):
                if (
                    sub_num in names
                    and sub_kind == "double"
                    and math.isfinite(sub_value)
                ):
                    mix[names[sub_num]] = round(sub_value, 3)
            result["energy_mix_24h"] = mix
    if cost_seen:
        result["cost_today_cents"] = round(cost_total, 2)
    return result


def empty_billing_cycle() -> dict[str, Any]:
    return {
        "available": False,
        "currency": None,
        "service_period_start": None,
        "data_through": None,
        "gross_charges_cents": None,
        "solar_credit_cents": None,
        "net_accrued_cents": None,
        "projected_charges_cents": None,
    }


def parse_current_billing_cycle(data: bytes) -> dict[str, Any]:
    """Parse GetCurrentBillingCycleResponse. Empty without Base billing."""
    result = empty_billing_cycle()
    cycle = next(
        (v for f, k, v in iter_fields(data) if f == 1 and k == "bytes"), None
    )
    if cycle is None:
        return result
    amounts = {
        5: "gross_charges_cents",
        6: "solar_credit_cents",
        7: "net_accrued_cents",
        8: "projected_charges_cents",
    }
    result["available"] = True
    for key in amounts.values():
        result[key] = 0
    for field_num, kind, value in iter_fields(cycle):
        if field_num == 1 and kind == "bytes":
            result["currency"] = value.decode("utf-8", errors="replace") or None
        elif field_num == 2 and kind == "bytes":
            result["service_period_start"] = _timestamp_seconds(value)
        elif field_num == 4 and kind == "bytes":
            result["data_through"] = _timestamp_seconds(value)
        elif field_num in amounts and kind == "varint":
            result[amounts[field_num]] = _int64(value)
    return result


def parse_recent_grid_voltage(data: bytes) -> dict[str, Any]:
    """Parse GetRecentGridVoltageResponse. The newest sample wins.

    Field 1 repeats {1 observed_at, 2 voltage_v}. Zero volts is left out of the
    message by proto3, so a sample with no voltage field is a real 0 V.
    """
    samples: list[tuple[int, float]] = []
    for field_num, kind, value in iter_fields(data):
        if field_num != 1 or kind != "bytes":
            continue
        stamp: int | None = None
        volts = 0.0
        for sub_num, sub_kind, sub_value in iter_fields(value):
            if sub_num == 1 and sub_kind == "bytes":
                stamp = _timestamp_seconds(sub_value)
            elif sub_num == 2 and sub_kind == "double":
                volts = sub_value
        if stamp is not None and math.isfinite(volts):
            samples.append((stamp, volts))
    if not samples:
        return {"grid_voltage": None, "latest_grid_voltage_epoch": None}
    stamp, volts = max(samples)
    return {"grid_voltage": volts, "latest_grid_voltage_epoch": stamp}


def grid_support_active(
    intervals: list[dict[str, int | None]], now: int
) -> bool:
    """True when now is inside an interval. A missing end means still running."""
    for interval in intervals:
        start = interval.get("start")
        end = interval.get("end")
        if start is not None and start <= now and (end is None or now < end):
            return True
    return False
