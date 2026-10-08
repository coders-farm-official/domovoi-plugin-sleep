"""A7-01 — which credential each sleep route takes, on both hops.

Play and stop are household actions. They used to be ``@open_endpoint``
on the core (anyone on the LAN could start a sound in any room for a day,
or stop one) while their dashboard proxies carried no marker, so the
web gate put them on the ADMIN tier and a paired phone was refused. Both
hops are now ``@device_endpoint``: the household token or an admin
Bearer, nothing else; the web proxy forwards the caller's credential to
the core route, which checks it again.

REV-14 (2026-10-08): the two READS that list the active sessions — the
dashboard's ``GET /overview`` and the core's ``GET /state`` — answered any
LAN host, and a session list says which rooms have somebody asleep and
until when. Both are device-tier reads now.

DB-free: the routers are mounted behind the real gate
(``domovoi.webkit.enforce_route_tier``) with ``domovoi.admin_auth``'s
database primitives replaced by an in-memory household (admin set up,
one valid token), the same seam the core's auth tests use.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from domovoi import admin_auth, webkit
from domovoi.sdk.testing import make_stub_sdk

from domovoi_plugin_sleep import core as core_mod
from domovoi_plugin_sleep import web as web_mod

TOKEN = "household-token-for-the-tier-tests"

# Every non-GET route, and every gated GET, with the tier it must carry.
CORE_TIERS = {
    ("GET", "/state"): "device",
    ("POST", "/play"): "device",
    ("POST", "/stop"): "device",
    ("POST", "/regenerate"): "admin",
}
WEB_TIERS = {
    ("GET", "/overview"): "device",
    ("POST", "/play"): "device",
    ("POST", "/stop"): "device",
    ("POST", "/regenerate"): "admin",
}


def _tiers(router) -> dict[tuple[str, str], str]:
    out = {}
    for route in router.routes:
        tier = webkit.endpoint_tier(getattr(route, "endpoint", None))
        for method in getattr(route, "methods", None) or ():
            if method in ("GET", "HEAD") and tier != "device":
                continue          # open reads, not what this table is about
            out[(method, route.path)] = tier
    return out


@pytest.fixture
def household(monkeypatch):
    """Admin set up (so no pre-setup grace), one valid household token."""
    admin_auth.DEVICE_TOKEN_BACKOFF.reset()

    @asynccontextmanager
    async def scope():
        yield object()

    async def has_admin_auth(_s):
        return True

    async def validate_token(_s, token):
        return False

    async def validate_device_token(_s, candidate):
        return candidate == TOKEN

    monkeypatch.setattr(admin_auth, "session_scope", scope)
    monkeypatch.setattr(admin_auth, "has_admin_auth", has_admin_auth)
    monkeypatch.setattr(admin_auth, "validate_token", validate_token)
    monkeypatch.setattr(admin_auth, "validate_device_token", validate_device_token)
    yield
    admin_auth.DEVICE_TOKEN_BACKOFF.reset()


def _gated(router, prefix: str) -> TestClient:
    app = FastAPI()
    app.include_router(
        router, prefix=prefix, dependencies=[Depends(webkit.enforce_route_tier)]
    )
    return TestClient(app, raise_server_exceptions=False)


class _ForwardingCore:
    """Records the credential each proxied call carried to the core."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.gets: list[str] = []

    async def get(self, path, *, params=None):
        self.gets.append(path)
        if path.endswith("/sounds"):
            return {"sounds": [], "generating": True,
                    "defaults": {"default_timer_min": 45, "file_length_min": 60}}
        return {}

    async def post(self, path, *, json=None):
        self.calls.append((path, None))
        return {"ok": True}

    async def post_admin(self, path, *, json=None, request=None, forward_auth=True):
        token = request.headers.get("x-device-token") if request is not None else None
        self.calls.append((path, token))
        return {"ok": True}


class _NoRows:
    """A session with no rooms and no sessions: the overview's two reads."""

    async def execute(self, *a, **kw):
        class _Result:
            def all(self):
                return []

            def mappings(self):
                return self

        return _Result()


class _Ctx:
    def __init__(self) -> None:
        self.slug = "sleep"
        self.core = _ForwardingCore()
        self.routers: list = []

    @asynccontextmanager
    async def db_session_scope(self):
        yield _NoRows()

    def add_router(self, router) -> None:
        self.routers.append(router)


def _core_router():
    return core_mod._build_core_router(make_stub_sdk("sleep"))


def _web():
    ctx = _Ctx()
    web_mod.register_web(ctx)
    return ctx, ctx.routers[0]


# ─── the tables ──────────────────────────────────────────────────────────


def test_core_routes_carry_the_household_tier() -> None:
    assert _tiers(_core_router()) == CORE_TIERS


def test_web_routes_carry_the_household_tier() -> None:
    _, router = _web()
    assert _tiers(router) == WEB_TIERS


def test_nothing_is_open_to_a_caller_with_no_credential() -> None:
    _, router = _web()
    for table in (_tiers(_core_router()), _tiers(router)):
        assert "open" not in table.values()


# ─── the gate, both hops ─────────────────────────────────────────────────


BODIES = {
    "/play": {"room_id": "bedroom", "sound": "brown noise", "minutes": 1440},
    "/stop": {"room_id": "bedroom"},
}


@pytest.mark.parametrize("path", sorted(BODIES))
def test_the_core_refuses_an_anonymous_caller(household, path) -> None:
    client = _gated(_core_router(), "/v1/plugins/sleep")
    resp = client.post(f"/v1/plugins/sleep{path}", json=BODIES[path])
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("path", sorted(BODIES))
def test_the_core_lets_the_household_token_through(household, path) -> None:
    client = _gated(_core_router(), "/v1/plugins/sleep")
    resp = client.post(
        f"/v1/plugins/sleep{path}", json=BODIES[path],
        headers={admin_auth.DEVICE_TOKEN_HEADER: TOKEN},
    )
    # Past the gate: whatever the stub SDK makes of the body, it is not
    # a credential refusal.
    assert resp.status_code not in (401, 403), resp.text


@pytest.mark.parametrize("path", sorted(BODIES))
def test_the_dashboard_hop_takes_the_household_token_and_forwards_it(
    household, path
) -> None:
    ctx, router = _web()
    client = _gated(router, "/api/plugins/sleep")
    anonymous = client.post(f"/api/plugins/sleep{path}", json=BODIES[path])
    assert anonymous.status_code == 401
    assert ctx.core.calls == []

    resp = client.post(
        f"/api/plugins/sleep{path}", json=BODIES[path],
        headers={admin_auth.DEVICE_TOKEN_HEADER: TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert ctx.core.calls == [(path.lstrip("/"), TOKEN)]


# ─── the session reads (REV-14) ──────────────────────────────────────────


def test_the_overview_refuses_an_anonymous_caller(household) -> None:
    """The phase-1 shape: ``curl /api/plugins/sleep/overview`` from any host
    on the LAN listed every room with somebody asleep."""
    ctx, router = _web()
    client = _gated(router, "/api/plugins/sleep")
    resp = client.get("/api/plugins/sleep/overview")
    assert resp.status_code == 401, resp.text
    assert ctx.core.gets == []


@pytest.mark.parametrize(
    "credential", ["header", "query"],
)
def test_the_overview_answers_a_paired_device(household, credential) -> None:
    ctx, router = _web()
    client = _gated(router, "/api/plugins/sleep")
    if credential == "header":
        resp = client.get(
            "/api/plugins/sleep/overview",
            headers={admin_auth.DEVICE_TOKEN_HEADER: TOKEN},
        )
    else:
        resp = client.get("/api/plugins/sleep/overview", params={"device_token": TOKEN})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sessions"] == [] and body["core_online"] is True
    # The defaults come with the catalog now: the overview never needs the
    # core's (device-tier) session read, so the web hop holds no credential
    # it would have to forward.
    assert body["defaults"] == {"default_timer_min": 45, "file_length_min": 60}
    assert ctx.core.gets == ["/v1/plugins/sleep/sounds"]


def test_the_core_session_read_refuses_an_anonymous_caller(household) -> None:
    client = _gated(_core_router(), "/v1/plugins/sleep")
    resp = client.get("/v1/plugins/sleep/state")
    assert resp.status_code == 401, resp.text


def test_the_core_session_read_lets_the_household_token_through(household) -> None:
    client = _gated(_core_router(), "/v1/plugins/sleep")
    resp = client.get(
        "/v1/plugins/sleep/state", headers={admin_auth.DEVICE_TOKEN_HEADER: TOKEN}
    )
    assert resp.status_code not in (401, 403), resp.text


def test_the_catalog_carries_the_defaults(stub_sdk) -> None:
    app = FastAPI()
    app.include_router(core_mod._build_core_router(stub_sdk), prefix="/v1/plugins/sleep")
    resp = TestClient(app).get("/v1/plugins/sleep/sounds")
    assert resp.status_code == 200, resp.text
    assert resp.json()["defaults"] == {
        "default_timer_min": int(stub_sdk.config.default_timer_min),
        "file_length_min": int(stub_sdk.config.file_length_min),
    }
