"""Thin client over the TeeItUp (Kenna) backend API.

Endpoints and payload shapes were recovered from the booking site's own
JavaScript bundle; every call below mirrors what the browser does.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import requests

from . import debuglog
from .courses import API_BASE, Course

log = logging.getLogger(__name__)

# Match a real browser closely enough that we are not an obvious outlier.
# The client hints below must agree with the user-agent: a request claiming to
# be Chrome but missing the headers every Chrome sends is a louder signal than
# not claiming to be Chrome at all. Bump _CHROME_MAJOR and both stay in step.
_CHROME_MAJOR = "140"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36"
)
_CLIENT_HINTS = {
    "sec-ch-ua": (
        f'"Chromium";v="{_CHROME_MAJOR}", "Not=A?Brand";v="24", '
        f'"Google Chrome";v="{_CHROME_MAJOR}"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    # The booking site and the API are different registrable domains.
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "accept-encoding": "gzip, deflate, br",
}


class ApiError(RuntimeError):
    """Any non-2xx from the backend, with the parsed body attached."""

    def __init__(self, status: int, body: Any, path: str,
                 retry_after: float | None = None):
        self.status = status
        self.body = body
        self.path = path
        # Seconds the server asked us to wait, when it bothered to say.
        self.retry_after = retry_after
        super().__init__(f"{path} -> HTTP {status}: {body}")

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_gone(self) -> bool:
        """Slot was claimed by someone else, or the hold lapsed."""
        return self.status in (404, 409, 410)


def parse_retry_after(resp) -> float | None:
    """Seconds from a Retry-After header, whether it is a delay or a date."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class TeeItUpClient:
    def __init__(self, course: Course, timeout: float = 10.0):
        self.course = course
        self.timeout = timeout
        self.base = API_BASE
        self.session_token: str | None = None
        self.customer: dict[str, Any] | None = None

        self.http = requests.Session()
        self.http.headers.update(
            {
                "x-be-alias": course.alias,
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/json",
                "origin": course.origin,
                "referer": course.origin + "/",
                "user-agent": _UA,
                **_CLIENT_HINTS,
            }
        )
        # Keep the TCP+TLS connection warm so the snipe request isn't paying
        # for a handshake at go-time.
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        self.http.mount("https://", adapter)

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, **kw) -> Any:
        url = API_BASE + path
        started = time.monotonic()
        try:
            resp = self.http.request(method, url, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            debuglog.log_call(method, url, request=kw.get("json") or kw.get("params"),
                              error=f"{type(e).__name__}: {e}",
                              elapsed_ms=(time.monotonic() - started) * 1000)
            raise
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        debuglog.log_call(
            method, url,
            request=kw.get("json") if kw.get("json") is not None else kw.get("params"),
            status=resp.status_code, response=body,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        if not resp.ok:
            raise ApiError(resp.status_code, body, path, parse_retry_after(resp))
        return body

    def get(self, path: str, **kw) -> Any:
        return self._request("GET", path, **kw)

    def post(self, path: str, **kw) -> Any:
        return self._request("POST", path, **kw)

    def put(self, path: str, **kw) -> Any:
        return self._request("PUT", path, **kw)

    def patch(self, path: str, **kw) -> Any:
        return self._request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self._request("DELETE", path, **kw)

    # -------------------------------------------------------------------- auth

    def login(self, username: str, password: str) -> dict[str, Any]:
        """POST /profile/authenticate -- native email+password, no browser needed."""
        data = self.post(
            "/profile/authenticate",
            json={"username": username, "credentials": password, "type": "basic"},
        )
        self.session_token = data["sessionToken"]
        self.customer = data.get("customer")
        # The browser stores this in a "phxprofile" cookie and replays it as the
        # "session" header; we skip the cookie and send the header directly.
        self.http.headers["session"] = self.session_token
        return data

    @property
    def customer_id(self) -> str | None:
        return (self.customer or {}).get("id")

    @property
    def facility_customer_id(self) -> str | None:
        fc = (self.customer or {}).get("facilityCustomers") or {}
        entry = fc.get(self.course.entity_id) or {}
        return entry.get("id")

    def get_settings(self) -> dict[str, Any]:
        return self.get("/settings")

    # ------------------------------------------------------------------ search

    def get_tee_times(self, date: dt.date, **extra) -> list[dict[str, Any]]:
        """GET /v2/tee-times for one day at this course.

        Returns the raw per-course groups; each has ``teetimes`` and, when the
        day has not unlocked yet, a ``message`` explaining when it will.
        """
        # Only six params are accepted (date, facilityIds, dateMax,
        # promotionCode, customerId, returnPromotedRates); anything else is a
        # 400, and an obviously bogus param trips the WAF. There is no
        # server-side filtering by holes or player count -- that is on us.
        params = {
            "date": date.isoformat(),
            "facilityIds": ",".join(str(f) for f in self.course.facility_ids),
            **extra,
        }
        return self.get("/v2/tee-times", params=params)
