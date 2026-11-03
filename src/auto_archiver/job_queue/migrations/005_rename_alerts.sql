-- Alerts are for whoever runs the queue, not clients, so leave room for client notifications later
ALTER TABLE alerts RENAME TO admin_alerts;
