"""Local credential/config storage.

Everything sensitive lives in config.json next to the bot, which .gitignore
excludes. Nothing is ever written back to the repo.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"


def config_path() -> Path:
    """config.json beside the bot, overridable with TEESNIPER_CONFIG."""
    env = os.environ.get("TEESNIPER_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / CONFIG_NAME


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class Card:
    number: str = ""
    exp_month: str = ""
    exp_year: str = ""
    cvv: str = ""
    name: str = ""
    zip: str = ""

    @property
    def filled(self) -> bool:
        return bool(self.number and self.exp_month and self.exp_year)

    @property
    def problems(self) -> list[str]:
        """Everything obviously wrong with this card, in plain English.

        Catching a typo here is worth a lot: the alternative is discovering it
        at 8:00:00 PM, after the bot has already won the slot, when there is no
        time left to fix anything.
        """
        import datetime as _dt

        out: list[str] = []
        digits = "".join(ch for ch in self.number if ch.isdigit())
        if not digits:
            out.append("no card number")
        elif not 13 <= len(digits) <= 19:
            out.append(f"card number is {len(digits)} digits (expected 13-19)")
        elif not _luhn_ok(digits):
            out.append("card number fails its checksum -- likely a typo")

        try:
            month = int(self.exp_month)
            if not 1 <= month <= 12:
                raise ValueError
        except (TypeError, ValueError):
            out.append(f"expiry month {self.exp_month!r} is not 01-12")
            month = None
        try:
            year = int(self.exp_year)
            year += 2000 if year < 100 else 0
        except (TypeError, ValueError):
            out.append(f"expiry year {self.exp_year!r} is not a year")
            year = None
        if month and year:
            today = _dt.date.today()
            if (year, month) < (today.year, today.month):
                out.append(f"card expired {month:02d}/{year}")

        cvv = "".join(ch for ch in self.cvv if ch.isdigit())
        if not 3 <= len(cvv) <= 4:
            out.append(f"CVV is {len(cvv)} digits (expected 3 or 4)")
        if not self.name.strip():
            out.append("no name on card")
        if len("".join(ch for ch in self.zip if ch.isdigit())) < 5:
            out.append(f"billing ZIP {self.zip!r} is not 5 digits")
        return out

    @property
    def usable(self) -> bool:
        return not self.problems

    @property
    def masked(self) -> str:
        if not self.number:
            return "(none)"
        digits = "".join(ch for ch in self.number if ch.isdigit())
        return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


@dataclass
class Config:
    username: str = ""
    password: str = ""
    phone: str = ""
    email_opt_in: bool = False
    sms_opt_in: bool = False
    card: Card = field(default_factory=Card)
    # False until someone has confirmed the shipped account and added a card.
    setup_complete: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.username and self.password and self.card.filled)

    @classmethod
    def load_or_empty(cls) -> "Config":
        """Like ``load`` but returns a blank config when there simply isn't one.

        A corrupt file still stops the run -- silently starting from blank would
        throw away a password the user thinks is saved.
        """
        if not config_path().exists():
            return cls()
        return cls.load()

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            how = "snipe.bat init" if sys.platform == "win32" else "python -m teesniper init"
            raise SystemExit(
                f"No {CONFIG_NAME} found at {path}.\n"
                f"Run:  {how}"
            )
        try:
            data: dict[str, Any] = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"{path} is not valid JSON ({e}).\n"
                f"Fix it, or delete it and run setup again."
            ) from e
        if not isinstance(data, dict):
            raise SystemExit(f"{path} should contain a JSON object.")
        raw_card = data.get("card") or {}
        known = {f.name for f in fields(Card)}
        card = Card(**{k: v for k, v in raw_card.items() if k in known})
        return cls(
            username=data.get("username", ""),
            password=data.get("password", ""),
            phone=data.get("phone", ""),
            email_opt_in=bool(data.get("email_opt_in", False)),
            sms_opt_in=bool(data.get("sms_opt_in", False)),
            card=card,
            setup_complete=bool(data.get("setup_complete", False)),
        )

    def save(self) -> Path:
        path = config_path()
        payload = {
            "username": self.username,
            "password": self.password,
            "phone": self.phone,
            "email_opt_in": self.email_opt_in,
            "sms_opt_in": self.sms_opt_in,
            "setup_complete": self.setup_complete,
            "card": {
                "number": self.card.number,
                "exp_month": self.card.exp_month,
                "exp_year": self.card.exp_year,
                "cvv": self.card.cvv,
                "name": self.card.name,
                "zip": self.card.zip,
            },
        }
        path.write_text(json.dumps(payload, indent=2))
        _lock_down(path)
        return path


def _lock_down(path: Path) -> None:
    """Make the config readable only by the current user, best-effort."""
    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME", "")
            if user:
                os.system(f'icacls "{path}" /inheritance:r /grant:r "{user}:F" >nul 2>&1')
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass
