-- Separate groups of worker VMs. A worker only claims jobs from the sheets of its own pool's organisations
CREATE TABLE pools (name text PRIMARY KEY);
INSERT INTO pools (name) VALUES ('glan'), ('test'), ('cir');

-- Who a sheet belongs to, eg for a dashboard each organisation can log in to. All of an organisation's sheets are
-- archived by its one pool
CREATE TABLE organisations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE,
    pool text NOT NULL REFERENCES pools
);

-- empty until set by hand: a sheet with no organisation is never claimed
ALTER TABLE sheets ADD COLUMN organisation_id bigint REFERENCES organisations;

-- each worker VM's pool, by its hostname. A worker that isn't here claims nothing
CREATE TABLE workers (
    name text PRIMARY KEY,
    pool text NOT NULL REFERENCES pools
);
