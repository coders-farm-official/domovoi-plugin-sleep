"""Test harness for the sleep plugin.

* ``USE_STUBS=true`` is forced BEFORE any domovoi import.
* ``DATABASE_URL`` is pinned to the ``_test`` sibling of the configured
  DB and the run REFUSES a DB whose name doesn't end in ``_test`` —
  same guard as core.
* The plugin's own migrations are applied to the test DB at session
  start through the CORE migration runner.
* ``stub_sdk`` — no-DB unit tier (regex, catalog, handler tests).
* ``live_sdk`` — DB-backed tier (sessions.py row/timer tests), with
  recording playback / realtime doubles and a programmable fake MPD
  client so no real player is ever touched.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

os.environ.setdefault("USE_STUBS", "true")

_DEFAULT_DB = "postgresql+asyncpg://domovoi:domovoi@localhost:6432/domovoi"


def _resolve_test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    prod = os.environ.get("DATABASE_URL") or _DEFAULT_DB
    head, sep, dbname = prod.rpartition("/")
    dbname_only, _, query = dbname.partition("?")
    if dbname_only.endswith("_test"):
        return prod
    return f"{head}{sep}{dbname_only}_test" + (f"?{query}" if query else "")


os.environ["DATABASE_URL"] = _resolve_test_database_url()
_dbname = os.environ["DATABASE_URL"].rsplit("/", 1)[-1].partition("?")[0]
if not _dbname.endswith("_test"):
    raise RuntimeError(
        f"Refusing to run tests against database {_dbname!r} — set "
        f"TEST_DATABASE_URL to a database whose name ends with '_test'."
    )

import pytest
import pytest_asyncio
from sqlalchemy import text

from domovoi.config import settings
from domovoi.db.session import SessionLocal, engine
from domovoi.plugins_runtime.migrations import PluginMigrationRunner
from domovoi.sdk.testing import make_stub_sdk

from domovoi_plugin_sleep.settings import SleepSettings


def _db_reachable() -> bool:
    url = settings.database_url
    try:
        tail = url.split("@", 1)[1]
        host_port = tail.split("/", 1)[0]
        host, _, port_s = host_port.rpartition(":")
        port = int(port_s)
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except (OSError, ValueError, IndexError):
        return False


_DB_OK = _db_reachable()

requires_db = pytest.mark.skipif(
    not _DB_OK,
    reason=(
        "Postgres not reachable at DATABASE_URL — start the dev "
        "container and apply the core baseline."
    ),
)


def apply_plugin_migrations() -> None:
    runner = PluginMigrationRunner("sleep", PLUGIN_ROOT / "migrations")
    asyncio.run(runner.apply_all())


if _DB_OK:
    apply_plugin_migrations()


# ─── doubles ─────────────────────────────────────────────────────────────

class FakeRealtime:
    def __init__(self) -> None:
        self.notified: list[tuple[str, str]] = []

    async def notify(self, session, suffix, reason=""):
        self.notified.append((suffix, reason))
        return f"plugin_sleep_{suffix}"


class FakeMPDClient:
    """Programmable per-room MPD double for fade/reconcile tests."""

    def __init__(self) -> None:
        self.volume: int | None = 100
        self.set_calls: list[int] = []
        self._state = "play"
        self._current: dict | None = None

    def program(self, *, state: str = "play", current: dict | None = None) -> None:
        self._state = state
        self._current = current

    async def get_volume(self) -> int | None:
        return self.volume

    async def set_volume(self, level: int) -> None:
        self.set_calls.append(int(level))
        self.volume = int(level)

    async def state(self) -> str:
        return self._state

    async def current_song(self) -> dict | None:
        return self._current


class RecordingNowPlaying:
    def __init__(self) -> None:
        self.cleared: list[tuple[str, str | None]] = []

    def clear(self, room_id: str, source: str | None = None) -> None:
        self.cleared.append((room_id, source))


def make_settings(**overrides) -> SleepSettings:
    """Settings isolated from the host env (SLEEP_* vars would
    otherwise shadow test intent)."""
    base = SleepSettings.model_construct().model_dump()
    base.update(overrides)
    return SleepSettings.model_construct(**base)


class _ClientMap(dict):
    """Per-room fake clients, auto-created on first access so tests can
    program a room's client before any code path has touched it."""

    def __missing__(self, key: str) -> FakeMPDClient:
        self[key] = FakeMPDClient()
        return self[key]


def wire_fake_mpd(sdk) -> "_ClientMap":
    """Give the sdk's playback facade a per-room fake MPD client map."""
    clients = _ClientMap()

    async def mpd_client_for(room_id: str) -> FakeMPDClient:
        return clients[room_id]

    sdk.playback.mpd_client_for = mpd_client_for
    return clients


# ─── stub tier ───────────────────────────────────────────────────────────

@pytest.fixture
def stub_sdk(tmp_path):
    sdk = make_stub_sdk("sleep", data_dir=tmp_path / "data")
    sdk.config = make_settings()
    sdk.realtime = FakeRealtime()
    # Point the core-config music dir at tmp so catalog scans are hermetic.
    sdk.core_config = SimpleNamespace(
        music_dir=str(tmp_path / "music"), use_stubs=True
    )
    return sdk


# ─── live tier (process singletons + real DB) ────────────────────────────

@pytest_asyncio.fixture
async def live_sdk(tmp_path):
    """A real PluginSDK facade on the test DB, with playback / realtime /
    now-playing swapped for recorders (no real MPD, no real NOTIFY)."""
    from domovoi.sdk.facade import build_sdk
    from domovoi.sdk.testing import RecordingPlayback

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE plugin_sleep.sleep_sessions"))

    sdk = build_sdk("sleep", version="1.0.0", data_dir=tmp_path / "data")
    sdk.config = make_settings()
    sdk.realtime = FakeRealtime()
    sdk.playback = RecordingPlayback()
    sdk.now_playing = RecordingNowPlaying()
    sdk.core_config = SimpleNamespace(
        music_dir=str(tmp_path / "music"), use_stubs=True
    )
    yield sdk
    sdk.teardown()
