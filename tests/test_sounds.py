"""Pure-unit sound tests: key normalization, catalog scanning, spoken
resolution, and generation drift (ffmpeg mocked — the fake just creates
the output file the real one would)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from domovoi_plugin_sleep import sounds

from tests.conftest import make_settings


def _folder(tmp_path, cfg) -> Path:
    return sounds.sounds_dir(str(tmp_path / "music"), cfg.sounds_subdir)


def _seed_builtins(tmp_path, cfg, *stems: str) -> Path:
    folder = _folder(tmp_path, cfg)
    folder.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (folder / f"{stem}.ogg").write_bytes(b"ogg")
    return folder


# ─── normalize_key ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Brown Noise", "brown noise"),
        ("Rain_On-Tent!", "rain on tent"),
        ("  ocean   waves ", "ocean waves"),
        ("box-fan (loud)", "box fan loud"),
    ],
)
def test_normalize_key(raw, expected):
    assert sounds.normalize_key(raw) == expected


# ─── catalog ─────────────────────────────────────────────────────────────

def test_catalog_builtins_and_user_files(tmp_path):
    cfg = make_settings()
    folder = _seed_builtins(tmp_path, cfg, "white-noise", "ocean")
    (folder / "Rain On Tent.mp3").write_bytes(b"mp3")
    (folder / "notes.txt").write_text("not audio")
    (folder / sounds.SIDECAR_NAME).write_text("{}")

    cat = sounds.catalog(cfg, str(tmp_path / "music"))
    assert set(cat) == {"white noise", "ocean", "rain on tent"}
    assert cat["white noise"].builtin is True
    assert cat["rain on tent"].builtin is False
    assert cat["rain on tent"].title == "Rain On Tent"


def test_catalog_missing_folder_is_empty(tmp_path):
    cfg = make_settings()
    assert sounds.catalog(cfg, str(tmp_path / "music")) == {}


# ─── resolve (the voice pattern strips the sounds/noise suffix) ──────────

@pytest.mark.parametrize(
    "spoken,expected_key",
    [
        ("white", "white noise"),            # "play white noise" capture
        ("white noise", "white noise"),
        ("brown", "brown noise"),
        ("ocean", "ocean"),
        ("waves", "ocean"),                  # alias
        ("the ocean", "ocean"),              # alias
        ("fan", "fan"),
        ("rain", "rain"),                    # natural pack
        ("thunder", "thunderstorm"),         # alias
        ("storm", "thunderstorm"),           # alias
        ("fireplace", "campfire"),           # alias
        ("creek", "stream"),                 # alias
        ("crickets", "crickets"),
        ("rain on tent", "rain on tent"),    # user file
        ("Rain On Tent", "rain on tent"),
    ],
)
def test_resolve_variants(tmp_path, spoken, expected_key):
    cfg = make_settings()
    folder = _seed_builtins(
        tmp_path, cfg, "white-noise", "brown-noise", "soft-fan", "ocean",
        "rain", "thunderstorm", "campfire", "stream", "crickets",
    )
    (folder / "rain on tent.mp3").write_bytes(b"mp3")
    sound = sounds.resolve(cfg, str(tmp_path / "music"), spoken)
    assert sound is not None and sound.key == expected_key


def test_resolve_unknown_is_none(tmp_path):
    cfg = make_settings()
    _seed_builtins(tmp_path, cfg, "white-noise")
    assert sounds.resolve(cfg, str(tmp_path / "music"), "whale songs") is None


def test_mpd_uri_is_subdir_relative(tmp_path):
    cfg = make_settings()
    sound = sounds.Sound(
        key="brown noise", title="Brown noise",
        filename="brown-noise.ogg", builtin=True,
    )
    assert sounds.mpd_uri(cfg, sound) == "Ambient sounds/brown-noise.ogg"


# ─── generation drift ────────────────────────────────────────────────────

@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Stands in for ffmpeg AND the numpy natural renderer: records
    invocations and creates the output file, like the real ones would
    (rendering an hour of audio has no place in the unit tier)."""
    calls: list[tuple[str, ...]] = []

    async def _run(*args, timeout=600.0):
        calls.append(args)
        Path(args[-1]).write_bytes(b"ogg")

    def _render_wav(key, out_path, *, duration_sec, stereo=False):
        calls.append(("render_wav", key, str(duration_sec)))
        Path(out_path).write_bytes(b"wav")

    monkeypatch.setattr(sounds, "_run_ffmpeg", _run)
    monkeypatch.setattr(sounds.synth, "render_wav", _render_wav)
    return calls


async def test_generate_fresh_renders_all(tmp_path, fake_ffmpeg):
    cfg = make_settings()
    generated = await sounds.generate_builtins(cfg, str(tmp_path / "music"))
    assert len(generated) == 11          # 5 ffmpeg recipes + 6 natural
    folder = _folder(tmp_path, cfg)
    assert (folder / "brown-noise.ogg").exists()
    assert (folder / "rain.ogg").exists()
    assert not (folder / "rain.wav.tmp").exists()   # temp WAV cleaned up
    sidecar = json.loads((folder / sounds.SIDECAR_NAME).read_text())
    assert sidecar["file_length_min"] == cfg.file_length_min
    assert sidecar["natural_version"] == sounds.synth.NATURAL_VERSION
    # Mono by default; duration is minutes*60.
    assert any("-ac" in c and "1" in c for c in fake_ffmpeg)
    assert any(str(cfg.file_length_min * 60) in c for c in fake_ffmpeg)
    assert ("render_wav", "rain", str(float(cfg.file_length_min * 60))) in fake_ffmpeg


async def test_generate_skips_when_fresh(tmp_path, fake_ffmpeg):
    cfg = make_settings()
    await sounds.generate_builtins(cfg, str(tmp_path / "music"))
    fake_ffmpeg.clear()
    generated = await sounds.generate_builtins(cfg, str(tmp_path / "music"))
    assert generated == []
    assert fake_ffmpeg == []


async def test_generate_regenerates_on_param_drift(tmp_path, fake_ffmpeg):
    cfg = make_settings()
    await sounds.generate_builtins(cfg, str(tmp_path / "music"))
    fake_ffmpeg.clear()

    longer = make_settings(file_length_min=120)
    generated = await sounds.generate_builtins(longer, str(tmp_path / "music"))
    assert len(generated) == 11
    assert any("7200" in c for c in fake_ffmpeg)


async def test_generate_leaves_user_files_alone(tmp_path, fake_ffmpeg):
    cfg = make_settings()
    folder = _folder(tmp_path, cfg)
    folder.mkdir(parents=True)
    (folder / "my rain.mp3").write_bytes(b"precious")
    await sounds.generate_builtins(cfg, str(tmp_path / "music"))
    assert (folder / "my rain.mp3").read_bytes() == b"precious"
