-- YouTube uses no cookies (only a PO token), so there's no account to get banned: 3 at once
INSERT INTO site_limits (site, max_running) VALUES ('youtube.com', 3)
    ON CONFLICT (site) DO UPDATE SET max_running = 3;
