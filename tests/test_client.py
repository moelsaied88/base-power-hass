"""BasePowerClient behavior that doesn't need Home Assistant running."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.base_power.api import (
    BasePowerAuthError,
    BasePowerClient,
    BasePowerConnectionError,
    BasePowerProtocolError,
    _newest_voltage,
)

from .wire import frame, msg, timestamp, trailer


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(session) -> BasePowerClient:
    client = BasePowerClient(session, session_id="sess", client_id="client")
    client.auth.get_jwt = AsyncMock(side_effect=["jwt-1", "jwt-2"])
    client.auth.reset = AsyncMock()
    return client


async def test_grpc_web_call_sends_raw_jwt_and_address():
    body = frame(msg(1, msg(3, timestamp(1, 5)))) + trailer("0")
    session = FakeSession(FakeResponse(body=body))
    result = await _client(session).get_battery_snapshot("addr")
    assert result["state"] == "on_grid"
    url, kwargs = session.calls[0]
    assert url == (
        "https://dashboard.baseapis.net/"
        "dashboard.mobile.v2.BatteryService/GetSnapshot"
    )
    assert kwargs["headers"]["authorization"] == "jwt-1"
    assert kwargs["headers"]["Content-Type"] == "application/grpc-web+proto"
    assert kwargs["data"] == frame(b"\x0a\x04addr")


async def test_grpc_web_retries_once_on_http_401():
    session = FakeSession(
        FakeResponse(status=401), FakeResponse(body=frame(b"") + trailer("0"))
    )
    client = _client(session)
    await client.get_battery_snapshot("addr")
    assert session.calls[1][1]["headers"]["authorization"] == "jwt-2"
    client.auth.reset.assert_awaited_once()


async def test_grpc_web_second_unauthenticated_raises_auth_error():
    session = FakeSession(
        FakeResponse(body=trailer("16")), FakeResponse(body=trailer("16"))
    )
    with pytest.raises(BasePowerAuthError):
        await _client(session).get_battery_snapshot("addr")


async def test_grpc_web_other_status_is_a_protocol_error():
    session = FakeSession(FakeResponse(body=trailer("12", "unimplemented")))
    with pytest.raises(BasePowerProtocolError, match="status 12"):
        await _client(session).get_battery_snapshot("addr")


async def test_grpc_web_http_error_is_a_protocol_error():
    session = FakeSession(FakeResponse(status=500))
    with pytest.raises(BasePowerProtocolError, match="HTTP 500"):
        await _client(session).get_battery_snapshot("addr")


@pytest.mark.parametrize("error", [aiohttp.ClientError(), TimeoutError()])
async def test_grpc_web_network_failure_is_a_connection_error(error):
    session = FakeSession(error)
    with pytest.raises(BasePowerConnectionError):
        await _client(session).get_battery_snapshot("addr")


@pytest.mark.parametrize("reply", [{}, {"unrelated": 1}, [], None])
async def test_service_context_without_telemetry_is_unknown(reply):
    client = _client(FakeSession())
    client._mobile_json = AsyncMock(return_value=reply)
    context = await client.get_service_context(1)
    assert context["available"] is False
    assert context["active_outage"] is None


async def test_service_context_omitted_flag_with_telemetry_is_false():
    client = _client(FakeSession())
    client._mobile_json = AsyncMock(return_value={"powerFlow": {"toHome": 5.0}})
    context = await client.get_service_context(1)
    assert context["available"] is True
    assert context["active_outage"] is False
    assert context["home_power_w"] == 5000.0


def test_newest_voltage_ignores_series_order():
    volts, stamp, epoch = _newest_voltage(
        [245.0, 0.0],
        ["2026-11-05T12:00:00Z", "2026-11-05T01:00:00Z"],
    )
    assert volts == 245.0
    assert stamp == "2026-11-05T12:00:00Z"
    assert epoch == int(datetime(2026, 11, 5, 12, tzinfo=UTC).timestamp())


def test_newest_voltage_without_timestamps_is_undated():
    assert _newest_voltage([240.0, 245.0], []) == (245.0, None, None)


def test_newest_voltage_with_bad_timestamps_is_undated():
    assert _newest_voltage([245.0], ["nonsense"]) == (245.0, "nonsense", None)


def test_newest_voltage_empty():
    assert _newest_voltage([], []) == (None, None, None)
