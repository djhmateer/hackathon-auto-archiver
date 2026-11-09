-- CPU use since the worker's previous heartbeat, 0-100 across all its cores, for the dashboard
ALTER TABLE heartbeats ADD COLUMN cpu_percent real;
