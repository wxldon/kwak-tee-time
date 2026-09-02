"""Small interactive prompt helpers -- stdlib only, so it runs anywhere."""

from __future__ import annotations

import datetime as dt
import getpass
import re

from .courses import COURSES, DEFAULT_COURSE_KEYS, MAX_DAYS_OUT
from .timing import TZ, now_local, release_time_for

_TIME_PATTERNS = (
    "%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H",
)


def parse_time(text: str) -> dt.time:
    t = text.strip().lower().replace(".", "")
    t = re.sub(r"\s+", " ", t)
    # Accept "7", "7am", "7:30 am", "19:30".
    for fmt in _TIME_PATTERNS:
        try:
            return dt.datetime.strptime(t.upper(), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Could not read a time from {text!r} (try '7:30 am' or '14:00')")


def parse_date(text: str) -> dt.date:
    t = text.strip().lower()
    today = now_local().date()
    if t in ("today", "t"):
        return today
    if t in ("tomorrow", "tm"):
        return today + dt.timedelta(days=1)
    if t == "max":
        return today + dt.timedelta(days=MAX_DAYS_OUT)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m/%d", "%b %d", "%B %d"):
        try:
            d = dt.datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
        if fmt in ("%m/%d", "%b %d", "%B %d"):
            d = d.replace(year=today.year)
            if d < today:
                d = d.replace(year=today.year + 1)
        return d
    raise ValueError(f"Could not read a date from {text!r} (try 2026-09-15 or 9/15)")


def ask(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"{question}{suffix}: ").strip()
        except EOFError:
            # Non-interactive (piped/redirected stdin): take the default.
            if default is not None:
                print(f"{question}{suffix}: {default}")
                return default
            raise
        if raw:
            return raw
        if default is not None:
            return default


def ask_secret(question: str, allow_empty: bool = False) -> str:
    """Prompt without echoing -- for passwords, card numbers, CVVs."""
    while True:
        try:
            raw = getpass.getpass(f"{question}: ").strip()
        except (EOFError, getpass.GetPassWarning):
            # Piped stdin has no tty; fall back to a normal read.
            try:
                raw = input(f"{question}: ").strip()
            except EOFError:
                return ""
        if raw or allow_empty:
            return raw
        print("  Required.")


def ask_date(question: str = "Play date") -> dt.date:
    while True:
        try:
            d = parse_date(ask(question + " (YYYY-MM-DD, 'tomorrow', or 'max')"))
        except ValueError as e:
            print(f"  {e}")
            continue
        today = now_local().date()
        if d < today:
            print("  That date is in the past.")
            continue
        horizon = today + dt.timedelta(days=MAX_DAYS_OUT)
        if d > horizon:
            rel = release_time_for(d)
            print(
                f"  {d} is beyond the {MAX_DAYS_OUT}-day booking window. "
                f"It opens {rel:%a %b %d at %I:%M %p %Z}."
            )
            if ask("  Wait for it anyway? (y/n)", "y").lower().startswith("y"):
                return d
            continue
        return d


def ask_time_range() -> tuple[dt.time, dt.time]:
    while True:
        try:
            start = parse_time(ask("Earliest acceptable tee time", "6:00 am"))
            end = parse_time(ask("Latest acceptable tee time", "11:00 am"))
        except ValueError as e:
            print(f"  {e}")
            continue
        return start, end


def ask_courses() -> list:
    print("\n  1) Los Verdes")
    print("  2) Alondra Park")
    print("  3) Both of the above")
    print("  4) Alondra Park Par 3  (a short par-3 course, not a regulation round)")
    print("  5) All three")
    while True:
        choice = ask("Course", "3").lower()
        if choice in ("1", "losverdes", "lv"):
            return [COURSES["losverdes"]]
        if choice in ("2", "alondra", "ap"):
            return [COURSES["alondra"]]
        if choice in ("3", "both"):
            return [COURSES[k] for k in DEFAULT_COURSE_KEYS]
        if choice in ("4", "par3", "alondra-par3"):
            return [COURSES["alondra-par3"]]
        if choice in ("5", "all"):
            return list(COURSES.values())
        print("  Pick 1-5.")


def ask_players() -> int:
    while True:
        raw = ask("Number of players", "2")
        if raw.isdigit() and 1 <= int(raw) <= 4:
            return int(raw)
        print("  Between 1 and 4.")


def ask_holes() -> int | None:
    raw = ask("Holes -- 9, 18, or 'any'", "any").lower()
    if raw in ("9", "18"):
        return int(raw)
    return None


def ask_yes(question: str, default: str = "n") -> bool:
    return ask(question + " (y/n)", default).lower().startswith("y")
