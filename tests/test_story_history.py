"""Storytime history (1.4.0): retention, the paged history read, room
names, and the Markdown downloads.

Retention: a finished story's audio files go KEEP_HOURS after it ended,
its row and passage text HISTORY_DAYS after; a live story is never
touched. The history lists finished stories newest first by a keyset
cursor (the story id), names rooms the way the Sleep page does (by the
room's name; a browser story says so), and downloads as .md files —
never an empty one.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from domovoi.db.session import engine
from domovoi.sdk.facade import PluginDB

from domovoi_plugin_sleep import stories
from domovoi_plugin_sleep import web as web_mod

from conftest import requires_db


def test_the_page_and_the_tidy_pass_keep_the_same_days():
    assert web_mod.HISTORY_DAYS == stories.HISTORY_DAYS == 30
    assert stories.KEEP_HOURS == 12


# ─── seeding ──────────────────────────────────────────────────────────────

async def _wipe() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE plugin_sleep.stories CASCADE"))


async def _seed_story(*, room_id: str | None = "kids_room", title: str = "The Sleepy Fox",
                      premise: str = "", status: str = "done", minutes: int = 10,
                      ended_ago: timedelta | None = timedelta(hours=1),
                      ran: float = 600.0, audio_sec: float = 600.0,
                      passages: list[str] | None = None, error: str | None = None,
                      legacy: bool = False) -> int:
    """One story row (+ its passages). ``ended_ago`` None = still live.
    ``legacy`` leaves ended_at NULL, the way a 1.3.x row arrives."""
    now = datetime.now(timezone.utc)
    ended = None if ended_ago is None else now - ended_ago
    started = (ended or now) - timedelta(seconds=ran)
    updated = ended or now
    async with PluginDB("sleep").session_scope() as s:
        story_id = (
            await s.execute(
                text(
                    "INSERT INTO stories (room_id, title, premise, minutes, status, "
                    "segments, audio_sec, words, playback_started_at, error, "
                    "created_at, updated_at, ended_at) VALUES (:room, :title, :premise, "
                    ":min, :status, :segs, :audio, :words, :started, :error, "
                    ":created, :updated, :ended) RETURNING id"
                ),
                {"room": room_id, "title": title, "premise": premise, "min": minutes,
                 "status": status, "segs": len(passages or []), "audio": audio_sec,
                 "words": sum(len(p.split()) for p in passages or []),
                 "started": started if ran > 0 else None, "error": error,
                 "created": started - timedelta(seconds=20), "updated": updated,
                 "ended": None if legacy else ended},
            )
        ).scalar_one()
        for n, passage in enumerate(passages or [], start=1):
            await s.execute(
                text(
                    "INSERT INTO story_segments (story_id, n, filename, duration_sec, text) "
                    "VALUES (:id, :n, :f, 100, :t)"
                ),
                {"id": story_id, "n": n, "f": stories.segment_filename(story_id, n),
                 "t": passage},
            )
    return story_id


async def _row(story_id: int):
    async with PluginDB("sleep").session_scope() as s:
        return (
            await s.execute(
                text("SELECT status, ended_at, files_tidied FROM stories WHERE id = :id"),
                {"id": story_id},
            )
        ).mappings().first()


async def _texts(story_id: int) -> list[str]:
    async with PluginDB("sleep").session_scope() as s:
        return list((
            await s.execute(
                text("SELECT text FROM story_segments WHERE story_id = :id ORDER BY n"),
                {"id": story_id},
            )
        ).scalars().all())


# ─── retention ────────────────────────────────────────────────────────────

def _write_files(sdk, story_id: int, n: int) -> list:
    folder = stories.stories_dir(sdk.config, sdk.core_config.music_dir)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(1, n + 1):
        f = folder / stories.segment_filename(story_id, i)
        f.write_bytes(b"OggS")
        out.append(f)
    return out


@requires_db
@pytest.mark.asyncio
async def test_files_go_at_12_hours_and_the_words_stay(live_sdk):
    sdk = live_sdk
    old = await _seed_story(ended_ago=timedelta(hours=13), passages=["One.", "Two."])
    fresh = await _seed_story(ended_ago=timedelta(hours=1), passages=["Three."])
    old_files = _write_files(sdk, old, 2)
    fresh_files = _write_files(sdk, fresh, 1)

    await stories._tidy(sdk)

    assert not any(f.exists() for f in old_files)
    assert await _texts(old) == ["One.", "Two."]          # the words stay
    assert (await _row(old))["files_tidied"] is True
    assert all(f.exists() for f in fresh_files)
    assert (await _row(fresh))["files_tidied"] is False


@requires_db
@pytest.mark.asyncio
async def test_rows_and_words_go_at_30_days(live_sdk):
    sdk = live_sdk
    gone = await _seed_story(ended_ago=timedelta(days=31), passages=["Old words."])
    legacy = await _seed_story(ended_ago=timedelta(days=31), legacy=True)
    kept = await _seed_story(ended_ago=timedelta(days=29), passages=["Kept words."])

    await stories._tidy(sdk)

    assert await _row(gone) is None and await _texts(gone) == []
    assert await _row(legacy) is None        # a 1.3.x row ages by its last update
    assert await _texts(kept) == ["Kept words."]


@requires_db
@pytest.mark.asyncio
async def test_live_stories_are_never_touched(live_sdk):
    sdk = live_sdk
    ids = []
    for status in stories.LIVE:
        sid = await _seed_story(status=status, ended_ago=None, passages=["Still telling."])
        async with engine.begin() as conn:      # an ancient, still-live row
            await conn.execute(
                text("UPDATE plugin_sleep.stories SET updated_at = NOW() - INTERVAL '40 days', "
                     "created_at = NOW() - INTERVAL '40 days' WHERE id = :id"),
                {"id": sid},
            )
        ids.append((sid, _write_files(sdk, sid, 1)))

    await stories._tidy(sdk)

    for sid, files in ids:
        assert (await _row(sid))["files_tidied"] is False
        assert await _texts(sid) == ["Still telling."]
        assert all(f.exists() for f in files)


@requires_db
@pytest.mark.asyncio
async def test_a_story_that_ends_records_when(live_sdk):
    sdk = live_sdk
    sid = await _seed_story(status="playing", ended_ago=None)
    assert (await _row(sid))["ended_at"] is None
    await stories.mark_stopped(sdk, sid)
    row = await _row(sid)
    assert row["status"] == "stopped" and row["ended_at"] is not None

    # A room's next story ends the one before it, and says when.
    other = await _seed_story(status="playing", ended_ago=None, room_id="den")
    await stories._end_room_stories(sdk, "den")
    assert (await _row(other))["ended_at"] is not None


# ─── the web read ────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self) -> None:
        self.slug = "sleep"
        self.db_session_scope = PluginDB("sleep").session_scope
        self.core = None
        self.routers: list = []

    def add_router(self, router) -> None:
        self.routers.append(router)


@pytest.fixture
def client():
    asyncio.run(_wipe())
    ctx = _Ctx()
    web_mod.register_web(ctx)
    app = FastAPI()
    app.include_router(ctx.routers[0], prefix="/api/plugins/sleep")
    return TestClient(app)


H = "/api/plugins/sleep/stories/history"


@requires_db
def test_history_pages_newest_first_by_cursor(client):
    ids = [asyncio.run(_seed_story(title=f"Story {i}", passages=["Words."])) for i in range(5)]
    live = asyncio.run(_seed_story(status="playing", ended_ago=None))

    first = client.get(H, params={"limit": 2}).json()
    assert [s["id"] for s in first["items"]] == [ids[4], ids[3]]
    assert first["has_more"] is True and first["next_cursor"] == str(ids[3])
    assert first["total"] == 5 and first["keep_days"] == 30

    second = client.get(H, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [s["id"] for s in second["items"]] == [ids[2], ids[1]]

    # A story that finishes while you scroll doesn't shift the next page.
    asyncio.run(_seed_story(title="Newest"))
    last = client.get(H, params={"limit": 2, "cursor": second["next_cursor"]}).json()
    assert [s["id"] for s in last["items"]] == [ids[0]]
    assert last["has_more"] is False and last["next_cursor"] is None

    every = client.get(H, params={"limit": 50}).json()["items"]
    assert live not in [s["id"] for s in every]          # live: the Storytime card's

    assert client.get(H, params={"cursor": "abc"}).status_code == 400
    assert client.get(H, params={"limit": 500}).status_code == 422


@requires_db
def test_history_entries_say_where_when_how_long_and_how_it_ended(client):
    asyncio.run(_seed_story(room_id="kids_room", premise="a fox who lost her blanket",
                            passages=["Once."]))
    asyncio.run(_seed_story(room_id=None, title="Browser Tale", status="stopped",
                            ran=240, audio_sec=600, passages=["Twice."]))
    asyncio.run(_seed_story(room_id="den", title="", status="failed", ran=0,
                            audio_sec=0, error="ollama is not running"))

    body = client.get(H).json()
    failed, browser, done = body["items"]
    assert done["room_id"] == "kids_room" and done["premise"] == "a fox who lost her blanket"
    assert done["status"] == "done" and done["ran_sec"] == 600 and done["has_text"] is True
    assert browser["room_id"] is None and browser["status"] == "stopped"
    assert browser["ran_sec"] == 240                     # up to when it was stopped
    assert failed["error"] == "ollama is not running" and failed["ran_sec"] == 0
    assert failed["has_text"] is False
    assert body["rooms"] == ["den", "kids_room"] and body["browser"] is True
    assert body["with_text"] == 2

    only = client.get(H, params={"room": "kids_room"}).json()
    assert [s["room_id"] for s in only["items"]] == ["kids_room"] and only["total"] == 1
    here = client.get(H, params={"room": web_mod.BROWSER_ROOM}).json()
    assert [s["title"] for s in here["items"]] == ["Browser Tale"]

    story = client.get(f"/api/plugins/sleep/stories/{done['id']}/text").json()
    assert story["passages"] == ["Once."] and story["story"]["id"] == done["id"]


def test_markdown_names_rooms_and_browsers():
    item = {"room_id": None, "title": "", "premise": "", "status": "stopped",
            "error": None, "ran_sec": 0,
            "created_at": datetime(2026, 11, 17, 20, 5, tzinfo=timezone.utc).isoformat()}
    md = web_mod._story_markdown(item, ["Words."], level=1)
    assert md.startswith("# Bedtime story\n")
    assert "played in a browser" in md and "didn't play" in md and "stopped early" in md
    md = web_mod._story_markdown({**item, "room_id": "kids_room"}, ["Words."], level=2)
    assert md.startswith("## Bedtime story\n") and "*kids\\_room ·" in md


# ─── the Markdown downloads ──────────────────────────────────────────────

def _assert_md_download(resp, name_re: str) -> str:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment; ")
    name = re.search(r'filename="([^"]+)"', cd).group(1)
    assert re.fullmatch(name_re, name), name
    assert resp.headers["cache-control"] == "no-store"
    return resp.content.decode("utf-8")


@requires_db
def test_one_story_downloads_as_markdown(client):
    sid = asyncio.run(_seed_story(
        title="The Sleepy Fox", premise="a fox — and her *blanket*",
        passages=["The fox yawned.", "- She slept.", "Snow fell, softly."]))

    md = _assert_md_download(client.get(f"/api/plugins/sleep/stories/{sid}/export"),
                             r"the-sleepy-fox-\d{4}-\d{2}-\d{2}\.md")
    lines = md.split("\n")
    assert lines[0] == "# The Sleepy Fox" and lines[1] == ""
    meta = lines[2]
    assert meta.startswith("*kids\\_room · ") and "ran 10 minutes · finished*" in meta
    assert "*About: a fox — and her \\*blanket\\**" in md       # UTF-8, markup escaped
    body = md.split("*About:", 1)[1].split("\n\n")[1:]
    assert body == ["The fox yawned.", "\\- She slept.", "Snow fell, softly.\n"]


@requires_db
def test_a_title_with_no_letters_still_gets_a_safe_md_name(client):
    sid = asyncio.run(_seed_story(title="Ça ✨ / ../..", passages=["Words."]))
    resp = client.get(f"/api/plugins/sleep/stories/{sid}/export")
    _assert_md_download(resp, r"ca-\d{4}-\d{2}-\d{2}\.md")
    sid = asyncio.run(_seed_story(title="✨", passages=["Words."]))
    _assert_md_download(client.get(f"/api/plugins/sleep/stories/{sid}/export"),
                        r"bedtime-story-\d{4}-\d{2}-\d{2}\.md")


@requires_db
def test_a_story_with_no_words_left_refuses_to_download(client):
    sid = asyncio.run(_seed_story(passages=[]))              # tidied by 1.3.x
    resp = client.get(f"/api/plugins/sleep/stories/{sid}/export")
    assert resp.status_code == 409
    assert "words are gone" in resp.json()["detail"]
    assert client.get("/api/plugins/sleep/stories/999999/export").status_code == 404


@requires_db
def test_the_whole_history_downloads_as_one_file(client):
    asyncio.run(_seed_story(title="First", passages=["One."]))
    asyncio.run(_seed_story(title="Wordless", passages=[]))
    asyncio.run(_seed_story(title="Second", room_id=None, passages=["Two."]))

    md = _assert_md_download(client.get(f"{H}/export"),
                             r"bedtime-stories-\d{4}-\d{2}-\d{2}\.md")
    assert md.startswith("# Bedtime stories\n")
    assert "*2 stories from the last 30 days" in md
    assert md.index("## Second") < md.index("## First")    # newest first
    assert "Wordless" not in md

    md = _assert_md_download(client.get(f"{H}/export", params={"room": "__browser__"}),
                             r"bedtime-stories-browser-\d{4}-\d{2}-\d{2}\.md")
    assert "## Second" in md and "## First" not in md

    resp = client.get(f"{H}/export", params={"room": "den"})   # nothing there
    assert resp.status_code == 409 and "no story" in resp.json()["detail"]
