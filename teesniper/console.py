"""A sticky one-line status bar that survives log output from other threads.

Both courses log from their own threads while the countdown ticks, so the
countdown cannot simply be printed -- it has to be erased before any log line
and redrawn after, under a lock. Falls back to printing nothing extra when
stdout is not a terminal, so piping to a file stays clean.
"""

from __future__ import annotations

import sys
import threading


class Console:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self._text = ""
        self._drawn = 0
        try:
            self.live = bool(self.stream.isatty())
        except (AttributeError, ValueError):
            self.live = False

    def log(self, message: str) -> None:
        """Print a line, keeping the status bar pinned underneath it."""
        with self._lock:
            self._erase()
            self.stream.write(message + "\n")
            self._draw()
            self.stream.flush()

    def status(self, text: str) -> None:
        with self._lock:
            if text == self._text:
                return
            self._text = text
            self._erase()
            self._draw()
            self.stream.flush()

    def clear(self) -> None:
        with self._lock:
            self._text = ""
            self._erase()
            self.stream.flush()

    # ------------------------------------------------------------- internals

    def _erase(self) -> None:
        if self._drawn:
            self.stream.write("\r" + " " * self._drawn + "\r")
            self._drawn = 0

    def _draw(self) -> None:
        if self.live and self._text:
            self.stream.write(self._text)
            self._drawn = len(self._text)


def countdown(seconds: float) -> str:
    """``1h 02m 13s`` / ``20m 13s`` / ``13s`` -- steady width as it ticks."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
