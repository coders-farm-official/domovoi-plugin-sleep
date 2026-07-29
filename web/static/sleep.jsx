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
 * apiPost, useApiObject, usePlayback.
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

  return (
    <Card>
      <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <RoomChip name={room} online={!!session}/>
          {session ? (
            <>
              <Pill tone="brand" live>playing</Pill>
              <span style={{ fontSize: 13, fontWeight: 500 }}>{session.title}</span>
              <span className="mono" style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--fg-muted)' }}>
                {remaining ? `${remaining} left` : 'to file end'}
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
          <div style={{ padding: '10px 16px', fontSize: 12, color: 'var(--warn)' }}>
            core service unreachable — controls are disabled until it's back
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
            <div style={{ fontSize: 12, color: 'var(--warn)' }}>
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
