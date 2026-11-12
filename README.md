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

## Storytime

> "tell a huge bedtime story about a dragon who lost his blanket in the kids room"

An AI bedtime story that runs for as long as you ask. The house's own Q&A
model (Ollama, on the Domovoi server) writes it a passage at a time while
it plays, staying a few minutes ahead of the listener, and each passage
is read in **the room's own voice** (a browser gets the server's voice).
Near the end the story slows down, and it lands a sleepy ending on time.

* **Lengths** — S 3 min · M 10 · L 20 · XL 40 · Huge 90, or any number
  of minutes (1–180). By voice: "a small / medium / long / extra long /
  huge story", "a 25 minute bedtime story", "tell me a story about otters
  for 20 minutes". Plain "tell me a story" (or "a short story") stays a
  quick one from the Q&A model; a size, a length or "bedtime" makes it
  Storytime. "Stop the story" ends it, as does "stop the sounds".
* **The Sleep page** — a Storytime card: what it's about (or *surprise
  me* for three ideas to pick from), length, tone (cozy, silly,
  adventure, magical, calm), who's listening (kids, grown-ups), an
  optional sleep sound underneath, and the room — or this browser.
* **A sleep sound underneath** — mixed under the voice, and it keeps
  playing after the story ends until the default sleep timer runs out.
* **Needs** Domovoi 1.9+ (SDK `speech.render`) and the household's AI
  features on. Nothing leaves the machine. Passages are written next to
  the sleep sounds (`Ambient sounds/Stories/`) and deleted 12 hours after
  the story ends.

## Install

Dashboard → **Plugins** → install, then either:

* **GitHub URL** — `https://github.com/coders-farm-official/domovoi-plugin-sleep`
* **Zip upload** — build one with `domovoi plugin pack .` from this directory

Requirements: `ffmpeg` on PATH (Domovoi installs already have it — the
radio plugin requires it too) and `numpy` (pinned to the exact version
in Domovoi's core `requirements.lock`, which the bundled radio plugin
pins too, so nothing new actually installs). **No network.**

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
| Storytime | on | The story writer worker. Stories need the household's AI features on too. |
| Story writer tick | `5` s | How often the writer checks whether a story needs its next passage. |

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
* Who can do what: playing and stopping a sound, and READING the active
  sessions (the Sleep page's overview, the core's `/state`), take a
  paired device — the household token or an admin session. A session
  list says which rooms have somebody asleep and until when, so since
  1.2.4 it is no longer open to anything on the LAN. Regenerating the
  sounds takes an admin. The sound catalog and the browser stream stay
  open.
* A sound started from the Sleep page, and a stop from the page or the
  timer, reach the room's satellite (since 1.2.5, on a Domovoi core
  with plugin SDK 1.6 or later: `sdk.playback.dispatch`). On an older
  core the room's player is queued but the satellite is never told, so
  the room stays silent: play by voice there, or update the core.
* While an upgrade of this plugin waits for a restart, the page says so
  ("restart Domovoi to finish the upgrade") instead of calling the core
  unreachable (since 1.2.5; the upgrade FROM 1.2.4 still shows the old
  wording, because the old page is what runs until the restart).

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
