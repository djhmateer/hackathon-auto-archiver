"""The coordinator's alert emails. Postmark is mocked: nothing is sent"""

import pytest

from auto_archiver.job_queue import alerts, db

SETTINGS = "POSTMARK_API_TOKEN=token1\nPOSTMARK_FROM_EMAIL=from@example.com\nALERT_EMAIL_TO=to@example.com\n"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(alerts, "_next_try", 0.0)


@pytest.fixture
def settings(tmp_path):
    with open(tmp_path / ".env", "a") as f:
        f.write(SETTINGS)


@pytest.fixture
def send(mocker):
    return mocker.patch.object(alerts, "send_email")


def unsent(conn):
    return [a["kind"] for a in conn.execute("SELECT kind FROM admin_alerts WHERE sent_at IS NULL ORDER BY id")]


def test_emails_a_site_blocked_alert_and_marks_it_sent(conn, settings, send):
    db.add_alert(conn, "site_blocked", "idf.il blocked tab tab1 row 2. Clear the row's status\n\nURL: <url>")
    alerts.email_alerts(conn)
    token, from_email, to, subject, body = send.call_args.args
    assert (token, from_email, to) == ("token1", "from@example.com", "to@example.com")
    assert subject == "Auto Archiver: idf.il blocked tab tab1 row 2. Clear the row's status"
    assert body.startswith("idf.il blocked tab tab1 row 2. Clear the row's status\n\nURL: <url>\n\nRaised ")
    assert unsent(conn) == []


def test_other_kinds_are_left_for_the_digest(conn, settings, send):
    db.add_alert(conn, "sheet_paused", "Sheet 1 paused")
    alerts.email_alerts(conn)
    send.assert_not_called()
    assert unsent(conn) == ["sheet_paused"]


def test_without_settings_nothing_is_sent_and_it_waits(conn, send, caplog):
    db.add_alert(conn, "site_blocked", "blocked")
    alerts.email_alerts(conn)
    send.assert_not_called()
    assert "isn't set in .env" in caplog.text
    assert unsent(conn) == ["site_blocked"]
    assert alerts._next_try > 0


def test_a_failed_send_stays_unsent_and_waits_before_trying_again(conn, settings, send):
    send.side_effect = RuntimeError("Postmark returned 500")
    db.add_alert(conn, "site_blocked", "blocked")
    alerts.email_alerts(conn)
    alerts.email_alerts(conn)
    assert send.call_count == 1
    assert unsent(conn) == ["site_blocked"]


def test_send_email_posts_to_postmark(mocker):
    post = mocker.patch.object(alerts.requests, "post")
    post.return_value.ok = True
    alerts.send_email("token1", "from@example.com", "to@example.com", "a subject", "a body")
    assert post.call_args.args == (alerts.POSTMARK_URL,)
    assert post.call_args.kwargs["headers"]["X-Postmark-Server-Token"] == "token1"
    assert post.call_args.kwargs["json"] == {
        "From": "from@example.com",
        "To": "to@example.com",
        "Subject": "a subject",
        "TextBody": "a body",
        "MessageStream": "outbound",
    }


def test_send_email_raises_on_an_error_response(mocker):
    post = mocker.patch.object(alerts.requests, "post")
    post.return_value.ok = False
    post.return_value.status_code = 422
    post.return_value.text = '{"ErrorCode": 300}'
    with pytest.raises(RuntimeError, match="422"):
        alerts.send_email("token1", "from@example.com", "to@example.com", "a subject", "a body")
