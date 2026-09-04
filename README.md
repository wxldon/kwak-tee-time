# teesniper

Books tee times at **Los Verdes** and **Alondra Park** the moment they are
released, from the Windows command line.

---

## Contents

- [How the drop works](#how-the-drop-works)
- [What the bot does](#what-the-bot-does)
- [Install](#install)
- [First run](#first-run)
- [Giving it your card](#giving-it-your-card)
- [Everyday use](#everyday-use)
- [All commands](#all-commands)
- [Full test run](#full-test-run)
- [Troubleshooting](#troubleshooting)
- [Known limits](#known-limits)

---

## How the drop works

Tee times at these courses do **not** trickle out. Every slot for a given day
becomes bookable at one instant:

> **8 days ahead, at 8:00 PM Pacific.**

So a tee time on **Tuesday Sept 15** goes live at **8:00 PM on Monday Sept 7**.
Before that moment the courses' own site returns nothing but a message saying
when it will open; a second after, the whole day is up for grabs and the good
times are gone in seconds.

Sniping is therefore a timing problem, not a searching problem.

Check any date:

```
snipe.bat when 2026-09-15
```
```
  2026-09-15 opens Monday, September 07 2026 at 08:00 PM PDT
  That is 147h 39m from now.
```

## What the bot does

Start it any time before the drop — hours early is fine. It then:

1. **Computes the exact release moment** for your date.
2. **Sleeps until 75 seconds out**, then wakes and opens a connection to the
   booking API so the handshake is already paid for.
3. **Polls cheaply while it waits**, about once every 5 seconds. The API
   supports conditional requests, so each check comes back empty ("nothing
   changed") until inventory appears.
4. **Starts its fast loop 3 seconds early.** The server's clock can't be read
   from outside, so a few seconds of lead absorbs the difference.
5. **The instant the listing changes**, filters to your time window, player
   count and hole preference, ranks the matches, and books the best one.
6. **If a slot is taken mid-attempt**, immediately tries the next-best — up to
   4 by default.

It aims within about a millisecond of the target moment.

Watching **both** courses at once is supported. It books **one** tee time —
whichever course produces a match first — never two.

### How hard it polls, and why

The tee time is won or lost in the first few seconds after the drop, so that is
where the effort goes — and nowhere else:

| When | Rate | Why |
|---|---|---|
| Last 75 seconds before the drop | 1 request / 5s | Keeps the connection warm and catches an early release. |
| Go-time to +10s | 5 / s | The decisive window. |
| +10s to +30s | 1.5 / s | Stragglers and re-releases. |
| +30s to the deadline | 0.5 / s | Only waiting for someone's 5-minute cart hold to lapse. |

That is roughly **170 requests per course per run**, concentrated where it
actually matters. A flat fast rate for the full three minutes would be four
times the traffic for no better odds — and traffic is what gets an account
flagged. A blocked account books nothing, so the two goals point the same way.

If the server answers `429` (too many requests) or `403` (blocked), the bot
**backs off** — honouring the server's own `Retry-After` when it sends one,
otherwise doubling the wait each time. After five refusals in a row it stops
that course rather than digging in.

---

## Install

You need **Python 3.11 or newer**.

> **Windows** uses `snipe.bat`; **macOS/Linux** uses `./snipe`. Everything below
> shows the Windows form — substitute `./snipe` on a Mac. `.bat` files do not
> run on macOS, so `./snipe.bat` there just gives "permission denied".

1. Install Python from [python.org](https://www.python.org/downloads/).
   **Tick "Add python.exe to PATH"** on the first screen of the installer —
   this is the single most common thing to get wrong.
2. Unzip this folder somewhere you can find it, e.g. `C:\teesniper`.
3. Open **Command Prompt**, and go to the folder:
   ```
   cd C:\teesniper
   ```
4. Run it once to bootstrap:
   ```
   snipe.bat check
   ```

The first launch creates a virtual environment and installs two small
dependencies. That takes a minute; afterwards startup is instant. You never
need to activate anything — `snipe.bat` handles it.

## First run

The first time you run any command it walks you through setup:

```
  First run -- let's get you set up.

  This copy is set up with:  golfer@example.com
  Use that account? (y/n) [y]:
```

- Press **Enter** to use the account the tool shipped with.
- Type **n** to enter your own TeeItUp email and password instead.

Either way it immediately tries that login against both courses and tells you
whether it worked, so a typo surfaces now rather than at 8pm.

Then it asks for the card to book with. Your password, card number and CVV are
**not shown on screen** as you type them.

Everything is written to `config.json` in the same folder. It never leaves your
machine except as the fields the booking site itself asks for.

To change the account later, delete `config.json` and run `snipe.bat check`
again.

## Giving it your card

Both courses charge at booking, so a card is required. Add or replace it any
time without retyping your login:

```
snipe.bat card
```

```
Current card: (none)

Card used at checkout. Both courses require one.
Nothing is echoed as you type the number and CVV.

Card number:
Expiry month (MM): 09
Expiry year (YYYY): 2030
CVV:
Name on card: Alex Rivera
Billing ZIP: 90045
```

You can also edit `config.json` by hand:

```json
{
  "username": "you@example.com",
  "password": "your-password",
  "phone": "13105551234",
  "card": {
    "number": "4111111111111111",
    "exp_month": "09",
    "exp_year": "2030",
    "cvv": "123",
    "name": "Alex Rivera",
    "zip": "90045"
  }
}
```

Confirm it took:

```
snipe.bat check
```
```
Card:   ****1111  (ok)
  Los Verdes Golf Course: logged in as Alex Rivera
  Alondra Park Golf Courses: logged in as Alex Rivera
```

---

### The card is checked before it is used

Whenever you enter or change a card, and again before a live snipe is armed,
the details are sanity-checked locally — length, the card number's own
checksum, expiry in the future, CVV length, ZIP. Nothing is sent anywhere to do
this. A typo caught at the prompt costs you thirty seconds; the same typo
caught at 8:00:00 PM costs you the tee time, because by then the bot has
already won the slot and there is no time left to fix anything.

`snipe.bat check` reports the same thing at any time.

---

## Everyday use

**Just double-click `snipe.bat`**, or run it with no arguments, and it asks for
everything:

```
snipe.bat
```
```
Play date (YYYY-MM-DD, 'tomorrow', or 'max'): 2026-09-15
Earliest acceptable tee time [6:00 am]: 7:00 am
Latest acceptable tee time [11:00 am]: 10:00 am
Number of players [2]: 2
Holes -- 9, 18, or 'any' [any]: 18

  1) Either  (takes whichever fits your time window first)
  2) Riding   (with a cart)
  3) Walking  (usually cheaper)
Transport [1]: 2

  1) Los Verdes
  2) Alondra Park
  3) Both of the above
  4) Alondra Park Par 3  (a short par-3 course, not a regulation round)
  5) All three
Course [3]:
```

Or give it everything up front and walk away:

```
snipe.bat snipe -d 2026-09-15 -p 2 -c both -s "7:00 am" -e "10:00 am"
```

It prints a plan, asks you to confirm, then waits:

```
  Plan
    Date     Tuesday, September 15 2026
    Time     07:00 AM - 10:00 AM
    Players  2
    Holes    any
    Courses  Los Verdes Golf Course, Alondra Park Golf Courses
    Card     ****1111
    Opens    Mon Sep 07 08:00 PM PDT  (in 147h 39m)
    Mode     LIVE -- will charge the card above

  Proceed? (y/n) [y]:
```

Leave the window open. At 7:58:45 PM it wakes up, and at 8:00:00 it goes.

### Dates you can type

`2026-09-15` · `9/15` · `Sep 15` · `tomorrow` · `max` (the furthest bookable day)

### Times you can type

`7am` · `7:30 am` · `7` · `14:00` · `2:30pm`

### Cancelling a snipe

Press **Ctrl-C** in the window. It tells both courses to stand down, waits for
them to finish whatever they were doing, and confirms what happened:

```
  Cancelling -- waiting for both courses to stand down...
  Cancelled. Nothing was booked and nothing was charged.
```

Closing the window works too, but Ctrl-C is better: it gives the bot a moment
to release any tee time it was holding in the cart, rather than leaving it
parked until the 5-minute hold lapses.

If a payment was already in flight when you cancelled, it says so instead —
check your reservations before running it again.

To change your mind about a booking the bot already made, cancel it on the
course's own website; this tool books, it does not cancel reservations.

### What the exit codes mean

Useful if you run it from a scheduled task.

| Code | Meaning |
|---|---|
| `0` | Booked |
| `1` | Nothing matched, or setup was incomplete |
| `2` | **Needs your attention** — a payment may have gone through |
| `130` | You cancelled with Ctrl-C |

---

## All commands

| Command | What it does |
|---|---|
| `snipe.bat` | Interactive snipe — asks for everything |
| `snipe.bat snipe [flags]` | Snipe with options preset |
| `snipe.bat list` | Show what's bookable right now |
| `snipe.bat when <date>` | Show when a date unlocks |
| `snipe.bat check` | Verify login and card |
| `snipe.bat card` | Add or replace the card |
| `snipe.bat init` | Re-enter login *and* card from scratch |
| `snipe.bat help` | Print a one-screen cheatsheet |

Mistyping a command prints the cheatsheet and, where it can tell, suggests what
you meant:

```
> snipe.bat lst

  argument cmd: invalid choice: 'lst' (choose from 'help', 'init', ...)
  Did you mean 'list'?

teesniper -- snipe tee times at Los Verdes and Alondra Park
  ...
```

### Choosing a course

| Value | What you get |
|---|---|
| `losverdes` | Los Verdes |
| `alondra` | Alondra Park, the regulation course |
| `alondra-par3` | Alondra Park Par 3 — a short course, cheap and quick |
| `both` | Los Verdes + Alondra regulation (the usual choice) |
| `all` | The above plus the par 3 |

With more than one course it watches them together and books **one** tee time —
whichever produces a match first, never two.

### Flags for `snipe` and `list`

| Flag | Meaning | Default |
|---|---|---|
| `-d`, `--date` | Play date | asked |
| `-p`, `--players` | 1–4 | asked |
| `-c`, `--course` | `losverdes`, `alondra`, `alondra-par3`, `both`, `all` | asked |
| `-s`, `--start` | Earliest acceptable time | asked |
| `-e`, `--end` | Latest acceptable time | asked |
| `--holes` | `9` or `18` | either |
| `--walking` | Walking rates only | either |
| `--riding` | Riding (cart) rates only | either |
| `--dry-run` | Find and stage a slot, stop before paying | off |
| `--yes` | Skip the confirmation prompt | off |
| `--tries` | Slots to attempt before giving up | 4 |
| `--deadline` | Seconds to keep hunting after the drop | 180 |
| `-v` | Verbose logging | off |

### Examples

**"When does September 15th open up for booking?"**
```
snipe.bat when 2026-09-15
```

**"What can I get tomorrow morning for two, at either course?"**
```
snipe.bat list -d tomorrow -p 2 -c both -s 6am -e 10am
```

**"Practice run for the 15th — find and hold a slot, but do not pay."**
```
snipe.bat snipe -d 2026-09-15 -p 2 -c both -s 7am -e 10am --dry-run
```

**"Book me a foursome at Los Verdes on the 15th, 18 holes with a cart, teeing
off between 7 and 9am."**
```
snipe.bat snipe -d 2026-09-15 -p 4 -c losverdes -s 7am -e 9am --holes 18 --riding
```

**"Cheap walking round for two at the par 3, any time that afternoon."**
```
snipe.bat snipe -d 2026-09-15 -p 2 -c alondra-par3 -s 12pm -e 5pm --walking
```

**"Set it and forget it — do not ask me to confirm."**
```
snipe.bat snipe -d 2026-09-15 -p 2 -c both -s 7am -e 10am --yes
```

---

## Full test run

Do this once before relying on it, ideally on a cheap slot.

**Step 1 — check the plumbing.** Confirms both logins and the card.

```
snipe.bat check
```

**Step 2 — dry run.** Goes through search, filtering, and staging a real slot in
the cart, then releases it. Charges nothing.

```
snipe.bat list -d tomorrow -p 2 -c both -s 6am -e 6pm
snipe.bat snipe -d tomorrow -p 2 -c both -s 6am -e 6pm --dry-run --yes
```

You should see `Staged ...` then `Dry run -- stopping before payment.`

**Step 3 — one real booking.** ⚠️ **This charges the card.**

Pick the cheapest thing available — **Alondra Park Par 3** is usually around
$18–22 for 2 players. Use `list` first to find one, then book a narrow window
so you get the slot you expect:

```
snipe.bat list -d tomorrow -p 2 -c alondra -s 6am -e 6pm
snipe.bat snipe -d tomorrow -p 2 -c alondra -s "2:00 pm" -e "2:30 pm"
```

Watch for:

```
  [alondra] Staged Tue Sep 08 02:12 PM | Alondra Park Par 3 | ...
  [alondra] Order created; requesting payment token.
  [alondra] Payment accepted.
  [alondra] BOOKED -- confirmation 12345678
```

Then **confirm it landed** — check your email for the confirmation, and log in
to the course website and look at your reservations. If the booking is not
there, or the amount charged looks wrong, stop and report exactly what the
terminal printed.

**Step 4 — a real snipe.** Pick a date 8 days out, start the bot any time
beforehand, and leave the window open through 8:00 PM.

---

## Troubleshooting

**`'snipe.bat' is not recognized`**
You're not in the right folder. `cd` to where you unzipped it.

**`Python was not found`**
Python isn't on PATH. Reinstall from python.org with **"Add python.exe to PATH"**
ticked, then open a *new* Command Prompt.

**`That email/password was rejected`**
The login is wrong, or the account doesn't exist at that course. Log in on the
course website in a browser to confirm, then delete `config.json` and rerun.

**`No card saved. Booking will fail at payment.`**
Run `snipe.bat card`.

**`Slot gone`**
Someone beat you to it. Normal at a busy drop — the bot moves to the next slot
automatically.

**`The card needs 3-D Secure`**
Your bank wants an extra confirmation the bot can't answer. It prints a link;
open it in a browser within 5 minutes to finish. If this keeps happening, try a
different card.

**`NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+ ... LibreSSL 2.8.3`**
Harmless, and already fixed for new installs. macOS ships Python 3.9 built
against a 2018 version of LibreSSL that urllib3 v2 declines to support, so it
complains once on every run. The tool works — the warning is noise, not a
failure. If you set up before this fix, clear it with:

```
.venv/bin/python -m pip install "urllib3<2"
```

Or delete the `.venv` folder and run `./snipe check` again; the launcher now
picks a newer Python when you have one, and installs a urllib3 that keeps quiet
when you don't.

**Something failed and it scrolled past**
Every run writes a full transcript to `logs/` next to `config.json` — every
request and response, with timings. Open the newest file:

```
ls -t logs | head -1
```

Card numbers, CVVs, passwords and session tokens are masked (a card shows as
`<16 digits ending 1111>`), so the file is safe to read and to send to someone
who can help. The last 20 runs are kept.

**Nothing matched**
Widen the time range, allow either hole count, or search `both` courses. Use
`list` to see what actually exists that day.

**It says a date is "beyond the booking window"**
Only 8 days out are bookable. The bot offers to wait for it anyway — say yes and
it sleeps until the drop.

---

## Known limits

- **If anything goes wrong after the card is charged, the bot stops.** It will
  never move on to another slot once payment has been submitted, because that
  is how a bot charges you twice. If you see lines beginning `!!`, read them:
  the money may have moved, and you should check your reservations and your
  statement before running it again. Exit code 2 means exactly that.
- **The payment path has not yet been tested end-to-end.** Everything up to and
  including staging a slot in the cart has been verified against live accounts;
  the three calls that create the order, charge the card and finalize were built
  from the booking site's own code but have never been executed. That is exactly
  what [Full test run](#full-test-run) step 3 is for. Until it has been done
  once, treat `--dry-run` as the trustworthy mode.
- **Alondra is really three courses** behind one booking page: the regulation
  course, a **par 3**, and a rental-only driving range. They are separate
  choices here, because the par 3's rates are *also* labelled "9 holes" and
  "18 holes" — so lumping them together would let a request for 18 holes quietly
  book an 18-hole par-3 round. `alondra` means the regulation course; pick
  `alondra-par3` (or `all`) if you actually want the short course. The driving
  range is never booked. Every result says which course it is on.
- **Walking and riding are separate rates at the same tee time**, often $10–30
  apart. Pick one with `--walking` / `--riding`, or leave it and the bot takes
  whichever fits your window first.
- **Los Verdes caps some rates at one booking per day per account.**
- **Nothing here is invisible to the course.** It uses your real account and
  your real card through the same API the website uses, and it is paced to look
  unremarkable — but repeated 8:00 PM bookings from one account are visible to
  staff whether or not a bot made them. Courses can and do cancel reservations
  or suspend accounts for booking behaviour they dislike. Use it the way you
  would book by hand.
- **Cart holds last 5 minutes.** The bot finishes well inside that, but if you
  get the 3-D Secure link, that's your window.
- **Keep the computer awake.** A sleeping laptop misses the drop. Turn off sleep
  in Windows power settings for the evening, or leave the machine plugged in.
- `config.json` holds a password and card in plain text. Anyone with access to
  that folder can read them.
