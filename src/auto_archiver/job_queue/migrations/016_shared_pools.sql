-- A shared pool's workers also claim the jobs of any other pool whose workers are all busy or down, eg the hms
-- workers help cir and glan when theirs are full
ALTER TABLE pools ADD COLUMN shared boolean NOT NULL DEFAULT false;
UPDATE pools SET shared = true WHERE name = 'hms';
