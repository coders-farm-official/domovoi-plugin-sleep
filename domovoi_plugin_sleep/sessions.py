"""Session + timer orchestration — the one place that writes
``sleep_sessions``.

One row per room (upserted): what's playing, since when, and when to
stop. The timer worker enforces ``stop_at`` with a gentle volume fade,
and its tick doubles as the reconciliation sweep: a session whose room
is no longer playing our file (user said "stop the music", file reached
its end, satellite skipped away) is marked inactive so the dashboard
and voice status never lie. Durability lives in the row, not in any
in-memory task — a core restart mid-nap changes nothing.

Fade discipline: the satellite hardware mixer is the household's single
volume control and core pins MPD at 100% (double-attenuation
invariant). The fade deliberately dips that for a few seconds, then
RESTORES the pre-fade level right after the stop — and if anything dies
mid-fade, the provisioner re-pins volume on the next container start
and any play/volume command re-pins it too. Bounded blast risk: none;
bounded quiet risk: one pin away.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from domovoi.sdk import PluginSDK, Response

from domovoi_plugin_sleep import SCHEMA, sounds

log = logging.getLogger(__name__)

NOW_PLAYING_SOURCE = "sleep"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── row access ───────────────────────────────────────────────────────────

async def active_sessions(sdk: PluginSDK) -> list[dict[str, Any]]:
    async with sdk.db.session_scope() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT room_id, sound_key, title, mpd_uri, started_at, "
                    f"stop_at, active FROM {SCHEMA}.sleep_sessions "
                    "WHERE active ORDER BY room_id"
                )
            )
        ).mappings().all()
        return [dict(r) for r in rows]


async def active_session(sdk: PluginSDK, room_id: str) -> dict[str, Any] | None:
    """The room's live sleep session, or None when this plugin is not
    the one playing there. Callers use it to leave other playback
    alone."""
    async with sdk.db.session_scope() as s:
        row = (
            await s.execute(
                text(
                    f"SELECT room_id, sound_key, title, mpd_uri, started_at, "
                    f"stop_at, active FROM {SCHEMA}.sleep_sessions "
                    "WHERE room_id = :room AND active"
                ),
                {"room": room_id},
            )
        ).mappings().first()
        return dict(row) if row else None


async def _upsert_session(
    sdk: PluginSDK,
    *,
    room_id: str,
    sound: sounds.Sound,
    mpd_uri: str,
    stop_at: datetime | None,
) -> None:
    await _upsert_row(
        sdk, room_id=room_id, key=sound.key, title=sound.title,
        mpd_uri=mpd_uri, stop_at=stop_at,
    )


async def start_story_session(
    sdk: PluginSDK, *, room_id: str, key: str, title: str, mpd_uri: str,
) -> None:
    """A story started in ``room_id`` (stories.py): the same session row
    a sound gets, so stop, the fade and the page all work on it. No timer
    — the story ends when its last passage does (or, with a sleep sound
    under it, ``set_stop_at`` sets one then)."""
    await _upsert_row(
        sdk, room_id=room_id, key=key, title=title, mpd_uri=mpd_uri, stop_at=None,
    )


async def set_stop_at(sdk: PluginSDK, room_id: str, stop_at: datetime | None) -> None:
    async with sdk.db.session_scope() as s:
        await s.execute(
            text(
                f"UPDATE {SCHEMA}.sleep_sessions SET stop_at = :stop_at, "
                "updated_at = NOW() WHERE room_id = :room AND active"
            ),
            {"room": room_id, "stop_at": stop_at},
        )
        await sdk.realtime.notify(s, "sessions_changed", "timer")


async def _upsert_row(
    sdk: PluginSDK,
    *,
    room_id: str,
    key: str,
    title: str,
    mpd_uri: str,
    stop_at: datetime | None,
) -> None:
    async with sdk.db.session_scope() as s:
        await s.execute(
            text(
                f"""
                INSERT INTO {SCHEMA}.sleep_sessions
                    (room_id, sound_key, title, mpd_uri, started_at,
                     stop_at, active, updated_at)
                VALUES (:room, :key, :title, :uri, NOW(), :stop_at, TRUE, NOW())
                ON CONFLICT (room_id) DO UPDATE SET
                    sound_key = :key, title = :title, mpd_uri = :uri,
                    started_at = NOW(), stop_at = :stop_at,
                    active = TRUE, updated_at = NOW()
                """
            ),
            {"room": room_id, "key": key, "title": title,
             "uri": mpd_uri, "stop_at": stop_at},
        )
        await sdk.realtime.notify(s, "sessions_changed", "started")


async def _deactivate(sdk: PluginSDK, room_id: str, reason: str) -> None:
    async with sdk.db.session_scope() as s:
        result = await s.execute(
            text(
                f"UPDATE {SCHEMA}.sleep_sessions "
                "SET active = FALSE, updated_at = NOW() "
                "WHERE room_id = :room AND active"
            ),
            {"room": room_id},
        )
        if (result.rowcount or 0) > 0:
            await sdk.realtime.notify(s, "sessions_changed", reason)


# ─── start / stop ─────────────────────────────────────────────────────────

async def start(
    sdk: PluginSDK,
    *,
    room_id: str,
    sound: sounds.Sound,
    timer_min: int | None,
) -> Response:
    """Queue the sound in the room's player and record the session.
    ``timer_min``: None = the configured default; 0 = play to file end.
    Returns play_url's Response — the caller returns it AS-IS (it may
    overwrite ``.text``); on player failure it carries no
    ``music_action`` and no session row is written."""
    cfg = sdk.config
    uri = sounds.mpd_uri(cfg, sound)

    resp = await sdk.playback.play_url(
        room_id,
        uri,
        title=sound.title,
        artist=None,
        source=NOW_PLAYING_SOURCE,
        now_playing_data={"sound": sound.key},
        record_play=True,
    )
    if resp.music_action != "start":
        return resp                      # player refused; keep the row honest

    minutes = int(cfg.default_timer_min) if timer_min is None else int(timer_min)
    # The file itself is the fail-safe ceiling; a timer can only shorten.
    minutes = min(minutes, int(cfg.file_length_min)) if minutes > 0 else 0
    stop_at = _now() + timedelta(minutes=minutes) if minutes > 0 else None

    await _upsert_session(
        sdk, room_id=room_id, sound=sound, mpd_uri=uri, stop_at=stop_at
    )
    return resp


async def stop(sdk: PluginSDK, room_id: str, *, fade: bool = True) -> Response:
    """Fade (optionally) and stop the room, then deactivate the session."""
    if fade and float(sdk.config.fade_out_sec) > 0:
        try:
            await _fade_down(sdk, room_id, float(sdk.config.fade_out_sec))
        except Exception as e:  # noqa: BLE001 — fade is best-effort
            log.debug("sleep: fade for room=%s failed: %s", room_id, e)
    resp = await sdk.playback.stop(room_id)
    await _restore_volume(sdk, room_id)
    await _deactivate(sdk, room_id, "stopped")
    return resp


async def deliver(sdk: PluginSDK, resp: Response, room_id: str) -> bool:
    """Tell the room's satellite about a start or stop that did NOT come
    from a voice turn: the page's play / stop buttons and the timer.

    ``play_url`` leaves the room's player paused for the satellite's
    music_start -> music_ready handshake; a voice turn's socket sends
    that, an HTTP route or a worker has none, so the page said "playing"
    while the room stayed silent and a timer's stop never reached the
    satellite (plugin board F-P017). The core's SDK 1.6
    ``playback.dispatch`` delivers it the way the dashboard's room
    controls do. An older core has no dispatch: nothing is sent, as
    before. Never call this for a voice handler's Response — the turn
    delivers that one itself."""
    dispatch = getattr(sdk.playback, "dispatch", None)
    if dispatch is None:
        log.info(
            "sleep: this core can't tell room=%s's satellite (no "
            "playback.dispatch before SDK 1.6); the room may stay %s",
            room_id, "silent" if resp.music_action == "start" else "playing",
        )
        return False
    try:
        return bool(await dispatch(resp, room_id))
    except Exception as e:  # noqa: BLE001 — the session row is already right
        log.warning(
            "sleep: telling room=%s's satellite (%s) failed: %s",
            room_id, resp.music_action, e,
        )
        return False


async def _fade_down(sdk: PluginSDK, room_id: str, fade_sec: float) -> None:
    """Ramp the room's software mixer down over ``fade_sec`` in a few
    steps. The pre-fade level is restored by ``_restore_volume`` right
    after the stop (see module docstring for the invariant)."""
    client = await sdk.playback.mpd_client_for(room_id)
    start_level = await client.get_volume()
    if start_level is None or start_level <= 0:
        return
    sdk.state[f"prefade_volume:{room_id}"] = int(start_level)
    steps = 6
    for i in range(1, steps + 1):
        level = int(start_level * (steps - i) / steps)
        await client.set_volume(max(level, 0))
        await asyncio.sleep(fade_sec / steps)


async def _restore_volume(sdk: PluginSDK, room_id: str) -> None:
    level = sdk.state.pop(f"prefade_volume:{room_id}", None)
    if level is None:
        return
    try:
        client = await sdk.playback.mpd_client_for(room_id)
        await client.set_volume(int(level))
    except Exception as e:  # noqa: BLE001 — the provisioner pin is the net
        log.debug("sleep: volume restore for room=%s failed: %s", room_id, e)


# ─── the timer tick ───────────────────────────────────────────────────────

async def enforce(sdk: PluginSDK) -> None:
    """One worker tick: stop due sessions (fade included, timed so the
    sound is silent AT ``stop_at``), then reconcile rows against what
    the rooms are actually playing."""
    fade_sec = float(sdk.config.fade_out_sec)

    for session in await active_sessions(sdk):
        room_id = session["room_id"]
        stop_at = session["stop_at"]

        # 1. Timer due? Start the fade early enough to finish on time.
        if stop_at is not None:
            due_at = stop_at - timedelta(seconds=fade_sec)
            if _now() >= due_at:
                log.info("sleep: timer elapsed for room=%s", room_id)
                await deliver(sdk, await stop(sdk, room_id, fade=True), room_id)
                continue

        # 2. Reconcile: is the room still playing OUR file? "pause" is
        #    kept — that's the satellite handshake window right after
        #    start, and a user-paused nap sound shouldn't lose its timer.
        try:
            client = await sdk.playback.mpd_client_for(room_id)
            state = await client.state()
            current = await client.current_song() or {}
        except Exception as e:  # noqa: BLE001 — player unreachable ≠ session over
            log.debug("sleep: reconcile skip room=%s: %s", room_id, e)
            continue
        if await _story_still_playing(sdk, session, state, current):
            continue
        if state == "stop" or current.get("file") != session["mpd_uri"]:
            # File ended on its own, or something else took the room.
            sdk.now_playing.clear(room_id, source=NOW_PLAYING_SOURCE)
            await _deactivate(sdk, room_id, "ended")


async def _story_still_playing(
    sdk: PluginSDK, session: dict[str, Any], state: str, current: dict[str, Any]
) -> bool:
    """A story session plays many files: any of its passages, or the
    sleep sound queued after its ending, still counts. A room that ran
    dry while the next passage is being written is waiting, not done.
    False for a sound's session (the caller's own check applies)."""
    from domovoi_plugin_sleep import stories   # stories imports this module

    story_id = stories.story_id_of(session["sound_key"])
    if story_id is None:
        return False
    story = await stories.get(sdk, story_id)
    if story is None or story["status"] not in stories.LIVE:
        return False
    if state == "stop":
        return not story["writing_done"]
    playing = current.get("file") or ""
    if playing.startswith(stories.story_uri_prefix(sdk.config, story_id)):
        return True
    if story["bed_sound"]:
        bed = sounds.resolve(sdk.config, sdk.core_config.music_dir, story["bed_sound"])
        if bed is not None and playing == sounds.mpd_uri(sdk.config, bed):
            return True
    return False
