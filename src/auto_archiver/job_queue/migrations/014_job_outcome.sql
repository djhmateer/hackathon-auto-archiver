-- What a finished job achieved, as 'done' only means the archiver ran to the end. Set by the worker from the same
-- status and notes as the sheet's status text:
--   success           archived, with no notes
--   partial           archived, with a known issue noted, eg a block, failed uploads (the detail is in the sheet text)
--   blocked           no extractor succeeded, and the site blocked us
--   nothing_archived  nothing usable, eg empty, or the site returned an HTTP 404
--   failed            the job failed
-- Empty for jobs that haven't finished, and for 'rejected' and 'cancelled' ones
ALTER TABLE jobs ADD COLUMN outcome text
    CHECK (outcome IN ('success', 'partial', 'blocked', 'nothing_archived', 'failed'));

-- Jobs finished before this, from the status text of their last sheet write. Every finished job has one
UPDATE jobs SET outcome = 'failed' WHERE status = 'failed';

UPDATE jobs j SET outcome = CASE
        WHEN w.s LIKE '%(site returned HTTP %' THEN 'nothing_archived'
        WHEN w.s NOT LIKE '%success%' AND (w.s LIKE '%(blocked by %' OR w.s LIKE '%(aa IP blocked)%') THEN 'blocked'
        WHEN w.s NOT LIKE '%success%' THEN 'nothing_archived'
        WHEN w.s LIKE '%success%(%' THEN 'partial'
        ELSE 'success'
    END
FROM (SELECT DISTINCT ON (job_id) job_id, cells->>'status' AS s FROM sheet_writes ORDER BY job_id, id DESC) w
WHERE w.job_id = j.id AND j.status = 'done';
