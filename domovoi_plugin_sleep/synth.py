"""Procedural natural-sounds renderer — the numpy half of generation.

ffmpeg's noise colors cover the abstract sounds (white/pink/brown, fan,
ocean); this module synthesizes the *natural* ones the way white-noise
apps do — as spectrally-shaped noise beds plus stochastic events:

  rain          droplet transients over a mid/high patter bed
  thunderstorm  rain, plus long low rumble events every half-minute-ish
  wind          a low broad bed breathing under a slow gust envelope
  crickets      pulsed ~4.5 kHz chirps from a few "individuals" over a
                faint night bed
  campfire      sparse bright crackle + occasional pops over a warm
                low rumble
  stream        two independently-burbling midband layers

Technique notes (kept deliberately scipy-free):

* Beds are rendered per block in the FREQUENCY domain — random phases
  against a magnitude envelope, then irfft. No filter state to carry.
* Blocks are rendered independently (per-block rng seeded by
  ``(sound, channel, block index)`` — deterministic output for a given
  NATURAL_VERSION) and joined with a 250 ms equal-power crossfade;
  noise-on-noise crossfades are inaudible.
* Events (droplets, chirps, crackle, rumbles) are placed inside each
  block with a margin so nothing straddles a boundary.
* Output is streamed to a 16-bit WAV in blocks — an hour of audio never
  lives in memory — then encoded to OGG by the caller (ffmpeg).

numpy is imported lazily inside the render path (core import-budget
rule); everything here is pure CPU and fully offline.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

NATURAL_VERSION = 1

SAMPLE_RATE = 44100
_BLOCK_SEC = 30.0
_XFADE_SEC = 0.25
# Events keep this far from block edges so none straddles a joint.
_EVENT_MARGIN_SEC = 1.0
# Design target: beds sit near this RMS; events ride on top; final
# samples are clipped at ±0.98 after a generous headroom design.
_BED_RMS = 0.10

# key → (title, filename stem, seed). Titles follow the catalog rules.
NATURAL_SOUNDS: dict[str, tuple[str, str, int]] = {
    "rain": ("Rain", "rain", 101),
    "thunderstorm": ("Thunderstorm", "thunderstorm", 102),
    "wind": ("Wind", "wind", 103),
    "crickets": ("Crickets", "crickets", 104),
    "campfire": ("Campfire", "campfire", 105),
    "stream": ("Stream", "stream", 106),
}


# ─── frequency-domain noise bed ──────────────────────────────────────────

def _shaped_noise(rng: Any, n: int, shape: Callable[[Any], Any]) -> Any:
    """``n`` samples of noise whose magnitude spectrum follows
    ``shape(freqs_hz)`` (arbitrary scale), unit-RMS normalized."""
    import numpy as np

    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    mags = shape(np.maximum(freqs, 1.0)).astype(np.float64)
    mags[0] = 0.0                                   # no DC
    phases = rng.uniform(0.0, 2.0 * np.pi, len(mags))
    spectrum = mags * np.exp(1j * phases)
    x = np.fft.irfft(spectrum, n)
    rms = float(np.sqrt(np.mean(x * x))) or 1.0
    return x / rms


def _band(f: Any, lo: float, hi: float, tilt: float = 0.0) -> Any:
    """Smooth log-domain band from ``lo``..``hi`` Hz with an optional
    power-law tilt (f^tilt) inside the band."""
    import numpy as np

    center = np.sqrt(lo * hi)
    width = np.log(hi / lo) / 2.0
    envelope = np.exp(-((np.log(f / center)) ** 2) / (2.0 * (width * 0.6) ** 2))
    return envelope * (f / center) ** tilt


def _slow_lfo(rng: Any, n: int, cutoff_hz: float) -> Any:
    """A slow, smooth 0-centered unit-ish modulation curve of length
    ``n`` — shaped noise way below the audio band, interpolated up."""
    import numpy as np

    lo_rate = 20.0                                   # LFO sample rate
    m = max(int(n / SAMPLE_RATE * lo_rate), 16)
    freqs = np.fft.rfftfreq(m, d=1.0 / lo_rate)
    mags = np.exp(-np.maximum(freqs, 1e-3) / cutoff_hz)
    mags[0] = 0.0
    phases = rng.uniform(0.0, 2.0 * np.pi, len(mags))
    curve = np.fft.irfft(mags * np.exp(1j * phases), m)
    curve /= max(float(np.max(np.abs(curve))), 1e-9)
    return np.interp(
        np.linspace(0.0, m - 1.0, n), np.arange(m, dtype=np.float64), curve
    )


def _add_event(block: Any, start: int, event: Any) -> None:
    end = min(start + len(event), len(block))
    if end > start >= 0:
        block[start:end] += event[: end - start]


def _decay_env(n: int, sharpness: float = 5.0) -> Any:
    import numpy as np

    return np.exp(-np.linspace(0.0, sharpness, n))


# ─── per-sound block renderers ───────────────────────────────────────────
#
# Each returns one float64 block. ``rng`` is exclusive to this block, so
# rendering is deterministic and order-independent.


def _rain_block(rng: Any, n: int, *, droplet_rate: float = 40.0,
                bed_gain: float = 1.0) -> Any:
    import numpy as np

    bed = _shaped_noise(rng, n, lambda f: _band(f, 300.0, 6000.0, tilt=-0.35))
    block = bed * (_BED_RMS * bed_gain)

    margin = int(_EVENT_MARGIN_SEC * SAMPLE_RATE)
    count = rng.poisson(droplet_rate * n / SAMPLE_RATE)
    for _ in range(count):
        length = int(rng.uniform(0.003, 0.010) * SAMPLE_RATE)
        burst = rng.standard_normal(length) * _decay_env(length, 6.0)
        # First difference brightens the tick into a droplet "pit".
        burst = np.diff(burst, prepend=0.0)
        burst *= rng.uniform(0.02, 0.10) / (np.max(np.abs(burst)) + 1e-9)
        _add_event(block, int(rng.integers(margin, n - margin)), burst)
    return block


def _thunderstorm_block(rng: Any, n: int) -> Any:
    import numpy as np

    block = _rain_block(rng, n, droplet_rate=55.0, bed_gain=1.1)

    margin = int(_EVENT_MARGIN_SEC * SAMPLE_RATE)
    # ~one rumble per 30 s block on average, sometimes none, sometimes two.
    for _ in range(rng.poisson(1.0)):
        length = int(rng.uniform(4.0, 9.0) * SAMPLE_RATE)
        if length >= n - 2 * margin:
            length = n - 2 * margin - 1
        rumble = _shaped_noise(rng, length, lambda f: _band(f, 22.0, 150.0, tilt=-0.8))
        attack = int(0.3 * SAMPLE_RATE)
        env = _decay_env(length, 4.0)
        env[:attack] *= np.linspace(0.0, 1.0, attack)
        rumble *= env * rng.uniform(0.18, 0.38)
        _add_event(block, int(rng.integers(margin, n - margin - length)), rumble)
    return block


def _wind_block(rng: Any, n: int) -> Any:
    bed = _shaped_noise(rng, n, lambda f: _band(f, 40.0, 800.0, tilt=-0.6))
    gust = _slow_lfo(rng, n, cutoff_hz=0.15)
    # Breathe between ~0.35x and ~1.0x of the bed level.
    return bed * (_BED_RMS * 1.4) * (0.675 + 0.325 * gust)


def _crickets_block(rng: Any, n: int) -> Any:
    import numpy as np

    # A very faint dark night bed so the silence never goes digital-dead.
    block = _shaped_noise(rng, n, lambda f: _band(f, 60.0, 500.0, tilt=-0.5))
    block *= _BED_RMS * 0.35

    margin = int(_EVENT_MARGIN_SEC * SAMPLE_RATE)
    t_pulse = np.arange(int(0.030 * SAMPLE_RATE), dtype=np.float64) / SAMPLE_RATE
    for _ in range(4):                                  # four individuals
        carrier_hz = rng.uniform(4100.0, 4900.0)
        pulse = np.sin(2.0 * np.pi * carrier_hz * t_pulse)
        pulse *= np.hanning(len(t_pulse))
        level = rng.uniform(0.02, 0.05)
        # Chirps (3-5 pulses, ~25 Hz pulse rate) roughly every second.
        for _ in range(rng.poisson(n / SAMPLE_RATE * 0.9)):
            start = int(rng.integers(margin, n - margin))
            for p in range(int(rng.integers(3, 6))):
                _add_event(block, start + int(p * 0.040 * SAMPLE_RATE),
                           pulse * level)
    return block


def _campfire_block(rng: Any, n: int) -> Any:
    import numpy as np

    warm = _shaped_noise(rng, n, lambda f: _band(f, 30.0, 500.0, tilt=-1.0))
    hiss = _shaped_noise(rng, n, lambda f: _band(f, 1000.0, 4000.0, tilt=0.0))
    breathing = 0.8 + 0.2 * _slow_lfo(rng, n, cutoff_hz=0.3)
    block = warm * (_BED_RMS * 0.75) * breathing + hiss * (_BED_RMS * 0.12)

    # Crackle must stand PROUD of the bed — a gaussian bed's own peaks
    # reach ~4x RMS over a block, so ticks/pops land well above that or
    # the fire reads as plain noise (locked by the crest-factor test).
    margin = int(_EVENT_MARGIN_SEC * SAMPLE_RATE)
    # Crackle: small bright ticks, ~8 per second.
    for _ in range(rng.poisson(8.0 * n / SAMPLE_RATE)):
        length = int(rng.uniform(0.001, 0.005) * SAMPLE_RATE) + 8
        tick = np.diff(rng.standard_normal(length), prepend=0.0)
        tick *= _decay_env(length, 8.0)
        tick *= rng.uniform(0.08, 0.22) / (np.max(np.abs(tick)) + 1e-9)
        _add_event(block, int(rng.integers(margin, n - margin)), tick)
    # Pops: rarer, fatter, unmistakably above the bed.
    for _ in range(rng.poisson(0.3 * n / SAMPLE_RATE)):
        length = int(rng.uniform(0.010, 0.030) * SAMPLE_RATE)
        pop = rng.standard_normal(length) * _decay_env(length, 5.0)
        pop *= rng.uniform(0.25, 0.50) / (np.max(np.abs(pop)) + 1e-9)
        _add_event(block, int(rng.integers(margin, n - margin)), pop)
    return block


def _stream_block(rng: Any, n: int) -> Any:
    mid = _shaped_noise(rng, n, lambda f: _band(f, 400.0, 2500.0, tilt=-0.3))
    splash = _shaped_noise(rng, n, lambda f: _band(f, 2000.0, 6000.0, tilt=0.0))
    burble_a = _slow_lfo(rng, n, cutoff_hz=1.5)
    burble_b = _slow_lfo(rng, n, cutoff_hz=2.5)
    wander = _slow_lfo(rng, n, cutoff_hz=0.05)
    level = 1.0 + 0.12 * wander
    return (
        mid * (_BED_RMS * 1.0) * (0.80 + 0.20 * burble_a)
        + splash * (_BED_RMS * 0.35) * (0.70 + 0.30 * burble_b)
    ) * level


_RENDERERS: dict[str, Callable[[Any, int], Any]] = {
    "rain": _rain_block,
    "thunderstorm": _thunderstorm_block,
    "wind": _wind_block,
    "crickets": _crickets_block,
    "campfire": _campfire_block,
    "stream": _stream_block,
}


# ─── block joiner + WAV streaming ────────────────────────────────────────

def render_blocks(key: str, *, duration_sec: float, channel_seed: int):
    """Yield successive float64 hop blocks (already crossfaded) for one
    channel of ``key``, totalling ``duration_sec`` of audio."""
    import numpy as np

    renderer = _RENDERERS[key]
    _, _, base_seed = NATURAL_SOUNDS[key]
    n_block = int(_BLOCK_SEC * SAMPLE_RATE)
    n_fade = int(_XFADE_SEC * SAMPLE_RATE)
    total = int(duration_sec * SAMPLE_RATE)

    fade_in = np.sqrt(np.linspace(0.0, 1.0, n_fade))    # equal-power
    fade_out = np.sqrt(np.linspace(1.0, 0.0, n_fade))

    emitted = 0
    index = 0
    tail = None                                          # previous block's fade region
    while emitted < total:
        rng = np.random.default_rng(
            (base_seed, channel_seed, NATURAL_VERSION, index)
        )
        block = renderer(rng, n_block)
        if tail is not None:
            block[:n_fade] = block[:n_fade] * fade_in + tail * fade_out
        tail = block[-n_fade:].copy()
        hop = block[:-n_fade]                            # keep tail for the next joint
        hop = hop[: max(total - emitted, 0)]
        emitted += len(hop)
        index += 1
        yield np.clip(hop, -0.98, 0.98)


def render_wav(
    key: str,
    out_path: Path,
    *,
    duration_sec: float,
    stereo: bool = False,
) -> None:
    """Stream one natural sound to a 16-bit WAV at ``out_path``. Stereo
    renders two independently-seeded channels (natural decorrelation)."""
    import numpy as np

    channels = (
        [render_blocks(key, duration_sec=duration_sec, channel_seed=0)]
        if not stereo
        else [
            render_blocks(key, duration_sec=duration_sec, channel_seed=c)
            for c in (0, 1)
        ]
    )
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(len(channels))
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for hops in zip(*channels):
            frame = np.stack(hops, axis=-1) if len(hops) > 1 else hops[0][:, None]
            wav.writeframes((frame * 32767.0).astype(np.int16).tobytes())
    log.info("sleep: rendered %s (%.0f min, %s)", key, duration_sec / 60.0,
             "stereo" if stereo else "mono")
