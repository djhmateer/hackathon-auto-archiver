-- Telegram: 2 at once. Every worker uses the same session, which is safe as they all go out through one IP
-- (Telegram revokes a session used from two IPs at the same time)
INSERT INTO site_limits (site, max_running) VALUES ('t.me', 2)
    ON CONFLICT (site) DO UPDATE SET max_running = 2;
