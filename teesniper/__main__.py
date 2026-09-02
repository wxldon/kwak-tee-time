"""CLI entry point:  python -m teesniper"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import logging
import re
import sys

from . import prompts
from .api import ApiError, TeeItUpClient
from .config import Card, Config, config_path
from .courses import COURSES, DEFAULT_COURSE_KEYS
from .search import extract_candidates, filter_and_rank
from .sniper import Poller, Target, hunt
from .timing import now_local, release_time_for, seconds_until_release


def _log_setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _ask_card(optional: bool = False) -> Card:
    print("\nCard used at checkout. Both courses require one.")
    print("Nothing is echoed as you type the number and CVV.")
    if optional:
        print("Press Enter at the card number to skip and add it later.")
    print()
    number = prompts.ask_secret("Card number", allow_empty=optional)
    if not number:
        print("  Skipped -- run `snipe.bat card` before your first real booking.")
        return Card()
    return Card(
        number=number,
        exp_month=prompts.ask("Expiry month (MM)"),
        exp_year=prompts.ask("Expiry year (YYYY)"),
        cvv=prompts.ask_secret("CVV"),
        name=prompts.ask("Name on card"),
        zip=prompts.ask("Billing ZIP"),
    )


def cmd_init(args) -> int:
    print("Setting up teesniper. Everything stays in config.json on this machine.\n")
    cfg = Config()
    cfg.username = prompts.ask("TeeItUp email")
    cfg.password = prompts.ask_secret("TeeItUp password")
    cfg.phone = prompts.ask("Mobile number (digits only)", "")
    cfg.card = _ask_card()
    cfg.setup_complete = True
    path = cfg.save()
    print(f"\nSaved to {path}")
    print("It holds your password and card -- gitignored, and readable only by you.")
    return 0


def cmd_card(args) -> int:
    """Add or replace just the card, leaving the login alone."""
    cfg = Config.load_or_empty()
    print(f"Current card: {cfg.card.masked}")
    cfg.card = _ask_card()
    cfg.setup_complete = True
    path = cfg.save()
    print(f"\nCard updated ({cfg.card.masked}) in {path}")
    return 0


def ensure_setup(need_card: bool = True) -> Config:
    """First-run walkthrough, then hand back a usable config.

    The tool ships with an account already filled in; this confirms whoever is
    running it actually wants that account, and collects the card, which is
    never shipped.
    """
    cfg = Config.load_or_empty()
    if cfg.setup_complete and cfg.ready:
        return cfg
    if cfg.setup_complete and not need_card:
        return cfg

    print("\n  First run -- let's get you set up.\n")

    if cfg.username:
        print(f"  This copy is set up with:  {cfg.username}")
        keep = prompts.ask_yes("  Use that account?", "y")
    else:
        keep = False

    if not keep:
        print("\n  Enter the TeeItUp login to book with.")
        print("  The same email/password works at both courses.\n")
        cfg.username = prompts.ask("  TeeItUp email")
        cfg.password = prompts.ask_secret("  TeeItUp password")
        cfg.phone = prompts.ask("  Mobile number (digits only)", cfg.phone or "")

    # Verify before we bother asking for a card.
    print("\n  Checking that login...")
    for course in COURSES.values():
        try:
            TeeItUpClient(course).login(cfg.username, cfg.password)
            print(f"    {course.name}: ok")
        except ApiError as e:
            print(f"    {course.name}: FAILED ({e.status})")
            if e.is_auth_error:
                print("\n  That email/password was rejected. Run setup again.")
                cfg.setup_complete = False
                cfg.save()
                raise SystemExit(1)

    if not cfg.card.filled:
        # Always offer it during first-time setup, even for commands that do not
        # strictly need it -- otherwise `check` would finish setup cardless.
        cfg.card = _ask_card(optional=not need_card)

    cfg.setup_complete = True
    path = cfg.save()
    print(f"\n  Saved to {path}\n")
    return cfg


def _client(course, cfg: Config) -> TeeItUpClient:
    c = TeeItUpClient(course)
    c.login(cfg.username, cfg.password)
    return c


def cmd_check(args) -> int:
    """Log in to both courses and show what the bot can see."""
    cfg = ensure_setup(need_card=False)
    print(f"Config: {config_path()}")
    print(f"Card:   {cfg.card.masked}  ({'ok' if cfg.card.filled else 'NOT SET -- booking will fail'})\n")
    for course in COURSES.values():
        try:
            c = _client(course, cfg)
            who = (c.customer or {}).get("name", {}).get("formatted", "?")
            print(f"  {course.name}: logged in as {who}")
            print(f"    facilities searched: {course.facility_ids}")
        except ApiError as e:
            print(f"  {course.name}: LOGIN FAILED ({e.status}) {e.body}")
    return 0


def _pick_courses(choice: str | None) -> list:
    if choice in ("both", "all"):
        keys = list(COURSES) if choice == "all" else list(DEFAULT_COURSE_KEYS)
        return [COURSES[k] for k in keys]
    if choice:
        return [COURSES[choice]]
    return prompts.ask_courses()


def cmd_list(args) -> int:
    """Show what is currently bookable for a date."""
    cfg = ensure_setup(need_card=False)
    date = prompts.parse_date(args.date) if args.date else prompts.ask_date()
    players = args.players or prompts.ask_players()
    courses = _pick_courses(args.course)
    start, end = (
        (prompts.parse_time(args.start), prompts.parse_time(args.end))
        if args.start and args.end
        else prompts.ask_time_range()
    )
    holes = args.holes if args.holes else None
    walking = args.walking

    for course in courses:
        print(f"\n=== {course.name} -- {date} ===")
        try:
            c = _client(course, cfg)
            groups = c.get_tee_times(date)
        except ApiError as e:
            print(f"  error {e.status}: {e.body}")
            continue
        msg = next((g.get("message") for g in groups if g.get("message")), None)
        if msg:
            print(f"  {msg}")
        cands = extract_candidates(groups, course, players, holes)
        if walking is not None:
            cands = [c for c in cands if c.walking == walking]
        hits = filter_and_rank(cands, start, end)
        if not hits:
            how = "" if walking is None else (" walking" if walking else " riding")
            print(f"  Nothing{how} between {start:%I:%M %p} and {end:%I:%M %p} for {players}.")
            continue
        for h in hits:
            print(f"  {h.label()}")
    return 0


def cmd_when(args) -> int:
    """Show when a date unlocks."""
    date = prompts.parse_date(args.date) if args.date else prompts.ask_date()
    rel = release_time_for(date)
    left = seconds_until_release(date)
    print(f"\n  {date} opens {rel:%A, %B %d %Y at %I:%M %p %Z}")
    if left > 0:
        print(f"  That is {int(left // 3600)}h {int(left % 3600 // 60)}m from now.")
    else:
        print("  Already open.")
    return 0


CHEATSHEET = """
teesniper -- snipe tee times at Los Verdes and Alondra Park

  Tee times unlock 8 days ahead at 8:00 PM Pacific. Start the bot any time
  before then and leave it running; it wakes up and books at the drop.

COMMANDS
  {run}                       Snipe, asking for everything interactively
  {run} snipe [flags]         Snipe with options preset
  {run} list  [flags]         Show what is bookable right now
  {run} when <date>           Show when a date unlocks
  {run} check                 Verify login and card
  {run} card                  Add or replace the saved card
  {run} init                  Re-enter login and card from scratch
  {run} help                  This cheatsheet

FLAGS  (for snipe and list)
  -d, --date DATE         2026-09-15 | 9/15 | Sep 15 | tomorrow | max
  -p, --players N         1-4
  -c, --course NAME       losverdes | alondra | alondra-par3 | both | all
  -s, --start TIME        earliest acceptable, e.g. "7:00 am"
  -e, --end   TIME        latest acceptable,   e.g. "10:00 am"
      --holes 9|18        default: either
      --walking           walking rates only
      --riding            riding (cart) rates only
  -v, --verbose           more logging

FLAGS  (snipe only)
      --dry-run           find and stage a slot, stop before paying
      --yes               skip the confirmation prompt
      --tries N           slots to attempt before giving up (default 4)
      --deadline SECS     how long to keep hunting after the drop (default 180)

EXAMPLES
  {run} when 2026-09-15
  {run} list -d tomorrow -p 2 -c both -s 6am -e 10am
  {run} snipe -d 2026-09-15 -p 2 -c both -s 7am -e 10am --dry-run
  {run} snipe -d 2026-09-15 -p 4 -c losverdes -s 7am -e 9am --holes 18 --riding

  Full documentation is in README.md
"""


def _runner() -> str:
    """How this tool is invoked on the current platform."""
    return "snipe.bat" if sys.platform == "win32" else "./snipe"


def print_cheatsheet() -> None:
    print(CHEATSHEET.format(run=_runner()))


class FriendlyParser(argparse.ArgumentParser):
    """argparse, but a wrong command prints the cheatsheet instead of a stub."""

    def error(self, message: str):
        sys.stderr.write(f"\n  {message}\n")
        near = _suggest(message)
        if near:
            sys.stderr.write(f"  Did you mean '{near}'?\n")
        print_cheatsheet()
        raise SystemExit(2)


def _suggest(message: str) -> str | None:
    """Suggest a correction, matched against the choices argparse itself listed.

    Using the message's own "choose from" list keeps the suggestion relevant to
    whichever argument was wrong -- a bad --course value should not be answered
    with the name of a command.
    """
    bad = re.search(r"invalid choice: '([^']+)'", message)
    if not bad:
        return None
    options = re.findall(r"'([^']+)'", message.split("choose from", 1)[-1])
    if not options:
        return None
    matches = difflib.get_close_matches(bad.group(1), options, n=1, cutoff=0.5)
    return matches[0] if matches else None


def build_parser() -> argparse.ArgumentParser:
    p = FriendlyParser(
        prog="teesniper",
        description="Snipe tee times at Los Verdes and Alondra Park.",
        add_help=False,
    )
    p.add_argument("-h", "--help", action="store_true", dest="show_help")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", parser_class=FriendlyParser)

    sub.add_parser("help", help="show the cheatsheet")
    sub.add_parser("init", help="store login and card details locally")
    sub.add_parser("card", help="add or replace the saved card")
    sub.add_parser("check", help="verify logins and config")

    w = sub.add_parser("when", help="show when a date unlocks")
    w.add_argument("date", nargs="?")

    for name, help_text in (("list", "show currently bookable times"),
                            ("snipe", "wait for the drop and book")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("-d", "--date")
        s.add_argument("-p", "--players", type=int)
        s.add_argument(
            "-c", "--course", choices=sorted(COURSES) + ["both", "all"],
            help="'both' = the two regulation courses; 'all' adds the par 3",
        )
        s.add_argument("-s", "--start")
        s.add_argument("-e", "--end")
        s.add_argument("--holes", type=int, choices=(9, 18))
        transport = s.add_mutually_exclusive_group()
        transport.add_argument("--walking", dest="walking", action="store_true",
                               default=None, help="only walking rates")
        transport.add_argument("--riding", dest="walking", action="store_false",
                               help="only riding (cart) rates")
        if name == "snipe":
            s.add_argument("--dry-run", action="store_true",
                           help="find and stage the slot but stop before paying")
            s.add_argument("--yes", action="store_true",
                           help="skip the confirmation prompt")
            s.add_argument("--tries", type=int, default=4,
                           help="how many ranked slots to attempt before giving up")
            s.add_argument("--deadline", type=float, default=180.0,
                           help="seconds to keep hunting after the drop")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "show_help", False) or args.cmd == "help":
        print_cheatsheet()
        return 0
    _log_setup(getattr(args, "verbose", False))
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "card":
        return cmd_card(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "when":
        return cmd_when(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "snipe":
        from .run_snipe import cmd_snipe
        return cmd_snipe(args)
    print_cheatsheet()
    return 0


if __name__ == "__main__":
    sys.exit(main())
