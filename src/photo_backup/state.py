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
    last_seen: datetime


def _index_path() -> Path:
    return state_dir() / "drives.json"


def _load_index() -> dict[str, dict[str, str]]:
    path = _index_path()
    return json.loads(path.read_text()) if path.is_file() else {}


def _save_index(index: dict[str, dict[str, str]]) -> None:
    _index_path().parent.mkdir(parents=True, exist_ok=True)
    _index_path().write_text(json.dumps(index, indent=2) + "\n")


def mirror_catalog(drive: Drive, catalog: Catalog) -> Path:
    dest = state_dir() / "drives" / f"{drive.uuid}.sqlite"
    catalog.snapshot_to(dest)
    index = _load_index()
    index[drive.uuid] = {
        "label": drive.label,
        "last_mount": str(drive.mount),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    _save_index(index)
    return dest


def mirrored_drives() -> list[MirroredDrive]:
    """One entry per drive label: the most recently seen mirror wins.

    A drive that was reformatted and re-initialised gets a new uuid but usually the same
    label; counting both mirrors would make one physical drive look like two copies.
    """
    latest: dict[str, MirroredDrive] = {}
    for drive_uuid, info in _load_index().items():
        path = state_dir() / "drives" / f"{drive_uuid}.sqlite"
        if not path.is_file():
            continue
        entry = MirroredDrive(
            drive_uuid, info["label"], path, datetime.fromisoformat(info["last_seen"])
        )
        current = latest.get(entry.label)
        if current is None or entry.last_seen > current.last_seen:
            latest[entry.label] = entry
    return sorted(latest.values(), key=lambda d: d.label)


def forget_drive(label: str) -> int:
    """Drop every mirror for ``label`` (lost, dead or wiped drive). Returns how many."""
    index = _load_index()
    gone = [u for u, info in index.items() if info["label"] == label]
    for drive_uuid in gone:
        del index[drive_uuid]
        (state_dir() / "drives" / f"{drive_uuid}.sqlite").unlink(missing_ok=True)
    _save_index(index)
    return len(gone)


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
