"""SIB-8 — the core router's /stop only stops what this plugin started.

Mounted against the live sdk (recording playback), so "playback
continues" is an assertion about what the plugin asked the player to
do, not a guess.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domovoi_plugin_sleep import core as core_mod
from domovoi_plugin_sleep import sessions, sounds

from tests.conftest import requires_db

pytestmark = requires_db

BROWN = sounds.Sound(
    key="brown noise", title="Brown noise", filename="brown-noise.ogg",
    builtin=True,
)


@pytest.fixture
def client(live_sdk):
    app = FastAPI()
    app.include_router(
        core_mod._build_core_router(live_sdk), prefix="/v1/plugins/sleep"
    )
    with TestClient(app) as c:
        yield c


async def test_stop_without_a_session_is_a_404_and_leaves_playback_alone(
    live_sdk, client
):
    resp = client.post("/v1/plugins/sleep/stop", json={"room_id": "kitchen"})
    assert resp.status_code == 404
    assert "no sleep sound is playing" in resp.json()["detail"]
    assert live_sdk.playback.calls == [], "nothing was asked to stop"


async def test_stop_ends_the_rooms_own_sleep_session(live_sdk, client):
    await sessions.start(
        live_sdk, room_id="bedroom", sound=BROWN, timer_min=30
    )
    live_sdk.playback.calls.clear()

    resp = client.post(
        "/v1/plugins/sleep/stop", json={"room_id": "bedroom", "fade": False}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "room_id": "bedroom"}
    assert await sessions.active_session(live_sdk, "bedroom") is None
    assert live_sdk.playback.calls, "the room was actually stopped"


async def test_a_second_stop_is_a_404(live_sdk, client):
    await sessions.start(
        live_sdk, room_id="bedroom", sound=BROWN, timer_min=30
    )
    client.post(
        "/v1/plugins/sleep/stop", json={"room_id": "bedroom", "fade": False}
    )
    live_sdk.playback.calls.clear()

    again = client.post(
        "/v1/plugins/sleep/stop", json={"room_id": "bedroom", "fade": False}
    )
    assert again.status_code == 404
    assert live_sdk.playback.calls == []


async def test_other_rooms_are_untouched(live_sdk, client):
    await sessions.start(
        live_sdk, room_id="bedroom", sound=BROWN, timer_min=30
    )
    live_sdk.playback.calls.clear()

    resp = client.post(
        "/v1/plugins/sleep/stop", json={"room_id": "office", "fade": False}
    )
    assert resp.status_code == 404
    assert live_sdk.playback.calls == []
    assert await sessions.active_session(live_sdk, "bedroom") is not None
