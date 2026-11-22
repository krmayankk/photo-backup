"""Byte-level progress tracking and a stall watchdog.

A USB/FUSE read can hang without ever raising an error. The watchdog cannot safely
interrupt such a read, but it tells the user within minutes instead of leaving a silent
terminal: the fix is Ctrl-C, reconnect, re-run (which resumes).
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import BinaryIO, TextIO


class Progress:
    """Bytes moved so far and when they last moved. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes = 0
        self._last_change = time.monotonic()
        self.files_done = 0
        self.current = ""

    def start_file(self, path: str) -> None:
        self.current = path

    def finish_file(self) -> None:
        with self._lock:
            self.files_done += 1

    def advance(self, n: int) -> None:
        with self._lock:
            self._bytes += n
            self._last_change = time.monotonic()

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    def idle_for(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_change


class CountingReader:
    """Wraps a binary stream and reports every chunk read to a :class:`Progress`."""

    def __init__(self, raw: BinaryIO, progress: Progress) -> None:
        self._raw = raw
        self._progress = progress

    def read(self, n: int = -1) -> bytes:
        data = self._raw.read(n)
        if data:
            self._progress.advance(len(data))
        return data


class StallWatchdog:
    """Calls ``warn`` once per stall when no bytes have moved for ``after`` seconds."""

    def __init__(
        self,
        progress: Progress,
        warn: Callable[[str], None],
        *,
        after: float = 180.0,
        poll: float = 5.0,
    ) -> None:
        self._progress = progress
        self._warn = warn
        self._after = after
        self._poll = poll
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="stall-watchdog", daemon=True)

    def _watch(self) -> None:
        warned_at_bytes: int | None = None
        while not self._stop.wait(self._poll):
            idle = self._progress.idle_for()
            moved = self._progress.bytes
            if idle >= self._after and warned_at_bytes != moved:
                warned_at_bytes = moved
                self._warn(
                    f"  !! no data for {idle / 60:.0f} min: is the phone still connected and "
                    "unlocked? If it stays stuck: Ctrl-C, reconnect, re-run (it resumes)."
                )

    def __enter__(self) -> StallWatchdog:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=self._poll * 2)


class LiveBar:
    """A single, self-updating progress line for interactive terminals.

    Disabled automatically when ``stream`` is not a TTY (pipes, logs, CI), so redirected
    output stays clean; the periodic progress lines in ``run.log`` are always written.
    Log lines printed while the bar is active go through :meth:`wrap`, which clears the
    bar, prints the line, and redraws the bar below it.
    """

    WIDTH = 24

    def __init__(
        self,
        progress: Progress,
        *,
        total_bytes: int,
        total_files: int,
        stream: TextIO | None = None,
        interval: float = 1.0,
        enabled: bool = True,
    ) -> None:
        self._progress = progress
        self._total_bytes = max(total_bytes, 1)
        self._total_files = total_files
        self._stream = stream or sys.stderr
        self._interval = interval
        self.enabled = enabled and self._stream.isatty()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="progress-bar", daemon=True)

    def render(self) -> str:
        done = self._progress.bytes
        frac = min(done / self._total_bytes, 1.0)
        filled = int(frac * self.WIDTH)
        elapsed = time.monotonic() - self._start
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (self._total_bytes - done) / rate if rate else 0.0
        bar = "█" * filled + "░" * (self.WIDTH - filled)
        gib = 1024**3
        current = self._progress.current.rsplit("/", 1)[-1]
        line = (
            f"{frac:4.0%} {bar} {done / gib:.1f}/{self._total_bytes / gib:.1f} GiB  "
            f"{rate / 1024**2:.1f} MiB/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s  "
            f"[{self._progress.files_done}/{self._total_files}] {current}"
        )
        width = shutil.get_terminal_size((100, 20)).columns - 1
        return line[:width]

    def _draw(self) -> None:
        self._stream.write("\r" + self.render() + "\x1b[K")
        self._stream.flush()

    def _clear(self) -> None:
        self._stream.write("\r\x1b[K")
        self._stream.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                self._draw()

    def wrap(self, out: Callable[[str], None]) -> Callable[[str], None]:
        if not self.enabled:
            return out

        def printer(line: str) -> None:
            with self._lock:
                self._clear()
                out(line)
                self._draw()

        return printer

    def __enter__(self) -> LiveBar:
        if self.enabled:
            self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.enabled:
            self._stop.set()
            self._thread.join(timeout=self._interval * 2)
            with self._lock:
                self._clear()
