"""Channel rules live in one file, `meydan/rules.py`: every part reads them from there, and an
app changes a value by building its own `Rules`, never by changing meydan's."""

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from meydan.channels import telegram
from meydan.observe.engagement import SEEN_BIAS
from meydan.rules import DEFAULT


def test_the_platforms_rules_as_meydan_ships_them():
    assert DEFAULT.of("whatsapp").reply_window == timedelta(hours=24)
    assert DEFAULT.of("telegram").reply_window is None  # no such window on Telegram
    assert DEFAULT.of("telegram").button_payload_max == 64
    assert DEFAULT.of("whatsapp").receipts == {"delivered", "read"}
    assert DEFAULT.of("pigeon").reply_window is None  # a channel without a record has none


def test_parts_read_the_rules_not_their_own_copy():
    assert DEFAULT.of("telegram").button_payload_max == telegram.CALLBACK_DATA_MAX
    assert SEEN_BIAS == {"whatsapp": "under", "instagram": "under", "email": "over"}


def test_an_app_changes_only_what_it_names_and_meydans_rules_stay():
    mine = DEFAULT.but(whatsapp={"reply_window": timedelta(hours=48)})
    assert mine.of("whatsapp").reply_window == timedelta(hours=48)
    assert mine.of("whatsapp").text_max == DEFAULT.of("whatsapp").text_max
    assert mine.of("telegram") == DEFAULT.of("telegram")
    assert DEFAULT.of("whatsapp").reply_window == timedelta(hours=24)
    with pytest.raises(FrozenInstanceError):
        DEFAULT.of("whatsapp").text_max = 1  # type: ignore[misc]


def test_a_rule_that_does_not_exist_is_an_error():
    with pytest.raises(TypeError, match="reply_window_hours"):
        DEFAULT.but(whatsapp={"reply_window_hours": 48})
