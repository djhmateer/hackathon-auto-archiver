-- The last error a sheet's jobs hit in setup, eg a missing token file, so the dashboard can say why it's paused
ALTER TABLE sheets ADD COLUMN setup_error text;
