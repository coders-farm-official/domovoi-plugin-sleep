-- V001: the sleep plugin's schema (runs with search_path = plugin_sleep, public).
--
-- One row per room (upserted): the room's current/last sleep session.
-- ``active`` rows are enforced by the timer worker (stop_at) and
-- reconciled against what the room's player is actually doing; inactive
-- rows are kept as a tiny "last sound used" memory per room.
--
-- No foreign keys into core tables (room_id is a soft reference to
-- public.mpd_rooms — rooms are user-controlled strings, and a session
-- for a since-removed room simply reconciles away).

CREATE TABLE sleep_sessions (
    room_id     TEXT PRIMARY KEY,
    sound_key   TEXT        NOT NULL,
    title       TEXT        NOT NULL,
    mpd_uri     TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stop_at     TIMESTAMPTZ,
    active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
