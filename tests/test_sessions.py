"""sessions.py tests on the live tier (requires_db): row lifecycle,
timer capping, fade + volume-restore discipline, and the enforce tick
(due timers + reconciliation) — playback and MPD are recorders/fakes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from domovoi.db.session import SessionLocal

from domovoi_plugin_sleep import SCHEMA, sessions, sounds

from tests.conftest import make_settings, requires_db, wire_fake_mpd

pytestmark = requires_db

BROWN = sounds.Sound(
    key="brown noise", title="Brown noise",
    filename="brown-noise.ogg", builtin=True,
)


async def _set_stop_at(room_id: str, *, seconds_from_now: float) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text(
                f"UPDATE {SCHEMA}.sleep_sessions "
                "SET stop_at = NOW() + make_interval(secs => :secs) "
                "WHERE room_id = :room"
            ),
            {"secs": seconds_from_now, "room": room_id},
        )
        await s.commit()


async def test_start_records_session_and_plays(live_sdk):
    resp = await sessions.start(
        live_sdk, room_id="bedroom", sound=BROWN, timer_min=None
    )
    assert resp.music_action == "start"

    play = live_sdk.playback.calls[0]
    assert play["stream_url"] == "Ambient sounds/brown-noise.ogg"
    assert play["title"] == "Brown noise"
    assert play["source"] == "sleep"

    (row,) = await sessions.active_sessions(live_sdk)
    assert row["room_id"] == "bedroom"
    assert row["sound_key"] == "brown noise"
    # Default timer 45 min, within a minute of tolerance.
    remaining = (row["stop_at"] - row["started_at"]).total_seconds()
    assert 44 * 60 <= remaining <= 46 * 60
    assert ("sessions_changed", "started") in live_sdk.realtime.notified


async def test_start_timer_zero_means_no_stop_at(live_sdk):
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=0)
    (row,) = await sessions.active_sessions(live_sdk)
    assert row["stop_at"] is None


async def test_start_caps_timer_to_file_length(live_sdk):
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=999)
    (row,) = await sessions.active_sessions(live_sdk)
    remaining = (row["stop_at"] - row["started_at"]).total_seconds()
    assert remaining <= 61 * 60          # file_length_min default 60


async def test_start_upserts_per_room(live_sdk):
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=10)
    ocean = sounds.Sound("ocean", "Ocean", "ocean.ogg", True)
    await sessions.start(live_sdk, room_id="bedroom", sound=ocean, timer_min=10)
    (row,) = await sessions.active_sessions(live_sdk)
    assert row["sound_key"] == "ocean"


async def test_stop_fades_and_restores_volume(live_sdk):
    live_sdk.config = make_settings(fade_out_sec=0.06)
    clients = wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=None)

    await sessions.stop(live_sdk, "bedroom", fade=True)

    client = clients["bedroom"]
    # Ramp down happened...
    assert client.set_calls[0] < 100
    assert 0 in client.set_calls
    # ...and the pre-fade level was restored AFTER the stop (the
    # single-volume-control invariant).
    assert client.set_calls[-1] == 100
    assert {"action": "stop", "room_id": "bedroom"} in live_sdk.playback.calls
    assert await sessions.active_sessions(live_sdk) == []
    assert ("sessions_changed", "stopped") in live_sdk.realtime.notified


async def test_stop_without_fade_is_immediate(live_sdk):
    clients = wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=None)
    await sessions.stop(live_sdk, "bedroom", fade=False)
    assert "bedroom" not in clients or clients["bedroom"].set_calls == []
    assert await sessions.active_sessions(live_sdk) == []


async def test_enforce_stops_due_sessions(live_sdk):
    live_sdk.config = make_settings(fade_out_sec=0.06)
    clients = wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=30)
    await _set_stop_at("bedroom", seconds_from_now=-5)

    await sessions.enforce(live_sdk)

    assert await sessions.active_sessions(live_sdk) == []
    assert {"action": "stop", "room_id": "bedroom"} in live_sdk.playback.calls
    assert clients["bedroom"].set_calls[-1] == 100      # restored


async def test_enforce_fade_leads_stop_at(live_sdk):
    """A session due INSIDE the fade window starts fading now so it's
    silent at stop_at — not fade_sec late."""
    live_sdk.config = make_settings(fade_out_sec=30.0, timer_interval_sec=5.0)
    wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=30)
    await _set_stop_at("bedroom", seconds_from_now=20)   # inside the 30 s fade

    # Patch the fade so the test doesn't sleep 30 s — we only care that
    # enforcement considered it due.
    fades: list[str] = []

    async def instant_fade(sdk, room_id, fade_sec):
        fades.append(room_id)

    import domovoi_plugin_sleep.sessions as sessions_mod
    original = sessions_mod._fade_down
    sessions_mod._fade_down = instant_fade
    try:
        await sessions.enforce(live_sdk)
    finally:
        sessions_mod._fade_down = original

    assert fades == ["bedroom"]
    assert await sessions.active_sessions(live_sdk) == []


async def test_enforce_reconciles_foreign_playback(live_sdk):
    """The room moved on (user played music) — the session row must
    deactivate and our now-playing stamp must clear."""
    clients = wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=0)
    clients["bedroom"].program(
        state="play", current={"file": "SomeBand/track.mp3"}
    )

    await sessions.enforce(live_sdk)

    assert await sessions.active_sessions(live_sdk) == []
    assert ("bedroom", "sleep") in live_sdk.now_playing.cleared
    assert ("sessions_changed", "ended") in live_sdk.realtime.notified


async def test_enforce_reconciles_natural_end(live_sdk):
    clients = wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=0)
    clients["bedroom"].program(state="stop", current=None)

    await sessions.enforce(live_sdk)
    assert await sessions.active_sessions(live_sdk) == []


async def test_enforce_keeps_handshake_pause(live_sdk):
    """'pause' with our file queued is the satellite handshake window
    (and a user-paused nap) — the session must survive."""
    clients = wire_fake_mpd(live_sdk)
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=0)
    clients["bedroom"].program(
        state="pause", current={"file": "Ambient sounds/brown-noise.ogg"}
    )

    await sessions.enforce(live_sdk)
    assert len(await sessions.active_sessions(live_sdk)) == 1


async def test_enforce_survives_unreachable_player(live_sdk):
    """Player down ≠ session over — the row outlives an MPD blip."""
    async def exploding_client(room_id):
        raise OSError("connection refused")

    live_sdk.playback.mpd_client_for = exploding_client
    await sessions.start(live_sdk, room_id="bedroom", sound=BROWN, timer_min=0)

    await sessions.enforce(live_sdk)
    assert len(await sessions.active_sessions(live_sdk)) == 1
