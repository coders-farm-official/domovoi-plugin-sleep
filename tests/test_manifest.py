"""Manifest + layout contract: the same parse the install pipeline runs,
plus the declarations the rest of the suite (and core.py) depends on."""

from __future__ import annotations

from pathlib import Path

from domovoi.plugins_runtime.manifest import parse_manifest_dir

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_parses_and_declares_the_contract():
    m = parse_manifest_dir(PLUGIN_ROOT)

    assert m.slug == "sleep"
    assert m.publisher == "Coders Farm"
    assert m.license == "MIT"

    assert m.entry_core == "domovoi_plugin_sleep.core"
    assert m.entry_web == "domovoi_plugin_sleep.web"

    # Explicit now-playing capabilities (contract-checked against the
    # register_source/register_matcher calls in core.py).
    assert set(m.provides) == {"now-playing-source:sleep", "now-playing-matcher"}

    # One handler at 275 — between spoken_audio (270) and radio (280),
    # ahead of music's greedy catch-all at 300. Fully offline.
    (handler,) = m.handlers
    assert handler.name == "sleep"
    assert handler.band == 275
    assert handler.requires_network == "no"
    assert "play white noise" in handler.corpus
    assert "stop the sleep sounds" in handler.corpus

    # Timer poll worker + generator startup hook.
    kinds = {w.name: w.kind for w in m.workers}
    assert kinds == {
        "sleep_timer": "poll",
        "sleep_generator": "startup",
    }

    # Realtime channel is namespaced and maps to the declared snapshot.
    (rt,) = m.realtime
    assert rt.notify_channel == "plugin_sleep_sessions_changed"
    assert rt.realtime_channel == "sleep.sessions"
    assert rt.snapshot == "snapshot_sessions"

    # The Sleep page.
    (page,) = m.web_pages
    assert page.route == "sleep"
    assert page.page == "SleepPage"
    assert page.nav_label == "Sleep"

    # Honesty contract: ffmpeg subprocess + music-dir writes disclosed;
    # and this plugin genuinely needs NO network.
    assert m.permissions.get("subprocess") is True
    assert m.permissions.get("filesystem_outside_data") is True
    assert not m.permissions.get("network")
    assert len(m.warnings) >= 3


def test_requirements_numpy_pin_matches_radio_and_lockfile_exists():
    m = parse_manifest_dir(PLUGIN_ROOT)
    # Same pin as the bundled radio plugin -> refcounted shared dist,
    # effectively already installed everywhere.
    assert m.python_requirements == ("numpy==2.1.2",)
    assert m.lockfile == "requirements.lock"
    lock = (PLUGIN_ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "numpy==2.1.2" in lock and "--hash=" in lock
    tools = {t.tool: t.required for t in m.system_requirements}
    assert tools == {"ffmpeg": True}
