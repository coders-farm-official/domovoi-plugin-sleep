"""Web-entry tests: import hygiene, the overview read surface, snapshot
contract, and the core-proxy wiring — against a duck-typed
WebPluginContext."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from domovoi.plugins_runtime.manifest import (
    check_web_import_hygiene,
    parse_manifest_dir,
)
from domovoi.sdk.facade import PluginDB

from domovoi_plugin_sleep import web as web_mod

from tests.conftest import requires_db

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_web_import_hygiene_clean() -> None:
    manifest = parse_manifest_dir(PLUGIN_ROOT)
    assert check_web_import_hygiene(PLUGIN_ROOT, manifest) == []


def test_snapshots_dict_matches_manifest() -> None:
    manifest = parse_manifest_dir(PLUGIN_ROOT)
    declared = {r.snapshot for r in manifest.realtime if r.snapshot}
    assert declared <= set(web_mod.SNAPSHOTS)


class _RecordingCore:
    def __init__(self) -> None:
        self.fail = False
        self.posts: list[tuple[str, dict | None, bool]] = []  # (path, json, had_request)
        self.sounds_payload: dict | None = None      # override for stream tests
        self.status_payload: dict = {"startup_hooks": []}
        self.plugin_routes_gone = False

    async def get(self, path: str, *, params=None):
        if self.fail:
            raise RuntimeError("core service unreachable")
        if self.plugin_routes_gone and not path.endswith("/status"):
            # An upgrade staged for restart: the core answers, but none
            # of this plugin's routes are mounted until the restart.
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Not Found")
        if path.endswith("/sounds"):
            return self.sounds_payload or {
                "sounds": [{"key": "brown noise", "title": "Brown noise",
                            "builtin": True, "filename": "brown-noise.ogg"}],
                "generating": False,
            }
        if path.endswith("/status"):
            return self.status_payload
        return {"sessions": [], "default_timer_min": 45, "file_length_min": 60}

    async def post(self, path: str, *, json=None):
        self.posts.append((path, json, False))
        return {"ok": True, "proxied": path}

    async def post_admin(self, path: str, *, json=None, request=None):
        self.posts.append((path, json, request is not None))
        return {"ok": True, "proxied": path}


class _Ctx:
    def __init__(self) -> None:
        self.slug = "sleep"
        self.db_session_scope = PluginDB("sleep").session_scope
        self.core = _RecordingCore()
        self.routers: list = []

    def add_router(self, router) -> None:
        self.routers.append(router)


@pytest.fixture
def web_client():
    ctx = _Ctx()
    web_mod.register_web(ctx)
    assert ctx.routers, "register_web must add a router"
    app = FastAPI()
    app.include_router(ctx.routers[0], prefix="/api/plugins/sleep")
    return ctx, TestClient(app)


async def _seed() -> None:
    async with PluginDB("sleep").session_scope() as s:
        await s.execute(text("TRUNCATE sleep_sessions"))
        await s.execute(
            text(
                "INSERT INTO public.mpd_rooms "
                "(room_id, control_port, http_port, container_name) "
                "VALUES ('bedroom', 16650, 18050, 'test-mpd-bedroom') "
                "ON CONFLICT (room_id) DO NOTHING"
            )
        )
        await s.execute(
            text(
                "INSERT INTO sleep_sessions "
                "(room_id, sound_key, title, mpd_uri, stop_at, active) "
                "VALUES ('bedroom', 'brown noise', 'Brown noise', "
                "'Ambient sounds/brown-noise.ogg', NOW() + interval '30 min', TRUE)"
            )
        )


@requires_db
def test_overview_reads_rooms_sessions_and_core(web_client) -> None:
    import asyncio
    asyncio.run(_seed())

    ctx, client = web_client
    resp = client.get("/api/plugins/sleep/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert "bedroom" in body["rooms"]
    assert body["core_online"] is True
    assert body["sounds"][0]["key"] == "brown noise"
    assert body["defaults"]["default_timer_min"] == 45
    (session,) = [s for s in body["sessions"] if s["room_id"] == "bedroom"]
    assert session["sound_key"] == "brown noise"
    assert session["stop_at"]                    # ISO timestamp, not a countdown


@requires_db
def test_overview_degrades_when_core_down(web_client) -> None:
    import asyncio
    asyncio.run(_seed())

    ctx, client = web_client
    ctx.core.fail = True
    resp = client.get("/api/plugins/sleep/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["core_online"] is False
    assert "bedroom" in body["rooms"]            # DB half still renders
    assert body["sounds"] == []


@requires_db
def test_overview_says_an_upgrade_waits_for_a_restart(web_client) -> None:
    """F-P026: after "I trust this publisher — upgrade" the core unloads the
    plugin until a restart; its routes 404 while the core itself answers.
    The page said "core service unreachable". The overview now reports the
    staged upgrade so the page can say "restart to finish the upgrade"."""
    import asyncio
    asyncio.run(_seed())

    ctx, client = web_client
    ctx.core.plugin_routes_gone = True
    ctx.core.status_payload = {
        "slug": "sleep", "version": "1.2.6", "startup_hooks": [],
        "pending_restart": {"slug": "sleep", "from_version": "1.2.5",
                            "to_version": "1.2.6", "since": "2026-10-17T03:00:00Z",
                            "where": ["core"]},
    }
    body = client.get("/api/plugins/sleep/overview").json()
    assert body["core_online"] is False            # its controls stay off
    assert body["waiting_for_restart"] == {"from_version": "1.2.5", "to_version": "1.2.6"}
    assert "bedroom" in body["rooms"]


@requires_db
def test_a_core_that_does_not_answer_is_still_unreachable(web_client) -> None:
    import asyncio
    asyncio.run(_seed())

    ctx, client = web_client
    ctx.core.fail = True
    body = client.get("/api/plugins/sleep/overview").json()
    assert body["core_online"] is False
    assert body["waiting_for_restart"] is None


def test_play_and_stop_proxy_forwards_the_callers_credential(web_client) -> None:
    ctx, client = web_client
    resp = client.post(
        "/api/plugins/sleep/play",
        json={"room_id": "bedroom", "sound": "brown noise", "minutes": 30},
    )
    assert resp.status_code == 200
    resp = client.post(
        "/api/plugins/sleep/stop", json={"room_id": "bedroom"}
    )
    assert resp.status_code == 200
    # Device tier on both hops: the household token the caller presented
    # rides to the core route, which checks it again.
    paths = [(p, had) for p, _, had in ctx.core.posts]
    assert paths == [("play", True), ("stop", True)]
    # The play body rode through the proxy intact.
    assert ctx.core.posts[0][1]["minutes"] == 30


def test_regenerate_forwards_admin_credential(web_client) -> None:
    ctx, client = web_client
    resp = client.post("/api/plugins/sleep/regenerate")
    assert resp.status_code == 200
    assert ("regenerate", None, True) in ctx.core.posts


# ─── Browser stream route ([[web.player_sources]]) ───────────────────────

def _sounds_payload(tmp_path: Path, filename: str = "brown-noise.ogg") -> dict:
    return {
        "sounds": [{"key": "brown noise", "title": "Brown noise",
                    "builtin": True, "filename": filename}],
        "dir": str(tmp_path),
        "generating": False,
    }


def test_stream_serves_sound_file(web_client, tmp_path) -> None:
    ctx, client = web_client
    (tmp_path / "brown-noise.ogg").write_bytes(b"OggS fake audio")
    ctx.core.sounds_payload = _sounds_payload(tmp_path)
    resp = client.get("/api/plugins/sleep/sounds/brown%20noise/stream")
    assert resp.status_code == 200
    assert resp.content == b"OggS fake audio"
    assert resp.headers["content-type"].startswith("audio/ogg")


def test_stream_unknown_sound_404(web_client, tmp_path) -> None:
    ctx, client = web_client
    ctx.core.sounds_payload = _sounds_payload(tmp_path)
    resp = client.get("/api/plugins/sleep/sounds/whale%20songs/stream")
    assert resp.status_code == 404


def test_stream_refuses_path_escape(web_client, tmp_path) -> None:
    ctx, client = web_client
    (tmp_path / "secret.txt").write_text("nope")
    inside = tmp_path / "sounds"
    inside.mkdir()
    payload = _sounds_payload(inside, filename="../secret.txt")
    ctx.core.sounds_payload = payload
    resp = client.get("/api/plugins/sleep/sounds/brown%20noise/stream")
    assert resp.status_code == 400


def test_stream_missing_file_409(web_client, tmp_path) -> None:
    ctx, client = web_client
    ctx.core.sounds_payload = _sounds_payload(tmp_path)   # file never written
    resp = client.get("/api/plugins/sleep/sounds/brown%20noise/stream")
    assert resp.status_code == 409


def test_stream_core_down_503(web_client) -> None:
    ctx, client = web_client
    ctx.core.fail = True
    resp = client.get("/api/plugins/sleep/sounds/brown%20noise/stream")
    assert resp.status_code == 503


@requires_db
def test_overview_surfaces_generator_error(web_client) -> None:
    """Empty catalog + failed hook → the page gets the honest reason
    instead of rendering silently empty (the exact state a first-boot
    ffmpeg failure leaves behind)."""
    ctx, client = web_client
    ctx.core.sounds_payload = {"sounds": [], "generating": False}
    ctx.core.status_payload = {
        "startup_hooks": [
            {"name": "sleep.sleep_generator", "state": "failed",
             "error": "ffmpeg failed: Unable to choose an output format"}
        ]
    }
    resp = client.get("/api/plugins/sleep/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert "Unable to choose an output format" in body["generator_error"]


@requires_db
async def test_snapshot_shape_is_diff_stable() -> None:
    """stop_at is a fixed timestamp per session; only updated_at churns
    — so the snapshot payload must not change between polls."""
    await _seed()
    async with PluginDB("sleep").session_scope() as s:
        snap1 = await web_mod.snapshot_sessions(s)
    async with PluginDB("sleep").session_scope() as s:
        await s.execute(text("UPDATE sleep_sessions SET updated_at = NOW()"))
    async with PluginDB("sleep").session_scope() as s:
        snap2 = await web_mod.snapshot_sessions(s)
    assert snap1 == snap2
    assert snap1["sessions"][0]["room_id"] == "bedroom"
