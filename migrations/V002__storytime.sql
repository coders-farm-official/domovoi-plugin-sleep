-- V002: Storytime (runs with search_path = plugin_sleep, public).
--
-- One row per story. room_id NULL is a story playing in a browser (the
-- Sleep page's "play here"), which heartbeats ``last_seen_at`` while it
-- listens. The writer worker reads ``summary`` + ``last_passage`` to
-- write the next passage, so a long story keeps its thread without a
-- long model context, and a core restart mid-story carries on from the
-- row.
--
-- status: writing (no audio yet) -> playing -> done | stopped | failed.
-- ``writing_done`` is set once the ending is written; the story is
-- "done" when its room or browser has played it out.

CREATE TABLE stories (
    id                  BIGSERIAL PRIMARY KEY,
    room_id             TEXT,
    title               TEXT        NOT NULL DEFAULT '',
    premise             TEXT        NOT NULL DEFAULT '',
    tone                TEXT        NOT NULL DEFAULT 'cozy',
    audience            TEXT        NOT NULL DEFAULT 'kids',
    minutes             INTEGER     NOT NULL,
    bed_sound           TEXT,
    status              TEXT        NOT NULL DEFAULT 'writing',
    writing_done        BOOLEAN     NOT NULL DEFAULT FALSE,
    summary             TEXT        NOT NULL DEFAULT '',
    last_passage        TEXT        NOT NULL DEFAULT '',
    segments            INTEGER     NOT NULL DEFAULT 0,
    audio_sec           DOUBLE PRECISION NOT NULL DEFAULT 0,
    words               INTEGER     NOT NULL DEFAULT 0,
    playback_started_at TIMESTAMPTZ,
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX stories_live ON stories (status) WHERE status IN ('writing', 'playing');

CREATE TABLE story_segments (
    story_id     BIGINT      NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    n            INTEGER     NOT NULL,
    filename     TEXT        NOT NULL,
    duration_sec DOUBLE PRECISION NOT NULL,
    text         TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (story_id, n)
);
