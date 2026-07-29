"""Web-process entry point: ``register_web(ctx)``.

Runs in the SEPARATE web dashboard process (:6369). Hard rules, both
tripwired at install and enforced by the web process's import guard:

* imports only ``domovoi.webkit``, stdlib, and packages already present
  in the web process — never ``core.py`` or any ``domovoi.*`` runtime
  module;
* DB access goes through ``ctx.db_session_scope`` (sessions arrive with
  ``search_path = plugin_sleep, public`` preset — which also makes the
  read-only ``public.mpd_rooms`` reference reachable);
* anything needing the live core (playing/stopping a room, the sound
  catalog scan, regeneration) is PROXIED to the plugin's own core
  endpoints via ``ctx.core``.

Router mounts at ``/api/plugins/sleep``; static JSX at
``/plugins/sleep/static``. ``SNAPSHOTS`` feeds the manifest-declared
realtime wiring: ``snapshot_sessions`` broadcasts the active sessions
verbatim on ``sleep.sessions`` — timestamps only (``stop_at``), never a
remaining-seconds field (the page computes countdowns client-side, so
the snapshot stays diff-stable).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Suffix → media type for the browser stream route. Matches the audio
# extensions the catalog accepts for user-dropped files.
_AUDIO_TYPES = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


class PlayBody(BaseModel):
    room_id: str = Field(..., min_length=1)
    sound: str = Field(..., min_length=1)
    minutes: int | None = Field(default=None, ge=0, le=1440)


class StopBody(BaseModel):
    room_id: str = Field(..., min_length=1)
    fade: bool = False


def register_web(ctx: Any) -> None:
    ctx.add_router(build_router(ctx))


def build_router(ctx: Any) -> APIRouter:
    """Split from ``register_web`` so tests can mount the router against
    a fake context object (``db_session_scope`` + ``core``)."""
    router = APIRouter()
    session_scope = ctx.db_session_scope

    # ── Overview (the Sleep page's one fetch) ─────────────────────────
    @router.get("/overview")
    async def overview() -> dict[str, Any]:
        """Rooms + active sessions from the DB; catalog + defaults from
        the core (its failure degrades to core_online=false with an
        empty catalog — the page still renders rooms and sessions)."""
        async with session_scope() as s:
            rooms = [
                r[0] for r in (
                    await s.execute(
                        text("SELECT room_id FROM public.mpd_rooms ORDER BY room_id")
                    )
                ).all()
            ]
            sessions = await _read_sessions(s)

        core_online = True
        catalog: list[dict[str, Any]] = []
        defaults: dict[str, Any] = {}
        generating = False
        generator_error: str | None = None
        try:
            snd = await ctx.core.get("/v1/plugins/sleep/sounds")
            catalog = snd.get("sounds", [])
            generating = bool(snd.get("generating"))
            state = await ctx.core.get("/v1/plugins/sleep/state")
            defaults = {
                "default_timer_min": state.get("default_timer_min"),
                "file_length_min": state.get("file_length_min"),
            }
            if not catalog and not generating:
                # Empty catalog with nothing running — the one state
                # where the generator hook's failure is the page's whole
                # story, so pull it from the plugin status (extra core
                # round-trip only on this already-broken path; its own
                # failure just leaves the error unexplained).
                try:
                    status = await ctx.core.get("/v1/plugins/sleep/status")
                    for hook in status.get("startup_hooks", []):
                        if hook.get("name", "").endswith("sleep_generator"):
                            generator_error = hook.get("error")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001 — core down is a rendered state
            log.debug("sleep overview: core fetch failed: %s", e)
            core_online = False

        return {
            "rooms": rooms,
            "sessions": sessions,
            "sounds": catalog,
            "generating": generating,
            "generator_error": generator_error,
            "defaults": defaults,
            "core_online": core_online,
        }

    # ── Actions (proxied to the plugin's CORE endpoints) ──────────────
    #
    # play/stop are open on the core side (daily-use, same as voice);
    # regenerate is admin-gated — forward the caller's credential.

    @router.post("/play")
    async def play(body: PlayBody) -> dict[str, Any]:
        return await ctx.core.post("play", json=body.model_dump())

    @router.post("/stop")
    async def stop(body: StopBody) -> dict[str, Any]:
        return await ctx.core.post("stop", json=body.model_dump())

    @router.post("/regenerate")
    async def regenerate(request: Request) -> dict[str, Any]:
        return await ctx.core.post_admin("regenerate", request=request)

    # ── Browser stream (the manifest's [[web.player_sources]] route) ──
    #
    # The dashboard player streams sounds through this route so the
    # sleep page can play in the browser — same posture as radio's
    # stream proxy (§9.1), except the "stream" is a local file: the
    # catalog + folder come from the core (it owns MUSIC_DIR), the
    # bytes are served from disk here (web and core share the host).

    @router.get("/sounds/{key}/stream")
    async def sound_stream(key: str):
        try:
            snd = await ctx.core.get("/v1/plugins/sleep/sounds")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — CoreDown et al. → 503
            raise HTTPException(
                status_code=503, detail=f"core service unreachable: {e}"
            )
        match = next(
            (s for s in snd.get("sounds", []) if s.get("key") == key), None
        )
        if match is None or not snd.get("dir"):
            raise HTTPException(
                status_code=404, detail=f"unknown sound {key!r}"
            )
        # Containment: user-dropped filenames come from a directory scan,
        # but never trust a path that leaves the sounds folder.
        root = Path(snd["dir"]).resolve(strict=False)
        target = (root / match.get("filename", "")).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="path escapes the sounds folder"
            )
        if not target.is_file():
            raise HTTPException(
                status_code=409,
                detail=f"sound file missing on disk: {target.name}",
            )
        media_type = _AUDIO_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        return FileResponse(str(target), media_type=media_type)

    return router


# ─── Rows → payload ──────────────────────────────────────────────────────

async def _read_sessions(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT room_id, sound_key, title, started_at, stop_at "
                "FROM sleep_sessions WHERE active ORDER BY room_id"
            )
        )
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        out.append(d)
    return out


# ─── Realtime snapshot (manifest [[realtime]] wiring) ────────────────────
#
# Broadcast VERBATIM on ``sleep.sessions``. stop_at is a fixed timestamp
# per session — countdowns are computed in the browser, so the payload
# only changes when a session actually starts/stops.


async def snapshot_sessions(session: AsyncSession) -> dict[str, Any]:
    return {"sessions": await _read_sessions(session)}


SNAPSHOTS = {
    "snapshot_sessions": snapshot_sessions,
}
