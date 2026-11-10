-- The test pool is now hms. Its organisations and workers move with it
INSERT INTO pools (name) VALUES ('hms');
UPDATE organisations SET pool = 'hms' WHERE pool = 'test';
UPDATE workers SET pool = 'hms' WHERE pool = 'test';
DELETE FROM pools WHERE name = 'test';
