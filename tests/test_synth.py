"""Natural-pack synthesizer tests — real numpy DSP on short renders:
exact lengths, determinism, clipping bounds, WAV shape, and spectral
sanity (rain must actually sound bright, wind low, crickets chirpy)."""

from __future__ import annotations

import wave

import numpy as np
import pytest

from domovoi_plugin_sleep import synth


def _render(key: str, seconds: float = 4.0) -> np.ndarray:
    hops = list(synth.render_blocks(key, duration_sec=seconds, channel_seed=0))
    return np.concatenate(hops)


def _band_energy(x: np.ndarray, lo: float, hi: float) -> float:
    spectrum = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), d=1.0 / synth.SAMPLE_RATE)
    mask = (freqs >= lo) & (freqs < hi)
    return float(spectrum[mask].sum())


@pytest.mark.parametrize("key", list(synth.NATURAL_SOUNDS))
def test_render_length_bounds_and_determinism(key):
    a = _render(key, 2.0)
    b = _render(key, 2.0)
    assert len(a) == 2 * synth.SAMPLE_RATE
    assert np.array_equal(a, b)                     # seeded → reproducible
    assert float(np.max(np.abs(a))) <= 0.98         # clip ceiling
    assert float(np.sqrt(np.mean(a * a))) > 0.01    # actually audible


def test_channels_are_decorrelated():
    left = np.concatenate(
        list(synth.render_blocks("rain", duration_sec=2.0, channel_seed=0))
    )
    right = np.concatenate(
        list(synth.render_blocks("rain", duration_sec=2.0, channel_seed=1))
    )
    corr = float(np.corrcoef(left, right)[0, 1])
    assert abs(corr) < 0.1


def test_multi_block_render_spans_joints():
    """A render longer than one block exercises the crossfade path and
    still lands sample-exact."""
    seconds = 35.0                                   # block is 30 s
    x = _render("stream", seconds)
    assert len(x) == int(seconds * synth.SAMPLE_RATE)
    # No dead zone or blowup around the joint region.
    joint = x[int(29.0 * synth.SAMPLE_RATE): int(31.0 * synth.SAMPLE_RATE)]
    assert float(np.sqrt(np.mean(joint * joint))) > 0.01
    assert float(np.max(np.abs(joint))) <= 0.98


# ─── spectral sanity: each sound lives where it should ───────────────────

def test_rain_is_brighter_than_wind():
    rain, wind = _render("rain"), _render("wind")
    rain_hf = _band_energy(rain, 2000, 8000) / _band_energy(rain, 20, 8000)
    wind_hf = _band_energy(wind, 2000, 8000) / _band_energy(wind, 20, 8000)
    assert rain_hf > 0.15
    assert wind_hf < 0.05
    assert rain_hf > 5 * wind_hf


def test_thunderstorm_adds_low_end_to_rain():
    rain = _render("rain", 8.0)
    # Rumbles are Poisson — average several blocks' worth by rendering
    # longer and relying on the seeded schedule including at least one.
    storm = _render("thunderstorm", 8.0)
    rain_low = _band_energy(rain, 20, 150) / _band_energy(rain, 20, 8000)
    storm_low = _band_energy(storm, 20, 150) / _band_energy(storm, 20, 8000)
    assert storm_low > rain_low


def test_crickets_chirp_near_carrier():
    x = _render("crickets")
    chirp_band = _band_energy(x, 3800, 5200)
    rest_high = _band_energy(x, 5600, 9000)
    assert chirp_band > 5 * rest_high


def test_campfire_has_crackle_transients():
    """Crackle shows up as a heavy-tailed sample distribution — the
    peak-to-RMS ratio must exceed a plain noise bed's."""
    fire, wind = _render("campfire"), _render("wind")
    fire_crest = float(np.max(np.abs(fire)) / np.sqrt(np.mean(fire * fire)))
    wind_crest = float(np.max(np.abs(wind)) / np.sqrt(np.mean(wind * wind)))
    assert fire_crest > wind_crest * 1.5


def test_stream_lives_in_the_midband():
    x = _render("stream")
    mid = _band_energy(x, 400, 2500)
    low = _band_energy(x, 20, 200)
    assert mid > 5 * low


# ─── WAV writer ──────────────────────────────────────────────────────────

def test_render_wav_mono_and_stereo(tmp_path):
    mono = tmp_path / "m.wav"
    stereo = tmp_path / "s.wav"
    synth.render_wav("wind", mono, duration_sec=1.0, stereo=False)
    synth.render_wav("wind", stereo, duration_sec=1.0, stereo=True)

    with wave.open(str(mono), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == synth.SAMPLE_RATE
        assert w.getnframes() == synth.SAMPLE_RATE
        assert w.getsampwidth() == 2
    with wave.open(str(stereo), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getnframes() == synth.SAMPLE_RATE
