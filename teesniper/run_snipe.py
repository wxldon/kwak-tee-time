"""The `snipe` command: wait for the drop, claim a slot, pay for it."""

from __future__ import annotations

import datetime as dt
import logging
import threading

from . import booking, checkout, prompts
from .api import ApiError, TeeItUpClient
from .config import Config
from .courses import CART_HOLD_MINUTES, COURSES
from .search import Candidate
from .sniper import Poller, Target, hunt
from .timing import release_time_for, seconds_until_release

log = logging.getLogger(__name__)


def _status(prefix: str):
    def emit(msg: str) -> None:
        print(f"  [{prefix}] {msg}", flush=True)
    return emit


def book_candidate(
    client: TeeItUpClient,
    c: Candidate,
    players: int,
    cfg: Config,
    dry_run: bool,
    say,
) -> bool:
    """Take one candidate all the way to a paid booking.

    Returns True only when the booking is actually confirmed. Anything that
    means "this slot is gone" returns False so the caller can try the next one.
    """
    cart_id = None
    cart_item_id = None
    try:
        cart_id = booking.create_cart(client)
        cart = booking.add_to_cart(client, cart_id, c, players)
        cart_item_id = cart["items"][0]["id"]
        say(f"Staged {c.label()}")

        if dry_run:
            say("Dry run -- stopping before payment. Releasing the cart.")
            booking.delete_cart(client, cart_id)
            return True

        order = checkout.create_order(client, cart_id, cart_item_id, c, players)
        say("Order created; requesting payment token.")

        token = checkout.get_tr_token(client)
        payload = checkout.build_tr_payload(client, order, c, players, cfg)
        result = checkout.add_reservation(client, payload, token)
        say("Payment accepted.")

        checkout.finalize(client, result, cart_id, cart_item_id)
        conf = (
            result.get("ConfirmationNumber")
            or result.get("ReservationID")
            or result.get("ReservationStatusID")
            or "(see your account)"
        )
        say(f"BOOKED -- confirmation {conf}")
        return True

    except booking.SlotGone as e:
        say(f"Slot gone: {e}")
        if cart_id:
            booking.delete_cart(client, cart_id)
        return False

    except checkout.PaymentPending as e:
        say("The card needs 3-D Secure, which this bot cannot complete.")
        say(f"Finish it in a browser within {CART_HOLD_MINUTES} minutes: {e.redirect_url}")
        return True  # stop hunting; a human must take over

    except checkout.CheckoutError as e:
        say(f"Checkout failed: {e}")
        if cart_id and cart_item_id:
            checkout.mark_failed(client, cart_id, cart_item_id, players)
            booking.delete_cart(client, cart_id)
        return False

    except ApiError as e:
        if e.is_auth_error:
            raise
        say(f"API error {e.status}: {e.body}")
        if cart_id:
            booking.delete_cart(client, cart_id)
        return False


def _run_one(course, target: Target, cfg: Config, args, stop: threading.Event,
             results: dict, claim: threading.Lock) -> None:
    say = _status(course.key)
    try:
        client = TeeItUpClient(course)
        client.login(cfg.username, cfg.password)
    except ApiError as e:
        say(f"Login failed ({e.status}): {e.body}")
        return

    poller = Poller(client, target)

    def on_hit(cands: list[Candidate]) -> bool:
        if stop.is_set():
            return True
        for c in cands[: args.tries]:
            # One booking total across all courses: whoever gets the lock first
            # books, everyone else stands down. Without this, searching "both"
            # would happily buy two tee times.
            with claim:
                if stop.is_set():
                    return True
                if book_candidate(client, c, target.players, cfg, args.dry_run, say):
                    stop.set()
                    results[course.key] = c
                    return True
        return False

    try:
        got = hunt(poller, on_hit, deadline_seconds=args.deadline, status=say)
        if not got and not stop.is_set():
            say("Window closed without a booking.")
    except ApiError as e:
        say(f"Stopped: {e}")
    finally:
        s = poller.stats
        say(f"{s.requests} requests ({s.not_modified} unchanged, {s.errors} errors)")


def cmd_snipe(args) -> int:
    from .__main__ import ensure_setup
    cfg = ensure_setup(need_card=not args.dry_run)

    date = prompts.parse_date(args.date) if args.date else prompts.ask_date()
    players = args.players or prompts.ask_players()
    courses = (
        list(COURSES.values()) if args.course == "both"
        else [COURSES[args.course]] if args.course
        else prompts.ask_courses()
    )
    if args.start and args.end:
        start, end = prompts.parse_time(args.start), prompts.parse_time(args.end)
    else:
        start, end = prompts.ask_time_range()
    holes = args.holes

    if not args.dry_run and not cfg.card.filled:
        print("\n  No card saved. Booking will fail at payment.")
        print("  Run `python -m teesniper init` first, or use --dry-run.")
        return 1

    rel = release_time_for(date)
    wait = seconds_until_release(date)

    print("\n  Plan")
    print(f"    Date     {date:%A, %B %d %Y}")
    print(f"    Time     {start:%I:%M %p} - {end:%I:%M %p}")
    print(f"    Players  {players}")
    print(f"    Holes    {holes or 'any'}")
    print(f"    Courses  {', '.join(c.name for c in courses)}")
    print(f"    Card     {cfg.card.masked}")
    if wait > 0:
        print(f"    Opens    {rel:%a %b %d %I:%M %p %Z}  (in {int(wait//3600)}h {int(wait%3600//60)}m)")
    else:
        print("    Opens    already open")
    if args.dry_run:
        print("    Mode     DRY RUN -- will not pay")
    else:
        print("    Mode     LIVE -- will charge the card above")

    if not args.yes and not prompts.ask_yes("\n  Proceed?", "y"):
        return 0

    target_for = lambda: Target(
        course=None, play_date=date, players=players,
        start=start, end=end, holes=holes,
    )

    stop = threading.Event()
    claim = threading.Lock()
    results: dict = {}
    threads = []
    for course in courses:
        t = target_for()
        t.course = course
        th = threading.Thread(
            target=_run_one, args=(course, t, cfg, args, stop, results, claim), daemon=True
        )
        threads.append(th)
        th.start()
    for th in threads:
        th.join()

    if results:
        for c in results.values():
            print(f"\n  Got it: {c.label()}")
        return 0
    print("\n  No booking made.")
    return 1
