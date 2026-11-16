-- What each step of an attempt achieved, for a client to see why a row is what it is:
--   success   it worked
--   partial   it worked, with a known issue noted, eg some uploads failed
--   blocked   the site blocked it
--   failed    it raised an error, or found nothing
--   skipped   an extractor that isn't for this site
-- detail is one short line safe to show a client, eg 'blocked by site - HTTP 403', never a raw error or log line.
-- Both empty for steps from before this, and for the step an attempt was in when it was killed or crashed
ALTER TABLE job_stages
    ADD COLUMN result text CHECK (result IN ('success', 'partial', 'blocked', 'failed', 'skipped')),
    ADD COLUMN detail text;
