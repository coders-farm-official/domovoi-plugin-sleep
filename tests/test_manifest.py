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


def test_requirements_numpy_pin_matches_core_lock_and_radio():
    import domovoi
    from domovoi.plugins_runtime.lockfile import parse_lockfile

    def numpy_pin(lock_path):
        (req,) = [
            r for r in parse_lockfile(lock_path.read_text(encoding="utf-8"))
            if r.key == "numpy"
        ]
        return req

    m = parse_manifest_dir(PLUGIN_ROOT)
    # Same pin as Domovoi's core requirements.lock (what a deployment
    # installs) and the bundled radio plugin -> refcounted shared dist,
    # effectively already installed everywhere; any other version fails
    # the install dry-run with requirements_conflict. Derived from the
    # actual locks (not hardcoded) so a coordinated version bump can't
    # leave this test asserting a stale literal.
    repo = Path(domovoi.__file__).resolve().parents[1]
    core_numpy = numpy_pin(repo / "requirements.lock")
    radio_numpy = numpy_pin(repo / "plugins" / "radio" / "requirements.lock")
    ours = numpy_pin(PLUGIN_ROOT / "requirements.lock")
    assert m.python_requirements == (core_numpy.spec,)
    assert radio_numpy.spec == core_numpy.spec
    assert ours.spec == core_numpy.spec
    assert ours.hashes and set(ours.hashes) == set(core_numpy.hashes)
    assert m.lockfile == "requirements.lock"
    tools = {t.tool: t.required for t in m.system_requirements}
    assert tools == {"ffmpeg": True}
