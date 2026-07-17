"""Recipe smoke test against REAL ffmpeg (skipped when it's absent).

The unit tier mocks ffmpeg, so an invalid filtergraph would otherwise
only surface on a user's first boot. This already caught one bug:
tremolo's frequency floor is 0.1 Hz, which rejected the original
0.08 Hz ocean swell."""

from __future__ import annotations

import shutil

import pytest

from domovoi_plugin_sleep import sounds

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"
)


@requires_ffmpeg
@pytest.mark.parametrize("key", list(sounds._RECIPES))
async def test_recipe_renders(tmp_path, key):
    _title, stem, graph = sounds._RECIPES[key]
    out = tmp_path / f"{stem}.ogg"
    await sounds._run_ffmpeg(
        "-f", "lavfi", "-i", graph, "-t", "1", "-ac", "1", "-ar", "44100",
        "-c:a", "libvorbis", "-q:a", "2", str(out), timeout=60.0,
    )
    assert out.exists() and out.stat().st_size > 1000
