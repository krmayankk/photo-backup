"""Backup drives: identity (drive.json), layout, locking and discovery."""

from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from photo_backup.catalog import Catalog

ROOT_DIR = "PhotoBackup"
META_DIR = ".photo-backup"
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DriveError(Exception):
    pass


@dataclass(frozen=True)
class Drive:
    mount: Path
    uuid: str
    label: str
    created: str

    @property
    def root(self) -> Path:
        return self.mount / ROOT_DIR

    @property
    def meta(self) -> Path:
        return self.root / META_DIR

    @property
    def catalog_path(self) -> Path:
        return self.meta / "catalog.sqlite"

    @property
    def health_path(self) -> Path:
        return self.meta / "health.tsv"

    def device_dir(self, device: str) -> Path:
        return self.root / "devices" / device

    def open_catalog(self) -> Catalog:
        return Catalog(self.catalog_path)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive writer lock; a second concurrent run fails fast instead of waiting."""
        self.meta.mkdir(parents=True, exist_ok=True)
        with open(self.meta / "lock", "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DriveError(f"another photo-backup run is using {self.label}") from exc
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def validate_label(label: str) -> str:
    if not _LABEL_RE.match(label):
        raise DriveError(f"invalid label {label!r}: use letters, digits, '.', '_' or '-'")
    return label


def load_drive(mount: Path) -> Drive | None:
    meta_file = mount / ROOT_DIR / META_DIR / "drive.json"
    if not meta_file.is_file():
        return None
    data = json.loads(meta_file.read_text())
    return Drive(mount=mount, uuid=data["uuid"], label=data["label"], created=data["created"])


def init_drive(mount: Path, label: str, *, require_mountpoint: bool = True) -> Drive:
    """Mark ``mount`` as a photo-backup drive. Refuses to re-initialise."""
    validate_label(label)
    mount = mount.resolve()
    if not mount.is_dir():
        raise DriveError(f"{mount} is not a directory")
    if require_mountpoint and not os.path.ismount(mount):
        raise DriveError(f"{mount} is not a mount point (pass the drive's mount directory)")
    existing = load_drive(mount)
    if existing:
        raise DriveError(f"{mount} is already initialised as {existing.label!r}")
    drive = Drive(
        mount=mount,
        uuid=str(uuid.uuid4()),
        label=label,
        created=datetime.now().isoformat(timespec="seconds"),
    )
    drive.meta.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in asdict(drive).items() if k != "mount"}
    (drive.meta / "drive.json").write_text(json.dumps(payload, indent=2) + "\n")
    return drive


def candidate_mounts() -> list[Path]:
    """Mount points where removable drives usually appear."""
    user = getpass.getuser()
    bases = [Path("/media") / user, Path("/run/media") / user, Path("/mnt")]
    found = []
    for base in bases:
        if base.is_dir():
            found.extend(p for p in sorted(base.iterdir()) if os.path.ismount(p))
    return found


def resolve_drive(spec: str) -> Drive:
    """Find a drive by mount path or by label."""
    path = Path(spec).expanduser()
    if path.exists():
        drive = load_drive(path.resolve())
        if drive is None:
            raise DriveError(
                f"{path} is not initialised; run: photo-backup init {path} --label NAME"
            )
        return drive
    for mount in candidate_mounts():
        drive = load_drive(mount)
        if drive and drive.label == spec:
            return drive
    raise DriveError(f"no attached drive with path or label {spec!r} (see: photo-backup drives)")


def block_device(mount: Path) -> str | None:
    """Whole-disk device (e.g. /dev/sdb) backing ``mount``, if it is a real block device."""
    try:
        source = subprocess.run(
            ["findmnt", "-no", "SOURCE", "--target", str(mount)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        if not source.startswith("/dev/"):
            return None
        parent = subprocess.run(
            ["lsblk", "-no", "PKNAME", source],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return f"/dev/{parent}" if parent else source
