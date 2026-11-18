"""Storytime (stories.py): sizes, the voice phrases, the writer's pacing,
and a story's life in a room and in a browser.

Unit tier needs no DB. The live tier runs the writer against the test DB
with the model, the voice and ffmpeg replaced: ``generate`` returns
canned prose, ``speech.render`` a WAV of a known length, and the encoder
just writes the file.
"""

from __future__ import annotations

import io
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from domovoi.db.session import engine

from domovoi_plugin_sleep import sessions, stories
from domovoi_plugin_sleep.handlers import sleep as handler_mod

from conftest import requires_db, wire_fake_mpd


# ─── unit tier ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("size,minutes", [
    ("s", 3), ("m", 10), ("l", 20), ("xl", 40), ("huge", 90),
    ("small", 3), ("short", 3), ("medium", 10), ("long", 20),
    ("extra long", 40), ("giant", 90), (None, 10), ("nonsense", 10),
])
def test_sizes(size, minutes):
    assert stories.size_minutes(size) == minutes


def _match(phrase):
    m = (handler_mod._STORY_SIZED_RE.match(phrase)
         or handler_mod._STORY_TIMED_RE.match(phrase))
    return {k: v for k, v in m.groupdict().items() if v} if m else None


@pytest.mark.parametrize("phrase,groups", [
    ("tell me a bedtime story about a sleepy dragon", {"about": "a sleepy dragon"}),
    ("tell a huge story in the kids room", {"size": "huge", "room": "kids room"}),
    ("tell me a story about otters for 20 minutes",
     {"about": "otters", "num": "20", "unit": "minutes"}),
    ("a 15 minute story", {"num2": "15", "unit2": "minute"}),
    ("tell me a 1 hour bedtime story", {"num2": "1", "unit2": "hour"}),
    ("tell me an extra long bedtime story about the moon in the bedroom",
     {"size": "extra long", "about": "the moon", "room": "bedroom"}),
    ("tell me a short bedtime story", {"casual": "short"}),
    ("tell me a long story", {"size": "long"}),
])
def test_storytime_phrases(phrase, groups):
    assert _match(phrase) == groups


@pytest.mark.parametrize("phrase", [
    # The Q&A model's short story stays its own.
    "tell me a story",
    "tell me a short story",
    "tell me a story about a fox",
    "what is the story of rome",
    "play rain sounds",
])
def test_phrases_storytime_leaves_alone(phrase):
    assert _match(phrase) is None


def test_stop_the_story():
    assert handler_mod._STOP_STORY_RE.match("stop the story")
    assert handler_mod._STOP_STORY_RE.match("stop the bedtime story in the bedroom")


def test_clean_passage_reads_aloud():
    raw = "**Title: The Sleepy Fox**\n\n# Passage 1\nOnce upon a *time*, a fox yawned.\n\nThe End"
    assert stories.clean_passage(raw) == "Once upon a time, a fox yawned."


def _story(**kw):
    base = {
        "id": 1, "room_id": "bedroom", "title": "", "premise": "a fox",
        "tone": "cozy", "audience": "kids", "minutes": 10, "bed_sound": None,
        "status": "writing", "writing_done": False, "summary": "",
        "last_passage": "", "segments": 0, "audio_sec": 0.0, "words": 0,
        "playback_started_at": None,
        "last_seen_at": datetime.now(timezone.utc),
    }
    base.update(kw)
    return base


NOW = datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc)


def test_first_passage_is_written_at_once():
    write, words, final, _ = stories.plan_next(_story(), NOW)
    assert write and not final and words == stories.PASSAGE_WORDS


def test_writer_waits_while_far_enough_ahead():
    st = _story(audio_sec=300.0, words=720, segments=3,
                playback_started_at=NOW - timedelta(seconds=30))
    assert stories.plan_next(st, NOW)[0] is False


def test_writer_catches_up_as_the_listener_nears_the_end():
    st = _story(audio_sec=300.0, words=720, segments=3,
                playback_started_at=NOW - timedelta(seconds=120))
    assert stories.plan_next(st, NOW)[0] is True


def test_last_passage_lands_on_the_length():
    # 10 minutes, 520 s written at 2.4 w/s (measured): one short passage left.
    st = _story(audio_sec=520.0, words=1248, segments=5,
                playback_started_at=NOW - timedelta(seconds=400))
    write, words, final, winding = stories.plan_next(st, NOW)
    assert write and final and winding
    assert 80 <= words <= 200


def test_prompts_carry_the_thread_and_the_ending():
    st = _story(summary="Fox lost her blanket.", last_passage="She looked under the oak.")
    middle = stories.passage_prompt(st, words=260, final=False, winding_down=True)
    assert "Fox lost her blanket." in middle and "under the oak" in middle
    assert "sleepy" in middle
    end = stories.passage_prompt(st, words=150, final=True, winding_down=True)
    assert "final passage" in end


# ─── live tier ───────────────────────────────────────────────────────────


def _wav(seconds: float, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture
def story_env(live_sdk, monkeypatch):
    sdk = live_sdk
    sdk.core_config = SimpleNamespace(
        music_dir=sdk.core_config.music_dir, use_stubs=True, ai_features=True,
        ollama_url="http://127.0.0.1:11434", ollama_model="llama3.2:3b",
    )
    clients = wire_fake_mpd(sdk)
    prompts: list[str] = []

    async def fake_generate(_sdk, prompt, *, max_tokens, temperature=0.8):
        prompts.append(prompt)
        if "title" in prompt.lower() and "reply with the title" in prompt.lower():
            return "The Sleepy Fox"
        if prompt.startswith("Update the summary"):
            return "A fox is looking for her blanket."
        return ("The little fox yawned and looked around the quiet meadow. "
                "The moon was round and the grass was soft and cool. " * 6)

    rendered: list[tuple[str, str | None]] = []

    async def fake_render(text_, *, room_id=None):
        rendered.append((text_, room_id))
        return _wav(100.0)

    async def fake_encode(args, out, title, *, timeout=600.0):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"OggS")

    monkeypatch.setattr(stories, "generate", fake_generate)
    monkeypatch.setattr(stories.sounds, "_encode_ogg", fake_encode)
    announced: list[tuple[str | None, str]] = []

    async def fake_announce(room_id, text_):
        announced.append((room_id, text_))
        return [room_id] if room_id else []

    sdk.speech = SimpleNamespace(render=fake_render, announce=fake_announce)
    return SimpleNamespace(sdk=sdk, clients=clients, prompts=prompts, rendered=rendered,
                           announced=announced)


@requires_db
@pytest.mark.asyncio
async def test_a_room_story_starts_in_the_rooms_voice(story_env):
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id="bedroom", minutes=10, premise="a fox")
    assert story["status"] == "writing"

    await stories.tick(sdk)

    st = await stories.get(sdk, story["id"])
    assert st["status"] == "playing" and st["segments"] == 1
    assert st["title"] == "The Sleepy Fox"
    assert st["summary"] == "A fox is looking for her blanket."
    assert story_env.rendered[0][1] == "bedroom"      # the room's voice
    uri = stories.segment_uri(sdk.config, stories.segment_filename(story["id"], 1))
    plays = [c for c in sdk.playback.calls if c["action"] == "play_url"]
    assert plays and plays[0]["stream_url"] == uri
    assert any(c["action"] == "dispatch" for c in sdk.playback.calls)
    active = await sessions.active_session(sdk, "bedroom")
    assert active["sound_key"] == stories.session_key(story["id"])
    assert story_env.clients["bedroom"].updated == [stories.stories_folder_uri(sdk.config)]
    out = stories.stories_dir(sdk.config, sdk.core_config.music_dir) / stories.segment_filename(story["id"], 1)
    assert out.exists()


@requires_db
@pytest.mark.asyncio
async def test_later_passages_join_the_rooms_queue(story_env):
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id="bedroom", minutes=20)
    await stories.tick(sdk)
    first = stories.segment_uri(sdk.config, stories.segment_filename(story["id"], 1))
    story_env.clients["bedroom"].program(state="play", current={"file": first})

    await stories.tick(sdk)                  # 100 s written, under the lead
    st = await stories.get(sdk, story["id"])
    assert st["segments"] == 2
    second = stories.segment_uri(sdk.config, stories.segment_filename(story["id"], 2))
    assert story_env.clients["bedroom"].appended == [second]
    assert len([c for c in sdk.playback.calls if c["action"] == "play_url"]) == 1


@requires_db
@pytest.mark.asyncio
async def test_the_timer_tick_keeps_a_story_that_moved_to_its_next_passage(story_env):
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id="bedroom", minutes=20)
    await stories.tick(sdk)
    second = stories.segment_uri(sdk.config, stories.segment_filename(story["id"], 2))
    story_env.clients["bedroom"].program(state="play", current={"file": second})
    await sessions.enforce(sdk)
    assert await sessions.active_session(sdk, "bedroom") is not None

    # Ran dry while the next passage is written: waiting, not over.
    story_env.clients["bedroom"].program(state="stop", current=None)
    await sessions.enforce(sdk)
    assert await sessions.active_session(sdk, "bedroom") is not None

    # Something else took the room: the session ends and so does the story.
    story_env.clients["bedroom"].program(state="play", current={"file": "Music/song.mp3"})
    await sessions.enforce(sdk)
    assert await sessions.active_session(sdk, "bedroom") is None
    await stories.tick(sdk)
    assert (await stories.get(sdk, story["id"]))["status"] == "stopped"


@requires_db
@pytest.mark.asyncio
async def test_a_story_ends_on_its_length(story_env):
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id="bedroom", minutes=3)
    for _ in range(4):
        st = await stories.get(sdk, story["id"])
        async with engine.begin() as conn:     # the listener keeps up
            await conn.execute(
                text("UPDATE plugin_sleep.stories SET playback_started_at = :t WHERE id = :id"),
                {"t": datetime.now(timezone.utc) - timedelta(seconds=st["audio_sec"] + 1),
                 "id": story["id"]},
            )
        await stories.tick(sdk)
    st = await stories.get(sdk, story["id"])
    assert st["writing_done"] is True
    assert st["segments"] == 2                 # 100 s + a short ending
    assert "final passage" in story_env.prompts[-1] or any(
        "final passage" in p for p in story_env.prompts)

    # Played out: the queue ends, the session ends, the story is done.
    story_env.clients["bedroom"].program(state="stop", current=None)
    await sessions.enforce(sdk)
    await stories.tick(sdk)
    assert (await stories.get(sdk, story["id"]))["status"] == "done"


@requires_db
@pytest.mark.asyncio
async def test_stop_ends_the_story_and_the_room(story_env):
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id="bedroom", minutes=10)
    await stories.tick(sdk)
    await stories.stop(sdk, story["id"])
    assert (await stories.get(sdk, story["id"]))["status"] == "stopped"
    assert await sessions.active_session(sdk, "bedroom") is None
    assert any(c["action"] == "stop" for c in sdk.playback.calls)


@requires_db
@pytest.mark.asyncio
async def test_a_browser_story_uses_the_server_voice_and_stops_when_unheard(story_env):
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id=None, minutes=10)
    await stories.tick(sdk)
    assert story_env.rendered[0][1] is None            # the server's voice
    assert not [c for c in sdk.playback.calls if c["action"] == "play_url"]

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE plugin_sleep.stories SET last_seen_at = NOW() - INTERVAL '10 minutes', "
                 "playback_started_at = NOW() - INTERVAL '10 minutes' WHERE id = :id"),
            {"id": story["id"]},
        )
    await stories.tick(sdk)
    assert (await stories.get(sdk, story["id"]))["status"] == "stopped"


@requires_db
@pytest.mark.asyncio
async def test_storytime_needs_the_ai_switch(story_env):
    sdk = story_env.sdk
    sdk.core_config.ai_features = False
    with pytest.raises(stories.StoryError, match="AI features"):
        await stories.start(sdk, room_id="bedroom", minutes=10)


@requires_db
@pytest.mark.asyncio
async def test_a_new_story_ends_the_rooms_old_one(story_env):
    sdk = story_env.sdk
    old = await stories.start(sdk, room_id="bedroom", minutes=10)
    await stories.start(sdk, room_id="bedroom", minutes=20)
    assert (await stories.get(sdk, old["id"]))["status"] == "stopped"


@requires_db
@pytest.mark.asyncio
async def test_a_failed_first_passage_is_reported(story_env, monkeypatch):
    sdk = story_env.sdk

    async def dead_model(*a, **kw):
        raise RuntimeError("ollama is not running")

    monkeypatch.setattr(stories, "generate", dead_model)
    story = await stories.start(sdk, room_id="bedroom", minutes=10)
    await stories.tick(sdk)
    st = await stories.get(sdk, story["id"])
    assert st["status"] == "failed" and "ollama" in st["error"]


# ─── a room story that never started says so out loud ────────────────────


async def _dead_model(*a, **kw):
    raise RuntimeError("ollama is not running")


@requires_db
@pytest.mark.asyncio
async def test_a_room_story_that_never_started_is_announced_once(story_env, monkeypatch):
    sdk = story_env.sdk
    monkeypatch.setattr(stories, "generate", _dead_model)
    story = await stories.start(sdk, room_id="bedroom", minutes=10)
    await stories.tick(sdk)
    await stories.tick(sdk)                  # failed: never written again

    assert (await stories.get(sdk, story["id"]))["status"] == "failed"
    assert story_env.announced == [("bedroom", stories.COULDNT_START)]


@requires_db
@pytest.mark.asyncio
async def test_a_failure_after_the_story_played_stays_quiet(story_env, monkeypatch):
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id="bedroom", minutes=20)
    await stories.tick(sdk)                  # passage 1 reaches the room
    first = stories.segment_uri(sdk.config, stories.segment_filename(story["id"], 1))
    story_env.clients["bedroom"].program(state="play", current={"file": first})

    monkeypatch.setattr(stories, "generate", _dead_model)
    await stories.tick(sdk)                  # passage 2 fails

    st = await stories.get(sdk, story["id"])
    assert st["status"] == "playing" and st["writing_done"] and "ollama" in st["error"]
    assert story_env.announced == []


@requires_db
@pytest.mark.asyncio
async def test_a_browser_story_failure_is_not_announced(story_env, monkeypatch):
    sdk = story_env.sdk
    monkeypatch.setattr(stories, "generate", _dead_model)
    story = await stories.start(sdk, room_id=None, minutes=10)
    await stories.tick(sdk)

    assert (await stories.get(sdk, story["id"]))["status"] == "failed"
    assert story_env.announced == []


@requires_db
@pytest.mark.asyncio
async def test_an_announcement_that_raises_never_breaks_the_writer(story_env, monkeypatch):
    sdk = story_env.sdk
    monkeypatch.setattr(stories, "generate", _dead_model)

    async def broken_announce(room_id, text_):
        raise RuntimeError("no satellites")

    sdk.speech.announce = broken_announce
    failing = await stories.start(sdk, room_id="bedroom", minutes=10)
    other = await stories.start(sdk, room_id="kids_room", minutes=10)
    await stories.tick(sdk)                  # must not raise

    assert (await stories.get(sdk, failing["id"]))["status"] == "failed"
    assert (await stories.get(sdk, other["id"]))["status"] == "failed"


# ─── the room's player only plays what its database knows ────────────────


class IndexingMPD:
    """A room's MPD the way MPD 0.23 indexes a folder it can't watch (a
    Docker Desktop bind mount gets no inotify events): a file is playable
    only once an ``update`` read it. ``update <folder>`` reads the folder
    from disk, adding it and any missing parents; ``update <file>`` reads
    the file ONLY when its folder is already in the database, and is
    silently a no-op otherwise. The second rule is what the first live
    story hit: the Stories folder is new, so the file update did nothing
    and the play was "No such song"."""

    def __init__(self, music_dir: str, known_dirs: set[str]) -> None:
        self.music = music_dir
        self.dirs = set(known_dirs)
        self.files: set[str] = set()
        self._state = "stop"

    async def update_paths(self, uris: list[str]) -> None:
        from pathlib import Path, PurePosixPath

        for uri in uris:
            disk = Path(self.music, *PurePosixPath(uri).parts)
            if disk.is_dir():
                parts = PurePosixPath(uri).parts
                for i in range(1, len(parts) + 1):
                    self.dirs.add("/".join(parts[:i]))
                for f in disk.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(self.music).as_posix()
                        self.files.add(rel)
                        self.dirs.add(str(PurePosixPath(rel).parent))
            elif disk.is_file() and str(PurePosixPath(uri).parent) in self.dirs:
                self.files.add(uri)

    async def state(self) -> str:
        return self._state

    async def queue_add_tracks(self, specs: list[dict]) -> list[dict]:
        return [{"file": s["file_path"], "id": 1} for s in specs
                if s["file_path"] in self.files]


def _indexing_room(sdk, monkeypatch, *, known_dirs: set[str]) -> IndexingMPD:
    """The room's player plus a play_url that, like the core's, fails
    (no music_action) for a file the player's database doesn't have."""
    from domovoi.models import Response

    mpd = IndexingMPD(sdk.core_config.music_dir, known_dirs)

    async def mpd_client_for(room_id):
        return mpd

    recorded = sdk.playback.play_url

    async def play_url(room_id, stream_url, **kw):
        if stream_url not in mpd.files:
            sdk.playback.calls.append({"action": "play_url_refused",
                                       "stream_url": stream_url})
            return Response(text="the music player wouldn't accept the stream.")
        mpd._state = "pause"
        return await recorded(room_id, stream_url, **kw)

    async def no_wait(_sec):
        return None

    sdk.playback.mpd_client_for = mpd_client_for
    sdk.playback.play_url = play_url
    monkeypatch.setattr(stories.asyncio, "sleep", no_wait)
    return mpd


@requires_db
@pytest.mark.asyncio
async def test_the_first_story_plays_in_a_room_that_never_saw_the_stories_folder(
        story_env, monkeypatch):
    # Live 2026-11-17, dining room: "Okay, a bedtime story … It'll start
    # in a moment", then silence: the room's player had the sounds folder
    # but not Stories/, so the passage was never indexed.
    sdk = story_env.sdk
    mpd = _indexing_room(sdk, monkeypatch, known_dirs={sdk.config.sounds_subdir})
    story = await stories.start(sdk, room_id="dining_room", minutes=3)

    await stories.tick(sdk)

    st = await stories.get(sdk, story["id"])
    assert st["status"] == "playing" and not st["error"], st["error"]
    uri = stories.segment_uri(sdk.config, stories.segment_filename(story["id"], 1))
    assert uri in mpd.files
    assert [c["stream_url"] for c in sdk.playback.calls if c["action"] == "play_url"] == [uri]
    assert any(c["action"] == "dispatch" and c["music_action"] == "start"
               for c in sdk.playback.calls)
    active = await sessions.active_session(sdk, "dining_room")
    assert active["sound_key"] == stories.session_key(story["id"])


@requires_db
@pytest.mark.asyncio
async def test_a_first_passage_the_room_wont_play_is_a_failed_story(story_env, monkeypatch):
    # Written, but it never reached the room: the Sleep page's room card
    # must show it failed (it shows failed stories for 10 minutes), not
    # "telling" for a tick and then nothing.
    sdk = story_env.sdk
    mpd = _indexing_room(sdk, monkeypatch, known_dirs=set())

    async def update_nothing(uris):
        return None

    mpd.update_paths = update_nothing
    story = await stories.start(sdk, room_id="dining_room", minutes=3)
    await stories.tick(sdk)

    st = await stories.get(sdk, story["id"])
    assert st["status"] == "failed"
    assert "wouldn't start the story" in st["error"]
    assert await sessions.active_session(sdk, "dining_room") is None


# ─── the page hears every way a story ends ──────────────────────────────
#
# Reported 2026-11-17: the Sleep page kept showing a story as playing
# well after it was over. The page re-reads on the ``sleep.sessions``
# snapshot changing; these pin that every end changes the snapshot (and
# sends the NOTIFY that wakes it at once).


async def _snapshot(sdk):
    from domovoi_plugin_sleep import web as web_mod

    async with sdk.db.session_scope() as s:
        await s.execute(text("SET search_path TO plugin_sleep, public"))
        return await web_mod.snapshot_sessions(s)


async def _play_story_to_its_end(story_env, **kw):
    """A 3-minute room story, written to its ending, listener keeping up."""
    sdk = story_env.sdk
    story = await stories.start(sdk, room_id="bedroom", minutes=3, **kw)
    for _ in range(4):
        st = await stories.get(sdk, story["id"])
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE plugin_sleep.stories SET playback_started_at = :t WHERE id = :id"),
                {"t": datetime.now(timezone.utc) - timedelta(seconds=st["audio_sec"] + 1),
                 "id": story["id"]},
            )
        await stories.tick(sdk)
    assert (await stories.get(sdk, story["id"]))["writing_done"] is True
    return story


@requires_db
@pytest.mark.asyncio
async def test_a_story_with_a_sound_under_it_is_over_when_its_ending_has_played(story_env):
    # The ending played and the room went on to the sleep sound: the story
    # used to stay "telling" (and the room card "story") until the sleep
    # timer ran out, 45 minutes later.
    sdk = story_env.sdk
    folder = stories.sounds.sounds_dir(sdk.core_config.music_dir, sdk.config.sounds_subdir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gentle hum.ogg").write_bytes(b"OggS")
    story = await _play_story_to_its_end(story_env, bed_sound="gentle hum")
    bed = stories.sounds.resolve(sdk.config, sdk.core_config.music_dir, "gentle hum")
    bed_uri = stories.sounds.mpd_uri(sdk.config, bed)
    stop_at = (await sessions.active_session(sdk, "bedroom"))["stop_at"]
    assert stop_at is not None                       # the sleep timer, from the ending

    # Still on the last passage: still the story.
    last = stories.segment_uri(sdk.config, stories.segment_filename(story["id"], 2))
    story_env.clients["bedroom"].program(state="play", current={"file": last})
    await sessions.enforce(sdk)
    assert (await stories.get(sdk, story["id"]))["status"] == "playing"

    sdk.realtime.notified.clear()
    story_env.clients["bedroom"].program(state="play", current={"file": bed_uri})
    await sessions.enforce(sdk)

    assert (await stories.get(sdk, story["id"]))["status"] == "done"
    active = await sessions.active_session(sdk, "bedroom")
    assert active["sound_key"] == bed.key and active["mpd_uri"] == bed_uri
    assert active["stop_at"] == stop_at              # the timer carries on
    assert sdk.realtime.notified                     # the page hears it now
    snap = await _snapshot(sdk)
    assert snap["stories"] == []
    assert [s["sound_key"] for s in snap["sessions"]] == [bed.key]

    # Later ticks leave the sound alone until its timer.
    await sessions.enforce(sdk)
    await stories.tick(sdk)
    assert (await sessions.active_session(sdk, "bedroom"))["sound_key"] == bed.key


@requires_db
@pytest.mark.asyncio
async def test_a_story_that_plays_out_leaves_the_snapshot(story_env):
    sdk = story_env.sdk
    story = await _play_story_to_its_end(story_env)
    assert [s["id"] for s in (await _snapshot(sdk))["stories"]] == [story["id"]]

    sdk.realtime.notified.clear()
    story_env.clients["bedroom"].program(state="stop", current=None)
    await sessions.enforce(sdk)                      # the room ran out: session ends
    await stories.tick(sdk)                          # ...and the story is done

    assert (await stories.get(sdk, story["id"]))["status"] == "done"
    reasons = [r for _, r in sdk.realtime.notified]
    assert "ended" in reasons and "story" in reasons
    assert await _snapshot(sdk) == {"sessions": [], "stories": []}


@requires_db
@pytest.mark.asyncio
async def test_every_other_way_a_story_ends_is_heard(story_env, monkeypatch):
    sdk = story_env.sdk

    # Stopped by voice ("stop the story").
    a = await stories.start(sdk, room_id="bedroom", minutes=10)
    await stories.tick(sdk)
    sdk.realtime.notified.clear()
    await stories.mark_stopped(sdk, a["id"])
    assert sdk.realtime.notified

    # The room moved on to something else.
    b = await stories.start(sdk, room_id="den", minutes=10)
    await stories.tick(sdk)
    story_env.clients["den"].program(state="play", current={"file": "Music/song.mp3"})
    sdk.realtime.notified.clear()
    await sessions.enforce(sdk)
    await stories.tick(sdk)
    assert (await stories.get(sdk, b["id"]))["status"] == "stopped"
    assert {"ended", "story"} <= {r for _, r in sdk.realtime.notified}

    # Failed before it started.
    monkeypatch.setattr(stories, "generate", _dead_model)
    c = await stories.start(sdk, room_id="attic", minutes=10)
    sdk.realtime.notified.clear()
    await stories.tick(sdk)
    assert (await stories.get(sdk, c["id"]))["status"] == "failed"
    assert sdk.realtime.notified
