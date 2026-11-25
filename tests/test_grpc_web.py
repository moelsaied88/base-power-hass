"""Tests for the gRPC-Web transport and parsers (Base API after 2026-11-24).

Messages are built by hand with the field numbers from Base's gRPC
reflection, matching the vectors in jerrit/ha-base-power's tests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "base_power"


def _load(name: str):
    pkg = sys.modules.get("base_power_pkg")
    if pkg is None:
        pkg = types.ModuleType("base_power_pkg")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["base_power_pkg"] = pkg
    full = f"base_power_pkg.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


gw = _load("grpc_web")


def _varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    return gw.encode_varint(value)


def num(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def msg(field: int, body: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(body)) + body


def text(field: int, value: str) -> bytes:
    return msg(field, value.encode())


def dbl(field: int, value: float) -> bytes:
    return _varint(field << 3 | 1) + struct.pack("<d", value)


def ts(field: int, seconds: int) -> bytes:
    return msg(field, num(1, seconds))


def frame(flag: int, payload: bytes) -> bytes:
    return bytes([flag]) + struct.pack(">I", len(payload)) + payload


def grpc_body(message: bytes = b"", status: str = "0", detail: str = "") -> bytes:
    trailer = f"grpc-status: {status}\r\n"
    if detail:
        trailer += f"grpc-message: {detail}\r\n"
    return frame(0, message) + frame(0x80, trailer.encode())


def snapshot(state_field: int, body: bytes) -> bytes:
    wifi = num(1, 4) + text(2, "HomeNet")
    return msg(1, msg(1, wifi) + msg(state_field, body))


# ---- framing -------------------------------------------------------------------


def test_frame_round_trip_and_trailer_status():
    assert gw.build_frame(b"abc") == b"\x00\x00\x00\x00\x03abc"
    message, status, detail = gw.read_response(grpc_body(b"xyz"))
    assert (message, status, detail) == (b"xyz", "0", "")
    _, status, detail = gw.read_response(grpc_body(status="12", detail="unknown%20method"))
    assert (status, detail) == ("12", "unknown method")
    # A header status is used when there is no trailer.
    assert gw.read_response(b"", "16", "")[1] == "16"


# ---- parsers ---------------------------------------------------------------------


def test_service_status():
    body = num(1, 246) + num(3, 1) + num(4, 1) + num(5, 5) + msg(6, text(1, "HomeNet"))
    parsed = gw.parse_service_status(body)
    assert parsed["grid_voltage"] == 246.0
    assert parsed["has_gateway"] is True
    assert parsed["gateway_connected"] is True
    assert parsed["active_outage"] is False
    assert parsed["energy_bars"] == 5
    assert parsed["wifi_state"] == "connected"
    outage = gw.parse_service_status(num(3, 1) + num(9, 1))
    assert outage["active_outage"] is True and outage["grid_voltage"] is None
    assert gw.parse_service_status(b"") is None


def test_on_grid_snapshot():
    flow = dbl(1, 0.9) + dbl(5, 0.9)
    parsed = gw.parse_battery_snapshot(
        snapshot(3, ts(1, 1_800_000_000) + msg(2, flow) + dbl(3, 25.61) + dbl(4, 30.0))
    )
    assert parsed["state"] == "on_grid"
    assert parsed["observed_at"] == 1_800_000_000
    assert parsed["backup_hours"] == 25.61
    assert parsed["backup_hours_750w"] == 30.0
    assert parsed["power_flow"]["from_grid_kw"] == 0.9
    assert parsed["power_flow"]["from_storage_kw"] == 0.0
    assert parsed["soe_pct"] is None
    assert parsed["wifi_state"] == "connected"


def test_outage_and_overcurrent_snapshots():
    out = gw.parse_battery_snapshot(
        snapshot(4, ts(1, 1) + num(2, 87) + msg(3, dbl(2, 1.4) + dbl(5, 1.4)) + dbl(4, 12.5))
    )
    assert out["state"] == "outage" and out["soe_pct"] == 87
    assert out["power_flow"]["from_storage_kw"] == 1.4
    assert out["backup_hours"] == 12.5
    standby = gw.parse_battery_snapshot(snapshot(7, num(2, 40) + dbl(5, 9.6)))
    assert standby["state"] == "overcurrent_standby"
    assert standby["overcurrent_limit_kw"] == 9.6
    assert gw.parse_battery_snapshot(snapshot(2, b""))["state"] == "telemetry_unavailable"
    assert gw.parse_battery_snapshot(b"") is None


def test_dashboard_root_and_locations():
    root = msg(1, num(1, 4321) + num(6, 1)) + msg(5, text(9, "America/Chicago"))
    parsed = gw.parse_dashboard_root(msg(1, root))
    assert parsed == {"service_location_id": 4321, "has_solar": True, "timezone": "America/Chicago"}
    listed = gw.parse_locations(msg(1, text(1, "addr-1") + msg(2, text(1, "1 Main St") + text(3, "Cypress"))))
    assert listed == [{"address_id": "addr-1", "label": "1 Main St, Cypress"}]
    assert gw.parse_available_locations(msg(1, text(1, "addr-2"))) == ["addr-2"]


def test_grid_voltage_and_sku():
    body = msg(1, ts(1, 100) + dbl(2, 240.5)) + msg(1, ts(1, 200) + dbl(2, 241.0))
    assert gw.parse_recent_grid_voltage(body) == (241.0, 200)
    assert gw.parse_recent_grid_voltage(b"") == (None, None)
    equipment = text(1, "Battery") + text(2, "GRO_DUAL")
    account = msg(1, msg(2, text(1, "Plan") + msg(3, msg(1, equipment))))
    assert gw.parse_battery_sku(account) == "GRO_DUAL"


def test_soe_estimate():
    # 50 kWh, 30 h at 750 W = 22.5 kWh above the 10 % reserve -> 55 %.
    assert gw.estimate_soe_pct(30.0, 50.0) == 55.0
    assert gw.estimate_soe_pct(200.0, 50.0) == 100.0
    assert gw.estimate_soe_pct(None, 50.0) is None
    assert gw.estimate_soe_pct(30.0, None) is None


# ---- client over a fake HTTP session ---------------------------------------------


def _client_module():
    try:
        return _load("api")
    except ModuleNotFoundError:  # aiohttp / protobuf missing
        return None


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[tuple[str, dict, bytes]] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, headers, data))
        path = url.split("baseapis.net/", 1)[1]
        return _FakeResp(200, self.routes.get(path, grpc_body(status="12")))


class _FakeAuth:
    async def get_jwt(self) -> str:
        return "jwt"

    async def reset(self) -> None:
        pass


def test_service_context_on_grid_uses_estimate():
    api = _client_module()
    if api is None:
        return
    flow = dbl(1, 0.4) + dbl(2, 0.0) + dbl(5, 1.2)
    account = msg(1, msg(2, msg(3, msg(1, text(2, "GRO_DUAL")))))
    session = _FakeSession(
        {
            "dashboard.DashboardAPI/GetServiceStatus": grpc_body(
                num(1, 241) + num(3, 1) + num(4, 1) + num(5, 4)
            ),
            "dashboard.mobile.v2.BatteryService/GetSnapshot": grpc_body(
                snapshot(3, ts(1, 5) + msg(2, flow) + dbl(3, 18.0) + dbl(4, 30.0))
            ),
            "dashboard.mobile.v2.BillingService/GetAccount": grpc_body(account),
        }
    )
    client = api.BasePowerClient(session, auth=_FakeAuth())
    ctx = asyncio.run(client.get_service_context(4321, "addr-1"))
    assert ctx["home_power_w"] == 1200.0
    assert ctx["power_from_grid_w"] == 400.0
    assert ctx["grid_voltage"] == 241.0
    assert ctx["state_of_energy_pct"] == 55.0
    assert ctx["state_of_energy_source"] == "backup_hours_estimate"
    assert ctx["active_outage"] is False
    assert ctx["home_has_power"] is True
    assert ctx["gateway_connected"] is True
    assert client.battery_sku == "GRO_DUAL"
    url, headers, body = session.calls[0]
    assert headers["Content-Type"] == "application/grpc-web+proto"
    assert headers["authorization"] == "jwt"
    assert body[:1] == b"\x00"


def test_service_context_outage_uses_exact_percent_and_retired_calls_skip():
    api = _client_module()
    if api is None:
        return
    session = _FakeSession(
        {
            "dashboard.mobile.v2.BatteryService/GetSnapshot": grpc_body(
                snapshot(4, num(2, 72) + msg(3, dbl(2, 2.0) + dbl(5, 2.0)) + dbl(4, 9.0))
            ),
        }
    )
    client = api.BasePowerClient(session, auth=_FakeAuth())
    ctx = asyncio.run(client.get_service_context(4321, "addr-1"))
    # GetServiceStatus is "retired" in this fake; the snapshot alone still works.
    assert ctx["state_of_energy_pct"] == 72.0
    assert ctx["state_of_energy_source"] == "snapshot"
    assert ctx["active_outage"] is True
    assert ctx["outage_available_backup_hours"] == 9.0
    before = len(session.calls)
    asyncio.run(client.get_service_context(4321, "addr-1"))
    retired = [c for c in session.calls[before:] if "GetServiceStatus" in c[0]]
    assert retired == []
