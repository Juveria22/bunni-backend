"""
the parts of the agent that run without the model

the confirmation classifier matters most: it decides whether a "yes" writes to
someone's calendar, and it is deliberately resolved in code rather than by the
model's read of history
"""

import pytest

from services.agent import (
    MAX_INSTANCES_PER_SERIES,
    _is_all_day,
    _slim_event,
    _thin_recurring,
    classify_confirmation,
)


@pytest.mark.parametrize(
    "reply",
    ["y", "yes", "yeah", "yuh", "bet", "ok", "sure", "do it", "go for it", "add it"],
)
def test_obvious_agreement_needs_no_model_call(reply):
    assert classify_confirmation(reply) == "confirm"


@pytest.mark.parametrize(
    "reply", ["n", "no", "nah", "nope", "nvm", "cancel", "skip", "forget it"]
)
def test_obvious_refusal_needs_no_model_call(reply):
    assert classify_confirmation(reply) == "decline"


@pytest.mark.parametrize("reply", ["YES", "Yes!", "yes.", "  yeah  ", "NO?"])
def test_case_padding_and_punctuation_are_tolerated(reply):
    """being fussy here just makes someone say it twice"""
    assert classify_confirmation(reply) in ("confirm", "decline")


def test_delete_it_confirms_rather_than_reading_as_cancel():
    assert classify_confirmation("delete it") == "confirm"


def test_cancel_declines_and_never_confirms():
    """
    cancel is a normal way to say no to "still want me to add x?", so it must
    not be read as agreement, and it is kept out of the opt-out keywords too
    """
    assert classify_confirmation("cancel") == "decline"


@pytest.mark.parametrize(
    "reply",
    ["make it 4pm instead", "what about tuesday", "actually move the other one", ""],
)
def test_anything_that_is_not_a_plain_yes_or_no_defers(reply):
    """
    unrelated means "not obvious" here, the caller falls through to the small
    model rather than writing anything off
    """
    assert classify_confirmation(reply) == "unrelated"


def test_all_day_when_no_start_time_is_given():
    assert _is_all_day({"title": "Vacation", "date": "2026-08-11"}) is True


def test_all_day_when_explicitly_asked_for():
    assert _is_all_day({"all_day": True, "start_time": "09:00"}) is True


def test_a_timed_event_is_not_all_day():
    assert _is_all_day({"date": "2026-08-11", "start_time": "15:00"}) is False


def test_missing_start_time_never_becomes_a_midnight_event():
    """00:00 would create a real event at 12am, which is not what all day means"""
    assert _is_all_day({"date": "2026-08-11"}) is True


def test_thinning_keeps_every_one_off_event():
    events = [{"id": f"e{i}"} for i in range(10)]
    assert len(_thin_recurring(events)) == 10


def test_thinning_caps_each_repeating_series():
    events = [{"id": f"e{i}", "recurringEventId": "standup"} for i in range(30)]
    assert len(_thin_recurring(events)) == MAX_INSTANCES_PER_SERIES


def test_thinning_counts_each_series_separately():
    events = [{"id": f"a{i}", "recurringEventId": "a"} for i in range(10)]
    events += [{"id": f"b{i}", "recurringEventId": "b"} for i in range(10)]
    assert len(_thin_recurring(events)) == MAX_INSTANCES_PER_SERIES * 2


def test_thinning_preserves_order():
    events = [{"id": "one"}, {"id": "two"}, {"id": "three"}]
    assert [e["id"] for e in _thin_recurring(events)] == ["one", "two", "three"]


def test_a_daily_standup_cannot_crowd_out_a_real_event():
    """the reason thinning exists at all"""
    events = [{"id": f"s{i}", "recurringEventId": "standup"} for i in range(30)]
    events.append({"id": "dentist"})
    kept = {e["id"] for e in _thin_recurring(events)}
    assert "dentist" in kept


def test_parked_action_stores_only_what_it_needs_to_replay():
    """a whole google event carries attendees, conference links and more"""
    event = {
        "id": "abc",
        "summary": "Dentist",
        "start": {"dateTime": "2026-08-11T15:00:00-04:00"},
        "end": {"dateTime": "2026-08-11T16:00:00-04:00"},
        "attendees": [{"email": "someone@example.com"}],
        "conferenceData": {"entryPoints": [{"uri": "https://meet.example"}]},
        "description": "a" * 5000,
    }
    slim = _slim_event(event)
    assert set(slim) == {"id", "summary", "start", "end"}
    assert "attendees" not in slim
    assert "conferenceData" not in slim
    assert "description" not in slim
