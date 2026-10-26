from datetime import UTC, date, datetime

import pytest

from meydan.clock import EN, TR, Clock

FROZEN = datetime(2026, 10, 26, 18, 25, tzinfo=UTC)  # Monday 21:25 in Istanbul


def istanbul() -> Clock:
    return Clock("Europe/Istanbul", TR, source=lambda: FROZEN)


def test_now_is_local():
    assert istanbul().now().isoformat() == "2026-10-26T21:25:00+03:00"


def test_formats_utc_instant_in_local_time():
    c = istanbul()
    assert c.format_datetime(datetime(2026, 10, 31, 15, 0, tzinfo=UTC)) == "31 Ekim Cumartesi 18:00"


def test_english_locale():
    c = Clock("Europe/London", EN, source=lambda: FROZEN)
    assert c.format_date(c.today()) == "26 October Monday"


def test_calendar_has_iso_and_text():
    cal = istanbul().calendar(7)
    assert cal[0] == "2026-10-26 26 Ekim Pazartesi"
    assert cal[5] == "2026-10-31 31 Ekim Cumartesi"
    assert len(cal) == 7


def test_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        istanbul().to_local(datetime(2026, 10, 31, 18, 0))


def test_format_window_at_its_precision():
    c = istanbul()
    assert c.format_window(date(2026, 12, 1), date(2027, 1, 1)) == "Aralık 2026"
    assert c.format_window(date(2027, 1, 1), date(2027, 2, 1)) == "Ocak 2027"
    assert c.format_window(date(2026, 12, 12), date(2026, 12, 13)) == "12 Aralık Cumartesi"
    assert (
        c.format_window(date(2026, 12, 12), date(2026, 12, 14))
        == "12 Aralık Cumartesi – 13 Aralık Pazar"
    )
