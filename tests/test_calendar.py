"""
calendar time math

the dst cases are the ones worth having: a fixed offset books everything an
hour early once est starts, and google's exclusive end date silently eats days
off a trip
"""

from datetime import timezone

import pytest

from services.calendar import (
    MAX_REMINDER_OVERRIDES,
    all_day_span_days,
    all_day_window,
    build_reminder_overrides,
    build_time_body,
    parse_event_window,
    read_event_window,
    summarize_event,
)


def test_event_window_length_matches_the_duration_asked_for():
    start, end = parse_event_window("2026-07-01", "14:00", 90)
    assert (end - start).total_seconds() == 90 * 60


def test_offset_is_edt_in_summer_and_est_in_winter():
    summer, _ = parse_event_window("2026-07-01", "14:00", 60)
    winter, _ = parse_event_window("2026-01-15", "14:00", 60)
    assert summer.utcoffset().total_seconds() == -4 * 3600
    assert winter.utcoffset().total_seconds() == -5 * 3600


def test_event_spanning_the_fall_back_keeps_its_real_length():
    """
    2026-11-01 is the fall-back date, so 1am happens twice. an event starting
    01:00 edt and running two real hours ends at 02:00 est: the wall clock only
    advanced an hour, but two hours actually elapsed

    measured in utc on purpose. subtracting two datetimes that share a tzinfo
    gives the naive wall-clock difference, so comparing them directly would
    read 60 minutes and look like a bug that isn't there
    """
    start, end = parse_event_window("2026-11-01", "01:00", 120)

    elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    assert elapsed.total_seconds() == 120 * 60

    assert start.utcoffset().total_seconds() == -4 * 3600   # edt
    assert end.utcoffset().total_seconds() == -5 * 3600     # est


def test_a_normal_day_needs_no_utc_conversion_to_look_right():
    """the contrast case: no transition, so wall clock and real time agree"""
    start, end = parse_event_window("2026-11-08", "01:00", 120)
    assert (end - start).total_seconds() == 120 * 60
    assert start.utcoffset() == end.utcoffset()


def test_all_day_window_covers_midnight_to_midnight():
    start, end = all_day_window("2026-08-11")
    assert start.hour == 0 and end.hour == 0
    assert (end - start).days == 1


def test_multi_day_all_day_window_spans_every_day():
    start, end = all_day_window("2026-08-11", "2026-08-15")
    assert (end - start).days == 5   # 11th through 15th inclusive


def test_all_day_body_uses_bare_dates_not_midnight_timestamps():
    """what puts an event in the top strip instead of at 12am"""
    body = build_time_body("2026-08-11", all_day=True)
    assert body["start"] == {"date": "2026-08-11"}
    assert "dateTime" not in body["start"]


def test_google_end_date_is_exclusive():
    body = build_time_body("2026-08-11", all_day=True)
    assert body["end"] == {"date": "2026-08-12"}


def test_timed_body_carries_a_named_zone():
    body = build_time_body("2026-08-11", "15:00", 60)
    assert body["start"]["timeZone"] == "America/New_York"


def test_read_event_window_round_trips_a_timed_event():
    event = {
        "start": {"dateTime": "2026-08-11T15:00:00-04:00"},
        "end": {"dateTime": "2026-08-11T16:30:00-04:00"},
    }
    date, start_time, minutes, all_day = read_event_window(event)
    assert (date, start_time, minutes, all_day) == ("2026-08-11", "15:00", 90, False)


def test_read_event_window_recognises_an_all_day_event():
    event = {"start": {"date": "2026-08-11"}, "end": {"date": "2026-08-12"}}
    date, start_time, _, all_day = read_event_window(event)
    assert (date, start_time, all_day) == ("2026-08-11", None, True)


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2026-08-11", "2026-08-12", 1),   # single day, exclusive end
        ("2026-08-11", "2026-08-16", 5),   # five day trip
    ],
)
def test_all_day_span_days(start, end, expected):
    event = {"start": {"date": start}, "end": {"date": end}}
    assert all_day_span_days(event) == expected


def test_timed_event_spans_one_day():
    event = {
        "start": {"dateTime": "2026-08-11T15:00:00-04:00"},
        "end": {"dateTime": "2026-08-11T16:00:00-04:00"},
    }
    assert all_day_span_days(event) == 1


@pytest.mark.parametrize(
    "offsets", [[60], [1440, 60], [10080, 1440, 60], [7, 6, 5, 4, 3, 2, 1]]
)
def test_reminder_overrides_never_exceed_googles_cap(offsets):
    """over five and google rejects the patch outright"""
    overrides, applied = build_reminder_overrides(offsets)
    assert len(overrides) <= MAX_REMINDER_OVERRIDES
    assert len(applied) <= len(offsets)


def test_two_offsets_keep_both_popup_and_email():
    overrides, applied = build_reminder_overrides([1440, 60])
    assert len(applied) == 2
    assert {o["method"] for o in overrides} == {"popup", "email"}


def test_three_offsets_drop_the_email_copy_to_stay_under_the_cap():
    overrides, applied = build_reminder_overrides([10080, 1440, 60])
    assert len(applied) == 3
    assert {o["method"] for o in overrides} == {"popup"}


def test_offsets_are_deduped_and_ordered_furthest_out_first():
    _, applied = build_reminder_overrides([60, 1440, 60])
    assert applied == [1440, 60]


def test_negative_offsets_are_dropped():
    _, applied = build_reminder_overrides([-30, 60])
    assert applied == [60]


def test_no_offsets_produces_nothing_to_send():
    assert build_reminder_overrides([]) == ([], [])


def test_summarize_event_spells_out_the_weekday():
    """users say "saturday's office thing", not a date"""
    event = {
        "id": "abc123",
        "summary": "Office Day",
        "start": {"dateTime": "2026-08-15T11:00:00-04:00"},
        "end": {"dateTime": "2026-08-15T18:00:00-04:00"},
    }
    out = summarize_event(event)
    assert out["day"] == "saturday aug 15"
    assert out["id"] == "abc123"
    assert out["start"] == "11:00"
    assert out["duration_minutes"] == 420


def test_summarize_event_flags_a_repeating_series():
    event = {
        "id": "x",
        "summary": "Standup",
        "recurringEventId": "series1",
        "start": {"dateTime": "2026-08-11T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-11T09:15:00-04:00"},
    }
    assert summarize_event(event)["repeats"] is True


def test_summarize_event_scrubs_a_hostile_title():
    event = {
        "id": "x",
        "summary": "line one\nline two",
        "start": {"date": "2026-08-11"},
        "end": {"date": "2026-08-12"},
    }
    assert "\n" not in summarize_event(event)["title"]


def test_untitled_event_still_summarizes():
    event = {"id": "x", "start": {"date": "2026-08-11"}, "end": {"date": "2026-08-12"}}
    assert summarize_event(event)["title"] == "untitled"
