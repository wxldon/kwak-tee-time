"""Local credential/config storage.

Everything sensitive lives in config.json next to the bot, which .gitignore
excludes. Nothing is ever written back to the repo.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"


def config_path() -> Path:
    """config.json beside the bot, overridable with TEESNIPER_CONFIG."""
    env = os.environ.get("TEESNIPER_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / CONFIG_NAME


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
        """Like ``load`` but returns a blank config instead of exiting."""
        try:
            return cls.load()
        except SystemExit:
            return cls()

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            how = "snipe.bat init" if sys.platform == "win32" else "python -m teesniper init"
            raise SystemExit(
                f"No {CONFIG_NAME} found at {path}.\n"
                f"Run:  {how}"
            )
        data: dict[str, Any] = json.loads(path.read_text())
        card = Card(**(data.get("card") or {}))
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
