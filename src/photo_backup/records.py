"""Human-readable run records: the run log and the append-only manifest/checksum files."""

from __future__ import annotations

import csv
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO

MANIFEST_FIELDS = ["src_path", "dest_path", "size", "mtime", "sha256", "kind", "status", "run_id"]


class RunLog:
    """Prints lines and, once a run folder exists, also appends them to ``run.log``.

    Thread-safe so the stall watchdog can write from its own thread.
    """

    def __init__(self, out: Callable[[str], None]) -> None:
        self._out = out
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._fh: TextIO | None = None

    def __call__(self, line: str = "") -> None:
        stamped = f"{datetime.now().isoformat(timespec='seconds')}  {line}"
        with self._lock:
            self._out(line)
            if self._fh:
                self._fh.write(stamped + "\n")
                self._fh.flush()
            else:
                self._buffer.append(stamped)

    def swap_out(self, out: Callable[[str], None]) -> Callable[[str], None]:
        """Replace the print function (e.g. to route through a live progress bar)."""
        with self._lock:
            previous, self._out = self._out, out
        return previous

    def attach(self, path: Path) -> None:
        with self._lock:
            self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()
            self._fh.write("\n".join(self._buffer) + "\n")
            self._fh.flush()
            self._buffer.clear()

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


class RunRecords:
    """Append-only ``manifest.tsv`` + ``SHA256SUMS`` for one month folder.

    Every line is fsynced before the catalog is updated, so the plain-text records are
    never behind the catalog. A crash between the two can leave one duplicate line,
    which is harmless (``sha256sum -c`` simply checks that file twice).
    """

    def __init__(self, run_dir: Path) -> None:
        self._files_dir = run_dir / "files"
        manifest = run_dir / "manifest.tsv"
        new = not manifest.exists()
        self._manifest = open(manifest, "a", newline="", encoding="utf-8")  # noqa: SIM115
        try:
            self._sums = open(run_dir / "SHA256SUMS", "a", encoding="utf-8")  # noqa: SIM115
        except BaseException:
            self._manifest.close()
            raise
        self._writer = csv.writer(self._manifest, delimiter="\t", lineterminator="\n")
        if new:
            self._writer.writerow(MANIFEST_FIELDS)

    def __enter__(self) -> RunRecords:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._manifest.close()
        self._sums.close()

    def add(
        self,
        *,
        src_path: str,
        dest: Path,
        size: int,
        mtime: int,
        sha256: str,
        kind: str,
        status: str,
        run_id: str,
    ) -> None:
        rel = dest.relative_to(self._files_dir).as_posix()
        self._writer.writerow([src_path, rel, size, mtime, sha256, kind, status, run_id])
        self._sums.write(f"{sha256}  {rel}\n")
        for fh in (self._manifest, self._sums):
            fh.flush()
            os.fsync(fh.fileno())
