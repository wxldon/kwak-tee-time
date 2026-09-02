"""Release-window math and the high-precision wait used to hit a drop."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

from .courses import COURSE_TZ, MAX_DAYS_OUT, RELEASE_MINUTES_AFTER_MIDNIGHT

TZ = ZoneInfo(COURSE_TZ)


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


def release_time_for(play_date: dt.date) -> dt.datetime:
    """When tee times for ``play_date`` become bookable.

    Confirmed rule: the whole day unlocks at once, ``MAX_DAYS_OUT`` days earlier
    at ``RELEASE_MINUTES_AFTER_MIDNIGHT`` past local midnight -- i.e. 8:00 PM
    Pacific, 8 days ahead. Verified against the API's own
    "Tee times will be available to book from <date> at 8:00 PM" message for
    several dates on both courses.
    """
    unlock_day = play_date - dt.timedelta(days=MAX_DAYS_OUT)
    midnight = dt.datetime.combine(unlock_day, dt.time(0, 0), tzinfo=TZ)
    return midnight + dt.timedelta(minutes=RELEASE_MINUTES_AFTER_MIDNIGHT)


def is_released(play_date: dt.date, now: dt.datetime | None = None) -> bool:
    return (now or now_local()) >= release_time_for(play_date)


def seconds_until_release(play_date: dt.date, now: dt.datetime | None = None) -> float:
    return (release_time_for(play_date) - (now or now_local())).total_seconds()


def sleep_until(target: dt.datetime, spin_window: float = 2.0) -> None:
    """Sleep until ``target``, then busy-wait the last ``spin_window`` seconds.

    ``time.sleep`` on Windows only resolves to ~15ms, which is a lot of slack
    when hundreds of people hit the same drop. Coarse-sleep most of the way,
    then spin so we fire within a millisecond of the mark.
    """
    while True:
        remaining = (target - dt.datetime.now(TZ)).total_seconds()
        if remaining <= spin_window:
            break
        time.sleep(min(remaining - spin_window, 30.0))

    while (target - dt.datetime.now(TZ)).total_seconds() > 0:
        time.sleep(0.0005)
