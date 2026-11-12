"""SleepHandler — the plugin's voice surface.

  * "play brown noise" / "play ocean sounds in the bedroom for 30
    minutes" — resolve the sound, queue it in the room's player,
    record the session + timer
  * "play sleep sounds" — the default sound (brown noise)
  * "stop the sounds" / "stop the white noise in the bedroom"
  * "what sounds can you play" — read the catalog
  * Storytime: "tell me a bedtime story about a sleepy dragon", "tell a
    huge story in the kids room", "tell me a story about otters for 20
    minutes", "a 15 minute story" — an AI story that runs that long
    (stories.py); "stop the story". A bare "tell me a story" is left to
    the Q&A model, which tells a short one: only a size, a length or
    "bedtime" makes it Storytime.

Every play pattern is anchored on a trailing "sounds/noise" noun —
that's the fence against MusicHandler's greedy ``^play (.+)$`` at 300
(a song literally titled "Ocean Sounds" is claimed by us by design,
the same tradeoff radio makes for "play 97.5 fm").

# band rationale: 275 — the anchored-media range, between spoken_audio
# (270) and radio (280), well before music's catch-all at 300.

``requires_network = "no"``: the catalog is a directory scan, playback
is the room's local player, timers are a DB row. Fully offline.

Room targeting mirrors the intercom handler's loose matching (spaces /
underscores / hyphens collapsed) against the provisioned rooms — read
from ``public.mpd_rooms``, the documented read-only convention for
core-owned reference data.

DB access: the router hands handlers a session with the CORE default
search_path, so nothing here uses it for plugin tables — row access
goes through sessions.py, which opens its own plugin-scoped sessions.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from domovoi.sdk import (
    Context,
    FastPath,
    Handler,
    HandlerDisplay,
    Intent,
    PluginSDK,
    Response,
)

from domovoi_plugin_sleep import sessions, sounds, stories

log = logging.getLogger(__name__)

_ROOM = r"(?: in (?:the )?(?P<room>[a-z0-9 _'\-]+?))?"
_TIMER = r"(?: for (?P<num>\d+) (?P<unit>minutes?|mins?|hours?|hrs?))?"

# "play [the] <sound> sounds|noise [in the <room>] [for N minutes]"
_PLAY_RE = re.compile(
    rf"^play (?:the |some )?(?P<sound>.+?) (?:sounds?|noise){_ROOM}{_TIMER}$"
)
# "play [some] sleep|ambient sounds ..." — the no-name default form.
_PLAY_DEFAULT_RE = re.compile(
    rf"^play (?:the |some )?(?:sleep|ambient) sounds?{_ROOM}{_TIMER}$"
)
# "stop the [sleep|ambient|<color>|...] sounds|noise [in the <room>]"
_STOP_RE = re.compile(
    rf"^stop (?:the )?(?:sleep |ambient |white |pink |brown |ocean |fan )?"
    rf"(?:sounds?|noise){_ROOM}$"
)
_LIST_RE = re.compile(
    r"^(?:what (?:sleep |ambient )?sounds? (?:are there|can you play|do you have)"
    r"|list (?:the )?(?:sleep|ambient) sounds?)$"
)

DEFAULT_SOUND_KEY = "brown noise"

# ── Storytime ────────────────────────────────────────────────────────────
_SIZE_WORDS = "|".join(
    sorted((re.escape(w) for w in stories.SIZE_WORDS
            if w not in stories.CASUAL_SIZE_WORDS), key=len, reverse=True)
)
_CASUAL_WORDS = "|".join(stories.CASUAL_SIZE_WORDS)
_TELL = (
    r"^(?:(?:can you |could you |will you |would you |please )?"
    r"(?:tell|read|play)(?: me| us| the kids| them| him| her)? )?"
    r"(?:a |an |another )?"
)
_ABOUT = r"(?: (?:about|where|with) (?P<about>.+?))?"
_KIND = r"(?:(?:bedtime|sleepy|sleep|night ?time|good ?night) )"
# "tell me a huge bedtime story about otters in the bedroom",
# "a 20 minute story", "tell me a bedtime story" — sized or bedtime.
_STORY_SIZED_RE = re.compile(
    _TELL
    + rf"(?:(?P<size>{_SIZE_WORDS}) {_KIND}?"
    + rf"|(?P<casual>{_CASUAL_WORDS}) {_KIND}"
    + r"|(?P<num2>\d+)[ -](?P<unit2>minute|min|hour)s? (?:long )?"
    + rf"{_KIND}?|{_KIND})"
    + rf"story(?: for (?:the )?(?:kids|me|us))?{_ABOUT}{_ROOM}{_TIMER}(?: please)?$"
)
# "tell me a story about otters for 20 minutes" — a length makes it ours.
_STORY_TIMED_RE = re.compile(
    _TELL + rf"story{_ABOUT}{_ROOM}"
    + r" for (?P<num>\d+) (?P<unit>minutes?|mins?|hours?|hrs?)(?: please)?$"
)
_STOP_STORY_RE = re.compile(
    rf"^(?:stop|end|cancel) (?:the |this |that )?(?:bedtime )?story{_ROOM}$"
)


class SleepHandler(Handler):
    name = "sleep"
    priority_band = 275
    requires_network = "no"
    display = HandlerDisplay(
        label="Sleep sounds", tone="media", icon="web/static/icon.svg"
    )

    tool_schema = {
        "name": "sleep",
        "description": (
            "Play continuous sleep/ambient sounds (white, pink, or brown "
            "noise, a soft fan, ocean waves, plus any custom sounds the "
            "household added) in a room, with an automatic sleep timer "
            "and fade-out. Also stops them and lists what's available. "
            "Also tells AI bedtime stories that run for a chosen length "
            "(action=story; only when a length, size or 'bedtime' is "
            "asked for). "
            "Example: 'play brown noise in the bedroom for 30 minutes', "
            "'tell a huge bedtime story about otters in the kids room'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "stop", "list", "story", "stop_story"],
                },
                "size": {
                    "type": "string",
                    "enum": ["s", "m", "l", "xl", "huge"],
                    "description": "Story length when action=story: s=3, "
                                   "m=10, l=20, xl=40, huge=90 minutes.",
                },
                "about": {
                    "type": "string",
                    "description": "What the story is about when action=story.",
                },
                "sound": {
                    "type": "string",
                    "description": "Sound name when action=play (e.g. 'brown noise').",
                },
                "room": {
                    "type": "string",
                    "description": "Room name; defaults to where the user spoke.",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Sleep timer in minutes; 0 plays to the end. "
                                   "For action=story, the story's length.",
                },
            },
            "required": ["action"],
        },
    }

    def __init__(self, sdk: PluginSDK) -> None:
        self.sdk = sdk
        self.fast_paths = [
            FastPath(_PLAY_DEFAULT_RE, SleepHandler._play_default_from_match),
            FastPath(_PLAY_RE, SleepHandler._play_from_match),
            FastPath(_STOP_RE, SleepHandler._stop_from_match),
            FastPath(_LIST_RE, SleepHandler._list_from_match),
            FastPath(_STORY_SIZED_RE, SleepHandler._story_from_match),
            FastPath(_STORY_TIMED_RE, SleepHandler._story_from_match),
            FastPath(_STOP_STORY_RE, SleepHandler._stop_story_from_match),
        ]

    # ─── Fast-path adapters ─────────────────────────────────────────────

    async def _play_from_match(self, m, ctx, session):
        return await self._play(
            ctx, session,
            spoken_sound=m.group("sound"),
            spoken_room=m.group("room"),
            minutes=_parse_minutes(m.group("num"), m.group("unit")),
        )

    async def _play_default_from_match(self, m, ctx, session):
        return await self._play(
            ctx, session,
            spoken_sound=None,
            spoken_room=m.group("room"),
            minutes=_parse_minutes(m.group("num"), m.group("unit")),
        )

    async def _stop_from_match(self, m, ctx, session):
        return await self._stop(ctx, session, spoken_room=m.group("room"))

    async def _list_from_match(self, m, ctx, session):
        return self._list(ctx)

    async def _story_from_match(self, m, ctx, session):
        groups = m.groupdict()
        minutes = _parse_minutes(groups.get("num"), groups.get("unit"))
        if minutes is None and groups.get("num2"):
            minutes = _parse_minutes(groups["num2"], groups.get("unit2"))
        return await self._story(
            ctx, session,
            size=groups.get("size") or groups.get("casual"),
            minutes=minutes,
            about=groups.get("about"),
            spoken_room=groups.get("room"),
        )

    async def _stop_story_from_match(self, m, ctx, session):
        return await self._stop_story(ctx, session, spoken_room=m.group("room"))

    # ─── Entry points ───────────────────────────────────────────────────

    async def execute(
        self, intent: Intent, ctx: Context, session: AsyncSession
    ) -> Response:
        # Routed here without a fast-path match — listing is the only
        # safe default (never start noise on a fuzzy match).
        return self._list(ctx)

    async def execute_from_tool(
        self, args: dict[str, Any], ctx: Context, session: AsyncSession
    ) -> Response:
        action = args.get("action")
        if action == "play":
            minutes = args.get("minutes")
            return await self._play(
                ctx, session,
                spoken_sound=args.get("sound"),
                spoken_room=args.get("room"),
                minutes=int(minutes) if isinstance(minutes, int) else None,
            )
        if action == "stop":
            return await self._stop(ctx, session, spoken_room=args.get("room"))
        if action == "story":
            minutes = args.get("minutes")
            return await self._story(
                ctx, session,
                size=args.get("size"),
                minutes=int(minutes) if isinstance(minutes, int) else None,
                about=args.get("about"),
                spoken_room=args.get("room"),
            )
        if action == "stop_story":
            return await self._stop_story(ctx, session, spoken_room=args.get("room"))
        return self._list(ctx)

    # ─── Actions ────────────────────────────────────────────────────────

    async def _play(
        self,
        ctx: Context,
        session: AsyncSession,
        *,
        spoken_sound: str | None,
        spoken_room: str | None,
        minutes: int | None,
    ) -> Response:
        cfg = self.sdk.config
        music_dir = self.sdk.core_config.music_dir

        if spoken_sound is None:
            sound = sounds.resolve(cfg, music_dir, DEFAULT_SOUND_KEY)
        else:
            sound = sounds.resolve(cfg, music_dir, spoken_sound)
        if sound is None:
            available = ", ".join(sounds.catalog(cfg, music_dir)) or None
            if available is None:
                return self._reply(
                    ctx,
                    "The sleep sounds haven't been generated yet — give me "
                    "a minute after startup and try again.",
                )
            asked = spoken_sound or DEFAULT_SOUND_KEY
            return self._reply(
                ctx,
                f"I don't have a sound called {asked}. I can play: {available}.",
            )

        room_id, room_err = await self._resolve_room(session, ctx, spoken_room)
        if room_id is None:
            return self._reply(ctx, room_err)

        resp = await sessions.start(
            self.sdk, room_id=room_id, sound=sound, timer_min=minutes,
        )
        if resp.music_action != "start":
            return resp                 # play_url's honest failure text

        effective = int(cfg.default_timer_min) if minutes is None else int(minutes)
        if effective > 0:
            effective = min(effective, int(cfg.file_length_min))
            tail = f" for {_speak_minutes(effective)}, then I'll fade it out"
        else:
            tail = f" — it winds down on its own after about {_speak_minutes(int(cfg.file_length_min))}"
        resp.text = f"{sound.title} in the {_speak_room(room_id)}{tail}. Sleep well."
        return resp

    async def _stop(
        self, ctx: Context, session: AsyncSession, *, spoken_room: str | None
    ) -> Response:
        room_id, room_err = await self._resolve_room(session, ctx, spoken_room)
        if room_id is None:
            return self._reply(ctx, room_err)
        active = {
            s["room_id"] for s in await sessions.active_sessions(self.sdk)
        }
        if room_id not in active:
            return self._reply(
                ctx, f"No sleep sounds are playing in the {_speak_room(room_id)}."
            )
        resp = await sessions.stop(self.sdk, room_id, fade=False)
        resp.text = "Stopped the sounds."
        return resp

    async def _story(
        self,
        ctx: Context,
        session: AsyncSession,
        *,
        size: str | None,
        minutes: int | None,
        about: str | None,
        spoken_room: str | None,
    ) -> Response:
        room_id, room_err = await self._resolve_room(session, ctx, spoken_room)
        if room_id is None:
            return self._reply(ctx, room_err.replace(
                "play brown noise in the bedroom",
                "tell a bedtime story in the bedroom",
            ))
        if minutes is None:
            minutes = stories.size_minutes(size)
        premise = (about or "").strip()
        try:
            await stories.start(
                self.sdk, room_id=room_id, minutes=minutes, premise=premise,
            )
        except stories.StoryError as e:
            return self._reply(ctx, str(e))
        what = f"a story about {premise}" if premise else "a bedtime story"
        return self._reply(
            ctx,
            f"Okay, {what}, about {_speak_minutes(minutes)} long, in the "
            f"{_speak_room(room_id)}. It'll start in a moment.",
        )

    async def _stop_story(
        self, ctx: Context, session: AsyncSession, *, spoken_room: str | None
    ) -> Response:
        room_id, room_err = await self._resolve_room(session, ctx, spoken_room)
        if room_id is None:
            return self._reply(ctx, room_err)
        active = await sessions.active_session(self.sdk, room_id)
        story_id = stories.story_id_of(active["sound_key"]) if active else None
        live = [
            st for st in await stories.live_stories(self.sdk)
            if st["room_id"] == room_id
        ]
        if story_id is None and not live:
            return self._reply(
                ctx, f"No story is playing in the {_speak_room(room_id)}."
            )
        for st in live:
            await stories.mark_stopped(self.sdk, st["id"])
        if story_id is not None:
            resp = await sessions.stop(self.sdk, room_id, fade=False)
            resp.text = "Okay, the story's over. Goodnight."
            return resp
        return self._reply(ctx, "Okay, I've stopped the story.")

    def _list(self, ctx: Context) -> Response:
        cat = sounds.catalog(self.sdk.config, self.sdk.core_config.music_dir)
        if not cat:
            return self._reply(
                ctx,
                "No sounds are ready yet — they generate shortly after "
                "startup.",
            )
        names = ", ".join(s.title for s in cat.values())
        return self._reply(
            ctx,
            f"I can play: {names}. Say something like 'play brown noise "
            "for 30 minutes'.",
        )

    # ─── Room resolution (intercom's loose-matching convention) ────────

    async def _resolve_room(
        self, session: AsyncSession, ctx: Context, spoken_room: str | None
    ) -> tuple[str | None, str]:
        """(room_id, error_text) — exactly one is meaningful."""
        if spoken_room is None:
            if ctx.room_id:
                return ctx.room_id, ""
            return None, "Which room? Say something like 'play brown noise in the bedroom'."

        rooms = await self._known_rooms(session)
        if not rooms:
            return None, "No rooms are set up yet — connect a satellite first."
        target = _compact(spoken_room)
        for room in rooms:
            if _compact(room) == target:
                return room, ""
        return None, f"I don't know a room called {spoken_room.strip()}."

    async def _known_rooms(self, session: AsyncSession) -> list[str]:
        # Read-only reference read of a core table (same convention as
        # the acquisition correlations in the radio plugin) — writes to
        # core tables stay behind SDK APIs.
        rows = await session.execute(
            text("SELECT room_id FROM public.mpd_rooms ORDER BY room_id")
        )
        return [r[0] for r in rows.all()]

    def _reply(self, ctx: Context, text_: str) -> Response:
        return Response(
            text=text_,
            session_id=ctx.session_id,
            matched_handler=self.name,
        )


# ─── little helpers ──────────────────────────────────────────────────────

def _parse_minutes(num: str | None, unit: str | None) -> int | None:
    if not num:
        return None
    minutes = int(num)
    if unit and unit.startswith(("hour", "hr")):
        minutes *= 60
    return minutes


def _compact(name: str) -> str:
    cleaned = name.strip().lower()
    if cleaned.startswith("the "):
        cleaned = cleaned[4:]
    return cleaned.replace(" ", "").replace("_", "").replace("-", "")


def _speak_room(room_id: str) -> str:
    return room_id.replace("_", " ").replace("-", " ")


def _speak_minutes(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minutes"
