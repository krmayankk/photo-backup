"""The ``verify`` command: re-hash backed-up files against the catalog."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime

from photo_backup.catalog import FileRow
from photo_backup.drive import Drive
from photo_backup.fsutil import hash_file
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
        good: list[FileRow] = []
        bad: list[str] = []
        missing: list[str] = []
        t0, done = time.monotonic(), 0
        for i, row in enumerate(rows, 1):
            path = drive.root / row.dest_path
            if not path.is_file():
                missing.append(row.dest_path)
                continue
            try:
                digest, size = hash_file(path)
            except OSError as exc:
                bad.append(f"{row.dest_path} (read error: {exc.strerror})")
                continue
            if digest == row.sha256 and size == row.size:
                good.append(row)
            else:
                bad.append(row.dest_path)
            done += row.size
            if i % PROGRESS_EVERY == 0:
                rate = done / max(time.monotonic() - t0, 1e-9) / 1024**2
                out(f"  [{i}/{len(rows)}] {human_bytes(done)}  {rate:.1f} MiB/s")

        catalog.mark_verified(good, datetime.now().isoformat(timespec="seconds"))
        mirror_catalog(drive, catalog)

    out(f"ok: {len(good)}   damaged: {len(bad)}   missing: {len(missing)}")
    for label, items in (("DAMAGED", bad), ("MISSING", missing)):
        for item in items[:20]:
            out(f"  {label} {item}")
        if len(items) > 20:
            out(f"  ... and {len(items) - 20} more")
    if bad or missing:
        out("RESULT: FAIL")
        return EXIT_VERIFY_FAILED
    out("RESULT: PASS")
    return EXIT_OK
