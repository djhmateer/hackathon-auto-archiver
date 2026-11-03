-- Shadow mode is gone: a tab that's off is no longer read, so nothing records what it would have queued
DROP TABLE shadow_jobs;
