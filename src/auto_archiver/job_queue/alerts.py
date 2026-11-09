"""
Emails the admin alerts that need acting on, through Postmark, from the coordinator. Other kinds of alert stay
unsent, for the daily digest in queue-spec.md.

Needs these lines in the coordinator's .env:
    POSTMARK_API_TOKEN=<server token>
    POSTMARK_FROM_EMAIL=<a sender signature in Postmark>
    ALERT_EMAIL_TO=<where alerts go>
"""

import time

import requests

from auto_archiver.job_queue import db
from auto_archiver.utils.custom_logger import logger

EMAIL_KINDS = (
    "site_blocked",  # queue_feeder_db: a site we expect to let us back in blocked the archiver, so requeue the row
    "sheet_paused",  # db.release_setup_failure: a sheet's jobs keep failing in setup, eg a bad config or missing secret
    "worker_paused",  # worker_supervisor: a worker's jobs keep failing quickly, so it has stopped claiming for a while
    "writer_discarded",  # db.discard_job_writes: a result couldn't go to the sheet, as its row moved or tab went
    "reboot_failed",  # worker_supervisor: a worker couldn't do its daily reboot
)
# not emailed: sheet_disagrees, raised once a day per row while a row's status and the queue disagree
POSTMARK_URL = "https://api.postmarkapp.com/email"
# after a failure, or with no settings, so a Postmark outage doesn't log every cycle
RETRY_SECONDS = 300

_next_try = 0.0


def send_email(token: str, from_email: str, to: str, subject: str, body: str) -> None:
    response = requests.post(
        POSTMARK_URL,
        headers={"X-Postmark-Server-Token": token, "Accept": "application/json"},
        json={"From": from_email, "To": to, "Subject": subject, "TextBody": body, "MessageStream": "outbound"},
        timeout=10,
    )
    if not response.ok:
        raise RuntimeError(f"Postmark returned {response.status_code}: {response.text}")


def email_alerts(conn) -> None:
    """Emails each unsent alert of EMAIL_KINDS and marks it sent. Its message's first line is the subject"""
    global _next_try
    if time.monotonic() < _next_try:
        return
    alerts = db.get_unsent_alerts(conn, EMAIL_KINDS)
    if not alerts:
        return

    token, from_email, to = (db.env_value(n) for n in ("POSTMARK_API_TOKEN", "POSTMARK_FROM_EMAIL", "ALERT_EMAIL_TO"))
    if not (token and from_email and to):
        logger.warning(
            f"{len(alerts)} alert(s) to email, but POSTMARK_API_TOKEN, POSTMARK_FROM_EMAIL or ALERT_EMAIL_TO "
            f"isn't set in .env. Trying again in {RETRY_SECONDS // 60} min"
        )
        _next_try = time.monotonic() + RETRY_SECONDS
        return

    for alert in alerts:
        subject = f"Auto Archiver: {alert['message'].splitlines()[0]}"[:200]
        body = (
            f"{alert['message']}\n\nRaised {alert['created_at']:%Y-%m-%d %H:%M} UTC, alert {alert['id']} "
            f"({alert['kind']}). See Troubleshooting in README_QUEUE.md"
        )
        try:
            send_email(token, from_email, to, subject, body)
        except Exception as e:
            logger.error(f"Couldn't email alert {alert['id']}, trying again in {RETRY_SECONDS // 60} min: {e}")
            _next_try = time.monotonic() + RETRY_SECONDS
            return
        db.mark_alert_sent(conn, alert["id"])
        logger.info(f"Emailed alert {alert['id']} ({alert['kind']})")
