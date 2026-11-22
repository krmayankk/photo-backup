"""Local (laptop-side) state: catalog mirrors of every drive and the latest phone listings.

This lets ``status`` answer "is this on two drives?" with the drives unplugged.
Location: ``$PHOTO_BACKUP_STATE`` or ``$XDG_DATA_HOME/photo-backup`` (~/.local/share).
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from photo_backup.catalog import Catalog
from photo_backup.drive import Drive
from photo_backup.model import Kind, SourceFile


def state_dir() -> Path:
    override = os.environ.get("PHOTO_BACKUP_STATE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "photo-backup"


@dataclass(frozen=True)
class MirroredDrive:
    uuid: str
    label: str
    catalog_path: Path


def mirror_catalog(drive: Drive, catalog: Catalog) -> Path:
    dest = state_dir() / "drives" / f"{drive.uuid}.sqlite"
    catalog.snapshot_to(dest)
    index_path = state_dir() / "drives.json"
    index = json.loads(index_path.read_text()) if index_path.is_file() else {}
    index[drive.uuid] = {
        "label": drive.label,
        "last_mount": str(drive.mount),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    index_path.write_text(json.dumps(index, indent=2) + "\n")
    return dest


def mirrored_drives() -> list[MirroredDrive]:
    index_path = state_dir() / "drives.json"
    if not index_path.is_file():
        return []
    index = json.loads(index_path.read_text())
    out = []
    for drive_uuid, info in sorted(index.items(), key=lambda kv: kv[1]["label"]):
        path = state_dir() / "drives" / f"{drive_uuid}.sqlite"
        if path.is_file():
            out.append(MirroredDrive(drive_uuid, info["label"], path))
    return out


@dataclass(frozen=True)
class ListedFile:
    path: str
    size: int
    mtime: int
    kind: Kind


_LISTING_FIELDS = ["path", "size", "mtime", "kind"]


def save_listing(device: str, files: list[SourceFile], kinds: dict[str, Kind]) -> Path:
    """Record everything the device had, so status can judge it later."""
    folder = state_dir() / "devices" / device
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    dated = folder / f"listing-{stamp}.tsv"
    with open(dated, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(_LISTING_FIELDS)
        for f in files:
            w.writerow([f.path, f.size, f.mtime, kinds[f.path].value])
    latest = folder / "latest-listing.tsv"
    latest.write_bytes(dated.read_bytes())
    return dated


def load_listing(device: str) -> tuple[list[ListedFile], datetime] | None:
    path = state_dir() / "devices" / device / "latest-listing.tsv"
    if not path.is_file():
        return None
    with open(path, newline="") as fh:
        rows = [
            ListedFile(r["path"], int(r["size"]), int(r["mtime"]), Kind(r["kind"]))
            for r in csv.DictReader(fh, delimiter="\t")
        ]
    return rows, datetime.fromtimestamp(path.stat().st_mtime)


def known_devices() -> list[str]:
    folder = state_dir() / "devices"
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if (p / "latest-listing.tsv").is_file())
