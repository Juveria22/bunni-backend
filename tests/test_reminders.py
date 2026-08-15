"""
the reminder sweep

the biggest spend in the service: every timed event costs two texts, so the
filters and the budget gate are what stand between a busy calendar and a bill
"""

import pytest

from services import budget, reminders
from services.reminders import NOW_WINDOW, SOON_WINDOW, _due_kind, _worth_reminding
from tests.fakes import BrokenRedis, FakeRedis


@pytest.fixture
def redis(monkeypatch):
    from services import redis_client

    monkeypatch.setattr(redis_client, "_redis", FakeRedis())
    monkeypatch.setattr(budget, "_cached_spend", 0)
    monkeypatch.setattr(budget, "_cached_at", 0.0)
    monkeypatch.setattr(budget, "_local_spend", {})
    monkeypatch.setattr(budget, "_announced", set())


# ---------- which events are due ----------

@pytest.mark.parametrize("minutes", [SOON_WINDOW[0], 60, SOON_WINDOW[1]])
def test_an_event_about_an_hour_out_is_due_soon(minutes):
    assert _due_kind(minutes) == "soon"


@pytest.mark.parametrize("minutes", [NOW_WINDOW[0], 0, NOW_WINDOW[1]])
def test_an_event_starting_about_now_is_due_now(minutes):
    assert _due_kind(minutes) == "now"


@pytest.mark.parametrize("minutes", [-30, 20, 40, 200])
def test_events_outside_both_windows_are_not_due(minutes):
    assert _due_kind(minutes) is None


def test_the_windows_are_wider_than_the_tick():
    """
    a slow sweep must not step over an event. the db claim is what stops the
    overlap turning into two texts
    """
    tick_minutes = reminders.SWEEP_INTERVAL_SECONDS / 60
    assert (SOON_WINDOW[1] - SOON_WINDOW[0]) > tick_minutes
    assert (NOW_WINDOW[1] - NOW_WINDOW[0]) > tick_minutes


# ---------- which events are worth a text ----------

def _timed(**overrides):
    event = {"start": {"dateTime": "2026-08-11T15:00:00-04:00"}}
    event.update(overrides)
    return event


def test_a_normal_timed_event_is_worth_reminding():
    assert _worth_reminding(_timed()) is True


def test_cancelled_events_are_skipped():
    assert _worth_reminding(_timed(status="cancelled")) is False


def test_all_day_events_are_skipped():
    """no start time, so "an hour before" means nothing"""
    assert _worth_reminding({"start": {"date": "2026-08-11"}}) is False


def test_events_marked_free_are_skipped():
    assert _worth_reminding(_timed(transparency="transparent")) is False


def test_declined_invites_are_skipped():
    event = _timed(attendees=[{"self": True, "responseStatus": "declined"}])
    assert _worth_reminding(event) is False


def test_someone_elses_decline_does_not_skip_it():
    event = _timed(attendees=[{"self": False, "responseStatus": "declined"}])
    assert _worth_reminding(event) is True


def test_an_accepted_invite_is_worth_reminding():
    event = _timed(attendees=[{"self": True, "responseStatus": "accepted"}])
    assert _worth_reminding(event) is True


# ---------- the budget gate ----------

async def test_the_sweep_stops_when_the_budget_is_gone(redis, monkeypatch):
    """reminders are the biggest spend, so they pause first"""
    await budget.record_sms(10_000)
    budget._cached_at = 0.0

    took_lock = []
    monkeypatch.setattr(
        reminders, "try_acquire_lock", lambda *a: took_lock.append(a) or True
    )

    assert await reminders.run_sweep() == 0
    assert took_lock == [], "budget is checked before the lock, so every replica stops"


async def test_the_sweep_gets_past_the_budget_when_there_is_money_left(
    redis, monkeypatch
):
    """
    the lock is deliberately lost here, so the sweep returns 0 either way. what
    is being asserted is that it reached the lock at all, meaning the budget
    gate let it through
    """
    reached_lock = []

    async def lose_the_lock(*args):
        reached_lock.append(args)
        return False

    monkeypatch.setattr(reminders, "try_acquire_lock", lose_the_lock)

    assert await reminders.run_sweep() == 0
    assert reached_lock, "budget gate should have allowed the sweep to continue"


async def test_a_failing_redis_does_not_pause_reminders(monkeypatch):
    """
    losing redis must not look like an exhausted budget, that would silently
    stop the product's main feature
    """
    from services import redis_client

    monkeypatch.setattr(redis_client, "_redis", BrokenRedis())
    monkeypatch.setattr(budget, "_cached_at", 0.0)
    monkeypatch.setattr(budget, "_local_spend", {})

    assert await budget.over_budget() is False


# ---------- fallback wording ----------

def test_the_fallback_reminder_names_the_event():
    assert "dentist" in reminders._fallback("Dentist", "soon").lower()


def test_the_fallback_distinguishes_soon_from_now():
    assert reminders._fallback("Dentist", "soon") != reminders._fallback("Dentist", "now")


def test_the_fallback_survives_a_missing_title():
    assert reminders._fallback(None, "soon")


def test_the_fallback_scrubs_a_hostile_title():
    assert "\n" not in reminders._fallback("a\nb", "soon")


def test_the_fallback_stays_within_one_segment():
    """a reminder is meant to be one cheap text"""
    from services.sms import segment_count

    long_title = "a very long event title " * 10
    assert segment_count(reminders._fallback(long_title, "soon")) == 1
