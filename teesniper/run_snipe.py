"""The `snipe` command: wait for the drop, claim a slot, pay for it."""

from __future__ import annotations

import datetime as dt
import enum
import logging
import random
import threading
import time

import requests

from . import booking, checkout, prompts
from .api import ApiError, TeeItUpClient
from .config import Config
from .console import Console, countdown
from .courses import CART_HOLD_MINUTES, COURSES, DEFAULT_COURSE_KEYS
from .search import Candidate
from .sniper import Poller, Target, hunt
from .timing import release_time_for, seconds_until_release

log = logging.getLogger(__name__)


def _status(prefix: str, console: Console | None = None):
    def emit(msg: str) -> None:
        line = f"  [{prefix}] {msg}"
        if console is None:
            print(line, flush=True)
        else:
            # Goes through the console so it does not land on top of the
            # countdown that the main thread is redrawing.
            console.log(line)
    return emit


class Outcome(enum.Enum):
    """What happened on one booking attempt.

    The distinction that matters is TRY_NEXT versus everything else. TRY_NEXT is
    the ONLY value that lets the caller attempt another slot, and it is only
    ever returned from a path where the card was definitely not charged.
    """

    BOOKED = "booked"        # confirmed; stop
    TRY_NEXT = "try_next"    # nothing was charged; another slot is fine
    STOP = "stop"            # money may have moved, or a human is needed


def book_candidate(
    client: TeeItUpClient,
    c: Candidate,
    players: int,
    cfg: Config,
    dry_run: bool,
    say,
) -> Outcome:
    """Take one candidate all the way to a paid booking.

    Every failure after the charge is submitted returns STOP, never TRY_NEXT --
    retrying past that point is how a bot charges a card twice.
    """
    cart_id = None
    cart_item_id = None
    charged = False
    try:
        cart_id = booking.create_cart(client)
        cart = booking.add_to_cart(client, cart_id, c, players)
        cart_item_id = cart["items"][0]["id"]
        say(f"Staged {c.label()}")

        if dry_run:
            say("Dry run -- stopping before payment. Releasing the cart.")
            booking.delete_cart(client, cart_id)
            return Outcome.BOOKED

        order = checkout.create_order(client, cart_id, cart_item_id, c, players)
        say("Order created; requesting payment token.")

        token = checkout.get_tr_token(client)
        payload = checkout.build_tr_payload(client, order, c, players, cfg)

        charged = True  # anything from here on may have taken money
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
        return Outcome.BOOKED

    except booking.SlotGone as e:
        say(f"Slot gone: {e}")
        if cart_id:
            booking.delete_cart(client, cart_id)
        return Outcome.TRY_NEXT

    except checkout.PaymentPending as e:
        say("!! The card needs 3-D Secure, which this bot cannot complete.")
        say(f"!! NOT BOOKED YET. Finish it in a browser within "
            f"{CART_HOLD_MINUTES} minutes: {e.redirect_url}")
        return Outcome.STOP

    except checkout.ChargeUncertain as e:
        say("!! THE CARD MAY HAVE BEEN CHARGED -- do not run this again yet.")
        say(f"!! {e}")
        say("!! Check your email and your reservations before retrying.")
        return Outcome.STOP

    except checkout.FinalizeFailed as e:
        say("!! THE CARD WAS CHARGED but the course did not confirm the booking.")
        say(f"!! {e}")
        say("!! Check your reservations and your statement before retrying.")
        return Outcome.STOP

    except checkout.CheckoutError as e:
        # Raised only on paths where the charge was rejected outright.
        say(f"Payment declined or rejected: {e}")
        if cart_id and cart_item_id:
            checkout.mark_failed(client, cart_id, cart_item_id, players)
            booking.delete_cart(client, cart_id)
        # A declined card will decline on the next slot too -- do not burn
        # three more attempts against it.
        return Outcome.STOP

    except (ApiError, requests.RequestException) as e:
        if charged:
            say("!! THE CARD MAY HAVE BEEN CHARGED -- do not run this again yet.")
            say(f"!! {e}")
            say("!! Check your email and your reservations before retrying.")
            return Outcome.STOP
        say(f"Booking call failed: {e}")
        if cart_id:
            booking.delete_cart(client, cart_id)
        return Outcome.TRY_NEXT

    except Exception as e:  # noqa: BLE001 -- last line of defence around money
        if charged:
            say("!! THE CARD MAY HAVE BEEN CHARGED -- do not run this again yet.")
            say(f"!! unexpected error after payment: {e!r}")
            return Outcome.STOP
        say(f"Unexpected error before payment: {e!r}")
        if cart_id:
            booking.delete_cart(client, cart_id)
        return Outcome.TRY_NEXT


def _run_one(course, target: Target, cfg: Config, args, stop: threading.Event,
             results: dict, claim: threading.Lock, needs_attention: dict,
             console: Console | None = None) -> None:
    say = _status(course.key, console)
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
            # attempts, everyone else stands down. Without this, searching
            # "both" would happily buy two tee times.
            with claim:
                if stop.is_set():
                    return True
                outcome = book_candidate(client, c, target.players, cfg, args.dry_run, say)
                if outcome is Outcome.BOOKED:
                    stop.set()
                    results[course.key] = c
                    return True
                if outcome is Outcome.STOP:
                    # Money may have moved, or a human has to finish it. Stand
                    # the other course down rather than buying a second slot.
                    stop.set()
                    needs_attention[course.key] = c
                    return True
        return False

    try:
        got = hunt(
            poller, on_hit,
            deadline_seconds=args.deadline,
            # Jitter the lead so the two courses do not fire the identical
            # first request from one IP at the identical microsecond. Both
            # still start comfortably ahead of the drop.
            lead_seconds=3.0 - random.uniform(0.0, 0.3),
            status=say,
            should_stop=stop.is_set,
        )
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
    if args.players is not None and not 1 <= args.players <= 4:
        print(f"  --players must be between 1 and 4 (got {args.players}).")
        return 1
    players = args.players or prompts.ask_players()
    if args.course in ("both", "all"):
        keys = list(COURSES) if args.course == "all" else list(DEFAULT_COURSE_KEYS)
        courses = [COURSES[k] for k in keys]
    elif args.course:
        courses = [COURSES[args.course]]
    else:
        courses = prompts.ask_courses()
    if args.start or args.end:
        if not (args.start and args.end):
            print("  Give both --start and --end, or neither.")
            return 1
        start, end = prompts.parse_time(args.start), prompts.parse_time(args.end)
    else:
        start, end = prompts.ask_time_range()
    holes = args.holes if args.holes else prompts.ask_holes()
    walking = args.walking if args.walking is not None else prompts.ask_transport()

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
    print(f"    Transport {'either' if walking is None else ('walking' if walking else 'riding')}")
    print(f"    Courses  {', '.join(c.name for c in courses)}")
    print(f"    Card     {cfg.card.masked}")
    transcript = getattr(args, "transcript", None)
    if transcript:
        print(f"    Log      {transcript}")
    card_problems = cfg.card.problems
    if card_problems and not args.dry_run:
        # Better to refuse now than to win the slot and fail at the charge,
        # hours later, with nobody watching.
        print("\n  That card will be declined:")
        for why in card_problems:
            print(f"    - {why}")
        from .__main__ import _runner
        print(f"  Fix it first:  {_runner()} card      (then run this again)")
        return 1
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
        start=start, end=end, holes=holes, walking=walking,
    )

    stop = threading.Event()
    claim = threading.Lock()
    results: dict = {}
    needs_attention: dict = {}
    console = Console()
    threads = []
    for course in courses:
        t = target_for()
        t.course = course
        th = threading.Thread(
            target=_run_one,
            args=(course, t, cfg, args, stop, results, claim, needs_attention,
                  console), daemon=True
        )
        threads.append(th)
        th.start()
    cancelled = False
    try:
        # join() in short slices, so Ctrl-C reaches us promptly instead of
        # blocking in a single uninterruptible wait -- and so the countdown
        # keeps ticking while we wait.
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.2)
            left = seconds_until_release(date)
            if left > 0:
                console.status(
                    f"  {countdown(left)} until the drop"
                    f" ({rel:%I:%M:%S %p})  --  Ctrl-C to cancel"
                )
            else:
                console.status("  Hunting...  --  Ctrl-C to cancel")
        console.clear()
    except KeyboardInterrupt:
        cancelled = True
        stop.set()
        console.clear()
        print("\n  Cancelling -- waiting for both courses to stand down...")
        deadline = time.monotonic() + 10.0
        while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
            for t in threads:
                t.join(timeout=0.2)
    finally:
        console.clear()

    if results:
        for c in results.values():
            print(f"\n  Got it: {c.label()}")
        return 0
    if cancelled:
        if needs_attention:
            for c in needs_attention.values():
                print(f"\n  NEEDS YOUR ATTENTION: {c.label()}")
            print("  Cancelled, but a payment was already in flight -- check your")
            print("  reservations and your statement before running this again.")
            return 2
        print("  Cancelled. Nothing was booked and nothing was charged.")
        return 130
    if needs_attention:
        for c in needs_attention.values():
            print(f"\n  NEEDS YOUR ATTENTION: {c.label()}")
        print("  Read the messages above -- the card may have been charged, or a")
        print("  3-D Secure step may be waiting. Check your reservations and your")
        print("  statement before running this again.")
        return 2
    print("\n  No booking made.")
    if transcript:
        print(f"  Every request and response is in:  {transcript}")
    return 1
