-- outbox read like email. It holds the writes waiting to go to the sheets, so name it for that
ALTER TABLE outbox RENAME TO sheet_writes;
ALTER INDEX outbox_by_job RENAME TO sheet_writes_by_job;
