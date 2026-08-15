"""
outbound message shaping, the last thing between a reply and the bill

the segment boundaries are the point: a lone sms holds 160 chars, a split one
153 per part, so the cost steps at 161 and every 153 after

the special characters are written as escapes throughout. a plain space and a
non-breaking space look identical in an editor, and that is precisely how the
substitution table came to carry a no-op where the nbsp was meant to be
"""

import pytest

from services.sms import (
    MAX_SMS_CHARS,
    MAX_WHATSAPP_CHARS,
    clamp,
    segment_count,
    to_gsm7,
)


@pytest.mark.parametrize(
    "length,expected",
    [
        (0, 0),
        (1, 1),
        (160, 1),    # last char that still fits one standalone sms
        (161, 2),    # spills, and now every part pays the 6 byte header
        (306, 2),    # 2 x 153, the boundary MAX_SMS_CHARS is pinned to
        (307, 3),
        (459, 3),    # 3 x 153
        (460, 4),
    ],
)
def test_segment_count_steps_at_the_real_boundaries(length, expected):
    assert segment_count("a" * length) == expected


def test_sms_cap_is_exactly_two_segments():
    """the regression that started this: 320 chars was 3 segments, not 2"""
    assert segment_count("a" * MAX_SMS_CHARS) == 2
    assert segment_count("a" * (MAX_SMS_CHARS + 1)) == 3


def test_clamp_never_exceeds_two_segments_however_long_the_reply():
    for length in (307, 500, 1000, 4000):
        assert segment_count(clamp("a" * length)) <= 2


def test_clamp_leaves_short_replies_alone():
    body = "added dentist friday at 3, reminder 1hr before"
    assert clamp(body) == body


def test_clamp_marks_that_it_trimmed():
    assert clamp("a" * 500).endswith("...")


def test_clamp_trim_marker_stays_in_gsm7():
    """plain dots, not the ellipsis character, which would force ucs-2"""
    assert "…" not in clamp("a" * 500)


def test_whatsapp_gets_the_longer_ceiling():
    body = "a" * 500
    assert len(clamp(body, channel="whatsapp")) == 500
    assert len(clamp(body, channel="sms")) <= MAX_SMS_CHARS
    assert MAX_WHATSAPP_CHARS > MAX_SMS_CHARS


@pytest.mark.parametrize(
    "raw,expected,label",
    [
        ("don’t", "don't", "right single quote"),
        ("‘quoted’", "'quoted'", "left/right single quotes"),
        ("“quoted”", '"quoted"', "curly double quotes"),
        ("a – b", "a - b", "en dash"),
        ("a — b", "a - b", "em dash"),
        ("5 − 3", "5 - 3", "minus sign"),
        ("wait…", "wait...", "ellipsis"),
        ("a b", "a b", "non-breaking space"),
        ("a b", "a b", "narrow no-break space"),
        ("a​b", "ab", "zero width space"),
        ("• item", "* item", "bullet"),
    ],
)
def test_to_gsm7_replaces_characters_that_would_double_the_cost(raw, expected, label):
    assert to_gsm7(raw) == expected, label


def test_no_substituted_character_survives_into_the_output():
    """
    one non-gsm7 character forces the whole message into ucs-2, which cuts the
    segment from 153 chars to 67, so a miss here costs real segments
    """
    for raw in (
        "‘’“”",
        "–—−",
        "…",
        "  ​",
        "•",
    ):
        out = to_gsm7(raw)
        assert not any(ord(c) > 127 for c in out), f"{raw!r} left non-ascii: {out!r}"


def test_a_reply_full_of_nbsp_still_bills_as_two_segments():
    """
    the regression this table exists to prevent: untouched, 306 non-breaking
    spaces would be ucs-2 and bill as five segments instead of two
    """
    body = clamp(to_gsm7("word " * 60))
    assert not any(ord(c) > 127 for c in body)
    assert segment_count(body) <= 2


def test_to_gsm7_leaves_plain_text_untouched():
    body = "moved saniyahs party to tomorrow at 12pm"
    assert to_gsm7(body) == body


def test_substitution_happens_before_clamping():
    """
    the ellipsis is 1 char but becomes 3, so clamping first would let a reply
    grow back over the cap after substitution
    """
    body = "…" * 200  # 200 chars in, 600 after substitution
    out = clamp(to_gsm7(body))
    assert len(out) <= MAX_SMS_CHARS
    assert segment_count(out) <= 2
