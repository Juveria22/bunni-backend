"""
failure counters

monitoring that can raise is worse than none, so most of this is about the
module staying quiet and safe when sentry is absent or broken
"""

import logging

import pytest

from services import monitoring
from services.monitoring import ALERT_EVENTS, count, report, snapshot


@pytest.fixture(autouse=True)
def clean_counters(monkeypatch):
    monkeypatch.setattr(monitoring, "_counts", monitoring.Counter())
    monkeypatch.setattr(monitoring, "_totals", monitoring.Counter())
    monkeypatch.setattr(monitoring, "_sentry", None)


def test_counting_accumulates():
    count("reminder.sent")
    count("reminder.sent", 3)
    assert snapshot()["reminder.sent"] == 4


def test_unseen_events_are_absent_rather_than_zero():
    assert "never.happened" not in snapshot()


def test_reporting_also_counts():
    report("agent.error")
    assert snapshot()["agent.error"] == 1


def test_reporting_works_without_sentry_configured():
    """the counters are the half that always works"""
    report("agent.error", ValueError("boom"), phone="+1***1234")
    assert snapshot()["agent.error"] == 1


def test_a_broken_sentry_cannot_take_down_the_caller():
    class ExplodingSentry:
        def push_scope(self):
            raise RuntimeError("sentry is broken")

    monitoring._sentry = ExplodingSentry()
    report("agent.error", ValueError("boom"))   # must not raise
    assert snapshot()["agent.error"] == 1


def test_alert_events_log_at_error_level(caplog):
    with caplog.at_level(logging.ERROR):
        report("agent.error", phone="+1***1234")
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_routine_events_do_not_log_at_error_level(caplog):
    with caplog.at_level(logging.WARNING):
        report("reminder.injection_suspected")
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_context_reaches_the_log_line(caplog):
    with caplog.at_level(logging.ERROR):
        report("sweep.overrun", seconds=110, users=4200)
    text = caplog.text
    assert "seconds=110" in text and "users=4200" in text


def test_the_silent_failures_are_all_alertable():
    """
    each of these fails without the user or the logs saying anything on their
    own, which is the entire reason this module exists
    """
    for event in (
        "reply.delivery_failed",
        "reminder.send_failed",
        "sweep.failed",
        "sweep.overrun",
        "agent.cache_miss",
        "budget.exhausted",
        "token.undecryptable",
    ):
        assert event in ALERT_EVENTS


def test_draining_clears_the_window_but_not_the_totals():
    count("reminder.sent", 5)
    window = monitoring._drain()
    assert window["reminder.sent"] == 5
    assert monitoring._drain() == {}
    assert snapshot()["reminder.sent"] == 5


def test_init_without_a_dsn_is_a_no_op(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monitoring.init_monitoring()
    assert monitoring._sentry is None


def test_init_with_an_unusable_dsn_does_not_raise(monkeypatch):
    """a broken monitoring config must never stop the app booting"""
    monkeypatch.setenv("SENTRY_DSN", "not-a-valid-dsn")
    monitoring.init_monitoring()   # must not raise
