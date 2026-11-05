"""Parsers and framing for dashboard.mobile.v2.

These messages are built by hand from the published field numbers. They prove
the parsing logic, not that Base's live responses match. The live check does
that.
"""

from __future__ import annotations

import pytest

from custom_components.base_power import api_v2

from .wire import double, frame, int_field, msg, text, timestamp, trailer


def _flow() -> bytes:
    return (
        double(1, 5.4) + double(2, -0.4) + double(3, 0.25) + double(4, 5.0) + double(5, 5.25)
    )


def test_snapshot_on_grid():
    state = timestamp(1, 1000) + msg(2, _flow()) + double(3, 8.909) + double(4, 59.3)
    result = api_v2.parse_battery_snapshot(msg(1, msg(3, state)))
    assert result["available"] is True
    assert result["state"] == "on_grid"
    assert result["observed_at"] == 1000
    assert result["backup_hours"] == 8.91
    assert result["backup_hours_750w"] == 59.3
    assert result["state_of_energy_percent"] is None
    assert result["power_flow"] == {
        "from_grid_kw": 5.4,
        "from_storage_kw": -0.4,
        "from_solar_kw": 0.25,
        "non_solar_to_home_kw": 5.0,
        "to_home_kw": 5.25,
    }


def test_snapshot_outage_reads_percent_and_treats_omitted_numbers_as_zero():
    state = timestamp(1, 1000) + int_field(2, 73) + msg(3, double(2, 1.5))
    result = api_v2.parse_battery_snapshot(msg(1, msg(4, state)))
    assert result["state"] == "outage"
    assert result["state_of_energy_percent"] == 73
    assert result["backup_hours"] == 0.0
    assert result["power_flow"]["from_storage_kw"] == 1.5
    assert result["power_flow"]["from_grid_kw"] == 0.0


@pytest.mark.parametrize(
    ("field", "state"),
    [
        (2, "telemetry_unavailable"),
        (5, "no_home_power"),
        (6, "overcurrent"),
        (7, "overcurrent_standby"),
    ],
)
def test_snapshot_other_states(field, state):
    result = api_v2.parse_battery_snapshot(msg(1, msg(field, b"")))
    assert result["state"] == state


def test_snapshot_overcurrent_limit():
    state = int_field(2, 40) + double(6, 11.5)
    result = api_v2.parse_battery_snapshot(msg(1, msg(6, state)))
    assert result["overcurrent_limit_kw"] == 11.5


@pytest.mark.parametrize("data", [b"", msg(1, b""), msg(1, msg(99, b"x"))])
def test_snapshot_without_state_is_unavailable(data):
    result = api_v2.parse_battery_snapshot(data)
    assert result["available"] is False
    assert result["state"] is None


def test_overview_counts_cost_only_inside_the_local_day():
    data = (
        msg(5, timestamp(1, 99) + double(2, 1000.0))
        + msg(5, timestamp(1, 100) + double(2, 12.5))
        + msg(5, timestamp(1, 199) + double(2, 7.5))
        + msg(5, timestamp(1, 200) + double(2, 1000.0))
        + text(6, "USD")
    )
    result = api_v2.parse_daily_overview(data, 100, 200)
    assert result["cost_today_cents"] == 20.0
    assert result["cost_currency"] == "USD"


def test_overview_cost_is_unknown_without_samples_in_the_day():
    result = api_v2.parse_daily_overview(
        msg(5, timestamp(1, 50) + double(2, 9.0)), 100, 200
    )
    assert result["available"] is True
    assert result["cost_today_cents"] is None


def test_overview_skips_a_non_finite_cost():
    data = msg(5, timestamp(1, 100) + double(2, float("nan"))) + msg(
        5, timestamp(1, 101) + double(2, 3.0)
    )
    assert api_v2.parse_daily_overview(data, 100, 200)["cost_today_cents"] == 3.0


def test_overview_mix_and_grid_support():
    data = msg(7, double(1, 24.1234) + double(3, 0.5)) + msg(
        3, timestamp(1, 10) + timestamp(2, 20)
    ) + msg(3, timestamp(1, 30))
    result = api_v2.parse_daily_overview(data, 0, 1)
    assert result["energy_mix_24h"] == {
        "grid_to_home_kwh": 24.123,
        "solar_to_home_kwh": 0.0,
        "battery_to_home_kwh": 0.5,
    }
    assert result["grid_support"] == [
        {"start": 10, "end": 20},
        {"start": 30, "end": None},
    ]


def test_grid_support_active():
    intervals = [{"start": 10, "end": 20}, {"start": 30, "end": None}]
    assert api_v2.grid_support_active(intervals, 15) is True
    assert api_v2.grid_support_active(intervals, 20) is False
    assert api_v2.grid_support_active(intervals, 25) is False
    assert api_v2.grid_support_active(intervals, 35) is True
    assert api_v2.grid_support_active([], 15) is False


def test_billing_cycle_amounts_and_negative_credit():
    cycle = (
        text(1, "USD")
        + timestamp(2, 1000)
        + timestamp(4, 2000)
        + int_field(5, 18853)
        + int_field(6, -250)
        + int_field(8, 30000)
    )
    result = api_v2.parse_current_billing_cycle(msg(1, cycle))
    assert result["available"] is True
    assert result["currency"] == "USD"
    assert result["service_period_start"] == 1000
    assert result["data_through"] == 2000
    assert result["gross_charges_cents"] == 18853
    assert result["solar_credit_cents"] == -250
    assert result["net_accrued_cents"] == 0
    assert result["projected_charges_cents"] == 30000


def test_billing_cycle_is_unavailable_without_base_billing():
    result = api_v2.parse_current_billing_cycle(b"")
    assert result["available"] is False
    assert result["net_accrued_cents"] is None


def test_recent_voltage_picks_the_newest_regardless_of_order():
    data = msg(1, timestamp(1, 300) + double(2, 245.3)) + msg(
        1, timestamp(1, 100) + double(2, 240.0)
    )
    assert api_v2.parse_recent_grid_voltage(data) == {
        "grid_voltage": 245.3,
        "latest_grid_voltage_epoch": 300,
    }


def test_recent_voltage_omitted_value_is_zero_volts():
    data = msg(1, timestamp(1, 300))
    assert api_v2.parse_recent_grid_voltage(data)["grid_voltage"] == 0.0


def test_recent_voltage_empty():
    assert api_v2.parse_recent_grid_voltage(b"")["grid_voltage"] is None


@pytest.mark.parametrize(
    "data",
    [b"\xff", b"\x0a\xff\xff", b"\x0a\x05ab", b"\x09\x01", b"\x0d\x01", b"\x0b\x0c"],
)
def test_malformed_messages_do_not_raise(data):
    api_v2.parse_battery_snapshot(data)
    api_v2.parse_daily_overview(data, 0, 1)
    api_v2.parse_current_billing_cycle(data)
    api_v2.parse_recent_grid_voltage(data)


def test_request_encoding():
    assert api_v2.encode_address_request("abc") == b"\x0a\x03abc"
    assert api_v2.encode_billing_cycle_request("abc") == b"\x0a\x07\x0a\x03abc\x10\x01"


def test_read_grpc_web_splits_message_and_trailer():
    body = frame(b"hello") + trailer("0")
    assert api_v2.read_grpc_web(body) == (b"hello", "0", "")


def test_read_grpc_web_trailer_error_beats_ok_header():
    body = trailer("14", "try%20later")
    assert api_v2.read_grpc_web(body, "0", "") == (b"", "14", "try later")


def test_read_grpc_web_header_status():
    assert api_v2.read_grpc_web(b"", "16", "nope") == (b"", "16", "nope")


def test_read_grpc_web_truncated_frame():
    assert api_v2.read_grpc_web(b"\x00\x00\x00\x00\x09abc") == (b"", "", "")
