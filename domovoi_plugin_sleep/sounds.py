"""Sound synthesis + catalog.

Where the sounds come from: **nowhere** — ffmpeg's ``anoisesrc`` filter
synthesizes the classic sleep-sound family locally (white / pink /
brown noise), and filter chains shape two more from brown noise: a soft
fan (gentle low-pass + faint flutter) and ocean swells (slow ~12 s
amplitude waves). No downloads, no licensing, no accounts, works
offline forever.

Files land in ``MUSIC_DIR/<sounds_subdir>/`` because the per-room MPD
players only see the music library. They are deliberately **finite**
(``file_length_min``): a finite file is a fail-safe sleep timer — even
if every timer mechanism dies, the sound ends on its own. Generation is
compared against a params sidecar (``.domovoi-sleep.json``) so only
drifted files are re-rendered.

The catalog is the union of the built-in recipes and any audio file the
user drops into the folder — a dropped ``rain on tent.mp3`` becomes the
voice-addressable sound "rain on tent".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from domovoi_plugin_sleep import synth

log = logging.getLogger(__name__)

# Version bump forces regeneration of every builtin (recipe changes).
RECIPE_VERSION = 1

SIDECAR_NAME = ".domovoi-sleep.json"

# Extensions we accept as user-dropped sounds.
_AUDIO_EXTS = {".mp3", ".ogg", ".opus", ".flac", ".wav", ".m4a"}

_KEY_RE = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class Sound:
    key: str            # normalized spoken name ("brown noise", "rain on tent")
    title: str          # display/MPD title ("Brown noise")
    filename: str       # file inside the sounds folder
    builtin: bool


# ─── builtin recipes ─────────────────────────────────────────────────────
#
# key → (title, filename stem, lavfi filtergraph). Amplitudes are chosen
# so the five sounds land at roughly similar loudness; the satellite's
# hardware mixer stays the single volume control (core invariant), so
# no loudnorm pass — deterministic output matters more.

_RECIPES: dict[str, tuple[str, str, str]] = {
    "white noise": (
        "White noise", "white-noise",
        "anoisesrc=color=white:amplitude=0.35",
    ),
    "pink noise": (
        "Pink noise", "pink-noise",
        "anoisesrc=color=pink:amplitude=0.5",
    ),
    "brown noise": (
        "Brown noise", "brown-noise",
        "anoisesrc=color=brown:amplitude=0.6",
    ),
    "fan": (
        "Soft fan", "soft-fan",
        # Low-passed brown noise with a faint 24 Hz flutter — reads as a
        # box fan two rooms away.
        "anoisesrc=color=brown:amplitude=0.7,lowpass=f=420,tremolo=f=24:d=0.12",
    ),
    "ocean": (
        "Ocean", "ocean",
        # Slow 10 s amplitude swells over muffled brown noise. NOTE:
        # tremolo's frequency floor is 0.1 Hz — anything slower fails
        # the filtergraph parse (caught by the recipe smoke test).
        "anoisesrc=color=brown:amplitude=0.8,lowpass=f=900,tremolo=f=0.1:d=0.75",
    ),
}

# Spoken aliases → canonical builtin key.
ALIASES: dict[str, str] = {
    "static": "white noise",
    "soft fan": "fan",
    "fan noise": "fan",
    "the fan": "fan",
    "ocean waves": "ocean",
    "waves": "ocean",
    "the ocean": "ocean",
    "sea": "ocean",
    # natural pack (synth.py)
    "raining": "rain",
    "thunder": "thunderstorm",
    "storm": "thunderstorm",
    "rain storm": "thunderstorm",
    "fire": "campfire",
    "fireplace": "campfire",
    "crackling fire": "campfire",
    "cricket": "crickets",
    "night": "crickets",
    "creek": "stream",
    "river": "stream",
    "brook": "stream",
    "babbling brook": "stream",
}


def sounds_dir(music_dir: str, subdir: str) -> Path:
    return Path(music_dir).expanduser() / subdir


def normalize_key(raw: str) -> str:
    """Lowercase, strip punctuation/underscores, collapse spaces —
    'Rain_On-Tent!' → 'rain on tent'."""
    cleaned = _KEY_RE.sub(" ", raw.lower().replace("_", " ").replace("-", " "))
    return " ".join(cleaned.split())


# ─── generation ──────────────────────────────────────────────────────────

def _wanted_params(cfg) -> dict:
    return {
        "recipe_version": RECIPE_VERSION,
        "natural_version": synth.NATURAL_VERSION,
        "file_length_min": int(cfg.file_length_min),
        "stereo": bool(cfg.stereo),
    }


def _read_sidecar(folder: Path) -> dict | None:
    path = folder / SIDECAR_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _run_ffmpeg(*args: str, timeout: float = 600.0) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="replace").strip()[:300]
        raise RuntimeError(f"ffmpeg failed: {err}")


async def _encode_ogg(
    input_args: list[str], out: Path, title: str, *, timeout: float = 600.0
) -> None:
    """Encode to ``<out>.tmp`` and atomically move into place so MPD
    never indexes a half-written file. The temp name ends in ``.tmp``,
    which ffmpeg can't infer a muxer from — ``-f ogg`` pins it."""
    tmp = out.with_suffix(".ogg.tmp")
    await _run_ffmpeg(
        *input_args,
        "-c:a", "libvorbis", "-q:a", "2",
        "-metadata", f"title={title}",
        "-metadata", "artist=Domovoi sleep sounds",
        "-f", "ogg",
        str(tmp),
        timeout=timeout,
    )
    tmp.replace(out)


async def generate_builtins(cfg, music_dir: str) -> list[str]:
    """Render every builtin whose file is missing or whose params
    drifted from the sidecar. Returns the keys (re)generated. Raises
    only on total failure (missing ffmpeg surfaces via the system-tool
    probe as a degraded load before this ever runs)."""
    folder = sounds_dir(music_dir, cfg.sounds_subdir)
    folder.mkdir(parents=True, exist_ok=True)

    wanted = _wanted_params(cfg)
    current = _read_sidecar(folder)
    drifted = current != wanted

    duration_sec = int(cfg.file_length_min) * 60
    channels = "2" if cfg.stereo else "1"
    generated: list[str] = []

    for key, (title, stem, filtergraph) in _RECIPES.items():
        out = folder / f"{stem}.ogg"
        if out.exists() and not drifted:
            continue
        log.info("sleep: rendering %s (%d min)", title, cfg.file_length_min)
        await _encode_ogg(
            [
                "-f", "lavfi",
                "-i", filtergraph,
                "-t", str(duration_sec),
                "-ac", channels,
                "-ar", "44100",
            ],
            out, title,
        )
        generated.append(key)

    # The natural pack: numpy renders a temp WAV (streamed, so an hour
    # of audio never lives in memory — but it does briefly live on disk,
    # ~300 MB per hour), ffmpeg encodes it, the WAV is deleted.
    for key, (title, stem, _seed) in synth.NATURAL_SOUNDS.items():
        out = folder / f"{stem}.ogg"
        if out.exists() and not drifted:
            continue
        log.info("sleep: rendering %s (%d min)", title, cfg.file_length_min)
        wav_tmp = folder / f"{stem}.wav.tmp"
        try:
            await asyncio.to_thread(
                synth.render_wav,
                key,
                wav_tmp,
                duration_sec=float(duration_sec),
                stereo=bool(cfg.stereo),
            )
            await _encode_ogg(["-i", str(wav_tmp)], out, title)
            generated.append(key)
        finally:
            wav_tmp.unlink(missing_ok=True)

    (folder / SIDECAR_NAME).write_text(
        json.dumps(wanted, indent=2), encoding="utf-8"
    )
    return generated


# ─── catalog ─────────────────────────────────────────────────────────────

def catalog(cfg, music_dir: str) -> dict[str, Sound]:
    """key → Sound for everything playable right now: builtins whose
    file exists, plus user-dropped audio files (named by their stem).
    Cheap — one directory scan."""
    folder = sounds_dir(music_dir, cfg.sounds_subdir)
    out: dict[str, Sound] = {}

    builtin_entries = [
        (key, title, stem) for key, (title, stem, _g) in _RECIPES.items()
    ] + [
        (key, title, stem) for key, (title, stem, _s) in synth.NATURAL_SOUNDS.items()
    ]
    builtin_files = {stem for _, _, stem in builtin_entries}
    for key, title, stem in builtin_entries:
        if (folder / f"{stem}.ogg").exists():
            out[key] = Sound(key=key, title=title, filename=f"{stem}.ogg", builtin=True)

    if folder.exists():
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in _AUDIO_EXTS:
                continue
            if path.stem in builtin_files:
                continue
            key = normalize_key(path.stem)
            if not key or key in out:
                continue
            out[key] = Sound(
                key=key, title=path.stem, filename=path.name, builtin=False
            )
    return out


def resolve(cfg, music_dir: str, spoken: str) -> Sound | None:
    """Resolve a spoken sound name against the catalog.

    The voice pattern anchors on a trailing "sounds/noise" word and
    strips it from the capture — so "play white noise" arrives here as
    "white" while the catalog key is "white noise". Try the normalized
    form, both suffixed forms, the de-suffixed form, then aliases.
    """
    cat = catalog(cfg, music_dir)
    key = normalize_key(spoken)

    candidates = [key, f"{key} noise", f"{key} sounds"]
    for suffix in (" noise", " sounds", " sound"):
        if key.endswith(suffix):
            candidates.append(key.removesuffix(suffix))
    for candidate in candidates:
        if candidate in cat:
            return cat[candidate]
        alias = ALIASES.get(candidate)
        if alias and alias in cat:
            return cat[alias]
    return None


def mpd_uri(cfg, sound: Sound) -> str:
    """The database-relative URI the room players address the file by."""
    return f"{cfg.sounds_subdir}/{sound.filename}"
