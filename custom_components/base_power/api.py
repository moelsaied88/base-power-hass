"""Base Power API client.

Two-layer design:

* `_ClerkAuth` handles authentication against Clerk. Base Power uses Clerk
  (https://clerk.com) for identity with a **passwordless email_code** flow:
  the user enters their email, Clerk emails a 6-digit code, we exchange the
  code for a long-lived Clerk ``session_id``. Subsequent API calls mint
  short-lived JWTs via ``/v1/client/sessions/{sid}/tokens``.
* `BasePowerClient` wraps Clerk auth and provides typed access to the
  ConnectRPC endpoints exposed by `account.basepowercompany.com/api/connect/*`.

Clerk's token endpoint requires the ``__client`` cookie set during sign-in.
We capture it from the sign-in response and persist it in the config entry
alongside ``session_id`` so JWT minting survives HA restarts.

Requests are encoded as binary protobuf (Content-Type: application/proto,
connect-protocol-version: 1). Message classes are built at runtime from the
FileDescriptorSet shipped inside this component (``file_descriptors.bin``).

Nothing in this module logs credentials, codes, JWTs, or full response bodies.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import aiohttp
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

try:  # protobuf 5.x
    from google.protobuf.message_factory import GetMessageClass as _get_message_class
except ImportError:  # pragma: no cover - fallback for older protobuf
    _get_message_class = None

_LOGGER = logging.getLogger(__name__)

ACCOUNT_ORIGIN = "https://account.basepowercompany.com"
CLERK_ORIGIN = "https://clerk.basepowercompany.com"
CONNECT_BASE = f"{ACCOUNT_ORIGIN}/api/connect"

# Observed from the web SPA - passed as query params on every Clerk request.
CLERK_API_VERSION = "2025-11-10"
CLERK_JS_VERSION = "5.125.9"

# Connect-RPC required headers.
CONNECT_CONTENT_TYPE = "application/proto"
CONNECT_PROTO_VERSION = "1"

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=20)

# JWT lifetime is typically 60 s; refresh a bit early so in-flight calls stay valid.
_TOKEN_LEEWAY_SECONDS = 10


class BasePowerError(Exception):
    """Base class for Base Power integration errors."""


class BasePowerAuthError(BasePowerError):
    """Raised when authentication fails (bad credentials, MFA required, etc.)."""


class BasePowerConnectionError(BasePowerError):
    """Raised on network / transport errors."""


class BasePowerProtocolError(BasePowerError):
    """Raised when the server returns an unexpected response shape."""


@dataclass
class AvailableLocation:
    """A location visible to the user (step 1 of discovery)."""

    address_id: str
    address_display: str


@dataclass
class ServiceLocation:
    """A Base Power service location (full, with numeric service_location_id)."""

    service_location_id: int
    address_id: str
    address_display: str
    has_gateway: bool
    has_solar: bool
    timezone: str


class _ProtoRegistry:
    """Lazily-loaded descriptor pool.

    Built once from ``file_descriptors.bin`` and shared across all client
    instances. Construction happens in an executor to avoid blocking the
    event loop.
    """

    _pool: descriptor_pool.DescriptorPool | None = None
    _lock = asyncio.Lock()

    @classmethod
    async def get(cls) -> descriptor_pool.DescriptorPool:
        if cls._pool is not None:
            return cls._pool
        async with cls._lock:
            if cls._pool is None:
                cls._pool = await asyncio.get_running_loop().run_in_executor(
                    None, cls._build
                )
        assert cls._pool is not None
        return cls._pool

    # File descriptors we don't care about and can silently skip if adding
    # them fails (well-known types often double-register, google.api.* are
    # only used for HTTP annotations which we don't consume).
    _SKIPPABLE_FILES = frozenset(
        {
            "google/protobuf/timestamp.proto",
            "google/protobuf/empty.proto",
            "google/protobuf/struct.proto",
            "google/protobuf/any.proto",
            "google/api/http.proto",
            "google/api/annotations.proto",
        }
    )

    @staticmethod
    def _build() -> descriptor_pool.DescriptorPool:
        desc_bytes = (Path(__file__).parent / "file_descriptors.bin").read_bytes()
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(desc_bytes)
        pool = descriptor_pool.DescriptorPool()
        _add_well_known_types(pool)
        added: set[str] = set()
        pending = list(fds.file)
        # Retry until stable. FileDescriptorProto.dependency from the Base
        # SPA bundle is heuristically reconstructed, so we let the pool
        # reject-and-retry rather than strictly topo-sort.
        for _ in range(len(pending) + 2):
            progress = False
            next_pending: list[descriptor_pb2.FileDescriptorProto] = []
            for fdp in pending:
                if fdp.name in added:
                    continue
                try:
                    pool.Add(fdp)
                    added.add(fdp.name)
                    progress = True
                except Exception:  # noqa: BLE001
                    next_pending.append(fdp)
            pending = next_pending
            if not progress or not pending:
                break
        # Anything left that isn't a harmless duplicate is worth surfacing.
        for fdp in pending:
            if fdp.name in _ProtoRegistry._SKIPPABLE_FILES:
                _LOGGER.debug(
                    "skipped duplicate well-known type %s", fdp.name
                )
            else:
                _LOGGER.warning(
                    "could not add %s to descriptor pool; some Base Power "
                    "entities may be unavailable",
                    fdp.name,
                )
        return pool


def _add_well_known_types(pool: descriptor_pool.DescriptorPool) -> None:
    """Register google.protobuf.Empty and Timestamp into the pool."""

    from google.protobuf import empty_pb2, timestamp_pb2  # noqa: F401

    # The act of importing these registers them in the default pool; but our
    # custom pool won't see them unless we explicitly add their descriptors.
    for msg_module in (empty_pb2, timestamp_pb2):
        file_descriptor = msg_module.DESCRIPTOR
        fdp = descriptor_pb2.FileDescriptorProto()
        file_descriptor.CopyToProto(fdp)
        try:
            pool.Add(fdp)
        except Exception:  # noqa: BLE001
            # Already present.
            pass


@lru_cache(maxsize=1)
def _user_agent() -> str:
    # Match the web SPA's UA family so our traffic looks like a browser.
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HomeAssistant/base_power-integration"
    )


class _ClerkAuth:
    """Clerk email_code sign-in + JWT minting.

    Two usage modes:

    * **Config flow**: instantiate with just an email, call :meth:`start_sign_in`
      to trigger the email, then :meth:`attempt_sign_in` with the 6-digit code.
      After that, :attr:`session_id` + :attr:`client_id` can be persisted.
    * **Coordinator / runtime**: instantiate with ``session_id`` + ``client_id``
      recovered from the config entry; :meth:`get_jwt` mints short-lived JWTs.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._session_id = session_id
        self._client_id = client_id
        self._sign_in_id: str | None = None
        self._supported_factors: list[Mapping[str, Any]] = []
        self._jwt: str | None = None
        self._jwt_expiry: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def client_id(self) -> str | None:
        return self._client_id

    def _clerk_cookies(self) -> dict[str, str] | None:
        """Return cookies to send on Clerk requests.

        The ``__client`` cookie is captured during sign-in. Passing it back
        on every Clerk request keeps the client bound to our session across
        HA restarts.
        """

        if self._client_id:
            return {"__client": self._client_id}
        return None

    async def _clerk_post(
        self,
        path: str,
        data: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        params = {
            "__clerk_api_version": CLERK_API_VERSION,
            "_clerk_js_version": CLERK_JS_VERSION,
        }
        headers = {
            "User-Agent": _user_agent(),
            "Origin": ACCOUNT_ORIGIN,
            "Referer": f"{ACCOUNT_ORIGIN}/",
        }
        try:
            async with self._session.post(
                f"{CLERK_ORIGIN}{path}",
                data=dict(data) if data else None,
                params=params,
                headers=headers,
                cookies=self._clerk_cookies(),
                timeout=DEFAULT_TIMEOUT,
            ) as resp:
                body_text = await resp.text()
                if resp.status >= 400:
                    _LOGGER.debug(
                        "clerk %s returned %s", path, resp.status
                    )
                    if resp.status in (400, 401, 403, 422):
                        raise BasePowerAuthError(
                            _clerk_error_message(path, resp.status, body_text)
                        )
                    raise BasePowerConnectionError(
                        f"Clerk {path} returned {resp.status}"
                    )
                self._capture_client_cookie(resp)
                try:
                    return await resp.json()
                except aiohttp.ContentTypeError as exc:
                    raise BasePowerProtocolError(
                        f"Clerk {path} returned non-JSON body"
                    ) from exc
        except aiohttp.ClientError as exc:
            raise BasePowerConnectionError(f"network error calling {path}") from exc

    def _capture_client_cookie(self, resp: aiohttp.ClientResponse) -> None:
        """Extract the ``__client`` cookie from a Clerk response if present."""

        cookie = resp.cookies.get("__client")
        if cookie is not None and cookie.value:
            self._client_id = cookie.value

    async def start_sign_in(self, email: str | None = None) -> None:
        """Begin Clerk email_code sign-in. Triggers the OTP email."""

        if email is not None:
            self._email = email
        if not self._email:
            raise BasePowerAuthError("email is required to start sign-in")

        async with self._lock:
            # Step 1: create the sign-in attempt.
            resp = await self._clerk_post(
                "/v1/client/sign_ins",
                data={"identifier": self._email},
            )
            sign_in = resp.get("response") or resp
            sign_in_id = sign_in.get("id")
            if not sign_in_id:
                raise BasePowerAuthError("Clerk sign_in returned no id")
            factors = sign_in.get("supported_first_factors") or []
            email_factor = next(
                (f for f in factors if f.get("strategy") == "email_code"),
                None,
            )
            if email_factor is None:
                raise BasePowerAuthError(
                    "Clerk did not offer email_code factor; unsupported account "
                    "configuration. Supported factors: "
                    f"{[f.get('strategy') for f in factors]}"
                )
            self._sign_in_id = sign_in_id
            self._supported_factors = factors

            # Step 2: ask Clerk to actually send the code.
            await self._clerk_post(
                f"/v1/client/sign_ins/{sign_in_id}/prepare_first_factor",
                data={
                    "strategy": "email_code",
                    "email_address_id": email_factor.get("email_address_id", ""),
                },
            )

    async def attempt_sign_in(self, code: str) -> None:
        """Submit the emailed OTP to complete sign-in."""

        async with self._lock:
            if not self._sign_in_id:
                raise BasePowerAuthError(
                    "attempt_sign_in called before start_sign_in"
                )
            resp = await self._clerk_post(
                f"/v1/client/sign_ins/{self._sign_in_id}/attempt_first_factor",
                data={"strategy": "email_code", "code": code.strip()},
            )
            sign_in = resp.get("response") or resp
            status = sign_in.get("status")
            if status != "complete":
                raise BasePowerAuthError(
                    f"Clerk sign-in status={status!r} (expected 'complete'); "
                    "code may be incorrect or expired"
                )
            created_session_id = sign_in.get("created_session_id")
            if not created_session_id:
                raise BasePowerAuthError(
                    "Clerk sign_in returned no created_session_id"
                )
            self._session_id = created_session_id
            if not self._client_id:
                # Fall back to client.id from the response body if we didn't
                # capture a Set-Cookie (some Clerk configs don't send one here).
                self._client_id = (resp.get("client") or {}).get("id")
            self._sign_in_id = None
            self._supported_factors = []
            self._jwt = None
            self._jwt_expiry = 0.0

    async def get_jwt(self) -> str:
        """Return a live Clerk JWT, refreshing if necessary."""

        now = time.monotonic()
        if self._jwt and now < self._jwt_expiry - _TOKEN_LEEWAY_SECONDS:
            return self._jwt
        async with self._lock:
            now = time.monotonic()
            if self._jwt and now < self._jwt_expiry - _TOKEN_LEEWAY_SECONDS:
                return self._jwt
            if not self._session_id:
                raise BasePowerAuthError("not signed in")
            resp = await self._clerk_post(
                f"/v1/client/sessions/{self._session_id}/tokens"
            )
            jwt = resp.get("jwt")
            if not jwt or not isinstance(jwt, str):
                raise BasePowerAuthError("Clerk /tokens returned no jwt")
            self._jwt = jwt
            # Tokens last ~60 s per Clerk defaults.
            self._jwt_expiry = time.monotonic() + 60.0
            return jwt

    async def reset(self) -> None:
        """Drop cached JWT so the next call re-mints.

        Does NOT drop ``session_id`` / ``client_id`` — those are the
        persistent credentials and should only be cleared by a fresh
        sign-in or explicit logout.
        """

        async with self._lock:
            self._jwt = None
            self._jwt_expiry = 0.0


def _clerk_error_message(path: str, status: int, body_text: str) -> str:
    """Produce a user-friendly error from a Clerk 4xx response."""

    try:
        import json

        data = json.loads(body_text)
        errs = data.get("errors") or []
        if errs:
            first = errs[0]
            code = first.get("code", "")
            msg = first.get("long_message") or first.get("message") or ""
            if msg:
                return f"Clerk {path} returned {status}: {code}: {msg}"
    except Exception:  # noqa: BLE001
        pass
    return f"Clerk {path} returned {status}"


class BasePowerClient:
    """High-level Base Power API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        auth: _ClerkAuth | None = None,
        email: str | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        self._session = session
        if auth is not None:
            self._auth = auth
        else:
            self._auth = _ClerkAuth(
                session,
                email=email,
                session_id=session_id,
                client_id=client_id,
            )
        self._pool: descriptor_pool.DescriptorPool | None = None

    @property
    def auth(self) -> _ClerkAuth:
        return self._auth

    async def _registry(self) -> descriptor_pool.DescriptorPool:
        if self._pool is None:
            self._pool = await _ProtoRegistry.get()
        return self._pool

    def _message_class(self, full_name: str):
        assert self._pool is not None
        descriptor = self._pool.FindMessageTypeByName(full_name)
        if _get_message_class is not None:
            return _get_message_class(descriptor)
        # protobuf 4.x fallback
        factory = message_factory.MessageFactory(self._pool)
        return factory.GetPrototype(descriptor)  # type: ignore[attr-defined]

    async def _connect_rpc(
        self,
        service: str,
        method: str,
        request_type: str,
        response_type: str,
        request_fields: Mapping[str, Any] | None = None,
    ) -> Any:
        """Issue a Connect-RPC call with binary protobuf encoding."""

        await self._registry()
        RequestCls = self._message_class(request_type)
        ResponseCls = self._message_class(response_type)

        req_msg = RequestCls()
        if request_fields:
            for k, v in request_fields.items():
                setattr(req_msg, k, v)
        body = req_msg.SerializeToString()

        jwt = await self._auth.get_jwt()
        headers = {
            "Content-Type": CONNECT_CONTENT_TYPE,
            "Accept": CONNECT_CONTENT_TYPE,
            "Connect-Protocol-Version": CONNECT_PROTO_VERSION,
            "Authorization": f"Bearer {jwt}",
            "User-Agent": _user_agent(),
            "Origin": ACCOUNT_ORIGIN,
            "Referer": f"{ACCOUNT_ORIGIN}/",
        }
        url = f"{CONNECT_BASE}/dashboard/{service}/{method}"
        try:
            async with self._session.post(
                url, data=body, headers=headers, timeout=DEFAULT_TIMEOUT
            ) as resp:
                if resp.status == 401:
                    # Clerk token rotation; force re-sign-in once then retry.
                    await self._auth.reset()
                    raise BasePowerAuthError("401 from Base API")
                if resp.status >= 400:
                    text = await resp.text()
                    raise BasePowerProtocolError(
                        f"{method} returned {resp.status}: {text[:200]}"
                    )
                raw = await resp.read()
        except aiohttp.ClientError as exc:
            raise BasePowerConnectionError(f"network error calling {method}") from exc

        resp_msg = ResponseCls()
        try:
            resp_msg.ParseFromString(raw)
        except Exception as exc:  # noqa: BLE001
            raise BasePowerProtocolError(
                f"failed to parse {response_type}: {exc}"
            ) from exc
        return resp_msg

    # ---- Typed endpoints --------------------------------------------------

    async def get_available_locations(self) -> list[AvailableLocation]:
        """Return the locations visible to the signed-in user.

        This is step 1 of the two-step discovery - each entry has an
        ``addressId`` but not yet a numeric ``serviceLocationId``.
        """

        resp = await self._connect_rpc(
            service="dashboard.DashboardAPI",
            method="GetAvailableLocations",
            request_type="google.protobuf.Empty",
            response_type="dashboard.GetAvailableLocationsResponse",
        )
        return [
            AvailableLocation(
                address_id=str(loc.addressId),
                address_display=_format_address(loc.address),
            )
            for loc in resp.locations
        ]

    async def resolve_service_location(self, address_id: str) -> ServiceLocation:
        """Step 2: given an addressId, fetch the numeric serviceLocationId."""

        resp = await self._connect_rpc(
            service="dashboard.DashboardAPI",
            method="MobileGetDashboardRoot",
            request_type="dashboard.MobileGetDashboardRootRequest",
            response_type="dashboard.MobileGetDashboardRootResponse",
            request_fields={"addressId": address_id, "newReferralsEnabled": False},
        )
        return ServiceLocation(
            service_location_id=int(resp.serviceLocationId),
            address_id=str(resp.addressId),
            address_display=_format_address(resp.address),
            has_gateway=False,  # not surfaced in mobile root response; populated later
            has_solar=bool(getattr(resp.battery, "hasSolar", False)),
            timezone=str(getattr(resp.address, "timezoneIdentifier", "") or ""),
        )

    async def get_service_status(self, service_location_id: int) -> dict[str, Any]:
        """Poll live service status (battery SoC, outage, grid voltage)."""

        resp = await self._connect_rpc(
            service="dashboard.DashboardAPI",
            method="GetServiceStatus",
            request_type="dashboard.GetServiceStatusRequest",
            response_type="dashboard.GetServiceStatusResponse",
            request_fields={"serviceLocationID": service_location_id},
        )
        return {
            "grid_voltage": getattr(resp, "gridVoltage", 0),
            "has_gateway": bool(getattr(resp, "hasGateway", False)),
            "gateway_connected": bool(getattr(resp, "gatewayConnection", False)),
            "state_of_energy": int(getattr(resp, "stateOfEnergy", 0)),
            "active_overcurrent": bool(getattr(resp, "activeOvercurrent", False)),
            "active_overcurrent_standby": bool(
                getattr(resp, "activeOvercurrentStandby", False)
            ),
            "active_outage": bool(getattr(resp, "activeOutage", False)),
            "syn_voltage": getattr(resp, "synVoltage", 0),
            "wifi_ssid": getattr(getattr(resp, "gatewayWifi", None), "ssid", "") or "",
            "wifi_state": int(
                getattr(getattr(resp, "gatewayWifi", None), "state", 0) or 0
            ),
        }

    async def get_recent_usage(self, address_id: str) -> dict[str, Any]:
        """Return recent time-series usage (power, energy, outage events)."""

        resp = await self._connect_rpc(
            service="dashboard.DashboardAPI",
            method="MobileGetRecentUsage",
            request_type="dashboard.MobileGetRecentUsageRequest",
            response_type="dashboard.MobileGetRecentUsageResponse",
            request_fields={"address_id": address_id},
        )

        def ts(pt: Any) -> int | None:
            t = getattr(pt, "time", None) or getattr(pt, "begin_time", None)
            if t is None:
                return None
            # google.protobuf.Timestamp -> epoch seconds
            return int(t.seconds or 0)

        latest_power_w: float | None = None
        latest_power_ts: int | None = None
        if resp.power_level_data:
            # Don't assume list ordering - Base sometimes returns newest-first.
            # Pick the point with the largest timestamp.
            newest = max(
                resp.power_level_data,
                key=lambda pt: int(getattr(pt, "time", None).seconds)
                if getattr(pt, "time", None) is not None
                else 0,
            )
            latest_power_w = float(newest.power_to_home_kw) * 1000.0
            if newest.HasField("time"):
                latest_power_ts = int(newest.time.seconds)

        return {
            "latest_power_w": latest_power_w,
            "latest_power_ts": latest_power_ts,
            "power_level_points": [
                {
                    "ts": ts(pt),
                    "power_to_home_kw": float(pt.power_to_home_kw),
                }
                for pt in resp.power_level_data
            ],
            "energy_usage_points": [
                {
                    "ts": ts(pt),
                    "energy_to_home_kwh": float(pt.energy_to_home_kwh),
                    "solar_to_home_kwh": float(pt.solar_to_home_kwh),
                    "solar_buyback_kwh": float(pt.solar_buyback_kwh),
                }
                for pt in resp.energy_usage_data
            ],
            "energy_source_kwh": {
                "grid_to_home": float(resp.energy_usage_source.grid_to_home_kwh),
                "solar_to_home": float(resp.energy_usage_source.solar_to_home_kwh),
                "storage_to_home": float(resp.energy_usage_source.storage_to_home_kwh),
            },
            "latest_duration_hours": _latest_by_time(
                resp.duration_data, "duration"
            ),
            "latest_duration_at_750w_hours": _latest_by_time(
                resp.duration_data, "duration_at_750w"
            ),
            "duration_points": [
                {
                    "ts": ts(pt),
                    "duration_hours": float(pt.duration),
                    "duration_at_750w_hours": float(pt.duration_at_750w),
                }
                for pt in resp.duration_data
            ],
            "grid_events": [
                {
                    "begin_ts": int(pt.begin_time.seconds) if pt.HasField("begin_time") else None,
                    "end_ts": int(pt.end_time.seconds) if pt.HasField("end_time") else None,
                }
                for pt in resp.grid_event_data
            ],
        }


def _latest_by_time(points: Any, field: str) -> float | None:
    """Return ``field`` from the point in ``points`` with the largest timestamp.

    Handles proto repeated messages that may be returned newest-first or
    oldest-first - we don't assume ordering.
    """

    if not points:
        return None

    def ts_of(pt: Any) -> int:
        t = getattr(pt, "time", None)
        if t is None:
            return 0
        return int(getattr(t, "seconds", 0) or 0)

    newest = max(points, key=ts_of)
    val = getattr(newest, field, None)
    return float(val) if val is not None else None


def _format_address(addr: Any) -> str:
    """Format a DashboardAddress protobuf message as a human-readable string."""

    if addr is None:
        return "Base Power installation"
    parts: list[str] = []
    line1 = getattr(addr, "line1", "") or ""
    line2 = getattr(addr, "line2", "") or ""
    city = getattr(addr, "city", "") or ""
    state = getattr(addr, "state", "") or ""
    postal = getattr(addr, "postalCode", "") or ""
    if line1:
        parts.append(line1)
    if line2:
        parts.append(line2)
    loc = " ".join(x for x in [city, state, postal] if x)
    if loc:
        parts.append(loc)
    return ", ".join(parts) if parts else "Base Power installation"
