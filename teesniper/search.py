"""Turn the raw /v2/tee-times payload into ranked, bookable candidates."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable

from .courses import Course
from .timing import TZ


@dataclass(frozen=True)
class Candidate:
    """One (tee time, rate) pair we could actually book."""

    course: Course
    teetime_utc: dt.datetime
    teetime_local: dt.datetime
    rate_id: int
    rate_name: str
    holes: int
    allowed_players: tuple[int, ...]
    max_players: int
    min_players: int
    booked_players: int
    price_cents: int
    due_online_cents: int
    course_id: str
    course_label: str
    rate_set_id: int
    group_size: int
    gn_facility_id: int
    raw_teetime: dict[str, Any]
    raw_rate: dict[str, Any]

    @property
    def open_slots(self) -> int:
        return self.max_players - self.booked_players

    @property
    def price(self) -> float:
        return self.price_cents / 100.0

    @property
    def due_online(self) -> float:
        return self.due_online_cents / 100.0

    @property
    def walking(self) -> bool:
        return "greenFeeWalking" in self.raw_rate

    @property
    def transportation(self) -> str:
        """The cart item calls this "Cart" or "Walking"; derived the same way."""
        return "Walking" if self.walking else "Cart"

    def label(self) -> str:
        return (
            f"{self.teetime_local:%a %b %d %I:%M %p} | {self.course_label} | "
            f"{self.rate_name} ({self.holes}h) | ${self.price:.2f}/player | "
            f"{self.open_slots} slot(s) open"
        )


def rate_price_cents(rate: dict[str, Any]) -> int:
    """Green fee for one player, in cents.

    Riding and walking rates carry the price under different keys
    (``greenFeeCart`` vs ``greenFeeWalking``); a rate only ever has one.
    """
    for key in ("greenFeeCart", "greenFeeWalking", "greenFee"):
        if key in rate:
            return rate[key] or 0
    return 0


def rate_due_online_cents(rate: dict[str, Any]) -> int:
    """Prepay/deposit hint carried on the search rate.

    This is NOT the checkout total -- the amount actually charged comes from the
    cart invoice (``totalDue.summary.total`` / ``dueOnline.summary.total``).
    Both courses report 0 here while still collecting payment at booking, so
    never use this to decide whether a card is needed.
    """
    for key in ("dueOnlineRiding", "dueOnlineWalking", "dueOnline"):
        if key in rate:
            return rate[key] or 0
    return 0


def _parse_utc(value: str) -> dt.datetime:
    # API returns e.g. "2026-09-05T22:50:00.000Z"
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def extract_candidates(
    groups: Iterable[dict[str, Any]],
    course: Course,
    players: int,
    holes: int | None = None,
) -> list[Candidate]:
    """Flatten the response into candidates that can seat ``players``.

    A rate is only bookable for a group size listed in its ``allowedPlayers``,
    and the slot must have that many seats still free.
    """
    out: list[Candidate] = []
    for group in groups or []:
        course_id = group.get("courseId", "")
        sub = course.subcourse_by_course_id(course_id)
        label = sub.name if sub else course.name
        for tt in group.get("teetimes") or []:
            utc = _parse_utc(tt["teetime"])
            local = utc.astimezone(TZ)
            max_players = tt.get("maxPlayers", 4)
            booked = tt.get("bookedPlayers", 0)
            if max_players - booked < players:
                continue
            for rate in tt.get("rates") or []:
                allowed = tuple(rate.get("allowedPlayers") or [])
                if allowed and players not in allowed:
                    continue
                if holes is not None and rate.get("holes") != holes:
                    continue
                out.append(
                    Candidate(
                        course=course,
                        teetime_utc=utc,
                        teetime_local=local,
                        rate_id=rate["_id"],
                        rate_name=rate.get("name", "?"),
                        holes=rate.get("holes", 0),
                        allowed_players=allowed,
                        max_players=max_players,
                        min_players=tt.get("minPlayers", 1),
                        booked_players=booked,
                        price_cents=rate_price_cents(rate),
                        due_online_cents=rate_due_online_cents(rate),
                        course_id=tt.get("courseId", course_id),
                        course_label=label,
                        rate_set_id=(rate.get("golfnow") or {}).get("GolfCourseId", 0),
                        # groupSize is 1 for every non-simulator rate; the
                        # server rejects anything else with "must be [1]".
                        group_size=(
                            max(allowed) if rate.get("isSimulator") and allowed else 1
                        ),
                        gn_facility_id=(rate.get("golfnow") or {}).get(
                            "GolfFacilityId", sub.gn_facility_id if sub else 0
                        ),
                        raw_teetime=tt,
                        raw_rate=rate,
                    )
                )
    return out


def in_time_window(c: Candidate, start: dt.time, end: dt.time) -> bool:
    """Inclusive local-time window. Handles a window that wraps midnight."""
    t = c.teetime_local.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def filter_and_rank(
    candidates: Iterable[Candidate],
    start: dt.time,
    end: dt.time,
    prefer: str = "earliest",
) -> list[Candidate]:
    """Keep candidates inside the window, best first.

    ``prefer``: ``earliest`` (default), ``latest``, or ``cheapest``.
    Ties break toward the cheaper rate, then the tighter slot -- booking a
    2-some into a 4-seat slot wastes inventory we may want for the next try.
    """
    hits = [c for c in candidates if in_time_window(c, start, end)]
    if prefer == "latest":
        key = lambda c: (-c.teetime_local.timestamp(), c.price_cents, c.open_slots)
    elif prefer == "cheapest":
        key = lambda c: (c.price_cents, c.teetime_local.timestamp(), c.open_slots)
    else:
        key = lambda c: (c.teetime_local.timestamp(), c.price_cents, c.open_slots)
    return sorted(hits, key=key)
