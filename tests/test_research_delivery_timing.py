from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.services.research_delivery.timing import publication_window_state


CN = ZoneInfo("Asia/Shanghai")
OPEN = datetime(2026, 10, 13, 17, 10, tzinfo=CN)
DEADLINE = datetime(2026, 10, 13, 19, 0, tzinfo=CN)


def test_window_boundaries_and_utc_are_consistent():
    for hour, minute, expected in ((17, 9, "waiting"), (17, 10, "open"),
                                  (18, 59, "open"), (19, 0, "expired")):
        local = datetime(2026, 10, 13, hour, minute, tzinfo=CN)
        assert publication_window_state(local, OPEN, DEADLINE, trading_day=True) == expected
        utc = local.astimezone(timezone.utc)
        assert publication_window_state(utc, OPEN, DEADLINE, trading_day=True) == expected


def test_unknown_calendar_and_closed_session_do_not_publish():
    assert publication_window_state(OPEN, OPEN, DEADLINE, trading_day=None) == "blocked"
    assert publication_window_state(OPEN, OPEN, DEADLINE, trading_day=False) == "closed"


def test_naive_clock_and_invalid_window_are_rejected():
    with pytest.raises(ValueError):
        publication_window_state(OPEN.replace(tzinfo=None), OPEN, DEADLINE, trading_day=True)
    with pytest.raises(ValueError):
        publication_window_state(OPEN, DEADLINE, OPEN, trading_day=True)
