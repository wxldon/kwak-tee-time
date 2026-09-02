"""The polling loop that waits for a drop and grabs the first matching slot."""

from __future__ import annotations

import datetime as dt
import logging
import random
import time

import requests
from dataclasses import dataclass
from typing import Callable

from .api import ApiError, TeeItUpClient
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
            raise ApiError(resp.status_code, _safe_body(resp), "/v2/tee-times")
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


def hunt(
    poller: Poller,
    on_hit: Callable[[list[Candidate]], bool],
    *,
    deadline_seconds: float = 180.0,
    hot_rate: float = 4.0,
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
        status(f"Drop at {release:%a %b %d %I:%M:%S %p %Z} -- waiting {_fmt(wait)}")
        # Warm the connection and capture a baseline ETag on the locked day.
        if wait > 90:
            sleep_until(release - dt.timedelta(seconds=75))
            try:
                poller.poll()
                status("Connection warm, baseline ETag captured.")
            except (ApiError, requests.RequestException) as e:
                log.debug("warmup poll failed: %s", e)
            # Gentle keepalive so the socket and TLS session stay hot.
            while seconds_until_release(target.play_date) > lead_seconds + 1:
                time.sleep(2.0)
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
        sleep_until(release - dt.timedelta(seconds=lead_seconds))
        status("Go time.")
    else:
        status("Date is already open -- searching now.")

    interval = 1.0 / hot_rate
    started = time.monotonic()
    while time.monotonic() - started < deadline_seconds:
        if should_stop is not None and should_stop():
            return False
        loop_start = time.monotonic()
        try:
            cands = poller.poll()
            if cands:
                status(f"{len(cands)} matching slot(s) -- attempting to book.")
                if on_hit(cands):
                    return True
                status("Those did not stick; still hunting.")
            elif cands is not None:
                # 200 with no match: inventory is live but nothing fits the filter.
                status("Inventory open but nothing matches yet.")
        except ApiError as e:
            if e.status == 403:
                status("Blocked by the WAF -- backing off. Check the request params.")
                time.sleep(5.0)
            elif e.status == 401:
                status("Session expired -- stopping this course.")
                raise
            elif e.status >= 500 or e.status == 429:
                time.sleep(0.5)
            else:
                raise
        except requests.RequestException as e:
            # A dropped connection or a slow response must never end the hunt --
            # this loop runs at exactly the moment everyone else is hammering
            # the same server, so blips are expected.
            poller.stats.errors += 1
            log.debug("poll failed, retrying: %s", e)
            time.sleep(0.25)
        # Jitter keeps us off exact second boundaries.
        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, interval - elapsed) + random.uniform(0, 0.05))
    return False


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
