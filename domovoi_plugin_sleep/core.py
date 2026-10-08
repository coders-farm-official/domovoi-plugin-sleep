"""Core-process entry point: ``register(ctx)``.

Wires, in order:

* config (``SLEEP_*`` FieldSpecs) — everything below reads it;
* the ``sleep`` now-playing source + matcher (explicit calls the
  manifest's declared capabilities are contract-checked against);
* the SleepHandler at band 275 (play / stop / list by voice);
* the timer poll worker (timer enforcement + session reconciliation);
* the ``sleep_generator`` startup hook — renders any missing/drifted
  sound files with ffmpeg and tells every room player to rescan. First
  run takes a few minutes (5 sounds x file_length); the handler answers
  honestly ("not generated yet") until it lands. Fully offline, so no
  ``requires_online``;
* the core HTTP router (``/v1/plugins/sleep/...``) — play/stop are
  daily-use ``device_endpoint`` actions (the household token any paired
  browser, phone or satellite sends, or an admin Bearer — the same tier
  radio's play takes); regeneration is admin-gated.

Import discipline: nothing heavy — ffmpeg runs as a subprocess inside
the hook/endpoint bodies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from domovoi.sdk import PluginSDK, device_endpoint

from domovoi_plugin_sleep import SCHEMA, sessions, sounds
from domovoi_plugin_sleep.handlers.sleep import SleepHandler
from domovoi_plugin_sleep.settings import SLEEP_FIELDSPECS, SleepSettings
from domovoi_plugin_sleep.workers.timer import SleepTimer

log = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    sdk: PluginSDK = ctx.sdk

    # ── Config first — everything below reads sdk.config. ──────────────
    ctx.add_config(SleepSettings, SLEEP_FIELDSPECS)

    # ── Now-playing plane: source + file-prefix matcher. ────────────────
    sdk.now_playing.register_source(sessions.NOW_PLAYING_SOURCE)
    sdk.now_playing.register_matcher(
        sessions.NOW_PLAYING_SOURCE, _make_matcher(sdk)
    )

    # ── Voice plane. ────────────────────────────────────────────────────
    ctx.add_handler(SleepHandler(sdk))

    # ── Background work. ────────────────────────────────────────────────
    ctx.add_worker(SleepTimer(sdk))

    async def _generate() -> None:
        """Render missing/drifted sounds, then the MPD rescan fan-out.
        Under stubs (test cores) nothing touches disk or ffmpeg.
        ``regen_running`` is set here too — not just in /regenerate — so
        the dashboard's "rendering…" card and its poll cover the first
        boot, when this render is the whole page's story."""
        if sdk.core_config.use_stubs:
            return
        sdk.state["regen_running"] = True
        try:
            generated = await sounds.generate_builtins(
                sdk.config, sdk.core_config.music_dir
            )
        finally:
            sdk.state["regen_running"] = False
        if generated:
            log.info("sleep: generated %d sound(s): %s",
                     len(generated), ", ".join(generated))
            await sdk.playback.update_library_all_rooms()

    ctx.add_startup_hook(
        _generate, name="sleep_generator", after="core.db_ready",
    )

    # ── HTTP (core-side endpoints). ─────────────────────────────────────
    ctx.add_router(_build_core_router(sdk))

    log.info("sleep plugin registered (band 275, timer worker + generator hook)")


# ─── Now-playing matcher ──────────────────────────────────────────────────


def _make_matcher(sdk: PluginSDK):
    async def match_now_playing(
        session: Any,
        *,
        mpd_file: str | None = None,
        title: str | None = None,
        **_: Any,
    ) -> dict[str, Any] | None:
        """Attribute a room's playing file to a sleep sound by its
        folder prefix; enrich from the active session row when one
        matches. NOTE: this session has the CORE search_path, so the
        plugin table is schema-qualified."""
        prefix = f"{sdk.config.sounds_subdir}/"
        if not mpd_file or not mpd_file.startswith(prefix):
            return None
        row = (
            await session.execute(
                text(
                    f"SELECT room_id, sound_key, title FROM "
                    f"{SCHEMA}.sleep_sessions "
                    "WHERE mpd_uri = :uri AND active LIMIT 1"
                ),
                {"uri": mpd_file},
            )
        ).first()
        return {
            "kind": "sleep",
            "sound": row[1] if row else None,
            "title": (row[2] if row else None) or title
                     or mpd_file.removeprefix(prefix),
        }

    return match_now_playing


# ─── The plugin's core router (/v1/plugins/sleep/...) ────────────────────
#
# play/stop opt OUT of the admin gate — they are the same daily-use
# actions the voice paths perform (listed on the install preview).
# Regeneration mutates files on disk -> stays admin-gated.
# NOTE: /state, not /status — core owns GET /v1/plugins/{slug}/status.


class PlayBody(BaseModel):
    room_id: str = Field(..., min_length=1)
    sound: str = Field(..., min_length=1)
    minutes: int | None = Field(default=None, ge=0, le=1440)


class StopBody(BaseModel):
    room_id: str = Field(..., min_length=1)
    fade: bool = False


def _build_core_router(sdk: PluginSDK) -> APIRouter:
    router = APIRouter()

    @router.get("/sounds")
    async def list_sounds():
        """Catalog + the on-disk location. ``dir``/``filename`` exist for
        the web process's browser-stream route (same host, separate
        process — it can read the files but doesn't know MUSIC_DIR)."""
        cat = sounds.catalog(sdk.config, sdk.core_config.music_dir)
        folder = sounds.sounds_dir(
            sdk.core_config.music_dir, sdk.config.sounds_subdir
        )
        return {
            "sounds": [
                {
                    "key": s.key,
                    "title": s.title,
                    "builtin": s.builtin,
                    "filename": s.filename,
                }
                for s in cat.values()
            ],
            "dir": str(folder),
            "generating": sdk.state.get("regen_running", False),
        }

    @router.get("/state")
    async def sleep_state():
        return {
            "sessions": [_session_payload(s) for s in await sessions.active_sessions(sdk)],
            "default_timer_min": int(sdk.config.default_timer_min),
            "file_length_min": int(sdk.config.file_length_min),
        }

    @router.post("/play")
    @device_endpoint        # daily household action: the household token
    async def play(body: PlayBody):
        sound = sounds.resolve(sdk.config, sdk.core_config.music_dir, body.sound)
        if sound is None:
            raise HTTPException(status_code=404, detail=f"unknown sound {body.sound!r}")
        resp = await sessions.start(
            sdk, room_id=body.room_id, sound=sound, timer_min=body.minutes,
        )
        if resp.music_action != "start":
            raise HTTPException(status_code=502, detail=resp.text)
        return {"ok": True, "room_id": body.room_id, "sound": sound.key}

    @router.post("/stop")
    @device_endpoint
    async def stop(body: StopBody):
        """Stop the sleep sound this plugin started. A room with no
        sleep session is a 404 and its playback is left alone — the
        voice path checks the same thing before it speaks."""
        if await sessions.active_session(sdk, body.room_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"no sleep sound is playing in {body.room_id}",
            )
        await sessions.stop(sdk, body.room_id, fade=body.fade)
        return {"ok": True, "room_id": body.room_id}

    @router.post("/regenerate")
    async def regenerate():
        """Re-render every builtin from scratch (admin; runs in the
        background — rendering is minutes, not milliseconds)."""
        if sdk.state.get("regen_running"):
            return {"ok": True, "already_running": True}

        async def _run() -> None:
            sdk.state["regen_running"] = True
            try:
                folder = sounds.sounds_dir(
                    sdk.core_config.music_dir, sdk.config.sounds_subdir
                )
                sidecar = folder / sounds.SIDECAR_NAME
                if sidecar.exists():
                    sidecar.unlink()
                await sounds.generate_builtins(
                    sdk.config, sdk.core_config.music_dir
                )
                await sdk.playback.update_library_all_rooms()
            except Exception:  # noqa: BLE001 — background isolation
                log.exception("sleep: regeneration failed")
            finally:
                sdk.state["regen_running"] = False

        asyncio.create_task(_run())
        return {"ok": True, "started": True}

    return router


def _session_payload(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out
