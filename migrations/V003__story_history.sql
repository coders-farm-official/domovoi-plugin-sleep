-- V003: Storytime history (runs with search_path = plugin_sleep, public).
--
-- The Sleep page lists the stories told in the last 30 days, in which
-- room, with their text. Two facts the history needs that V002 didn't
-- keep:
--
-- * ``ended_at`` — when the story stopped being live (done, stopped or
--   failed). ``updated_at`` stood in for it, but any later write moves
--   it; the history's "how long it ran" and the 30-day clock read this.
-- * ``files_tidied`` — the passages' audio files are deleted 12 hours
--   after the story ends (disk matters; nobody replays a story), but the
--   passage TEXT stays in story_segments for the 30 days. This marks the
--   stories whose files are already gone, so the tidy pass doesn't
--   re-walk them every tick.
--
-- Stories that ended before this migration take their last update as
-- their end.

ALTER TABLE stories ADD COLUMN ended_at TIMESTAMPTZ;
ALTER TABLE stories ADD COLUMN files_tidied BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE stories SET ended_at = updated_at
 WHERE status NOT IN ('writing', 'playing');

-- The history reads newest first by id (its cursor); the tidy pass reads
-- ended stories by age.
CREATE INDEX stories_ended ON stories (ended_at) WHERE ended_at IS NOT NULL;
