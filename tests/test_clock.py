from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from dfh.clock import current_time


def test_clock_uses_configured_demo_datetime(monkeypatch):
    monkeypatch.setenv("DFH_DEMO_NOW", "2026-10-31T09:00:00")
    assert current_time() == datetime(2026, 10, 31, 9, 0)


def test_clock_defaults_to_current_time(monkeypatch):
    monkeypatch.delenv("DFH_DEMO_NOW", raising=False)
    monkeypatch.setenv("DFH_TIMEZONE", "Asia/Kolkata")
    timezone = ZoneInfo("Asia/Kolkata")
    before = datetime.now(timezone).replace(tzinfo=None)
    value = current_time()
    after = datetime.now(timezone).replace(tzinfo=None)
    assert before <= value <= after


def test_demo_clock_rejects_timezone_aware_values(monkeypatch):
    monkeypatch.setenv("DFH_DEMO_NOW", "2026-10-31T09:00:00+05:30")
    with pytest.raises(ValueError, match="timezone-naive"):
        current_time()
