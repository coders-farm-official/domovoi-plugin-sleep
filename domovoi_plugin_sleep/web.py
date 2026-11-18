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
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
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


# Storytime history (1.4.0): what was told, where, kept for
# stories.HISTORY_DAYS after it ended (the core side's tidy pass deletes
# older rows; this process only reads). Mirrored, like STORY_SIZES.
HISTORY_DAYS = 30
HISTORY_PAGE = 20
HISTORY_PAGE_MAX = 200
#: The room filter's value for "played in a browser" (a story with no room).
BROWSER_ROOM = "__browser__"


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

    # ── Storytime history ─────────────────────────────────────────────
    #
    # Declared before /stories/{story_id}: "history" would otherwise be
    # read as a story id. Device tier, like the other reads (a history
    # says what was told in which room, and when).

    @router.get("/stories/history")
    @device_endpoint
    async def story_history(
        limit: int = Query(HISTORY_PAGE, ge=1, le=HISTORY_PAGE_MAX),
        cursor: str | None = Query(None, max_length=20),
        room: str | None = Query(None, max_length=200),
    ) -> dict[str, Any]:
        """Finished stories, newest first, a page at a time. ``cursor`` is
        the id the previous page ended on (keyset: a story that ends while
        you scroll never shifts a page). ``room`` narrows it to one room,
        or to ``__browser__`` for the stories played in a browser."""
        before = _cursor_id(cursor)
        where, params = _history_where(room)
        async with session_scope() as s:
            rows = (
                await s.execute(
                    text(
                        f"SELECT {_HISTORY_COLS} FROM stories t WHERE {where} "
                        "AND (CAST(:before AS BIGINT) IS NULL OR t.id < :before) "
                        "ORDER BY t.id DESC LIMIT :lim"
                    ),
                    {**params, "before": before, "lim": limit + 1},
                )
            ).mappings().all()
            counts = (
                await s.execute(
                    text(
                        "SELECT COUNT(*), COUNT(*) FILTER (WHERE EXISTS ("
                        "SELECT 1 FROM story_segments g WHERE g.story_id = t.id)) "
                        f"FROM stories t WHERE {where}"
                    ),
                    params,
                )
            ).one()
            total, with_text = counts[0], counts[1]
            rooms = (
                await s.execute(
                    text(
                        "SELECT DISTINCT room_id FROM stories "
                        "WHERE status NOT IN ('writing', 'playing')"
                    )
                )
            ).scalars().all()
        has_more = len(rows) > limit
        items = [_history_item(r) for r in rows[:limit]]
        return {
            "items": items,
            "total": total,
            "has_more": has_more,
            "next_cursor": str(items[-1]["id"]) if has_more else None,
            "keep_days": HISTORY_DAYS,
            # How many of them still have their words (the download-all
            # button shows only when there is something to save).
            "with_text": with_text,
            # The filter's choices: the rooms with history, and whether
            # any story played in a browser.
            "rooms": sorted(r for r in rooms if r is not None),
            "browser": any(r is None for r in rooms),
        }

    @router.get("/stories/history/export")
    @device_endpoint
    async def story_history_export(
        room: str | None = Query(None, max_length=200),
    ) -> Response:
        """Every kept story (or one room's) with its text, as one Markdown
        file. Stories whose text is gone are left out; none at all is a
        409, never an empty file."""
        where, params = _history_where(room)
        async with session_scope() as s:
            rows = (
                await s.execute(
                    text(
                        f"SELECT {_HISTORY_COLS} FROM stories t WHERE {where} "
                        "ORDER BY t.id DESC"
                    ),
                    params,
                )
            ).mappings().all()
            texts = await _passages(s, [r["id"] for r in rows])
        told = [(r, texts[r["id"]]) for r in rows if texts.get(r["id"])]
        if not told:
            raise HTTPException(
                status_code=409,
                detail="There's no story text to save: no story in this list "
                "has its words left.",
            )
        if not room:
            part = ""
        elif room == BROWSER_ROOM:
            part = "-browser"
        else:
            part = f"-{_slug(room) or 'room'}"
        today = datetime.now().astimezone()
        return _markdown_response(
            _history_markdown(told, room),
            f"bedtime-stories{part}-{today:%Y-%m-%d}.md",
        )

    @router.get("/stories/{story_id}/text")
    @device_endpoint
    async def story_text(story_id: int) -> dict[str, Any]:
        """One story's history entry and its passages' text, in order
        (empty once its words are gone)."""
        row, passages = await _story_with_text(story_id)
        return {"story": _history_item(row), "passages": passages}

    @router.get("/stories/{story_id}/export")
    @device_endpoint
    async def story_export(story_id: int) -> Response:
        """One story as a Markdown file. A story with no text left is a
        409 saying so, never an empty file."""
        row, passages = await _story_with_text(story_id)
        if not passages:
            raise HTTPException(
                status_code=409,
                detail="This story's words are gone, so there's nothing to save.",
            )
        item = _history_item(row)
        slug = _slug(item["title"]) or "bedtime-story"
        when = _local(row["created_at"])
        return _markdown_response(
            _story_markdown(item, passages, level=1), f"{slug}-{when:%Y-%m-%d}.md"
        )

    async def _story_with_text(story_id: int) -> tuple[Any, list[str]]:
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(f"SELECT {_HISTORY_COLS} FROM stories t WHERE t.id = :id"),
                    {"id": story_id},
                )
            ).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="no such story")
            texts = await _passages(s, [story_id])
        return row, texts.get(story_id, [])

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


# ─── Storytime history ───────────────────────────────────────────────────

# ``has_text``: whether any passage's words are still kept (the text goes
# with the row after HISTORY_DAYS; stories tidied by 1.3.x lost theirs at
# 12 hours, and a story that failed before its first passage has none).
_HISTORY_COLS = (
    "t.id, t.room_id, t.title, t.premise, t.minutes, t.status, t.error, "
    "t.segments, t.audio_sec, t.words, t.playback_started_at, t.created_at, "
    "COALESCE(t.ended_at, t.updated_at) AS ended_at, "
    "EXISTS (SELECT 1 FROM story_segments g WHERE g.story_id = t.id) AS has_text"
)


def _cursor_id(cursor: str | None) -> int | None:
    if cursor in (None, ""):
        return None
    try:
        value = int(cursor)
    except ValueError:
        value = 0
    if value < 1:
        raise HTTPException(status_code=400, detail="that page cursor isn't one of ours")
    return value


def _history_where(room: str | None) -> tuple[str, dict[str, Any]]:
    """Finished stories only (a live one is on the Storytime card), and
    one room's, or the browser's, when the filter asks."""
    where = "t.status NOT IN ('writing', 'playing')"
    if not room:
        return where, {}
    if room == BROWSER_ROOM:
        return where + " AND t.room_id IS NULL", {}
    return where + " AND t.room_id = :room", {"room": room}


def _ran_sec(row: Any) -> float:
    """How long the story played: all of it when it finished; up to the
    moment it was stopped otherwise (never more than was written)."""
    audio = float(row["audio_sec"] or 0)
    started = row["playback_started_at"]
    if started is None or audio <= 0:
        return 0.0
    if row["status"] == "done":
        return audio
    ended = row["ended_at"]
    if ended is None:
        return audio
    return max(0.0, min(audio, (ended - started).total_seconds()))


def _history_item(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "title": row["title"] or "",
        "premise": row["premise"] or "",
        "minutes": row["minutes"],
        "status": row["status"],
        "error": row["error"] if row["status"] == "failed" else None,
        "passages": row["segments"],
        "words": row["words"],
        "ran_sec": round(_ran_sec(row)),
        "has_text": bool(row["has_text"]),
        "created_at": row["created_at"].isoformat(),
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
    }


async def _passages(session: AsyncSession, ids: list[int]) -> dict[int, list[str]]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT story_id, text FROM story_segments "
                "WHERE story_id = ANY(:ids) ORDER BY story_id, n"
            ),
            {"ids": ids},
        )
    ).all()
    out: dict[int, list[str]] = {}
    for story_id, passage in rows:
        if passage and passage.strip():
            out.setdefault(story_id, []).append(passage.strip())
    return out


def _local(dt: datetime) -> datetime:
    """An instant on the house's wall clock: the server's own zone, the
    one "what time is it" answers in."""
    return dt.astimezone()


def _when_words(dt: datetime) -> str:
    local = _local(dt)
    hour = local.hour % 12 or 12
    ampm = "am" if local.hour < 12 else "pm"
    return f"{local:%A} {local.day} {local:%B %Y}, {hour}:{local:%M} {ampm}"


def _length_words(sec: float) -> str:
    minutes = int(round(sec / 60))
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    h, m = divmod(minutes, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} min" if m else "")


def _ending_words(item: dict[str, Any]) -> str:
    if item["status"] == "done":
        return "finished"
    if item["status"] == "stopped":
        return "stopped early"
    if item["status"] == "failed":
        return "couldn't be told" + (f": {item['error']}" if item.get("error") else "")
    return item["status"]


def _where_words(room_id: str | None) -> str:
    return room_id if room_id else "played in a browser"


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|!~])")


def _md_line(value: str) -> str:
    """One line of plain text that Markdown won't read as markup."""
    value = re.sub(r"\s+", " ", value or "").strip()
    return _MD_SPECIAL.sub(r"\\\1", value)


def _md_paragraph(value: str) -> str:
    value = _md_line(value)
    # A paragraph that starts like a list item stays a paragraph.
    return re.sub(r"^([-+]|\d+[.)])(\s)", r"\\\1\2", value)


def _story_markdown(item: dict[str, Any], passages: list[str], *, level: int) -> str:
    title = item["title"] or "Bedtime story"
    meta = [
        _where_words(item["room_id"]),
        _when_words(datetime.fromisoformat(item["created_at"])),
        f"ran {_length_words(item['ran_sec'])}" if item["ran_sec"] > 0 else "didn't play",
        _ending_words(item),
    ]
    lines = [f"{'#' * level} {_md_line(title)}", ""]
    lines.append("*" + _md_line(" · ".join(meta)) + "*")
    if item["premise"]:
        lines += ["", "*About: " + _md_line(item["premise"]) + "*"]
    for passage in passages:
        lines += ["", _md_paragraph(passage)]
    return "\n".join(lines) + "\n"


def _history_markdown(told: list[tuple[Any, list[str]]], room: str | None) -> str:
    if not room:
        heading = "Bedtime stories"
    elif room == BROWSER_ROOM:
        heading = "Bedtime stories played in a browser"
    else:
        heading = f"Bedtime stories in {room}"
    count = len(told)
    parts = [
        f"# {_md_line(heading)}",
        "",
        f"*{count} {'story' if count == 1 else 'stories'} from the last "
        f"{HISTORY_DAYS} days, saved {_md_line(_when_words(datetime.now().astimezone()))}*",
    ]
    for row, passages in told:
        parts += ["", "---", "", _story_markdown(_history_item(row), passages, level=2).rstrip("\n")]
    return "\n".join(parts) + "\n"


def _slug(value: str) -> str:
    """A filename-safe slug: ASCII letters, digits and dashes, at most 60."""
    ascii_ = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return slug[:60].rstrip("-")


def _markdown_response(md: str, filename: str) -> Response:
    """A Markdown download: attachment, a .md name (plain ASCII, so the
    quoted form needs no escaping), UTF-8, and never cached (the history
    changes)."""
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
