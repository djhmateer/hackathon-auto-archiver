-- Sent by each worker with its heartbeat, for the dashboard: the code it runs, and its disk, memory and swap
ALTER TABLE heartbeats
    ADD COLUMN git_commit text,
    ADD COLUMN disk_free_bytes bigint,
    ADD COLUMN mem_available_bytes bigint,
    ADD COLUMN mem_total_bytes bigint,
    ADD COLUMN swap_used_bytes bigint;
