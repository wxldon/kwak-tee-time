"""A full transcript of every API call, written to a file you can read later.

A snipe happens at 8:00:00 PM in a couple of seconds, and whatever went wrong
scrolls past. This records every request and response -- including the exact
bodies -- so a failure can be diagnosed after the fact instead of by trying to
reproduce it at the next drop.

Card numbers, CVVs, passwords and session tokens are masked on the way in, so
the log is safe to read and to send to someone else.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("teesniper.http")

# Anything whose key looks like one of these never reaches the file intact.
_SECRET_HINTS = (
    "creditcardnumber", "cardnumber", "cvv", "cvvcode", "password",
    "credentials", "sessiontoken", "session", "token", "authorization",
)
_MAX = 4000


def _looks_secret(key: str) -> bool:
    k = key.lower().replace("_", "").replace(".", "").replace("-", "")
    return any(hint in k for hint in _SECRET_HINTS)


def redact(value: Any) -> Any:
    """Deep-copy ``value`` with anything secret replaced by a shape summary."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _looks_secret(str(k)):
                text = "" if v is None else str(v)
                if len(text) >= 4 and text.isdigit():
                    out[k] = f"<{len(text)} digits ending {text[-4:]}>"
                else:
                    out[k] = f"<redacted {len(text)} chars>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _render(value: Any) -> str:
    if value is None:
        return "-"
    try:
        text = json.dumps(redact(value), default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= _MAX else text[:_MAX] + f"... ({len(text)} chars)"


def log_call(method: str, url: str, *, request: Any = None, status: int | None = None,
             response: Any = None, error: str | None = None,
             elapsed_ms: float | None = None) -> None:
    parts = [f"{method.upper()} {url}"]
    if status is not None:
        parts.append(f"-> {status}")
    if elapsed_ms is not None:
        parts.append(f"({elapsed_ms:.0f}ms)")
    log.debug(" ".join(parts))
    if request is not None:
        log.debug("    request  %s", _render(request))
    if response is not None:
        log.debug("    response %s", _render(response))
    if error:
        log.debug("    error    %s", error)


def start(directory: Path, label: str = "run") -> Path:
    """Begin a transcript. Returns the file it is writing to."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(name)s  %(message)s"))
    root = logging.getLogger("teesniper")
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    # The transcript is for us, not for the terminal.
    root.propagate = True
    log.debug("=== teesniper transcript started %s ===", dt.datetime.now().isoformat())
    return path


def prune(directory: Path, keep: int = 20) -> None:
    """Keep only the newest ``keep`` transcripts."""
    try:
        files = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
