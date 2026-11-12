"""Full loader round-trip: the REAL PluginLoader imports the package,
runs register(), executes every contract check (manifest/code drift,
corpus collisions against the core registry + every sibling plugin's
declared phrases), and tears down cleanly on unload."""

from __future__ import annotations

from pathlib import Path

import pytest

from domovoi import bootstrap
from domovoi.handlers import HANDLER_BY_NAME
from domovoi.now_playing import NOW_PLAYING
from domovoi.plugins_runtime.loader import LOADER
from domovoi.plugins_runtime.manifest import parse_manifest_dir
from domovoi.plugins_runtime.workers import WORKERS

from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Sibling plugins' declared corpora — the fence around band 275: radio's
# "play 97.5 fm" (280) and music's "play the beatles" (core corpus,
# checked automatically) must keep resolving to their owners.
FOREIGN = [
    ("play 97.5 fm", "radio"),
    ("tune to the news station", "radio"),
    ("start the video server", "jellyfin"),
    ("is the video server running", "jellyfin"),
    ("start the game server", "romm"),
    ("is the game server running", "romm"),
]


async def test_load_register_contracts_unload() -> None:
    bootstrap.register_nvidia_dlls()
    manifest = parse_manifest_dir(PLUGIN_ROOT)

    lp = await LOADER.load_plugin(
        slug="sleep",
        install_dir=PLUGIN_ROOT,
        manifest=manifest,
        foreign_corpus=FOREIGN,
        update_registry_status=False,
    )
    try:
        # Voice plane live, at the declared band.
        handler = HANDLER_BY_NAME.get("sleep")
        assert handler is not None and handler.plugin_slug == "sleep"
        assert handler.priority_band == 275

        # Now-playing plane live (both declared capabilities).
        assert "sleep" in NOW_PLAYING.sources()
        assert any(slug == "sleep" for slug, _ in NOW_PLAYING.matchers())

        # Worker + startup hook registered under the owner.
        assert WORKERS.worker_names("sleep") == {
            "sleep_timer": "poll", "story_writer": "poll",
        }
        assert WORKERS.hook_names("sleep") == ["sleep_generator"]

        # Config contribution live on the SDK facade.
        assert lp.sdk.config is not None
        assert lp.sdk.config.sounds_subdir == "Ambient sounds"
        assert lp.sdk.config.default_timer_min == 45
    finally:
        await LOADER.unload_plugin("sleep")

    # Clean teardown: nothing of ours survives.
    assert "sleep" not in HANDLER_BY_NAME
    assert "sleep" not in NOW_PLAYING.sources()
    assert WORKERS.worker_names("sleep") == {}
    assert WORKERS.hook_names("sleep") == []
