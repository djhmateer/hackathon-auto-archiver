-- Sites that may run more than one job at once. Any other site runs one at a time
CREATE TABLE site_limits (
    site text PRIMARY KEY,  -- as jobs.domain, eg instagram.com
    max_running int NOT NULL
);

-- Drive and Docs jobs use no cookies, so there's no account to get banned: 100 is more than there are workers, so no
-- limit. Instagram's log in, so 2 lets a long profile job run without holding back every post
INSERT INTO site_limits (site, max_running) VALUES ('drive.google.com', 100), ('docs.google.com', 100), ('instagram.com', 2);
