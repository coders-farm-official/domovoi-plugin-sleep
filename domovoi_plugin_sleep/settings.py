"""Sleep-sounds plugin settings — env prefix ``SLEEP_``.

Values persist to ``~/.domovoi/plugins/sleep.env`` via the core config
bridge; OS env vars shadow the file (documented core-wide caveat).

Generation-shape fields (file length, stereo) are ``restart`` tier: the
``sleep_generator`` startup hook compares them against the sidecar the
last generation wrote and regenerates only what drifted.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from domovoi.sdk import FieldSpec


class SleepSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SLEEP_", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Sounds ──────────────────────────────────────────────────────────
    # Subfolder of the music library the generated files live in. The
    # per-room players only see the music library, which is why the
    # files live there and not in the plugin data dir.
    sounds_subdir: str = "Ambient sounds"
    # Length of each generated file, minutes. Also the hard ceiling on a
    # session: MPD simply reaches the end of the file and stops, so a
    # crashed timer can never leave noise running until morning.
    file_length_min: int = 60
    # Stereo doubles the disk footprint; satellites are mono speakers.
    stereo: bool = False

    # ── Timers ──────────────────────────────────────────────────────────
    # Default sleep timer when none is spoken. 0 = play to file end.
    default_timer_min: int = 45
    # Fade length before a timed stop. 0 = hard stop.
    fade_out_sec: float = 20.0
    # Timer-enforcement tick. Also bounds fade granularity.
    timer_interval_sec: float = 5.0
    timer_enabled: bool = True


SLEEP_FIELDSPECS: list[FieldSpec] = [
    FieldSpec(
        name="sounds_subdir", label="Sounds folder",
        help="Subfolder of the music library holding the generated sounds. "
             "Drop your own audio files there (real rain, a box fan) and "
             "they become voice-addressable by filename.",
        group="Sounds", kind="text", tier="restart",
    ),
    FieldSpec(
        name="file_length_min", label="Sound length (minutes)",
        help="How long each generated sound plays before stopping on its "
             "own. Longer files use more disk (roughly 30 MB per hour per "
             "sound). Regenerated on next core restart.",
        group="Sounds", kind="int", tier="restart",
    ),
    FieldSpec(
        name="stereo", label="Stereo",
        help="Generate stereo files (double the disk). Satellite speakers "
             "are mono — leave off unless a stereo player uses them.",
        group="Sounds", kind="bool", tier="restart",
    ),
    FieldSpec(
        name="default_timer_min", label="Default sleep timer (minutes)",
        help="Used when no duration is spoken ('play brown noise'). "
             "0 plays to the end of the file.",
        group="Timers", kind="int",
    ),
    FieldSpec(
        name="fade_out_sec", label="Fade-out (seconds)",
        help="Gentle volume ramp before a timed stop. 0 stops abruptly.",
        group="Timers", kind="float",
    ),
    FieldSpec(
        name="timer_enabled", label="Timer worker",
        help="Enforces sleep timers and cleans up finished sessions.",
        group="Timers", kind="bool", tier="restart",
    ),
    FieldSpec(
        name="timer_interval_sec", label="Timer tick (seconds)",
        help="How often timers are checked.",
        group="Timers", kind="float",
    ),
]
