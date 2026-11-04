"""Application clock with an explicit deterministic-demo override.

Production and ordinary local runs use the host's current local time.
Set DFH_DEMO_NOW to an ISO local datetime only for repeatable demo/test data.
The value is intentionally timezone-naive because the current schema stores
local wall-clock timestamps without timezone metadata.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo


def current_time() -> datetime:
    configured = os.getenv("DFH_DEMO_NOW")
    if configured:
        value = datetime.fromisoformat(configured)
        if value.tzinfo is not None:
            raise ValueError("DFH_DEMO_NOW must be a timezone-naive local datetime")
        return value
    timezone = ZoneInfo(os.getenv("DFH_TIMEZONE", "Asia/Kolkata"))
    return datetime.now(timezone).replace(tzinfo=None)
