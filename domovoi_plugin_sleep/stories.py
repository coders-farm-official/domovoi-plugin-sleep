"""Storytime: an AI-written bedtime story that runs for as long as asked.

How a story plays:

* ``start`` writes a ``stories`` row (room, premise, length, tone, who's
  listening, an optional sleep sound underneath). Nothing is spoken yet.
* The ``story_writer`` worker (``tick``) writes the story a passage at a
  time with the house's Q&A model, keeping about ``LEAD_SEC`` of audio
  ahead of the listener. Each passage is written from the premise, a
  short running summary and the passage before it, so a 90-minute story
  keeps its thread without a long model context. Past ~70% of the length
  the passages slow toward rest; the last one lands a sleepy ending.
* Each passage is rendered in the room's voice (``sdk.speech.render``,
  SDK 1.9; a browser story gets the server's voice), mixed over the
  sleep sound when one was picked, and written as an OGG next to the
  sleep sounds (``<sounds folder>/Stories``) so the room's player can
  queue it. The first passage starts the room through the same sleep
  session row the sounds use, so the timer, the fade and "stop the
  sounds" all work on a story; later passages are appended to the
  room's queue.
* A browser story has no room: the Sleep page polls ``/stories/{id}``
  for passages and queues them in the dashboard player. It heartbeats
  while it listens; a story nobody is listening to stops being written.

The row is the durable truth: a core restart mid-story carries on from
the summary and the last passage.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from domovoi.sdk import PluginSDK

from domovoi_plugin_sleep import SCHEMA, sessions, sounds

log = logging.getLogger(__name__)

# ─── lengths ──────────────────────────────────────────────────────────────

#: The page's and the voice's named sizes, in minutes (owner's call,
#: 2026-11-12).
SIZES: dict[str, int] = {"s": 3, "m": 10, "l": 20, "xl": 40, "huge": 90}
DEFAULT_SIZE = "m"
MIN_MINUTES = 1
MAX_MINUTES = 180

#: Spoken words for each size.
#: Words that only mean Storytime next to "bedtime" ("tell me a short
#: story" stays the Q&A model's short story).
CASUAL_SIZE_WORDS = ("short", "quick", "little", "tiny")
SIZE_WORDS: dict[str, str] = {
    "small": "s", "short": "s", "quick": "s", "little": "s", "tiny": "s",
    "medium": "m", "regular": "m",
    "long": "l", "large": "l", "big": "l", "longer": "l",
    "extra long": "xl", "extra large": "xl", "xl": "xl", "very long": "xl",
    "really long": "xl", "super long": "xl",
    "huge": "huge", "giant": "huge", "epic": "huge", "enormous": "huge",
    "massive": "huge",
}

TONES = ("cozy", "silly", "adventure", "magical", "calm")
AUDIENCES = ("kids", "grown-ups")

#: Audio kept written ahead of the listener.
LEAD_SEC = 240.0
#: A browser story nobody has polled for this long stops being written.
BROWSER_IDLE_SEC = 150.0
#: Narration pace before a story has measured its own (words per second).
DEFAULT_WPS = 2.8
PASSAGE_WORDS = 260
#: Gap after each passage, so passages don't run into each other.
PASSAGE_GAP_SEC = 0.8
#: The sleep sound under the story, as a gain on the narration mix.
BED_GAIN = 0.22
#: Finished stories' files are deleted after this long.
KEEP_HOURS = 12

STORIES_SUBDIR = "Stories"
LIVE = ("writing", "playing")


class StoryError(Exception):
    """A story can't start (AI off, no room, bad length) — the text is
    what to tell the person."""


def size_minutes(size: str | None) -> int:
    key = (size or DEFAULT_SIZE).strip().lower()
    key = SIZE_WORDS.get(key, key)
    return SIZES.get(key, SIZES[DEFAULT_SIZE])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── files ────────────────────────────────────────────────────────────────

def stories_dir(cfg, music_dir: str) -> Path:
    return sounds.sounds_dir(music_dir, cfg.sounds_subdir) / STORIES_SUBDIR


def segment_filename(story_id: int, n: int) -> str:
    # Unique basenames: the room player finds a fresh file by its name.
    return f"story-{story_id}-{n:03d}.ogg"


def segment_uri(cfg, filename: str) -> str:
    return f"{cfg.sounds_subdir}/{STORIES_SUBDIR}/{filename}"


def story_uri_prefix(cfg, story_id: int) -> str:
    return f"{cfg.sounds_subdir}/{STORIES_SUBDIR}/story-{story_id}-"


def session_key(story_id: int) -> str:
    """The sleep session's ``sound_key`` while a story plays in a room."""
    return f"story:{story_id}"


def story_id_of(sound_key: str | None) -> int | None:
    if sound_key and sound_key.startswith("story:"):
        try:
            return int(sound_key.split(":", 1)[1])
        except ValueError:
            return None
    return None


# ─── rows ─────────────────────────────────────────────────────────────────

_COLS = (
    "id, room_id, title, premise, tone, audience, minutes, bed_sound, "
    "status, writing_done, summary, last_passage, segments, audio_sec, "
    "words, playback_started_at, last_seen_at, error, created_at"
)


async def get(sdk: PluginSDK, story_id: int) -> dict[str, Any] | None:
    async with sdk.db.session_scope() as s:
        row = (
            await s.execute(
                text(f"SELECT {_COLS} FROM {SCHEMA}.stories WHERE id = :id"),
                {"id": story_id},
            )
        ).mappings().first()
        return dict(row) if row else None


async def live_stories(sdk: PluginSDK) -> list[dict[str, Any]]:
    async with sdk.db.session_scope() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT {_COLS} FROM {SCHEMA}.stories "
                    "WHERE status IN ('writing', 'playing') ORDER BY id"
                )
            )
        ).mappings().all()
        return [dict(r) for r in rows]


async def segments(sdk: PluginSDK, story_id: int) -> list[dict[str, Any]]:
    async with sdk.db.session_scope() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT n, filename, duration_sec FROM {SCHEMA}.story_segments "
                    "WHERE story_id = :id ORDER BY n"
                ),
                {"id": story_id},
            )
        ).mappings().all()
        return [dict(r) for r in rows]


async def _update(sdk: PluginSDK, story_id: int, *, notify: str | None = None,
                  **fields: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    async with sdk.db.session_scope() as s:
        await s.execute(
            text(
                f"UPDATE {SCHEMA}.stories SET {sets}, updated_at = NOW() "
                "WHERE id = :id"
            ),
            {"id": story_id, **fields},
        )
        if notify:
            await sdk.realtime.notify(s, "sessions_changed", notify)


async def _end_room_stories(sdk: PluginSDK, room_id: str, *, keep: int | None = None) -> None:
    """A room plays one story: a new one (or anything else) ends the old."""
    async with sdk.db.session_scope() as s:
        await s.execute(
            text(
                f"UPDATE {SCHEMA}.stories SET status = 'stopped', updated_at = NOW() "
                "WHERE room_id = :room AND status IN ('writing', 'playing') "
                "AND (CAST(:keep AS BIGINT) IS NULL OR id <> :keep)"
            ),
            {"room": room_id, "keep": keep},
        )


# ─── start / stop ─────────────────────────────────────────────────────────

async def start(
    sdk: PluginSDK,
    *,
    room_id: str | None,
    minutes: int,
    premise: str = "",
    tone: str = "cozy",
    audience: str = "kids",
    bed_sound: str | None = None,
) -> dict[str, Any]:
    """Record a new story; the writer worker does the rest. Raises
    :class:`StoryError` with words for the person when it can't."""
    if not sdk.core_config.ai_features:
        raise StoryError(
            "Storytime needs the AI features, and they're turned off on "
            "this Domovoi."
        )
    minutes = int(minutes)
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise StoryError(
            f"A story can run from {MIN_MINUTES} to {MAX_MINUTES} minutes."
        )
    tone = tone if tone in TONES else "cozy"
    audience = audience if audience in AUDIENCES else "kids"
    bed_key: str | None = None
    if bed_sound:
        bed = sounds.resolve(sdk.config, sdk.core_config.music_dir, bed_sound)
        if bed is None:
            raise StoryError(f"I don't have a sound called {bed_sound}.")
        bed_key = bed.key
    premise = re.sub(r"\s+", " ", premise or "").strip()[:500]

    if room_id:
        await _end_room_stories(sdk, room_id)
    async with sdk.db.session_scope() as s:
        row = (
            await s.execute(
                text(
                    f"INSERT INTO {SCHEMA}.stories "
                    "(room_id, premise, tone, audience, minutes, bed_sound) "
                    "VALUES (:room, :premise, :tone, :aud, :min, :bed) "
                    f"RETURNING {_COLS}"
                ),
                {"room": room_id, "premise": premise, "tone": tone,
                 "aud": audience, "min": minutes, "bed": bed_key},
            )
        ).mappings().first()
        await sdk.realtime.notify(s, "sessions_changed", "story")
    story = dict(row)
    kick(sdk)
    return story


async def stop(sdk: PluginSDK, story_id: int) -> dict[str, Any] | None:
    """Stop a story: the room's playback too, when it is still this
    story's. Returns the stopped row (None for an unknown id)."""
    story = await get(sdk, story_id)
    if story is None:
        return None
    if story["status"] in LIVE:
        await _update(sdk, story_id, status="stopped", notify="story")
    room_id = story["room_id"]
    if room_id:
        active = await sessions.active_session(sdk, room_id)
        if active and story_id_of(active["sound_key"]) == story_id:
            resp = await sessions.stop(sdk, room_id, fade=False)
            await sessions.deliver(sdk, resp, room_id)
    return await get(sdk, story_id)


async def mark_stopped(sdk: PluginSDK, story_id: int) -> None:
    await _update(sdk, story_id, status="stopped", notify="story")


async def heartbeat(sdk: PluginSDK, story_id: int) -> None:
    async with sdk.db.session_scope() as s:
        await s.execute(
            text(
                f"UPDATE {SCHEMA}.stories SET last_seen_at = NOW() "
                "WHERE id = :id"
            ),
            {"id": story_id},
        )


def kick(sdk: PluginSDK) -> None:
    """Wake the writer now rather than at its next tick: the first
    passage is what the listener is waiting for."""
    if sdk.core_config.use_stubs:
        return
    task = sdk.state.get("story_kick")
    if task is not None and not task.done():
        return
    sdk.state["story_kick"] = asyncio.create_task(tick(sdk))


# ─── the model ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a gentle bedtime storyteller. You write calm, slow, soothing "
    "prose meant to be read aloud to someone falling asleep. Plain "
    "sentences only: no headings, titles, lists, markdown, sound effects "
    "in brackets, or notes to the reader. Nothing scary, violent or "
    "exciting enough to wake anyone up."
)


async def generate(sdk: PluginSDK, prompt: str, *, max_tokens: int,
                   temperature: float = 0.8) -> str:
    """One completion from the house's Q&A model, through ``sdk.http``
    (so the household's AI switch applies)."""
    url = f"{str(sdk.core_config.ollama_url).rstrip('/')}/api/generate"
    async with sdk.http.client(timeout=240.0) as client:
        resp = await client.post(url, json={
            "model": str(sdk.core_config.ollama_model),
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "10m",
            "options": {"num_predict": max_tokens, "temperature": temperature},
        })
        resp.raise_for_status()
        return str(resp.json().get("response") or "").strip()


def _audience_words(audience: str) -> str:
    return ("young children (simple words, warm and reassuring)"
            if audience == "kids" else "grown-ups (richer language, still restful)")


def _tone_words(tone: str) -> str:
    return {
        "cozy": "cozy and warm",
        "silly": "gently silly and funny, never loud",
        "adventure": "a soft, unhurried adventure",
        "magical": "dreamy and magical",
        "calm": "very calm and slow, almost a meditation",
    }.get(tone, "cozy and warm")


def passage_prompt(story: dict[str, Any], *, words: int, final: bool,
                   winding_down: bool) -> str:
    premise = story["premise"] or "something gentle of your choosing"
    parts = [
        f"Bedtime story for {_audience_words(story['audience'])}. "
        f"Tone: {_tone_words(story['tone'])}.",
        f"What it's about: {premise}.",
    ]
    if story["summary"]:
        parts.append(f"The story so far: {story['summary']}")
    if story["last_passage"]:
        tail = story["last_passage"][-900:]
        parts.append(f"The last passage ended like this: \"{tail}\"")
    if final:
        parts.append(
            f"Write the final passage, about {words} words: bring the story "
            "to a peaceful, sleepy ending where everyone is safe, warm and "
            "drifting off to sleep. End softly."
        )
    elif not story["last_passage"]:
        parts.append(
            f"Begin the story. Write the first passage, about {words} words. "
            "Introduce the main character and the setting slowly."
        )
    else:
        parts.append(
            f"Continue the story with the next passage, about {words} words. "
            "Don't repeat what already happened and don't end the story yet."
        )
        if winding_down:
            parts.append(
                "The story is nearing its end: let things grow quieter and "
                "slower, with the characters starting to feel sleepy."
            )
    return "\n\n".join(parts)


_MARKUP_RE = re.compile(r"[*_#`>\[\]]+")


def clean_passage(raw: str) -> str:
    """Model text -> something to read aloud: no markdown, no title or
    "Passage 2:" lines, no stray quotes around the whole thing."""
    lines: list[str] = []
    for line in (raw or "").splitlines():
        stripped = _MARKUP_RE.sub("", line).strip()
        if not stripped:
            continue
        if re.match(r"^(title|chapter|passage|part|the end)\b[^.!?]*:?$", stripped,
                    re.IGNORECASE):
            continue
        if re.match(r"^(title|chapter|passage|part)\s*\d*\s*:", stripped,
                    re.IGNORECASE):
            stripped = stripped.split(":", 1)[1].strip()
            if not stripped:
                continue
        lines.append(stripped)
    out = " ".join(lines).strip().strip('"').strip()
    return re.sub(r"\s+", " ", out)


async def _title(sdk: PluginSDK, story: dict[str, Any], first_passage: str) -> str:
    raw = await generate(
        sdk,
        "Give this bedtime story a short, gentle title of at most six words. "
        "Reply with the title only.\n\n" + first_passage[:1200],
        max_tokens=20, temperature=0.5,
    )
    title = clean_passage(raw).splitlines()[0] if raw else ""
    title = title.strip(" .\"'")[:80]
    return title or (story["premise"][:60] or "A bedtime story")


async def _summarize(sdk: PluginSDK, summary: str, passage: str) -> str:
    raw = await generate(
        sdk,
        "Update the summary of a bedtime story. Keep the characters' names, "
        "where they are and what has happened, in at most four sentences."
        f"\n\nSummary so far: {summary or '(the story just began)'}"
        f"\n\nNew passage: {passage}\n\nUpdated summary:",
        max_tokens=180, temperature=0.3,
    )
    return clean_passage(raw)[:1200] or summary


async def ideas(sdk: PluginSDK, *, tone: str = "cozy", audience: str = "kids",
                hint: str = "") -> list[str]:
    """Three story ideas for the page's "surprise me"."""
    if not sdk.core_config.ai_features:
        raise StoryError("Storytime needs the AI features, which are turned off.")
    raw = await generate(
        sdk,
        f"Suggest three different bedtime story ideas for "
        f"{_audience_words(audience)}, {_tone_words(tone)}."
        + (f" Loosely inspired by: {hint}." if hint.strip() else "")
        + " One idea per line, each a single sentence of at most 18 words. "
        "No numbering, no titles, nothing else.",
        max_tokens=160, temperature=1.0,
    )
    out: list[str] = []
    for line in raw.splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line)
        line = clean_passage(line)
        if len(line) > 8:
            out.append(line[:200])
    return out[:3]


# ─── the writer ───────────────────────────────────────────────────────────

def _wps(story: dict[str, Any]) -> float:
    if story["audio_sec"] > 30 and story["words"] > 50:
        return max(1.2, min(4.0, story["words"] / story["audio_sec"]))
    return DEFAULT_WPS


def plan_next(story: dict[str, Any], now: datetime) -> tuple[bool, int, bool, bool]:
    """(write_now, words, final, winding_down) for the story's next
    passage. Writes while less than ``LEAD_SEC`` of audio is ahead of the
    listener; the last passage is the one that brings the story to its
    length."""
    target = story["minutes"] * 60.0
    started = story["playback_started_at"]
    played = (now - started).total_seconds() if started else 0.0
    ahead = story["audio_sec"] - max(0.0, played)
    if ahead >= LEAD_SEC:
        return (False, 0, False, False)
    wps = _wps(story)
    remaining = target - story["audio_sec"]
    passage_sec = PASSAGE_WORDS / wps
    final = remaining <= passage_sec * 1.5
    if final:
        words = int(max(80, min(PASSAGE_WORDS * 1.5, remaining * wps)))
    else:
        words = PASSAGE_WORDS
    winding_down = story["audio_sec"] >= target * 0.7
    return (True, words, final, winding_down)


def wav_seconds(wav: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate() or 1)
    except wave.Error:
        return 0.0


async def _encode_passage(sdk: PluginSDK, story: dict[str, Any], wav: bytes,
                          out: Path, title: str) -> None:
    """WAV -> OGG next to the sleep sounds, over the sleep sound when the
    story has one (picked up where the last passage left it)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    wav_tmp = out.with_suffix(".wav.tmp")
    wav_tmp.write_bytes(wav)
    try:
        bed = None
        if story["bed_sound"]:
            bed = sounds.resolve(sdk.config, sdk.core_config.music_dir,
                                 story["bed_sound"])
        pad = f"apad=pad_dur={PASSAGE_GAP_SEC}"
        if bed is None:
            args = ["-i", str(wav_tmp), "-af", pad, "-ac", "1"]
        else:
            bed_path = sounds.sounds_dir(
                sdk.core_config.music_dir, sdk.config.sounds_subdir
            ) / bed.filename
            loop_sec = max(60.0, float(sdk.config.file_length_min) * 60.0)
            offset = story["audio_sec"] % loop_sec
            args = [
                "-i", str(wav_tmp),
                "-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", str(bed_path),
                "-filter_complex",
                f"[0:a]aresample=44100,{pad}[v];"
                f"[1:a]aresample=44100,volume={BED_GAIN}[b];"
                "[v][b]amix=inputs=2:duration=first:normalize=0[m]",
                "-map", "[m]", "-ac", "1",
            ]
        await sounds._encode_ogg(args, out, title, timeout=300.0)
    finally:
        wav_tmp.unlink(missing_ok=True)


async def write_passage(sdk: PluginSDK, story: dict[str, Any], *, words: int,
                        final: bool, winding_down: bool) -> dict[str, Any]:
    """Write, voice and file one passage; record it. Returns the
    segment row."""
    raw = await generate(
        sdk,
        passage_prompt(story, words=words, final=final, winding_down=winding_down),
        max_tokens=int(words * 2.2) + 60,
    )
    passage = clean_passage(raw)
    if len(passage.split()) < 15:
        raise RuntimeError("the model wrote almost nothing")

    n = story["segments"] + 1
    title = story["title"]
    if n == 1:
        title = await _title(sdk, story, passage)

    wav = await sdk.speech.render(passage, room_id=story["room_id"])
    duration = wav_seconds(wav) + PASSAGE_GAP_SEC
    filename = segment_filename(story["id"], n)
    out = stories_dir(sdk.config, sdk.core_config.music_dir) / filename
    await _encode_passage(sdk, story, wav, out, f"{title} ({n})")

    summary = story["summary"]
    if not final:
        try:
            summary = await _summarize(sdk, summary, passage)
        except Exception as e:  # noqa: BLE001 - the last passage still carries it
            log.warning("storytime: summary for story %s failed: %s", story["id"], e)

    async with sdk.db.session_scope() as s:
        await s.execute(
            text(
                f"INSERT INTO {SCHEMA}.story_segments "
                "(story_id, n, filename, duration_sec, text) "
                "VALUES (:id, :n, :f, :d, :t)"
            ),
            {"id": story["id"], "n": n, "f": filename, "d": duration, "t": passage},
        )
        await s.execute(
            text(
                f"UPDATE {SCHEMA}.stories SET title = :title, summary = :summary, "
                "last_passage = :passage, segments = :n, "
                "audio_sec = audio_sec + :d, words = words + :w, "
                "writing_done = :final, "
                "status = CASE WHEN status = 'writing' THEN 'playing' ELSE status END, "
                "playback_started_at = COALESCE(playback_started_at, NOW()), "
                "updated_at = NOW() WHERE id = :id"
            ),
            {"id": story["id"], "title": title, "summary": summary,
             "passage": passage, "n": n, "d": duration,
             "w": len(passage.split()), "final": final},
        )
        await sdk.realtime.notify(s, "sessions_changed", "story")
    return {"n": n, "filename": filename, "duration_sec": duration}


async def _to_room(sdk: PluginSDK, story: dict[str, Any], seg: dict[str, Any]) -> None:
    """Put a fresh passage in the story's room: the first one (or one
    after the room ran dry) starts it, later ones join its queue."""
    room_id = story["room_id"]
    cfg = sdk.config
    uri = segment_uri(cfg, seg["filename"])
    client = await sdk.playback.mpd_client_for(room_id)
    await client.update_paths([uri])

    active = await sessions.active_session(sdk, room_id)
    ours = active is not None and story_id_of(active["sound_key"]) == story["id"]
    state = None
    if ours:
        try:
            state = await client.state()
        except Exception:  # noqa: BLE001
            state = None
    if ours and state != "stop":
        for _ in range(20):          # the player indexes the new file
            if await client.queue_add_tracks([{"file_path": uri}]):
                break
            await asyncio.sleep(0.5)
        else:
            log.warning("storytime: room %s never found %s", room_id, uri)
        return
    if seg["n"] > 1 and not ours:
        return                       # the room moved on; the writer stops it
    for _ in range(20):
        resp = await sdk.playback.play_url(
            room_id, uri, title=story_title(story), artist=None,
            source=sessions.NOW_PLAYING_SOURCE,
            now_playing_data={"sound": session_key(story["id"])},
            record_play=False,
        )
        if resp.music_action == "start":
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"the {room_id} player wouldn't start the story")
    await sessions.start_story_session(
        sdk, room_id=room_id, key=session_key(story["id"]),
        title=story_title(story), mpd_uri=uri,
    )
    await sessions.deliver(sdk, resp, room_id)


def story_title(story: dict[str, Any]) -> str:
    return story["title"] or "Bedtime story"


async def _finish_in_room(sdk: PluginSDK, story: dict[str, Any]) -> None:
    """After the ending is queued: the sleep sound carries on under the
    sleep timer, or the room simply plays out the last passage."""
    room_id = story["room_id"]
    if not story["bed_sound"]:
        return
    bed = sounds.resolve(sdk.config, sdk.core_config.music_dir, story["bed_sound"])
    if bed is None:
        return
    client = await sdk.playback.mpd_client_for(room_id)
    await client.queue_add_tracks([{"file_path": sounds.mpd_uri(sdk.config, bed)}])
    fresh = await get(sdk, story["id"]) or story
    started = fresh["playback_started_at"] or _now()
    stop_at = (started + timedelta(seconds=fresh["audio_sec"])
               + timedelta(minutes=int(sdk.config.default_timer_min)))
    await sessions.set_stop_at(sdk, room_id, stop_at)


async def _still_wanted(sdk: PluginSDK, story: dict[str, Any], now: datetime) -> bool:
    """False (and the row says why) for a story nobody is listening to."""
    if story["room_id"] is None:
        if (now - story["last_seen_at"]).total_seconds() > BROWSER_IDLE_SEC:
            await _update(sdk, story["id"], status="stopped", notify="story")
            return False
        return True
    if story["segments"] == 0:
        return True                  # not started in the room yet
    active = await sessions.active_session(sdk, story["room_id"])
    if active is None or story_id_of(active["sound_key"]) != story["id"]:
        await _update(
            sdk, story["id"], notify="story",
            status="done" if story["writing_done"] else "stopped",
        )
        return False
    return True


async def _settle_finished(sdk: PluginSDK, story: dict[str, Any], now: datetime) -> None:
    """A fully written story is done once it has played out."""
    if story["room_id"] is None:
        started = story["playback_started_at"] or now
        idle = (now - story["last_seen_at"]).total_seconds() > BROWSER_IDLE_SEC
        if idle or (now - started).total_seconds() > story["audio_sec"] + 60:
            await _update(sdk, story["id"], status="done", notify="story")
        return
    await _still_wanted(sdk, story, now)


async def tick(sdk: PluginSDK) -> None:
    """One writer pass: each live story gets at most one passage, oldest
    first, then old files are tidied away. Serialized: a kick and the
    worker's tick never write the same passage twice."""
    lock = sdk.state.setdefault("story_lock", asyncio.Lock())
    async with lock:
        for story in await live_stories(sdk):
            now = _now()
            try:
                if story["writing_done"]:
                    await _settle_finished(sdk, story, now)
                    continue
                if not await _still_wanted(sdk, story, now):
                    continue
                write, words, final, winding = plan_next(story, now)
                if not write:
                    continue
                seg = await write_passage(
                    sdk, story, words=words, final=final, winding_down=winding,
                )
                story = await get(sdk, story["id"]) or story
                if story["status"] not in LIVE:
                    continue         # stopped while it was being written
                if story["room_id"]:
                    await _to_room(sdk, story, seg)
                    if final:
                        await _finish_in_room(sdk, story)
            except Exception as e:  # noqa: BLE001 - one story's failure is its own
                log.exception("storytime: story %s failed", story["id"])
                failed = story["segments"] == 0
                await _update(
                    sdk, story["id"], notify="story",
                    error=str(e)[:300],
                    **({"status": "failed"} if failed else {}),
                )
                if not failed:
                    # A passage failed mid-story: count it written so the
                    # story ends on what it has rather than looping.
                    await _update(sdk, story["id"], writing_done=True)
        await _tidy(sdk)


async def _tidy(sdk: PluginSDK) -> None:
    """Delete the files of stories that finished more than ``KEEP_HOURS``
    ago (their rows stay, without segments)."""
    cutoff = _now() - timedelta(hours=KEEP_HOURS)
    async with sdk.db.session_scope() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT g.story_id, g.filename FROM {SCHEMA}.story_segments g "
                    f"JOIN {SCHEMA}.stories t ON t.id = g.story_id "
                    "WHERE t.status NOT IN ('writing', 'playing') "
                    "AND t.updated_at < :cutoff"
                ),
                {"cutoff": cutoff},
            )
        ).all()
        if not rows:
            return
        folder = stories_dir(sdk.config, sdk.core_config.music_dir)
        for _story_id, filename in rows:
            try:
                (folder / filename).unlink(missing_ok=True)
            except OSError as e:
                log.debug("storytime: could not delete %s: %s", filename, e)
        await s.execute(
            text(
                f"DELETE FROM {SCHEMA}.story_segments WHERE story_id IN ("
                f"SELECT id FROM {SCHEMA}.stories "
                "WHERE status NOT IN ('writing', 'playing') AND updated_at < :cutoff)"
            ),
            {"cutoff": cutoff},
        )
