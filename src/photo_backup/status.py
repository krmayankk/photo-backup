"""The ``status`` command: how many verified drive copies each phone item has.

Works entirely from laptop-side state (the last listing of each device and the mirrored
catalog of each drive), so the drives do not need to be plugged in.

Safety rules:

* only copies with a non-NULL ``verified_at`` count (``verify`` clears it on damage);
* each drive label counts once, however many times it was re-initialised;
* a file is "covered" only when **every file of its Photos item** (photo, Live clip,
  edit sidecar, rendered edit) is on at least ``min_copies`` drives, because deleting
  the item on the phone deletes all of them.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from photo_backup.classify import asset_key
from photo_backup.model import Kind
from photo_backup.report import EXIT_OK, human_bytes, table
from photo_backup.state import (
    MirroredDrive,
    known_devices,
    load_listing,
    mirrored_drives,
    state_dir,
)

Key = tuple[str, int, int]  # (src_path, size, mtime)
STALE_AFTER = timedelta(days=180)

_VERDICT_WHEN_COVERED = {
    Kind.VIDEO: "SAFE TO DELETE",
    Kind.PHOTO: "safe to delete (optional)",
    Kind.LIVE_CLIP: "goes with its photo",
    Kind.SIDECAR: "goes with its photo",
    Kind.EDITED: "goes with its photo",
    Kind.OTHER: "safe to delete",
}


@dataclass
class _DriveView:
    label: str
    keys: set[Key]
    last_run: tuple[str, str | None] | None  # (run_id, result)
    last_seen: datetime


@dataclass
class _KindTotals:
    files: int = 0
    size: int = 0
    covered: int = 0
    on_zero: int = 0


def _load_drive_view(drive: MirroredDrive, device: str) -> _DriveView:
    db = sqlite3.connect(f"file:{drive.catalog_path}?mode=ro", uri=True)
    try:
        keys: set[Key] = {
            (p, s, m)
            for p, s, m in db.execute(
                "SELECT src_path, size, mtime FROM files "
                "WHERE device=? AND verified_at IS NOT NULL",
                (device,),
            )
        }
        last = db.execute(
            "SELECT run_id, result FROM runs WHERE device=? ORDER BY run_id DESC LIMIT 1",
            (device,),
        ).fetchone()
    finally:
        db.close()
    return _DriveView(drive.label, keys, tuple(last) if last else None, drive.last_seen)


def run_status(
    *,
    device: str | None = None,
    min_copies: int = 2,
    out: Callable[[str], None] = print,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now()
    drives = mirrored_drives()
    devices = [device] if device else known_devices()
    if not devices:
        out("no devices recorded yet: run a backup (or a --dry-run) first")
        return EXIT_OK

    for dev in devices:
        listing = load_listing(dev)
        if listing is None:
            out(f"{dev}: no listing recorded")
            continue
        files, listed_at = listing
        views = [_load_drive_view(d, dev) for d in drives]

        holders = {
            f.path: [v.label for v in views if (f.path, f.size, f.mtime) in v.keys] for f in files
        }
        # An item is covered only if every one of its files has enough copies.
        item_ok: dict[tuple[str, str], bool] = defaultdict(lambda: True)
        for f in files:
            item_ok[asset_key(f.path)] &= len(holders[f.path]) >= min_copies

        totals: dict[Kind, _KindTotals] = defaultdict(_KindTotals)
        report_rows = []
        for f in files:
            t = totals[f.kind]
            t.files += 1
            t.size += f.size
            t.covered += item_ok[asset_key(f.path)]
            t.on_zero += not holders[f.path]
            report_rows.append(
                [
                    f.path,
                    f.kind.value,
                    f.size,
                    len(holders[f.path]),
                    ",".join(holders[f.path]),
                    "yes" if item_ok[asset_key(f.path)] else "no",
                ]
            )

        out(f"{dev}   (phone last listed {listed_at:%Y-%m-%d %H:%M})")
        rows = []
        for kind in Kind:
            if kind not in totals:
                continue
            t = totals[kind]
            verdict = (
                _VERDICT_WHEN_COVERED[kind]
                if t.covered == t.files
                else f"NOT YET: {t.files - t.covered} not on {min_copies} drives"
            )
            rows.append([kind.value, t.files, human_bytes(t.size), t.covered, t.on_zero, verdict])
        headers = ["kind", "files", "size", "covered", "on 0", "verdict"]
        for line in table(headers, rows):
            out("  " + line)
        out(f"  covered = the whole item (photo + clip + edits) is on >= {min_copies} drives")
        for v in views:
            run = (
                f"last run {v.last_run[0]} {v.last_run[1] or 'unfinished'}"
                if v.last_run
                else ("no runs for this device")
            )
            stale = (
                f"  !! not seen since {v.last_seen:%Y-%m-%d}: plug it in and run verify, "
                f"or `photo-backup forget {v.label}` if it is gone"
                if now - v.last_seen > STALE_AFTER
                else ""
            )
            out(f"  drive {v.label}: {run}{stale}")
        out(f"  per-file report: {_write_report(dev, report_rows)}")
        out("")
    return EXIT_OK


def _write_report(device: str, rows: list[list[object]]) -> Path:
    folder = state_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"status-{device}-{datetime.now():%Y-%m-%d}.tsv"
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["path", "kind", "size", "copies", "drives", "item_covered"])
        w.writerows(rows)
    return path
