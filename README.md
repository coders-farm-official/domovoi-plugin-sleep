# domovoi-plugin-sleep

A [Domovoi](https://github.com/coders-farm-official/domovoi) plugin for
per-room sleep sounds. Published by Coders Farm, MIT licensed.

> "play rain sounds in the bedroom for 45 minutes"

The sounds come from **nowhere**: they're synthesized locally. ffmpeg's
noise generator makes the abstract five (white / pink / brown noise,
soft fan, ocean swells), and a numpy DSP renderer makes the natural six
the way white-noise apps actually do — **rain** (droplet transients
over a patter bed), **thunderstorm** (rain plus long low rumbles),
**wind** (a slow gusting envelope), **crickets** (pulsed ~4.5 kHz
chirps), **campfire** (crackle and pops over a warm rumble), and
**stream** (independently burbling midband layers). No downloads, no
accounts, no licensing, fully offline, forever. Timers fade the sound
out gently, every file ends on its own as a fail-safe, and each room
runs independently through its own player.

## What you get

* **Voice** — "play white noise", "play ocean sounds in the bedroom for
  30 minutes", "play sleep sounds" (the default, brown noise), "stop
  the sounds", "what sounds can you play". Works from any satellite;
  defaults to the room you spoke in.
* **A Sleep tab** on the dashboard — every room as a card: what's
  playing, a live countdown, sound picker + timer presets, stop.
* **Sleep timers with fade-out** — the timer worker ramps the volume
  down (default 20 s) so the stop never startles anyone, then restores
  the level. Timers live in the database: a core restart mid-nap
  changes nothing.
* **Your own sounds** — drop any audio file into the sounds folder
  (default `Ambient sounds/` inside your music library) and it becomes
  voice-addressable by its filename: `rain on tent.mp3` → *"play rain
  on tent sounds"*.

## Install

Dashboard → **Plugins** → install, then either:

* **GitHub URL** — `https://github.com/coders-farm-official/domovoi-plugin-sleep`
* **Zip upload** — build one with `domovoi plugin pack .` from this directory

Requirements: `ffmpeg` on PATH (Domovoi installs already have it — the
radio plugin requires it too) and `numpy` (pinned to the exact version
the bundled radio plugin already ships, so nothing new actually
installs). **No network.**

First start renders the eleven sound files (a few minutes, one-time);
the voice handler and Sleep tab answer honestly until they're ready.
Files are re-rendered only when generation settings change. Rendering
briefly uses ~300 MB of temp disk per natural sound (a WAV that is
encoded to OGG and deleted).

Prefer AI-generated or recorded textures? Generate them anywhere (e.g.
Google's MusicFX / Lyria in AI Studio), download, and drop the files
into the sounds folder — they become voice-addressable by filename. The
plugin itself ships only what it synthesizes, so its zero-network,
zero-licensing story stays intact.

## Settings (dashboard → Settings → Plugins → Sleep Sounds)

| Setting | Default | Notes |
|---|---|---|
| Sounds folder | `Ambient sounds` | Subfolder of the music library (the per-room players only see the music library). |
| Sound length | `60` min | Also the hard ceiling on any session — the file simply ends. ~30 MB per hour per sound. |
| Default sleep timer | `45` min | Used when no duration is spoken. 0 = play to file end. |
| Fade-out | `20` s | Volume ramp before a timed stop. |
| Stereo | off | Satellites are mono speakers; stereo doubles disk. |

## How it behaves

* The fade dips the room player's software volume and **restores it
  right after the stop** — the satellite's hardware mixer stays the
  single volume control (core invariant), and the core's startup
  volume pin is the safety net if anything dies mid-fade.
* The timer worker's tick doubles as reconciliation: if the room moved
  on (someone played music, the file ended, the room was stopped some
  other way), the session row deactivates so the dashboard and voice
  status never lie.
* Stopping only ever stops **this plugin's** sound: asking a room with
  no sleep session to stop answers `404` and leaves whatever is playing
  there playing, the same answer the voice path gives ("No sleep sounds
  are playing in the kitchen").
* "Sounds" phrasings are fenced off by the trailing noun: "play ocean
  **sounds**" is this plugin at band 275; "play the beatles" still
  belongs to the music handler at 300; "play 97.5 fm" still belongs to
  radio at 280. A song literally titled "Ocean Sounds" is claimed by
  this plugin — say "play the song ocean sounds" to route it to music.
* Uninstall-purge removes the plugin's data; the generated files in
  your music library are yours — delete the `Ambient sounds` folder if
  you want them gone.

## Development

```bash
# from a Domovoi dev checkout's parent directory
domovoi plugin dev ./domovoi-plugin-sleep     # register in place
python -m domovoi.main                         # restart core to load

pytest          # unit tier runs anywhere; DB tier needs the dev Postgres
domovoi plugin pack .                          # installable zip
```

The suite covers key normalization + catalog + spoken-name resolution,
generation drift against the params sidecar (ffmpeg mocked), a
real-ffmpeg recipe smoke render (it caught a filtergraph bug already),
the handler's anchoring matrix (including the phrasings it must NOT
claim), session lifecycle + fade/restore discipline + timer
enforcement + reconciliation, the web surface, and a full loader
round-trip against the radio, jellyfin, and romm corpora.
