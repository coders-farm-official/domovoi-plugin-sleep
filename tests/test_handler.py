"""Handler tests on the stub tier: fast-path anchoring (both what
matches and what must NOT — the 'play X' fence matters at band 275),
timer parsing, and the action flows with sounds/sessions monkeypatched
(no DB, no disk, no player)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from domovoi.sdk import Response

from domovoi_plugin_sleep import sessions, sounds
from domovoi_plugin_sleep.handlers import sleep as handler_mod
from domovoi_plugin_sleep.handlers.sleep import (
    SleepHandler,
    _parse_minutes,
    _speak_minutes,
)

from tests.conftest import make_settings

CTX = SimpleNamespace(session_id=None, room_id="bedroom")
CTX_NO_ROOM = SimpleNamespace(session_id=None, room_id=None)

BROWN = sounds.Sound(
    key="brown noise", title="Brown noise",
    filename="brown-noise.ogg", builtin=True,
)


def _match(handler, phrase):
    for fp in handler.fast_paths:
        pattern, method = fp          # FastPath supports 2-tuple unpacking
        m = pattern.match(phrase)
        if m:
            return m, method
    return None, None


# ─── anchoring matrix ────────────────────────────────────────────────────

MATCHES = [
    "play white noise",
    "play brown noise for 30 minutes",
    "play the fan noise",
    "play ocean sounds",
    "play ocean sounds in the bedroom",
    "play ocean sounds in the bedroom for 2 hours",
    "play rain on tent sounds in the office for 45 minutes",
    "play sleep sounds",
    "play some ambient sounds in the den for 1 hour",
    "stop the sounds",
    "stop the noise",
    "stop the white noise",
    "stop the sleep sounds in the bedroom",
    "what sounds can you play",
    "what sleep sounds are there",
    "list sleep sounds",
]

# Phrasings that belong to OTHER handlers — music's territory above all.
NON_MATCHES = [
    "play 97.5 fm",                    # radio's
    "play the beatles",                # music's greedy catch-all
    "play sounds of silence",          # a SONG — no trailing sounds-noun
    "play the sound of silence",
    "play some jazz",
    "stop",                            # bare stop is music's
    "stop the music",
    "what songs do you have",          # library's
    "play white noise and jazz",       # trailing junk breaks the anchor
]


@pytest.mark.parametrize("phrase", MATCHES)
def test_fast_paths_match(stub_sdk, phrase):
    m, _ = _match(SleepHandler(stub_sdk), phrase)
    assert m is not None, f"expected a fast path to claim {phrase!r}"


@pytest.mark.parametrize("phrase", NON_MATCHES)
def test_fast_paths_leave_foreign_phrasings_alone(stub_sdk, phrase):
    m, _ = _match(SleepHandler(stub_sdk), phrase)
    assert m is None, f"no fast path should claim {phrase!r}"


# ─── little parsers ──────────────────────────────────────────────────────

def test_parse_minutes():
    assert _parse_minutes(None, None) is None
    assert _parse_minutes("30", "minutes") == 30
    assert _parse_minutes("2", "hours") == 120
    assert _parse_minutes("1", "hr") == 60


def test_speak_minutes():
    assert _speak_minutes(45) == "45 minutes"
    assert _speak_minutes(60) == "1 hour"
    assert _speak_minutes(120) == "2 hours"


# ─── action flows ────────────────────────────────────────────────────────

class _Recorder:
    """Monkeypatches the sounds catalog + sessions module boundary."""

    def __init__(self, monkeypatch, *, catalog_keys=("brown noise",), active=()):
        self.starts: list[dict] = []
        self.stops: list[str] = []
        cat = {
            k: sounds.Sound(k, k.title(), f"{k.replace(' ', '-')}.ogg", True)
            for k in catalog_keys
        }

        def fake_catalog(cfg, music_dir):
            return cat

        def fake_resolve(cfg, music_dir, spoken):
            key = sounds.normalize_key(spoken)
            for candidate in (key, f"{key} noise", f"{key} sounds"):
                if candidate in cat:
                    return cat[candidate]
            return None

        async def fake_start(sdk, *, room_id, sound, timer_min):
            self.starts.append(
                {"room_id": room_id, "sound": sound.key, "timer_min": timer_min}
            )
            return Response(text=f"Playing {sound.title}.", music_action="start")

        async def fake_stop(sdk, room_id, *, fade=True):
            self.stops.append(room_id)
            return Response(text="Stopped.", music_action="stop")

        async def fake_active(sdk):
            return [{"room_id": r} for r in active]

        monkeypatch.setattr(sounds, "catalog", fake_catalog)
        monkeypatch.setattr(sounds, "resolve", fake_resolve)
        monkeypatch.setattr(sessions, "start", fake_start)
        monkeypatch.setattr(sessions, "stop", fake_stop)
        monkeypatch.setattr(sessions, "active_sessions", fake_active)

        async def fake_known_rooms(self_handler, session):
            return ["bedroom", "living_room"]

        monkeypatch.setattr(SleepHandler, "_known_rooms", fake_known_rooms)


async def _dispatch(handler, phrase, ctx=CTX):
    m, method = _match(handler, phrase)
    assert m is not None, f"{phrase!r} did not match"
    return await method(handler, m, ctx, None)


async def test_play_defaults(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch)
    resp = await _dispatch(SleepHandler(stub_sdk), "play brown noise")
    assert rec.starts == [
        {"room_id": "bedroom", "sound": "brown noise", "timer_min": None}
    ]
    # Default timer (45) is spoken back, with the fade promise.
    assert "45 minutes" in resp.text and "bedroom" in resp.text


async def test_play_with_timer_and_room(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch)
    resp = await _dispatch(
        SleepHandler(stub_sdk),
        "play brown noise in the living room for 30 minutes",
    )
    assert rec.starts[0]["room_id"] == "living_room"    # loose room matching
    assert rec.starts[0]["timer_min"] == 30
    assert "30 minutes" in resp.text


async def test_play_timer_capped_to_file_length_in_reply(monkeypatch, stub_sdk):
    _Recorder(monkeypatch)
    resp = await _dispatch(
        SleepHandler(stub_sdk), "play brown noise for 2 hours"
    )
    # file_length_min default 60 caps the spoken promise to 1 hour.
    assert "1 hour" in resp.text


async def test_play_default_sound_form(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch)
    await _dispatch(SleepHandler(stub_sdk), "play sleep sounds")
    assert rec.starts[0]["sound"] == "brown noise"


async def test_play_unknown_sound_lists_catalog(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch)
    resp = await _dispatch(SleepHandler(stub_sdk), "play whale sounds")
    assert rec.starts == []
    assert "brown noise" in resp.text


async def test_play_empty_catalog_is_honest(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch, catalog_keys=())
    resp = await _dispatch(SleepHandler(stub_sdk), "play brown noise")
    assert rec.starts == []
    assert "haven't been generated yet" in resp.text


async def test_play_unknown_room(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch)
    resp = await _dispatch(
        SleepHandler(stub_sdk), "play brown noise in the attic"
    )
    assert rec.starts == []
    assert "don't know a room called attic" in resp.text


async def test_play_without_any_room(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch)
    resp = await _dispatch(
        SleepHandler(stub_sdk), "play brown noise", ctx=CTX_NO_ROOM
    )
    assert rec.starts == []
    assert "Which room" in resp.text


async def test_stop_active_room(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch, active=("bedroom",))
    resp = await _dispatch(SleepHandler(stub_sdk), "stop the sounds")
    assert rec.stops == ["bedroom"]
    assert "Stopped" in resp.text


async def test_stop_when_quiet(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch, active=())
    resp = await _dispatch(SleepHandler(stub_sdk), "stop the sounds")
    assert rec.stops == []
    assert "No sleep sounds are playing" in resp.text


async def test_execute_defaults_to_list(monkeypatch, stub_sdk):
    """LLM-routed execute without a fast-path match must never start
    noise on a fuzzy transcript."""
    rec = _Recorder(monkeypatch)
    intent = SimpleNamespace(transcript="something about sounds")
    resp = await SleepHandler(stub_sdk).execute(intent, CTX, None)
    assert rec.starts == []
    assert "I can play" in resp.text


async def test_execute_from_tool_routing(monkeypatch, stub_sdk):
    rec = _Recorder(monkeypatch, active=("bedroom",))
    handler = SleepHandler(stub_sdk)
    await handler.execute_from_tool(
        {"action": "play", "sound": "brown noise", "minutes": 20}, CTX, None
    )
    assert rec.starts[0]["timer_min"] == 20
    await handler.execute_from_tool({"action": "stop"}, CTX, None)
    assert rec.stops == ["bedroom"]
