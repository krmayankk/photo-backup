"""The ``status`` command: how many verified drive copies each phone item has.

Works entirely from laptop-side state (the last listing of each device and the mirrored
catalog of each drive), so the drives do not need to be plugged in.
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
    last_run: tuple[str, str | None, str | None] | None  # (run_id, finished, result)


def _load_drive_view(drive: MirroredDrive, device: str) -> _DriveView:
    db = sqlite3.connect(f"file:{drive.catalog_path}?mode=ro", uri=True)
    try:
        keys = {
            (p, s, m)
            for p, s, m in db.execute(
                "SELECT src_path, size, mtime FROM files "
                "WHERE device=? AND verified_at IS NOT NULL",
                (device,),
            )
        }
        last = db.execute(
            "SELECT run_id, finished, result FROM runs WHERE device=? ORDER BY run_id DESC LIMIT 1",
            (device,),
        ).fetchone()
    finally:
        db.close()
    return _DriveView(drive.label, keys, tuple(last) if last else None)


def run_status(
    *,
    device: str | None = None,
    min_copies: int = 2,
    out: Callable[[str], None] = print,
) -> int:
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

        per_kind: dict[Kind, list[int]] = {}  # kind -> [files, bytes, covered, zero]
        report_rows = []
        for f in files:
            holders = [v.label for v in views if (f.path, f.size, f.mtime) in v.keys]
            acc = per_kind.setdefault(f.kind, [0, 0, 0, 0])
            acc[0] += 1
            acc[1] += f.size
            acc[2] += len(holders) >= min_copies
            acc[3] += not holders
            report_rows.append([f.path, f.kind.value, f.size, len(holders), ",".join(holders)])

        out(f"{dev}   (phone last listed {listed_at:%Y-%m-%d %H:%M})")
        rows = []
        for kind in Kind:
            if kind not in per_kind:
                continue
            n, size, covered, zero = per_kind[kind]
            verdict = (
                _VERDICT_WHEN_COVERED[kind]
                if covered == n
                else f"NOT YET: {n - covered} on <{min_copies} drives"
            )
            rows.append([kind.value, n, human_bytes(size), covered, zero, verdict])
        headers = ["kind", "files", "size", f"on>={min_copies}", "on 0", "verdict"]
        for line in table(headers, rows):
            out("  " + line)
        for v in views:
            if v.last_run:
                run_id, _finished, result = v.last_run
                out(f"  drive {v.label}: last run {run_id} {result or 'unfinished'}")
            else:
                out(f"  drive {v.label}: no runs for this device")
        report = _write_report(dev, report_rows)
        out(f"  per-file report: {report}")
        out("")
    return EXIT_OK


def _write_report(device: str, rows: list[list[object]]) -> Path:
    folder = state_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"status-{device}-{datetime.now():%Y-%m-%d}.tsv"
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["path", "kind", "size", "copies", "drives"])
        w.writerows(rows)
    return path
