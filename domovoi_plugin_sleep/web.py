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

from domovoi.webkit import device_endpoint

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


class StoryBody(BaseModel):
    room_id: str | None = Field(default=None, min_length=1)
    size: str | None = None
    minutes: int | None = Field(default=None, ge=1, le=180)
    premise: str = Field(default="", max_length=500)
    tone: str = "cozy"
    audience: str = "kids"
    bed_sound: str | None = None


class IdeasBody(BaseModel):
    tone: str = "cozy"
    audience: str = "kids"
    hint: str = Field(default="", max_length=200)

# The story sizes (stories.SIZES — this process can't import the core
# side's modules, so the page's copy lives here).
STORY_SIZES = [
    {"key": "s", "label": "S", "minutes": 3},
    {"key": "m", "label": "M", "minutes": 10},
    {"key": "l", "label": "L", "minutes": 20},
    {"key": "xl", "label": "XL", "minutes": 40},
    {"key": "huge", "label": "Huge", "minutes": 90},
]


def register_web(ctx: Any) -> None:
    ctx.add_router(build_router(ctx))


def build_router(ctx: Any) -> APIRouter:
    """Split from ``register_web`` so tests can mount the router against
    a fake context object (``db_session_scope`` + ``core``)."""
    router = APIRouter()
    session_scope = ctx.db_session_scope

    # ── Overview (the Sleep page's one fetch) ─────────────────────────
    #
    # Device tier (REV-14): the active sessions say which rooms have
    # somebody asleep and until when — presence, a paired device's read.
    # The page reads it with the household token like every other read.
    @router.get("/overview")
    @device_endpoint
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
            room_stories = await _read_room_stories(s)

        core_online = True
        waiting_for_restart: dict[str, Any] | None = None
        catalog: list[dict[str, Any]] = []
        defaults: dict[str, Any] = {}
        generating = False
        generator_error: str | None = None
        try:
            snd = await ctx.core.get("/v1/plugins/sleep/sounds")
            catalog = snd.get("sounds", [])
            generating = bool(snd.get("generating"))
            if isinstance(snd.get("defaults"), dict):
                defaults = {
                    "default_timer_min": snd["defaults"].get("default_timer_min"),
                    "file_length_min": snd["defaults"].get("file_length_min"),
                }
            else:
                # A core still running the previous version (an upgrade
                # staged for restart) has no defaults on /sounds; its
                # /state is still open, so ask that once.
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
            # The core may be up with this plugin's routes gone: an upgrade
            # installed from the Plugins page waits for a restart, and until
            # then the core serves none of the plugin (plugin board F-P026:
            # the page said "core service unreachable" while it answered).
            waiting_for_restart = await _pending_restart()

        return {
            "rooms": rooms,
            "sessions": sessions,
            "stories": room_stories,
            "story_sizes": STORY_SIZES,
            "sounds": catalog,
            "generating": generating,
            "generator_error": generator_error,
            "defaults": defaults,
            "core_online": core_online,
            "waiting_for_restart": waiting_for_restart,
        }

    async def _pending_restart() -> dict[str, Any] | None:
        """``{from_version, to_version}`` when the core says this plugin
        was upgraded and loads at the next restart, else None (including
        when the core does not answer at all)."""
        try:
            status = await ctx.core.get("/v1/plugins/sleep/status")
        except Exception:  # noqa: BLE001 — really unreachable
            return None
        pending = status.get("pending_restart") if isinstance(status, dict) else None
        if not isinstance(pending, dict):
            return None
        return {
            "from_version": pending.get("from_version"),
            "to_version": pending.get("to_version") or status.get("version"),
        }

    # ── Actions (proxied to the plugin's CORE endpoints) ──────────────
    #
    # play/stop are household actions on BOTH hops (device tier: the
    # household token or an admin Bearer), so a paired phone or browser
    # can use them without the admin password, and the caller's
    # credential is forwarded to the core route, which checks it again.
    # regenerate is admin-gated — the same forwarding.

    @router.post("/play")
    @device_endpoint
    async def play(body: PlayBody, request: Request) -> dict[str, Any]:
        return await ctx.core.post_admin(
            "play", json=body.model_dump(), request=request
        )

    @router.post("/stop")
    @device_endpoint
    async def stop(body: StopBody, request: Request) -> dict[str, Any]:
        return await ctx.core.post_admin(
            "stop", json=body.model_dump(), request=request
        )

    # ── Storytime ─────────────────────────────────────────────────────

    @router.post("/stories")
    @device_endpoint
    async def story_start(body: StoryBody, request: Request) -> dict[str, Any]:
        return await ctx.core.post_admin(
            "stories", json=body.model_dump(), request=request
        )

    @router.post("/stories/ideas")
    @device_endpoint
    async def story_ideas(body: IdeasBody, request: Request) -> dict[str, Any]:
        return await ctx.core.post_admin(
            "stories/ideas", json=body.model_dump(), request=request
        )

    @router.post("/stories/{story_id}/stop")
    @device_endpoint
    async def story_stop(story_id: int, request: Request) -> dict[str, Any]:
        return await ctx.core.post_admin(f"stories/{story_id}/stop", request=request)

    @router.get("/stories/{story_id}")
    @device_endpoint
    async def story_read(story_id: int) -> dict[str, Any]:
        """A story's state and its passages so far. A browser playing the
        story polls this; the read is its heartbeat (the writer stops a
        browser story nobody polls)."""
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT id, room_id, title, premise, minutes, bed_sound, "
                        "status, writing_done, segments, audio_sec, error, created_at "
                        "FROM stories WHERE id = :id"
                    ),
                    {"id": story_id},
                )
            ).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="no such story")
            if row["room_id"] is None and row["status"] in ("writing", "playing"):
                await s.execute(
                    text("UPDATE stories SET last_seen_at = NOW() WHERE id = :id"),
                    {"id": story_id},
                )
            segs = (
                await s.execute(
                    text(
                        "SELECT n, duration_sec FROM story_segments "
                        "WHERE story_id = :id ORDER BY n"
                    ),
                    {"id": story_id},
                )
            ).mappings().all()
        return {
            "story": _isoformat(dict(row)),
            "segments": [
                {
                    "n": g["n"],
                    "duration_sec": g["duration_sec"],
                    "src": f"/api/plugins/sleep/stories/{story_id}/segments/{g['n']}/stream",
                }
                for g in segs
            ],
        }

    @router.get("/stories/{story_id}/segments/{n}/stream")
    async def story_segment_stream(story_id: int, n: int):
        """One passage's audio for the browser player (the same posture as
        the sounds' stream route: a local file, served from disk)."""
        async with session_scope() as s:
            filename = (
                await s.execute(
                    text(
                        "SELECT filename FROM story_segments "
                        "WHERE story_id = :id AND n = :n"
                    ),
                    {"id": story_id, "n": n},
                )
            ).scalar()
        if not filename:
            raise HTTPException(status_code=404, detail="no such passage")
        try:
            snd = await ctx.core.get("/v1/plugins/sleep/sounds")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — CoreDown et al. → 503
            raise HTTPException(
                status_code=503, detail=f"core service unreachable: {e}"
            )
        if not snd.get("dir"):
            raise HTTPException(status_code=503, detail="no sounds folder")
        root = (Path(snd["dir"]) / "Stories").resolve(strict=False)
        target = (root / filename).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes the stories folder")
        if not target.is_file():
            raise HTTPException(status_code=410, detail="that passage was tidied away")
        return FileResponse(str(target), media_type="audio/ogg")

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


def _isoformat(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}


async def _read_room_stories(session: AsyncSession) -> list[dict[str, Any]]:
    """Live stories that belong to a room (a browser story is its own
    page's business), so a room card can say "writing…" before the
    first passage reaches the room."""
    rows = (
        await session.execute(
            text(
                "SELECT id, room_id, title, premise, minutes, status, "
                "writing_done, segments, audio_sec, error, created_at "
                "FROM stories WHERE room_id IS NOT NULL "
                "AND (status IN ('writing', 'playing') "
                "     OR (status = 'failed' AND updated_at > NOW() - INTERVAL '10 minutes')) "
                "ORDER BY id"
            )
        )
    ).mappings().all()
    return [_isoformat(dict(r)) for r in rows]


# ─── Realtime snapshot (manifest [[realtime]] wiring) ────────────────────
#
# Broadcast VERBATIM on ``sleep.sessions``. stop_at is a fixed timestamp
# per session — countdowns are computed in the browser, so the payload
# only changes when a session actually starts/stops.


async def snapshot_sessions(session: AsyncSession) -> dict[str, Any]:
    # Room stories ride along so a card's "writing…" turns into the story
    # (or its failure) without a reload.
    return {
        "sessions": await _read_sessions(session),
        "stories": await _read_room_stories(session),
    }


SNAPSHOTS = {
    "snapshot_sessions": snapshot_sessions,
}
