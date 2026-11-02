{
    "name": "Queue Feeder Database",
    "type": ["feeder", "database"],
    "entry_point": "queue_feeder_db::QueueFeederDB",
    "requires_setup": True,
    "dependencies": {
        "python": ["psycopg", "loguru"],
    },
    "configs": {
        "job_id": {"default": None, "help": "the id of the job the supervisor claimed", "type": "int"},
        "lease_token": {"default": None, "help": "the lease token of that claim"},
    },
    "description": """
Archives the one job the job queue's supervisor claimed, and saves the result to Postgres. The sheet writer then
updates the sheet, so a Sheets outage can't fail a row. See queue-spec.md

The supervisor sets these settings. The connection string comes from the .env file in the repo root.
""",
}
