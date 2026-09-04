"""The polling loop that waits for a drop and grabs the first matching slot."""

from __future__ import annotations

import datetime as dt
import logging
import random
import time

import requests
from dataclasses import dataclass
from typing import Callable

from .api import ApiError, TeeItUpClient, parse_retry_after
from .courses import Course
from .search import Candidate, extract_candidates, filter_and_rank
from .timing import now_local, release_time_for, seconds_until_release, sleep_until

log = logging.getLogger(__name__)


@dataclass
class Target:
    course: Course
    play_date: dt.date
    players: int
    start: dt.time
    end: dt.time
    holes: int | None = None
    prefer: str = "earliest"
    walking: bool | None = None   # None = either


@dataclass
class PollStats:
    requests: int = 0
    not_modified: int = 0
    errors: int = 0


class Poller:
    """Polls one course for one date, using conditional GETs while idle.

    The API honours If-None-Match, so while the day is still locked every poll
    costs a 304 with no body. When inventory appears the ETag changes and we get
    a real payload -- that transition is the drop signal.
    """

    def __init__(self, client: TeeItUpClient, target: Target):
        self.client = client
        self.target = target
        self.etag: str | None = None
        self.stats = PollStats()

    def poll(self) -> list[Candidate] | None:
        """One request. ``None`` means nothing changed since the last poll."""
        params = {
            "date": self.target.play_date.isoformat(),
            "facilityIds": ",".join(str(f) for f in self.target.course.facility_ids),
        }
        headers = {"If-None-Match": self.etag} if self.etag else {}
        self.stats.requests += 1
        resp = self.client.http.get(
            self.client.base + "/v2/tee-times",
            params=params,
            headers=headers,
            timeout=3.0,
        )
        if resp.status_code == 304:
            self.stats.not_modified += 1
            return None
        if not resp.ok:
            self.stats.errors += 1
            raise ApiError(
                resp.status_code, _safe_body(resp), "/v2/tee-times",
                parse_retry_after(resp),
            )
        self.etag = resp.headers.get("ETag") or self.etag
        groups = resp.json()
        cands = extract_candidates(
            groups, self.target.course, self.target.players, self.target.holes
        )
        return self._apply_prefs(cands)

    def _apply_prefs(self, cands: list[Candidate]) -> list[Candidate]:
        t = self.target
        if t.walking is not None:
            cands = [c for c in cands if c.walking == t.walking]
        return filter_and_rank(cands, t.start, t.end, t.prefer)


def _safe_body(resp) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:300]


# Requests per second, by seconds elapsed since go-time. The slot is won or
# lost in the first handful of seconds -- so we poll *harder* than before
# during the burst, then decay. After the burst we are only waiting for a
# competitor's five-minute cart hold to lapse, which does not need four
# requests a second and only makes us conspicuous.
RATE_PLAN: tuple[tuple[float, float], ...] = (
    (13.0, 5.0),            # the decisive window (starts lead_seconds early)
    (30.0, 1.5),            # stragglers and re-releases
    (float("inf"), 0.5),    # long tail: lapsed carts
)

# How often to touch the server while waiting out the last minute before the
# drop. Frequent enough to hold the TCP+TLS session open and to catch an early
# release; sparse enough that it is not a heartbeat anyone would notice.
KEEPALIVE_SECONDS = 5.0


def rate_at(elapsed: float, plan=RATE_PLAN) -> float:
    for until, rate in plan:
        if elapsed < until:
            return rate
    return plan[-1][1]


class Backoff:
    """Exponential backoff that honours Retry-After.

    Grinding through a 429 or a 403 is what turns a throttle into a ban, and a
    banned account books nothing -- so backing off is the booking-friendly
    move, not the timid one. After ``max_strikes`` consecutive refusals we give
    up on this course rather than dig the hole deeper.
    """

    max_strikes = 5

    def __init__(self) -> None:
        self.strikes = 0

    def reset(self) -> None:
        self.strikes = 0

    def penalty(self, retry_after: float | None = None) -> float | None:
        """Seconds to wait, or None if we have been refused too many times."""
        self.strikes += 1
        if self.strikes > self.max_strikes:
            return None
        if retry_after is not None:
            return min(retry_after, 30.0)
        return min(1.5 * 2 ** (self.strikes - 1), 20.0) + random.uniform(0, 0.4)


def hunt(
    poller: Poller,
    on_hit: Callable[[list[Candidate]], bool],
    *,
    deadline_seconds: float = 180.0,
    rate_plan: tuple[tuple[float, float], ...] = RATE_PLAN,
    lead_seconds: float = 3.0,
    status: Callable[[str], None] = lambda m: None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Wait for the drop, then hammer until a matching slot is claimed.

    ``on_hit`` receives the ranked candidates and returns True once it has
    actually booked one; returning False means "none of these worked, keep
    looking" (e.g. every slot was sniped out from under us).

    We start ``lead_seconds`` before the computed release because the origin
    clock is not observable -- the Date header is Cloudflare's, 1-second
    granular. Starting early costs a few free 304s and covers the skew.
    """
    target = poller.target
    release = release_time_for(target.play_date)
    wait = seconds_until_release(target.play_date)

    if wait > 0:
        # The remaining time is shown live by the countdown bar; this line is
        # the one-time record of what we are waiting for.
        status(f"Drop at {release:%a %b %d %I:%M:%S %p %Z}")
        # Warm the connection and capture a baseline ETag on the locked day.
        if wait > 90:
            if not sleep_until(release - dt.timedelta(seconds=75), should_stop=should_stop):
                return False
            try:
                poller.poll()
                status("Connection warm, baseline ETag captured.")
            except (ApiError, requests.RequestException) as e:
                log.debug("warmup poll failed: %s", e)
            # Gentle keepalive so the socket and TLS session stay hot.
            while True:
                if should_stop is not None and should_stop():
                    return False
                remaining = seconds_until_release(target.play_date)
                if remaining <= lead_seconds + 1:
                    break
                # Never nap past go-time: sleeping a flat KEEPALIVE_SECONDS
                # here could land us seconds *into* the drop with the burst
                # already missed.
                nap = min(
                    KEEPALIVE_SECONDS + random.uniform(0, 0.5),
                    remaining - lead_seconds - 1,
                )
                if nap < 0.25:
                    # Too close to go-time to be worth another touch; a nap
                    # this short would just spin on the network round-trip.
                    break
                if not sleep_until(
                    now_local() + dt.timedelta(seconds=nap),
                    spin_window=0.0,
                    should_stop=should_stop,
                ):
                    return False
                try:
                    # Keep the ETag current, but never discard real inventory:
                    # if the drop lands early, act on it instead of throwing it
                    # away and waiting for the next poll.
                    early = poller.poll()
                    if early:
                        status("Inventory appeared early -- going now.")
                        if on_hit(early):
                            return True
                except (ApiError, requests.RequestException):
                    pass
        if not sleep_until(
            release - dt.timedelta(seconds=lead_seconds), should_stop=should_stop
        ):
            return False
        status("Go time.")
    else:
        status("Date is already open -- searching now.")

    started = time.monotonic()
    backoff = Backoff()
    announced_open = False
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= deadline_seconds:
            break
        if should_stop is not None and should_stop():
            return False
        loop_start = time.monotonic()
        try:
            cands = poller.poll()
            backoff.reset()
            if cands:
                status(f"{len(cands)} matching slot(s) -- attempting to book.")
                if on_hit(cands):
                    return True
                status("Those did not stick; still hunting.")
            elif cands is not None and not announced_open:
                # 200 with no match: inventory is live but nothing fits the
                # filter. Say so once -- we keep watching at the decayed rate
                # in case a held cart lapses and frees a slot.
                announced_open = True
                status("Inventory open but nothing matches yet -- still watching.")
        except ApiError as e:
            if e.status == 401:
                status("Session expired -- stopping this course.")
                raise
            if e.status not in (403, 429) and e.status < 500:
                raise
            wait = backoff.penalty(e.retry_after)
            if wait is None:
                status(
                    f"Refused {backoff.max_strikes} times in a row (HTTP {e.status}) "
                    "-- standing down to protect the account."
                )
                raise
            what = "Rate limited" if e.status == 429 else (
                "Blocked by the WAF" if e.status == 403 else "Server error"
            )
            status(f"{what} (HTTP {e.status}) -- waiting {wait:.1f}s before retrying.")
            time.sleep(wait)
            continue
        except requests.RequestException as e:
            # A dropped connection or a slow response must never end the hunt --
            # this loop runs at exactly the moment everyone else is hammering
            # the same server, so blips are expected.
            poller.stats.errors += 1
            log.debug("poll failed, retrying: %s", e)
            time.sleep(0.25)
        # Jitter keeps us off exact second boundaries.
        interval = 1.0 / rate_at(time.monotonic() - started, rate_plan)
        spent = time.monotonic() - loop_start
        time.sleep(max(0.0, interval - spent) + random.uniform(0, 0.05))
    return False

