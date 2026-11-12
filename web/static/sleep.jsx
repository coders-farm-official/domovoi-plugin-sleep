/* Sleep page — the sleep plugin's dashboard page.
 *
 * Loaded by the app bootstrap per the plugin manifest ([web].scripts),
 * wrapped in an IIFE and Babel-compiled. The page is exported ONLY
 * through the namespaced window registry at the bottom
 * (window.DomovoiPlugins.sleep.pages.SleepPage) — never a bare
 * window global.
 *
 * Data sources (all under the plugin's own API prefix):
 *   * GET  /api/plugins/sleep/overview      — rooms, sessions, catalog
 *   * POST /api/plugins/sleep/play|stop     — open (daily-use)
 *   * POST /api/plugins/sleep/regenerate    — admin
 *   * POST /api/plugins/sleep/stories[/ideas|/{id}/stop] — Storytime
 *   * GET  /api/plugins/sleep/stories/{id}  — a browser story's passages
 *     (and its heartbeat)
 *   * GET  /api/plugins/sleep/sounds/{key}/stream — browser playback
 *     (the manifest's [[web.player_sources]] route, via usePlayback)
 *   * /ws/state · `sleep.sessions.changed`  — live refetch nudge
 *
 * Countdowns are computed CLIENT-SIDE from each session's fixed
 * `stop_at` timestamp — the realtime snapshot stays diff-stable and a
 * 1 s local interval drives the ticking display.
 *
 * Core-bundle globals used (loaded before any plugin script): React,
 * Card, Button, Icon, Pill, Empty, PageHeader, RoomChip, useToast,
 * apiGet, apiPost, useApiObject, usePlayback.
 */

const SLEEP_API = '/api/plugins/sleep';
const TIMER_PRESETS = [15, 30, 45, 60];   // minutes; 0 = to file end

/* Queue item for the shared browser player (same shape radio builds
 * against its stream proxy). Finite files, so a duration renders a
 * real progress bar; not library tracks, so never offline-cached. */
const sleepQueueItem = (s, durationSec) => ({
  uid: `sleep-${s.key}-${Math.random().toString(36).slice(2, 7)}`,
  kind: 'sleep',
  trackId: null,
  title: s.title,
  artist: 'Domovoi sleep sounds',
  album: '',
  src: `${SLEEP_API}/sounds/${encodeURIComponent(s.key)}/stream`,
  coverUrl: null,
  durationSec: durationSec || null,
  seekable: true,
  cacheable: false,
  meta: {},
});

/* mm:ss (or h:mm:ss) until an ISO timestamp; null when past/absent. */
const countdownTo = (iso, nowMs) => {
  if (!iso) return null;
  const remain = Math.floor((new Date(iso).getTime() - nowMs) / 1000);
  if (remain <= 0) return '0:00';
  const h = Math.floor(remain / 3600);
  const m = Math.floor((remain % 3600) / 60);
  const s = remain % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
};

/* Ticks once a second while any session is live. */
const useNow = (enabled) => {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!enabled) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [enabled]);
  return now;
};

/* ---- One room's card --------------------------------------------- */
const RoomCard = ({ room, session, sounds, defaults, busy, onPlay, onStop, nowMs }) => {
  const [soundKey, setSoundKey] = React.useState(null);
  const [minutes, setMinutes] = React.useState(null);

  const chosenSound = soundKey || (sounds[0] && sounds[0].key) || null;
  const chosenMinutes = minutes == null
    ? (defaults.default_timer_min ?? 45)
    : minutes;
  const remaining = session ? countdownTo(session.stop_at, nowMs) : null;
  const isStory = !!(session && String(session.sound_key || '').startsWith('story:'));

  return (
    <Card>
      <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <RoomChip name={room} online={!!session}/>
          {session ? (
            <>
              <Pill tone="brand" live>{isStory ? 'story' : 'playing'}</Pill>
              <span style={{ fontSize: 13, fontWeight: 500 }}>{session.title}</span>
              <span className="mono" style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--fg-muted)' }}>
                {remaining ? `${remaining} left` : (isStory ? 'until the end' : 'to file end')}
              </span>
            </>
          ) : (
            <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--fg-faint)' }}>
              quiet
            </span>
          )}
        </div>

        {session ? (
          <div style={{ display: 'flex', gap: 8 }}>
            <Button icon="square" disabled={busy} onClick={() => onStop(room)}>
              stop
            </Button>
          </div>
        ) : (
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            {/* sound picker */}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {sounds.map(s => (
                <button
                  key={s.key}
                  className={`btn ${chosenSound === s.key ? 'btn-primary' : 'btn-secondary'}`}
                  onClick={() => setSoundKey(s.key)}
                >
                  {s.title.toLowerCase()}
                </button>
              ))}
            </div>
            {/* timer presets */}
            <div style={{ display: 'flex', gap: 6, marginLeft: 'auto', alignItems: 'center' }}>
              {TIMER_PRESETS.map(m => (
                <button
                  key={m}
                  className={`btn ${chosenMinutes === m ? 'btn-primary' : 'btn-ghost'}`}
                  style={{ fontSize: 11 }}
                  onClick={() => setMinutes(m)}
                >
                  {m}m
                </button>
              ))}
              <Button icon="play" disabled={busy || !chosenSound}
                      onClick={() => onPlay(room, chosenSound, chosenMinutes)}>
                play
              </Button>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
};

/* ---- Play-in-this-browser card ------------------------------------ */
/* The no-satellite path: sounds play through the app-level browser
 * player (mini-player bar), with its sleep timer driving the fade-out
 * the room path gets from the timer worker. */
const BrowserPlayCard = ({ sounds, defaults, fire }) => {
  const player = usePlayback();
  const [minutes, setMinutes] = React.useState(null);
  const chosenMinutes = minutes == null
    ? (defaults.default_timer_min ?? 45)
    : minutes;

  const onPlayHere = (s) => {
    if (!player.available) { fire('browser player not available'); return; }
    const durationSec = (defaults.file_length_min || 0) * 60;
    player.playItems([sleepQueueItem(s, durationSec)]);
    if (chosenMinutes > 0) player.setSleep(chosenMinutes);
    fire(chosenMinutes > 0
      ? `${s.title.toLowerCase()} here for ${chosenMinutes} min`
      : `${s.title.toLowerCase()} here — plays to file end`);
  };

  return (
    <Card title="play in this browser">
      <div style={{ padding: '4px 14px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {sounds.map(s => (
            <button key={s.key} className="btn btn-secondary"
                    onClick={() => onPlayHere(s)}>
              {s.title.toLowerCase()}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: 'var(--fg-muted)' }}>stop after</span>
          {TIMER_PRESETS.map(m => (
            <button key={m}
                    className={`btn ${chosenMinutes === m ? 'btn-primary' : 'btn-ghost'}`}
                    style={{ fontSize: 11 }}
                    onClick={() => setMinutes(m)}>
              {m}m
            </button>
          ))}
          <button className={`btn ${chosenMinutes === 0 ? 'btn-primary' : 'btn-ghost'}`}
                  style={{ fontSize: 11 }}
                  onClick={() => setMinutes(0)}>
            file end
          </button>
        </div>
      </div>
    </Card>
  );
};

/* ---- Storytime ----------------------------------------------------- */
/* An AI bedtime story that runs as long as asked. A room story is
 * written, voiced in that room's voice and queued in the room by the
 * core's writer; a browser story is voiced in the server's voice and
 * `storyPoller` feeds its passages to the dashboard player as they land
 * (polling GET /stories/{id}, which is also its heartbeat). The poller
 * lives outside the page, so the story keeps coming while you browse
 * elsewhere in the dashboard; the player is app-level too. */
const STORY_TONES = ['cozy', 'silly', 'adventure', 'magical', 'calm'];
const STORY_AUDIENCES = [['kids', 'kids'], ['grown-ups', 'grown-ups']];
const STORY_POLL_MS = 5000;
const BROWSER_TARGET = '__browser__';

const storyQueueItem = (story, seg) => ({
  uid: `story-${story.id}-${seg.n}`,
  kind: 'sleep',
  trackId: null,
  title: story.title || 'Bedtime story',
  artist: 'Domovoi storytime',
  album: '',
  src: seg.src,
  coverUrl: null,
  durationSec: seg.duration_sec || null,
  seekable: true,
  cacheable: false,
  meta: { story: story.id, part: seg.n },
});

const storyStatusWords = (st) => {
  if (!st) return '';
  if (st.status === 'writing') return 'writing the first part…';
  if (st.status === 'playing') return st.writing_done ? 'ending written' : 'telling';
  if (st.status === 'failed') return `couldn't write it${st.error ? `: ${st.error}` : ''}`;
  return st.status;
};

/* One browser story at a time, polled for as long as the dashboard is
 * open. `player` is the latest usePlayback() value the Sleep page saw;
 * `listener` is the mounted card's state setter (null while the page is
 * away). */
const storyPoller = {
  story: null, queued: new Set(), bedQueued: false, timer: null,
  player: null, listener: null, sounds: [], timerMin: 45,

  start(story) {
    this.stop();
    this.story = story;
    this.queued = new Set();
    this.bedQueued = false;
    this.poll();
    this.timer = setInterval(() => this.poll(), STORY_POLL_MS);
  },
  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  },
  forget() {
    this.stop();
    this.story = null;
    if (this.listener) this.listener(null);
  },
  async poll() {
    const story = this.story;
    const player = this.player;
    if (!story || !player) return;
    let data;
    try {
      data = await apiGet(`${SLEEP_API}/stories/${story.id}`, { quiet: true });
    } catch (e) {
      return;                       /* retried on the next poll */
    }
    if (!this.story || this.story.id !== story.id) return;
    const fresh = data.segments.filter(g => !this.queued.has(g.n));
    if (fresh.length) {
      const items = fresh.map(g => storyQueueItem(data.story, g));
      const first = this.queued.size === 0;
      fresh.forEach(g => this.queued.add(g.n));
      if (first) {
        player.playItems(items);
      } else {
        const wasIdle = player.status !== 'playing';
        player.enqueue(items);
        if (wasIdle) player.next();  /* it ran dry waiting for this one */
      }
    }
    const st = data.story;
    // The sleep sound carries on after the ending, until the sleep timer.
    if (st.writing_done && st.bed_sound && !this.bedQueued
        && this.queued.size === data.segments.length) {
      const bed = this.sounds.find(x => x.key === st.bed_sound);
      if (bed) {
        this.bedQueued = true;
        player.enqueue([sleepQueueItem(bed, null)]);
        const left = data.segments.reduce((a, g) => a + (g.duration_sec || 0), 0)
          - Math.max(0, (Date.now() - new Date(st.created_at).getTime()) / 1000);
        player.setSleep(Math.max(1, Math.round(Math.max(0, left) / 60) + this.timerMin));
      }
    }
    this.story = st;
    if (this.listener) this.listener(st);
    if (!['writing', 'playing'].includes(st.status)) this.stop();
  },
};

const inputStyle = {
  width: '100%', boxSizing: 'border-box', fontSize: 13, padding: '8px 10px',
  borderRadius: 8, border: '1px solid var(--line, var(--border, rgba(127,127,127,.35)))',
  background: 'transparent', color: 'inherit', fontFamily: 'inherit',
};

const ChipRow = ({ label, children }) => (
  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
    <span style={{ fontSize: 11, color: 'var(--fg-muted)', minWidth: 64 }}>{label}</span>
    {children}
  </div>
);

const StoryCard = ({ rooms, sounds, sizes, roomStories, defaults, busy, fire, refresh }) => {
  const player = usePlayback();
  const [target, setTarget] = React.useState(null);
  const [sizeKey, setSizeKey] = React.useState('m');
  const [customMin, setCustomMin] = React.useState('');
  const [premise, setPremise] = React.useState('');
  const [tone, setTone] = React.useState('cozy');
  const [audience, setAudience] = React.useState('kids');
  const [bed, setBed] = React.useState('');
  const [ideas, setIdeas] = React.useState([]);
  const [thinking, setThinking] = React.useState(false);
  const [starting, setStarting] = React.useState(false);
  const [browserStory, setBrowserStory] = React.useState(storyPoller.story);

  const chosenTarget = target || rooms[0] || BROWSER_TARGET;
  const custom = parseInt(customMin, 10);
  const minutes = custom > 0
    ? Math.min(custom, 180)
    : (sizes.find(s => s.key === sizeKey) || { minutes: 10 }).minutes;

  // The poller outlives this card: hand it the latest player and sounds,
  // and listen to it while the card is on screen.
  storyPoller.player = player;
  storyPoller.sounds = sounds;
  storyPoller.timerMin = (defaults && defaults.default_timer_min) || 45;
  React.useEffect(() => {
    storyPoller.listener = setBrowserStory;
    return () => { storyPoller.listener = null; };
  }, []);

  const surprise = async () => {
    setThinking(true);
    try {
      const r = await apiPost(`${SLEEP_API}/stories/ideas`, { tone, audience, hint: premise });
      setIdeas(r.ideas || []);
      if (!(r.ideas || []).length) fire('no ideas came back — try again');
    } catch (e) {
      fire(`couldn't get ideas: ${e.message}`);
    } finally {
      setThinking(false);
    }
  };

  const start = async () => {
    if (chosenTarget === BROWSER_TARGET && !player.available) {
      fire('browser player not available'); return;
    }
    setStarting(true);
    try {
      const r = await apiPost(`${SLEEP_API}/stories`, {
        room_id: chosenTarget === BROWSER_TARGET ? null : chosenTarget,
        minutes, premise, tone, audience, bed_sound: bed || null,
      });
      if (chosenTarget === BROWSER_TARGET) {
        setBrowserStory(r.story);
        storyPoller.start(r.story);
        fire(`writing a ${minutes}-minute story — it starts here in a moment`);
      } else {
        fire(`writing a ${minutes}-minute story for ${chosenTarget}`);
      }
      setIdeas([]);
      refresh();
    } catch (e) {
      fire(`couldn't start the story: ${e.message}`);
    } finally {
      setStarting(false);
    }
  };

  const stopStory = async (id, isBrowser) => {
    try {
      await apiPost(`${SLEEP_API}/stories/${id}/stop`, {});
      if (isBrowser) {
        player.stop();
        storyPoller.forget();
      }
      fire('story stopped');
      refresh();
    } catch (e) {
      fire(`stop failed: ${e.message}`);
    }
  };

  const liveBrowser = browserStory && ['writing', 'playing'].includes(browserStory.status)
    ? browserStory : null;

  return (
    <Card title="storytime">
      <div style={{ padding: '4px 14px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        {(roomStories.length > 0 || browserStory) && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {roomStories.map(st => (
              <div key={st.id} style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <RoomChip name={st.room_id} online={st.status === 'playing'}/>
                <Pill tone={st.status === 'failed' ? 'warn' : 'brand'} live={st.status !== 'failed'}>
                  {storyStatusWords(st)}
                </Pill>
                <span style={{ fontSize: 13, fontWeight: 500 }}>{st.title || st.premise || 'a bedtime story'}</span>
                <span className="mono" style={{ fontSize: 11, color: 'var(--fg-muted)' }}>{st.minutes}m</span>
                {st.status !== 'failed' && (
                  <Button icon="square" disabled={busy} style={{ marginLeft: 'auto' }}
                          onClick={() => stopStory(st.id, false)}>stop</Button>
                )}
              </div>
            ))}
            {browserStory && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <Pill tone={browserStory.status === 'failed' ? 'warn' : 'brand'} live={!!liveBrowser}>
                  this browser · {storyStatusWords(browserStory)}
                </Pill>
                <span style={{ fontSize: 13, fontWeight: 500 }}>
                  {browserStory.title || browserStory.premise || 'a bedtime story'}
                </span>
                {liveBrowser ? (
                  <Button icon="square" style={{ marginLeft: 'auto' }}
                          onClick={() => stopStory(browserStory.id, true)}>stop</Button>
                ) : (
                  <button className="btn btn-ghost" style={{ marginLeft: 'auto', fontSize: 11 }}
                          onClick={() => storyPoller.forget()}>clear</button>
                )}
              </div>
            )}
          </div>
        )}

        <textarea
          rows={2}
          style={{ ...inputStyle, resize: 'vertical' }}
          placeholder="what's it about? e.g. a sleepy dragon who can't find his blanket (leave empty and I'll pick)"
          value={premise}
          maxLength={500}
          onChange={(e) => setPremise(e.target.value)}
        />

        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          <button className="btn btn-secondary" disabled={thinking} onClick={surprise}>
            {thinking ? 'thinking…' : 'surprise me'}
          </button>
          {ideas.map((idea, i) => (
            <button key={i} className={`btn ${premise === idea ? 'btn-primary' : 'btn-ghost'}`}
                    style={{ fontSize: 12, textAlign: 'left', whiteSpace: 'normal' }}
                    onClick={() => setPremise(idea)}>
              {idea}
            </button>
          ))}
        </div>

        <ChipRow label="length">
          {sizes.map(s => (
            <button key={s.key}
                    className={`btn ${!(custom > 0) && sizeKey === s.key ? 'btn-primary' : 'btn-ghost'}`}
                    style={{ fontSize: 11 }}
                    title={`${s.minutes} minutes`}
                    onClick={() => { setSizeKey(s.key); setCustomMin(''); }}>
              {s.label} · {s.minutes}m
            </button>
          ))}
          <input type="number" min="1" max="180" placeholder="min"
                 value={customMin} onChange={(e) => setCustomMin(e.target.value)}
                 style={{ ...inputStyle, width: 72, padding: '4px 8px', fontSize: 12 }}
                 aria-label="custom length in minutes"/>
        </ChipRow>

        <ChipRow label="tone">
          {STORY_TONES.map(t => (
            <button key={t} className={`btn ${tone === t ? 'btn-primary' : 'btn-ghost'}`}
                    style={{ fontSize: 11 }} onClick={() => setTone(t)}>{t}</button>
          ))}
        </ChipRow>

        <ChipRow label="for">
          {STORY_AUDIENCES.map(([k, label]) => (
            <button key={k} className={`btn ${audience === k ? 'btn-primary' : 'btn-ghost'}`}
                    style={{ fontSize: 11 }} onClick={() => setAudience(k)}>{label}</button>
          ))}
        </ChipRow>

        <ChipRow label="underneath">
          <select value={bed} onChange={(e) => setBed(e.target.value)}
                  style={{ ...inputStyle, width: 'auto', padding: '4px 8px', fontSize: 12 }}>
            <option value="">no sound</option>
            {sounds.map(s => <option key={s.key} value={s.key}>{s.title.toLowerCase()}</option>)}
          </select>
          {bed && (
            <span style={{ fontSize: 11, color: 'var(--fg-faint)' }}>
              keeps playing after the story, until the sleep timer
            </span>
          )}
        </ChipRow>

        <ChipRow label="play in">
          <select value={chosenTarget} onChange={(e) => setTarget(e.target.value)}
                  style={{ ...inputStyle, width: 'auto', padding: '4px 8px', fontSize: 12 }}>
            {rooms.map(r => <option key={r} value={r}>{r}</option>)}
            <option value={BROWSER_TARGET}>this browser</option>
          </select>
          <Button icon="play" disabled={busy || starting} style={{ marginLeft: 'auto' }}
                  onClick={start}>
            {starting ? 'starting…' : `tell a ${minutes}-minute story`}
          </Button>
        </ChipRow>

        <div style={{ fontSize: 11, color: 'var(--fg-faint)' }}>
          written as it plays by the house's AI model, in the room's own voice
          (this browser: the server's voice) · or just say "tell me a huge
          bedtime story about otters"
        </div>
      </div>
    </Card>
  );
};

/* ---- Page ---------------------------------------------------------- */
const SleepPage = () => {
  const [fire, toastNode] = useToast();
  const [busy, setBusy] = React.useState(false);

  const { data: overview, loading, refresh } = useApiObject(
    `${SLEEP_API}/overview`,
    { eventTypes: ['sleep.sessions.changed'] },
  );

  const sessions = overview?.sessions || [];
  const rooms = overview?.rooms || [];
  const sounds = overview?.sounds || [];
  const defaults = overview?.defaults || {};
  const nowMs = useNow(sessions.length > 0);
  const byRoom = Object.fromEntries(sessions.map(s => [s.room_id, s]));

  // First-run rendering takes minutes and there's no NOTIFY for it —
  // poll gently while the core reports generation in flight so the
  // sounds appear without a manual reload.
  const generating = !!overview?.generating;
  React.useEffect(() => {
    if (!generating) return;
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [generating, refresh]);

  const onPlay = async (room, sound, minutes) => {
    setBusy(true);
    try {
      await apiPost(`${SLEEP_API}/play`, { room_id: room, sound, minutes });
      fire(`${sound} in ${room}`);
      refresh();
    } catch (e) {
      fire(`play failed: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const onStop = async (room) => {
    setBusy(true);
    try {
      await apiPost(`${SLEEP_API}/stop`, { room_id: room, fade: false });
      fire(`stopped in ${room}`);
      refresh();
    } catch (e) {
      fire(`stop failed: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const onRegenerate = async () => {
    setBusy(true);
    try {
      await apiPost(`${SLEEP_API}/regenerate`, {});
      fire('rendering sounds — this takes a few minutes');
      refresh();
    } catch (e) {
      // 401/403 pops the admin login modal (data.js); anything else is
      // an honest failure toast.
      fire(`couldn't start rendering: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Sleep"
        sub={sessions.length
          ? `${sessions.length} room${sessions.length !== 1 ? 's' : ''} playing · sounds are synthesized locally`
          : 'per-room sleep sounds · synthesized locally, fully offline'}
      />

      {overview && overview.core_online === false && (
        <Card>
          <div style={{ padding: '10px 16px', fontSize: 12, color: 'var(--warn-ink, var(--warn))' }}>
            {overview.waiting_for_restart
              ? `Sleep ${overview.waiting_for_restart.to_version || ''} is installed — restart `
                + 'Domovoi to finish the upgrade (Plugins page). Controls are off until then.'
              : "core service unreachable — controls are disabled until it's back"}
          </div>
        </Card>
      )}
      {overview?.generating && (
        <Card>
          <div style={{ padding: '10px 16px', fontSize: 12, color: 'var(--fg-muted)' }}>
            rendering sound files — this takes a few minutes on first run
          </div>
        </Card>
      )}
      {overview && overview.core_online !== false && !overview.generating
        && sounds.length === 0 && overview.generator_error && (
        <Card>
          <div style={{ padding: '10px 16px', display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ fontSize: 12, color: 'var(--warn-ink, var(--warn))' }}>
              sound generation failed — no files were rendered
            </div>
            <div className="mono" style={{ fontSize: 11, color: 'var(--fg-muted)',
                                           whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {String(overview.generator_error).slice(0, 300)}
            </div>
            <div>
              <Button icon="refresh-cw" disabled={busy} onClick={onRegenerate}>
                retry rendering
              </Button>
            </div>
          </div>
        </Card>
      )}

      {sounds.length > 0 && (
        <BrowserPlayCard sounds={sounds} defaults={defaults} fire={fire}/>
      )}

      {overview && overview.core_online !== false && (overview.story_sizes || []).length > 0 && (
        <StoryCard rooms={rooms} sounds={sounds} sizes={overview.story_sizes}
                   roomStories={overview.stories || []} defaults={defaults} busy={busy}
                   fire={fire} refresh={refresh}/>
      )}

      {loading && !overview ? (
        <div style={{ padding: 28, textAlign: 'center', fontSize: 12, color: 'var(--fg-muted)' }}>
          loading…
        </div>
      ) : rooms.length === 0 ? (
        <Card>
          <Empty
            title="no rooms yet"
            sub="connect a satellite for per-room playback — until then, sounds play right here in the browser"
          />
        </Card>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {rooms.map(room => (
            <RoomCard key={room} room={room} session={byRoom[room] || null}
                      sounds={sounds} defaults={defaults} busy={busy}
                      onPlay={onPlay} onStop={onStop} nowMs={nowMs}/>
          ))}
        </div>
      )}

      <Card title="add your own sounds">
        <div style={{ padding: '4px 16px 14px', fontSize: 12, color: 'var(--fg-muted)',
                      display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div>
            · drop any audio file into the <span className="mono">Ambient sounds</span>{' '}
            folder inside your music library — it becomes a voice-addressable
            sound named after the file
          </div>
          <div>
            · voice works from any room: "play brown noise", "play ocean
            sounds in the bedroom for 30 minutes", "stop the sounds"
          </div>
          <div>
            · sounds fade out when their timer ends, and every file ends on
            its own — nothing plays until morning by accident
          </div>
        </div>
      </Card>

      {toastNode}
    </div>
  );
};

/* Namespaced window registry — the only global export. */
window.DomovoiPlugins = window.DomovoiPlugins || {};
window.DomovoiPlugins.sleep = window.DomovoiPlugins.sleep || {};
window.DomovoiPlugins.sleep.pages = Object.assign(
  {}, window.DomovoiPlugins.sleep.pages, { SleepPage },
);
