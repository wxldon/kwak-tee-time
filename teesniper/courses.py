"""Static course configuration for the two TeeItUp venues we snipe."""

from __future__ import annotations

from dataclasses import dataclass, field

API_BASE = "https://phx-api-be-east-1b.kenna.io"
TR_BASE = "https://tr.gnsvc.com"

# Both venues are in Southern California.
COURSE_TZ = "America/Los_Angeles"


@dataclass(frozen=True)
class SubCourse:
    """One bookable course inside a venue.

    Alondra's booking site fronts three GolfNow facilities; querying only the
    headline one silently hides most of the inventory, so we track them all.
    """

    gn_facility_id: int
    course_id: str
    name: str
    bookable: bool = True   # the driving range is rental-only, never a tee time


@dataclass(frozen=True)
class Course:
    key: str            # short name used on the CLI
    name: str           # human label
    alias: str          # value of the x-be-alias header
    entity_id: str      # Kenna entity/facility object id for the venue
    site: str           # public booking site, used for Origin/Referer
    subcourses: tuple[SubCourse, ...] = field(default_factory=tuple)

    @property
    def origin(self) -> str:
        return self.site.rstrip("/")

    @property
    def facility_ids(self) -> list[int]:
        """Every facility we should search, as sent in facilityIds=."""
        return [s.gn_facility_id for s in self.subcourses if s.bookable]

    def subcourse_by_course_id(self, course_id: str) -> SubCourse | None:
        for s in self.subcourses:
            if s.course_id == course_id:
                return s
        return None


LOS_VERDES = Course(
    key="losverdes",
    name="Los Verdes Golf Course",
    alias="los-verdes-golf-course",
    entity_id="54f14b9b0c8ad60378b01295",
    site="https://los-verdes-golf-course-public.book.teeitup.com",
    subcourses=(
        SubCourse(832, "54f14b9b0c8ad60378b01295", "Los Verdes Golf Course"),
    ),
)

# Alondra's booking site fronts three GolfNow facilities under one alias: the
# regulation course, a par-3 course, and a rental-only driving range. They are
# offered as separate choices because the par-3's rates are ALSO labelled
# "9"/"18 holes" -- so folding it in would let a request for 18 holes quietly
# book an 18-hole par-3 round, which is not the same game.
ALONDRA_PARK = Course(
    key="alondra",
    name="Alondra Park Golf Course",
    alias="alondra-park-golf-courses",
    entity_id="58ba11ecd08b967f00a5c615",
    site="https://alondra-park-golf-courses.book.teeitup.com",
    subcourses=(
        SubCourse(15142, "58ba11ecd08b967f00a5c615", "Alondra Park Golf Course"),
    ),
)

ALONDRA_PAR3 = Course(
    key="alondra-par3",
    name="Alondra Park Par 3",
    alias="alondra-park-golf-courses",
    entity_id="58ba11ecd08b967f00a5c615",
    site="https://alondra-park-golf-courses.book.teeitup.com",
    subcourses=(
        SubCourse(9546, "54f14f000c8ad60378b05ba9", "Alondra Park Par 3"),
    ),
)

# Not a tee time at all -- listed so it is obvious it was considered and skipped.
DRIVING_RANGE_FACILITY_ID = 19776

COURSES = {c.key: c for c in (LOS_VERDES, ALONDRA_PARK, ALONDRA_PAR3)}

# What "both" means on the CLI: the two regulation courses.
DEFAULT_COURSE_KEYS = ("losverdes", "alondra")

# Booking window, from GET /settings on both courses and verified against the
# "available to book from ..." message the API returns for future dates.
MAX_DAYS_OUT = 8
RELEASE_MINUTES_AFTER_MIDNIGHT = 1200  # 20:00 local
CART_HOLD_MINUTES = 5                  # checkout.reservationWindow
