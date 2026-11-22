"""The ``verify`` command: re-hash backed-up files against the catalog.

Good copies get a fresh ``verified_at``; damaged or missing ones are marked damaged so
``status`` stops counting them and the next ``backup`` copies them again.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime

from photo_backup.catalog import FileRow
from photo_backup.drive import Drive
from photo_backup.fsutil import hash_file
from photo_backup.health import records_check
from photo_backup.model import Level
from photo_backup.progress import LiveBar, Progress
from photo_backup.report import EXIT_OK, EXIT_VERIFY_FAILED, human_bytes
from photo_backup.state import mirror_catalog

PROGRESS_EVERY = 500


def run_verify(
    drive: Drive,
    *,
    device: str | None = None,
    month: str | None = None,
    sample_pct: float | None = None,
    seed: int | None = None,
    live_progress: bool = True,
    out: Callable[[str], None] = print,
) -> int:
    with drive.lock(), drive.open_catalog() as catalog:
        rows = list(catalog.files(device, month))
        if sample_pct is not None and rows:
            k = max(1, round(len(rows) * sample_pct / 100))
            rows = random.Random(seed).sample(rows, min(k, len(rows)))
        if not rows:
            out("nothing to verify")
            return EXIT_OK

        total = sum(r.size for r in rows)
        out(f"verifying {len(rows)} files ({human_bytes(total)}) on {drive.label}")
        good, bad, missing = _check(drive, rows, total, live_progress, out)
        records = records_check(drive, catalog)

        catalog.mark_verified(good, datetime.now().isoformat(timespec="seconds"))
        catalog.mark_damaged(bad + missing)
        mirror_catalog(drive, catalog)

    out(f"ok: {len(good)}   damaged: {len(bad)}   missing: {len(missing)}")
    out(str(records))
    for label, items in (("DAMAGED", bad), ("MISSING", missing)):
        for row in items[:20]:
            out(f"  {label} {row.dest_path}")
        if len(items) > 20:
            out(f"  ... and {len(items) - 20} more")
    if records.level is Level.FAIL:
        out("RESULT: FAIL  (manifest/checksum files were modified in place)")
        return EXIT_VERIFY_FAILED
    if bad or missing:
        out("RESULT: FAIL  (these copies no longer count in status; re-run backup to recopy)")
        return EXIT_VERIFY_FAILED
    out("RESULT: PASS")
    return EXIT_OK


def _check(
    drive: Drive,
    rows: list[FileRow],
    total: int,
    live_progress: bool,
    out: Callable[[str], None],
) -> tuple[list[FileRow], list[FileRow], list[FileRow]]:
    """Re-hash every row's file. Returns (good, damaged, missing)."""
    good: list[FileRow] = []
    bad: list[FileRow] = []
    missing: list[FileRow] = []
    progress = Progress()
    bar = LiveBar(progress, total_bytes=total, total_files=len(rows), enabled=live_progress)
    printer = bar.wrap(out)
    t0 = time.monotonic()
    with bar:
        for i, row in enumerate(rows, 1):
            progress.start_file(row.dest_path)
            path = drive.root / row.dest_path
            if not path.is_file():
                missing.append(row)
            else:
                try:
                    digest, size = hash_file(path)
                except OSError:
                    bad.append(row)  # unreadable counts as damaged
                else:
                    ok = digest == row.sha256 and size == row.size
                    (good if ok else bad).append(row)
            progress.advance(row.size)
            progress.finish_file()
            if i % PROGRESS_EVERY == 0:
                rate = progress.bytes / max(time.monotonic() - t0, 1e-9) / 1024**2
                printer(f"  [{i}/{len(rows)}] {human_bytes(progress.bytes)}  {rate:.1f} MiB/s")
    return good, bad, missing
