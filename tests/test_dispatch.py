"""F-P017 — a sleep sound started from the page, or stopped by the page or
the timer, reaches the room's satellite.

``play_url`` queues the room's player PAUSED for the satellite's
music_start -> music_ready handshake. A voice turn's socket sends that; the
page's play button (an HTTP route) and the timer (a worker) had nothing,
so the page counted down while the room stayed silent, and a stop never
told the satellite. With the core's SDK 1.6 ``playback.dispatch`` they
hand the Response over; on an older core there is no dispatch and nothing
changes (the plugin still loads and plays as before).

The voice handler keeps returning its Response as-is (the turn delivers
it), so it must never dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domovoi.sdk import Response

from domovoi_plugin_sleep import core as core_mod
from domovoi_plugin_sleep import sessions, sounds

from tests.conftest import make_settings, requires_db, wire_fake_mpd

BROWN = sounds.Sound(
    key="brown noise", title="Brown noise", filename="brown-noise.ogg",
    builtin=True,
)


def _record_dispatch(sdk) -> list[tuple[str, str]]:
    """Give the sdk's playback a recording SDK-1.6 dispatch."""
    sent: list[tuple[str, str]] = []

    async def dispatch(resp: Response, room_id: str) -> bool:
        sent.append((resp.music_action, room_id))
        return resp.music_action in ("start", "stop")

    sdk.playback.dispatch = dispatch
    return sent


# ─── deliver(): DB-free ───────────────────────────────────────────────────

async def test_deliver_hands_the_response_to_the_cores_dispatch(stub_sdk):
    sent = _record_dispatch(stub_sdk)
    start = Response(text="Playing Brown noise.", music_action="start",
                     music_stream_url="http://mpd/bedroom")
    assert await sessions.deliver(stub_sdk, start, "bedroom") is True
    assert sent == [("start", "bedroom")]


async def test_on_a_core_without_dispatch_nothing_is_sent_and_nothing_breaks(stub_sdk):
    stub_sdk.playback = SimpleNamespace()          # an SDK before 1.6
    resp = Response(text="Stopped.", music_action="stop")
    assert await sessions.deliver(stub_sdk, resp, "bedroom") is False


async def test_a_failing_dispatch_does_not_fail_the_action(stub_sdk):
    async def dispatch(resp, room_id):
        raise RuntimeError("socket gone")

    stub_sdk.playback.dispatch = dispatch
    assert await sessions.deliver(
        stub_sdk, Response(text="Stopped.", music_action="stop"), "bedroom",
    ) is False


# ─── the page's routes and the timer (live SDK on the test DB) ───────────

@pytest.fixture
def client(live_sdk, monkeypatch):
    monkeypatch.setattr(sounds, "resolve", lambda cfg, music_dir, spoken: BROWN)
    app = FastAPI()
    app.include_router(
        core_mod._build_core_router(live_sdk), prefix="/v1/plugins/sleep"
    )
    with TestClient(app) as c:
        yield c


@requires_db
async def test_play_from_the_page_sends_the_room_its_music_start(live_sdk, client):
    sent = _record_dispatch(live_sdk)
    resp = client.post(
        "/v1/plugins/sleep/play",
        json={"room_id": "bedroom", "sound": "brown noise", "minutes": 15},
    )
    assert resp.status_code == 200, resp.text
    assert sent == [("start", "bedroom")]


@requires_db
async def test_stop_from_the_page_sends_the_room_its_music_stop(live_sdk, client):
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=30)
    sent = _record_dispatch(live_sdk)
    resp = client.post(
        "/v1/plugins/sleep/stop", json={"room_id": "bedroom", "fade": False}
    )
    assert resp.status_code == 200, resp.text
    assert sent == [("stop", "bedroom")]


@requires_db
async def test_the_timer_running_out_sends_the_room_its_music_stop(live_sdk):
    from tests.test_sessions import _set_stop_at

    live_sdk.config = make_settings(fade_out_sec=0.06)
    wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=30)
    await _set_stop_at("bedroom", seconds_from_now=-5)
    sent = _record_dispatch(live_sdk)

    await sessions.enforce(live_sdk)

    assert sent == [("stop", "bedroom")]
    assert await sessions.active_sessions(live_sdk) == []


@requires_db
async def test_the_voice_handler_never_dispatches(live_sdk):
    """A voice turn delivers its own Response; dispatching it too would
    send the satellite a second music_start."""
    from domovoi_plugin_sleep.handlers import sleep as handler_mod

    src = open(handler_mod.__file__, encoding="utf-8").read()
    assert "dispatch" not in src and "deliver(" not in src
