"""Sending each alert once, and knowing when not to record one.

The poll runs every ten minutes and a deadline stays due for hours, so the
interesting behaviour is all about what is *not* sent — and about the one case
where recording a failure would lose an alert for good.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from turonomics_api.db.models import (
    Notification,
    PushSubscription,
    Task,
    TaskKind,
    TaskState,
    Vehicle,
)
from turonomics_api.notify import webpush
from turonomics_api.notify.dispatch import LOG_ONLY, dispatch
from turonomics_api.notify.ece import b64url_encode
from turonomics_api.notify.vapid import generate_private_key

from .conftest import requires_db

pytestmark = requires_db

# From RFC 8291's worked example: a real P-256 point and a real auth secret, so
# the encryption actually runs rather than being stubbed past.
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH_SECRET = "BTBZMqHH6r4Tts7J_aSIgg"

NOW = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)
DUE = NOW + timedelta(hours=11)


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", generate_private_key())
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:ops@example.com")
    monkeypatch.setenv("ASP_ALERT_LEAD_MINUTES", "720,60")
    return True


@pytest.fixture()
def fleet(session):
    vehicle = Vehicle(nickname="Jimmy", make="Toyota", model="4Runner", year=2023)
    session.add(vehicle)
    session.flush()
    session.add(
        Task(
            vehicle_id=vehicle.id,
            kind=TaskKind.asp_move,
            state=TaskState.open,
            title="Move for street cleaning",
            due_by=DUE,
            location_label="Bergen St — north side",
        )
    )
    session.commit()
    return vehicle


def _subscribe(session, endpoint: str = "https://push.example.net/s/abc") -> PushSubscription:
    row = PushSubscription(endpoint=endpoint, p256dh=UA_PUBLIC, auth=AUTH_SECRET, label="iPhone")
    session.add(row)
    session.commit()
    return row


def _transport(status: int = 201, seen: list | None = None) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_an_approaching_deadline_is_sent_once_and_not_again(session, fleet, configured):
    """The condition stays true for eleven hours. Without a record of what has
    gone out, this alert arrives sixty-six times."""
    _subscribe(session)
    seen: list = []
    first = dispatch(session, now=NOW, http=_transport(seen=seen))
    session.commit()
    assert (first.considered, first.sent) == (1, 1)
    assert len(seen) == 1

    second = dispatch(session, now=NOW + timedelta(minutes=10), http=_transport(seen=seen))
    session.commit()
    assert (second.sent, second.already_sent) == (0, 1)
    assert len(seen) == 1, "nothing should have been posted the second time"


def test_what_is_posted_is_encrypted_and_signed(session, fleet, configured):
    _subscribe(session)
    seen: list = []
    dispatch(session, now=NOW, http=_transport(seen=seen))
    session.commit()
    request = seen[0]
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["authorization"].startswith("vapid t=")
    assert request.headers["ttl"] == str(webpush.TTL_SECONDS)
    body = request.read()
    assert b"Jimmy" not in body, "the push service must not be able to read it"
    assert len(body) > 86, "header plus a non-empty ciphertext"


def test_urgency_reaches_the_push_service_as_a_header(session, fleet, configured):
    """A push service reads ``Urgency`` to decide whether to wake a dozing
    device. Sending everything as high is how a phone's owner turns the whole
    thing off; sending everything as normal is how the last warning before a
    deadline arrives after it."""
    _subscribe(session)
    seen: list = []
    dispatch(session, now=NOW, http=_transport(seen=seen))
    session.commit()
    assert seen[0].headers["urgency"] == "normal", "twelve hours out is not urgent"

    task = session.scalars(select(Task)).one()
    task.due_by = NOW + timedelta(days=1, hours=1)
    session.commit()
    dispatch(session, now=NOW + timedelta(days=1, minutes=30), http=_transport(seen=seen))
    session.commit()
    assert seen[1].headers["urgency"] == "high", "half an hour out is"


def test_a_gone_subscription_is_deleted_rather_than_retried(session, fleet, configured):
    """410 is permanent. A row that can never succeed would otherwise report a
    failure every poll forever and bury the real ones."""
    _subscribe(session)
    result = dispatch(session, now=NOW, http=_transport(status=410))
    session.commit()
    assert result.subscriptions_dropped == 1
    assert session.scalars(select(PushSubscription)).all() == []


def test_a_transient_failure_is_not_recorded_so_the_next_poll_retries(
    session, fleet, configured
):
    """The opposite of the 410 case, and the one that matters: recording a
    failed send would discard the alert over a push service having a bad
    minute, and nothing would ever send it again."""
    _subscribe(session)
    failed = dispatch(session, now=NOW, http=_transport(status=503))
    session.commit()
    assert (failed.sent, failed.retrying) == (0, 1)
    assert session.scalars(select(Notification)).all() == []

    recovered = dispatch(session, now=NOW + timedelta(minutes=10), http=_transport(status=201))
    session.commit()
    assert recovered.sent == 1


def test_with_no_channel_configured_the_alert_is_still_decided_and_recorded(
    session, fleet, monkeypatch, caplog
):
    """Before any key exists the rules still run, so their timing can be
    checked against a real fleet. Silence would make "push is not configured"
    indistinguishable from "nothing was due"."""
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    with caplog.at_level("INFO"):
        result = dispatch(session, now=NOW)
    session.commit()
    assert result.sent == 1
    recorded = session.scalars(select(Notification)).one()
    assert recorded.channel == LOG_ONLY
    assert recorded.delivered == 0
    assert "Jimmy" in caplog.text


def test_nobody_subscribed_is_recorded_rather_than_retried_forever(
    session, fleet, configured
):
    """The state the app is in before anyone presses the button. There is
    nothing to retry against, so recording it keeps the log quiet; ``delivered``
    being zero is what tells it apart from a real send."""
    result = dispatch(session, now=NOW, http=_transport())
    session.commit()
    assert (result.sent, result.retrying) == (1, 0)
    assert session.scalars(select(Notification)).one().delivered == 0


def test_a_done_task_is_not_alerted_on(session, fleet, configured):
    task = session.scalars(select(Task)).one()
    task.state = TaskState.done
    session.commit()
    assert dispatch(session, now=NOW, http=_transport()).considered == 0


def test_a_quiet_poll_still_says_so(session, fleet, configured, caplog):
    """"Nothing is due" and "the dispatcher is not running" produce identical
    output if a quiet poll logs nothing — and the second one is the failure
    that matters. This project has shipped that bug twice already."""
    task = session.scalars(select(Task)).one()
    task.state = TaskState.done
    session.commit()
    with caplog.at_level("INFO", logger="turonomics.notify"):
        dispatch(session, now=NOW, http=_transport())
    assert "alerts: 0 due" in caplog.text


def test_the_record_points_at_the_task_so_an_alert_can_be_traced_back(
    session, fleet, configured
):
    dispatch(session, now=NOW, http=_transport())
    session.commit()
    task = session.scalars(select(Task)).one()
    assert session.scalars(select(Notification)).one().task_id == task.id


def test_one_bad_subscription_does_not_stop_the_others(session, fleet, configured):
    """This is the only path by which the operator's phone is told anything."""
    session.add(
        PushSubscription(
            endpoint="https://push.example.net/s/broken",
            p256dh=b64url_encode(b"not a curve point"),
            auth=AUTH_SECRET,
        )
    )
    session.commit()
    _subscribe(session, endpoint="https://push.example.net/s/good")
    seen: list = []
    dispatch(session, now=NOW, http=_transport(seen=seen))
    session.commit()
    assert len(seen) == 1, "the healthy subscription was still posted to"
    assert session.scalars(select(Notification)).one().delivered == 1


def test_a_deadline_that_rolls_forward_alerts_again(session, fleet, configured):
    """Same task row, next week's cleaning. Keyed on the task alone this is
    silently suppressed and the car stops being alerted on."""
    dispatch(session, now=NOW, http=_transport())
    session.commit()
    task = session.scalars(select(Task)).one()
    task.due_by = DUE + timedelta(days=7)
    session.commit()
    later = dispatch(session, now=NOW + timedelta(days=7), http=_transport())
    session.commit()
    assert later.sent == 1
    assert len(session.scalars(select(Notification)).all()) == 2


def test_a_broken_rule_does_not_stop_the_poll(session, fleet, configured, monkeypatch):
    """Alerting runs inside the poll that keeps the fleet view current. It is
    not allowed to take that down with it."""
    import turonomics_api.notify.dispatch as module

    def boom(*_args, **_kwargs):
        raise RuntimeError("bad rule")

    monkeypatch.setattr(module, "alerts_due", boom)
    assert dispatch(session, now=NOW).considered == 0


def test_a_test_push_is_not_recorded_against_the_dedupe_table(session, fleet, configured):
    """A wire test with a dedupe key would either suppress a real alert or be
    unrepeatable. It is sent straight through instead."""
    _subscribe(session)
    from turonomics_api.notify.alerts import Alert

    webpush.send(
        session,
        Alert(dedupe_key=f"test:{uuid.uuid4()}", title="t", body="b", url="https://x/"),
        http=_transport(),
    )
    session.commit()
    assert session.scalars(select(Notification)).all() == []
