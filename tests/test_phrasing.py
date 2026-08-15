"""
reply wording

this is where the sms segment count is actually decided, and where a clash
count that lied would let someone confirm over more than they realised
"""

import pytest

from services.phrasing import (
    MAX_CONFLICTS_NAMED,
    fmt_all_day_reminder,
    fmt_applied_reminders,
    fmt_conflict_warning,
    fmt_day_span,
    fmt_event_span,
    fmt_reminder,
    fmt_time,
)


def _timed(summary, start, end, day="2026-08-11"):
    return {
        "summary": summary,
        "start": {"dateTime": f"{day}T{start}:00-04:00"},
        "end": {"dateTime": f"{day}T{end}:00-04:00"},
    }


@pytest.mark.parametrize(
    "hhmm,expected",
    [
        ("15:30", "3:30pm"),
        ("09:05", "9:05am"),
        ("00:15", "12:15am"),
        ("12:00", "12:00pm"),
        ("23:59", "11:59pm"),
    ],
)
def test_fmt_time(hhmm, expected):
    assert fmt_time(hhmm) == expected


def test_fmt_time_passes_through_what_it_cannot_parse():
    assert fmt_time("not a time") == "not a time"


@pytest.mark.parametrize(
    "minutes,expected",
    [(30, "30min"), (60, "1hr"), (90, "1hr"), (1440, "1d"), (2880, "2d")],
)
def test_fmt_reminder_is_compact(minutes, expected):
    assert fmt_reminder(minutes) == expected


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (900, "9am the day before"),   # google's own all-day default
        (0, "on the day"),
        (1440, "1 day before"),
        (2880, "2 days before"),
    ],
)
def test_all_day_reminders_say_when_they_actually_fire(minutes, expected):
    assert fmt_all_day_reminder(minutes) == expected


def test_fmt_day_span():
    assert fmt_day_span("2026-08-11", None) == "on 2026-08-11"
    assert fmt_day_span("2026-08-11", "2026-08-11") == "on 2026-08-11"
    assert fmt_day_span("2026-08-11", "2026-08-15") == "2026-08-11 to 2026-08-15"


def test_event_span_reads_as_a_range():
    assert fmt_event_span(_timed("Standup", "15:00", "15:30")) == "standup 3:00pm to 3:30pm"


def test_compact_span_drops_the_end_time():
    """end times are what tip a long clash list over a segment"""
    out = fmt_event_span(_timed("Standup", "15:00", "15:30"), compact=True)
    assert out == "standup 3:00pm"


def test_all_day_event_says_so_instead_of_a_time():
    event = {"summary": "Vacation", "start": {"date": "2026-08-11"}}
    assert fmt_event_span(event) == "vacation all day"


def test_event_span_scrubs_a_hostile_title():
    assert "\n" not in fmt_event_span(_timed("a\nb", "15:00", "15:30"))


def test_single_clash_is_spelled_out():
    out = fmt_conflict_warning("dentist", [_timed("Standup", "15:00", "15:30")])
    assert "standup 3:00pm to 3:30pm" in out
    assert "still want me to add dentist?" in out


def test_clash_count_is_always_truthful():
    """a count that lied would hide what someone is agreeing to book over"""
    conflicts = [_timed(f"Thing {i}", "15:00", "15:30") for i in range(9)]
    out = fmt_conflict_warning("dentist", conflicts)
    assert "9 things" in out


def test_only_the_first_few_clashes_are_named():
    # distinct nonsense titles so the count cannot collide with the word
    # "things" in the summary line
    names = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    conflicts = [
        _timed(n, f"{9 + i:02d}:00", f"{10 + i:02d}:00") for i, n in enumerate(names)
    ]
    out = fmt_conflict_warning("dentist", conflicts)

    named = [n for n in names if n in out]
    assert len(named) == MAX_CONFLICTS_NAMED
    assert named == names[:MAX_CONFLICTS_NAMED]
    assert "and 3 more" in out


def test_moving_asks_a_different_question():
    out = fmt_conflict_warning(
        "dentist", [_timed("Standup", "15:00", "15:30")], moving=True
    )
    assert "move it there anyway?" in out


def test_same_name_clash_warns_about_duplicating():
    out = fmt_conflict_warning("standup", [_timed("Standup", "15:00", "15:30")] * 2)
    assert "two of them" in out


def test_moving_onto_itself_does_not_warn_about_duplicating():
    out = fmt_conflict_warning(
        "standup", [_timed("Standup", "15:00", "15:30")] * 2, moving=True
    )
    assert "two of them" not in out


def test_applied_reminders_read_furthest_out_first():
    assert fmt_applied_reminders([60, 1440]) == "1 day before + 1 hour before"


def test_applied_reminders_pluralise():
    assert fmt_applied_reminders([2880]) == "2 days before"
    assert fmt_applied_reminders([120]) == "2 hours before"


def test_applied_reminders_falls_back_when_empty():
    assert fmt_applied_reminders([]) == "as specified"


def test_no_em_dashes_anywhere_in_generated_copy():
    """they are outside gsm-7 and would drag a reply into ucs-2"""
    samples = [
        fmt_conflict_warning("dentist", [_timed("Standup", "15:00", "15:30")]),
        fmt_all_day_reminder(900),
        fmt_applied_reminders([1440, 60]),
        fmt_event_span(_timed("Standup", "15:00", "15:30")),
    ]
    for text in samples:
        assert "—" not in text and "–" not in text
