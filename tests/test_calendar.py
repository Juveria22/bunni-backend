"""
calendar time math

the dst cases are the ones worth having: a fixed offset books everything an
hour early once est starts, and google's exclusive end date silently eats days
off a trip
"""

from datetime import timezone

import pytest

from services.calendar import (
    COLOR_CHOICES,
    MAX_DESCRIPTION_READ_CHARS,
    MAX_GUESTS_LISTED,
    EVENT_COLORS,
    MAX_DESCRIPTION_CHARS,
    MAX_REMINDER_OVERRIDES,
    MAX_TITLE_CHARS,
    all_day_span_days,
    all_day_window,
    build_details_body,
    build_reminder_overrides,
    build_time_body,
    looks_like_email,
    merge_guests,
    parse_event_window,
    read_event_extras,
    read_event_window,
    resolve_color,
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


def test_every_offered_colour_resolves_and_the_palette_is_fully_reachable():
    """
    the model is handed COLOR_CHOICES as an enum, so any word in it that does
    not resolve is a colour the model can pick and we then refuse

    all eleven ids reachable matters too, a synonym table that quietly collapsed
    two words onto one id would lose a colour with nothing to show for it
    """
    ids = [resolve_color(name) for name in COLOR_CHOICES]
    assert None not in ids
    assert sorted(int(i) for i in ids) == list(range(1, 12))


@pytest.mark.parametrize("said", ["RED", " red ", "Light  Blue", "light blue"])
def test_colour_words_survive_case_and_spacing(said):
    assert resolve_color(said) == EVENT_COLORS[" ".join(said.lower().split())]


@pytest.mark.parametrize("said", ["chartreuse", "", None, "blurple"])
def test_an_unknown_colour_resolves_to_nothing_rather_than_a_guess(said):
    """None is the signal to say so, picking a near colour would be silent"""
    assert resolve_color(said) is None


def test_a_field_nobody_mentioned_is_left_out_of_the_patch():
    """patch merges, so an absent key is what keeps the current value"""
    body = build_details_body(title="Dentist")
    assert body == {"summary": "Dentist"}


def test_an_empty_string_clears_a_field_rather_than_being_dropped():
    """
    the whole absent/empty distinction: "remove the location" has to write
    something, a body with no location key would leave it there
    """
    body = build_details_body(location="", description="")
    assert body["location"] == ""
    assert body["description"] == ""


def test_nothing_asked_for_builds_an_empty_body():
    assert build_details_body() == {}


def test_free_and_busy_map_onto_googles_transparency_vocabulary():
    assert build_details_body(busy=False)["transparency"] == "transparent"
    assert build_details_body(busy=True)["transparency"] == "opaque"


def test_busy_false_is_a_real_change_not_a_missing_field():
    """False is falsy, so a truthiness check here would silently drop it"""
    assert "transparency" in build_details_body(busy=False)


def test_oversized_text_is_trimmed_instead_of_being_rejected_by_google():
    body = build_details_body(title="t" * 5000, description="d" * 20000)
    assert len(body["summary"]) == MAX_TITLE_CHARS
    assert len(body["description"]) == MAX_DESCRIPTION_CHARS


def test_details_body_never_touches_the_time():
    """the one thing this must not do, start/end belong to build_time_body"""
    body = build_details_body(title="x", location="y", color_id="11", visibility="private")
    assert "start" not in body
    assert "end" not in body


def test_a_free_event_says_so_in_the_summary():
    """free events are invisible to the clash check, the model needs to know"""
    event = {
        "id": "x",
        "summary": "Gym",
        "transparency": "transparent",
        "start": {"dateTime": "2026-08-11T07:00:00-04:00"},
        "end": {"dateTime": "2026-08-11T08:00:00-04:00"},
    }
    assert summarize_event(event)["busy"] is False


def test_a_normal_event_carries_no_busy_flag_at_all():
    """it is sent only when unusual, every key is input tokens on every call"""
    event = {
        "id": "x",
        "summary": "Standup",
        "start": {"dateTime": "2026-08-11T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-11T09:15:00-04:00"},
    }
    assert "busy" not in summarize_event(event)


# ---------- guests ----------

@pytest.mark.parametrize(
    "address",
    ["jake@example.com", "Jake.Smith+cal@mail.co.uk", "  jake@example.com  "],
)
def test_real_addresses_pass(address):
    assert looks_like_email(address) is True


@pytest.mark.parametrize(
    "address", ["jake", "jake@", "@example.com", "jake smith@x.com", "jake@x", "", None]
)
def test_anything_google_could_not_invite_is_rejected(address):
    """the case this exists for is a model turning "invite jake" into an address"""
    assert looks_like_email(address) is False


def test_merging_keeps_everyone_already_on_the_event():
    """patching attendees replaces the array, a dropped name is an uninvite"""
    event = {"attendees": [{"email": "me@x.com", "responseStatus": "accepted"}]}
    attendees, added = merge_guests(event, ["jake@y.com"])

    assert {a["email"] for a in attendees} == {"me@x.com", "jake@y.com"}
    assert added == ["jake@y.com"]


def test_merging_preserves_an_existing_rsvp_verbatim():
    """rebuilding an attendee from the address alone resets them to needsAction"""
    event = {"attendees": [{"email": "me@x.com", "responseStatus": "accepted", "self": True}]}
    attendees, _ = merge_guests(event, ["jake@y.com"])

    kept = next(a for a in attendees if a["email"] == "me@x.com")
    assert kept["responseStatus"] == "accepted"
    assert kept["self"] is True


def test_somebody_already_invited_is_not_added_twice():
    event = {"attendees": [{"email": "Jake@Y.com"}]}
    attendees, added = merge_guests(event, ["jake@y.com"])
    assert added == []
    assert len(attendees) == 1


def test_an_event_with_no_guests_yet_still_merges():
    attendees, added = merge_guests({}, ["jake@y.com"])
    assert added == ["jake@y.com"]
    assert attendees == [{"email": "jake@y.com"}]


# ---------- reading one event on demand ----------

def test_reading_details_scrubs_the_description():
    """a description is the richest place on an event for someone else's text"""
    event = {"id": "x", "summary": "Standup", "description": "line one\nline two"}
    assert read_event_extras(event)["description"] == "line one line two"


def test_a_long_description_is_capped():
    event = {"id": "x", "summary": "x", "description": "d" * 5000}
    assert len(read_event_extras(event)["description"]) == MAX_DESCRIPTION_READ_CHARS


def test_no_description_reads_back_as_nothing_rather_than_being_absent():
    """the model needs to know it looked and there was nothing, not re-ask"""
    extras = read_event_extras({"id": "x", "summary": "x"})
    assert "description" in extras
    assert extras["description"] is None


def test_reading_details_lists_guests_with_their_rsvp():
    event = {
        "id": "x",
        "summary": "Standup",
        "attendees": [
            {"email": "me@x.com", "self": True, "responseStatus": "accepted"},
            {"email": "jake@y.com"},
        ],
    }
    extras = read_event_extras(event)
    assert extras["guest_count"] == 2
    assert extras["guests"][1] == {"email": "jake@y.com", "status": "needsAction"}


def test_a_room_booking_is_not_a_guest():
    event = {"id": "x", "summary": "x", "attendees": [{"email": "room@x.com", "resource": True}]}
    assert "guests" not in read_event_extras(event)


def test_a_huge_guest_list_is_capped_but_still_counted_truthfully():
    event = {
        "id": "x",
        "summary": "x",
        "attendees": [{"email": f"g{i}@x.com"} for i in range(50)],
    }
    extras = read_event_extras(event)
    assert len(extras["guests"]) == MAX_GUESTS_LISTED
    assert extras["guest_count"] == 50


def test_colour_reads_back_as_the_word_someone_would_have_asked_for():
    """set with "make it red", so it reads back red and not tomato"""
    assert read_event_extras({"id": "x", "summary": "x", "colorId": "11"})["color"] == "red"
