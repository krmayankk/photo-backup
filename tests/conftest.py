from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import pytest

from photo_backup.backup import BackupOptions
from photo_backup.drive import Drive, init_drive
from photo_backup.health import GateOptions
from photo_backup.model import SourceFile
from photo_backup.sources.base import Source

NOW = datetime(2026, 11, 22, 10, 0, 0)

HEALTHY_SMART = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"id": 5, "raw": {"value": 0}},
            {"id": 197, "raw": {"value": 0}},
            {"id": 198, "raw": {"value": 0}},
        ]
    },
    "temperature": {"current": 34},
    "power_on_time": {"hours": 1200},
}


class FakeSource(Source):
    """An in-memory phone. ``files`` maps path -> (bytes, mtime)."""

    def __init__(self, files: dict[str, tuple[bytes, int]], name: str = "fake-phone") -> None:
        self.files = dict(files)
        self.name = name
        self.info = {"type": "fake"}
        self.broken: set[str] = set()  # paths whose open() fails
        self.lie_about_size: dict[str, int] = {}  # path -> size reported by list_files

    def list_files(self) -> list[SourceFile]:
        return sorted(
            (
                SourceFile(p, self.lie_about_size.get(p, len(data)), mtime)
                for p, (data, mtime) in self.files.items()
            ),
            key=lambda f: f.path,
        )

    def open(self, path: str) -> BinaryIO:
        if path in self.broken:
            raise OSError(5, "Input/output error")
        return io.BytesIO(self.files[path][0])


def fake_runner(smart: dict | None = None, journal: str = "") -> object:
    """A Runner that answers smartctl and journalctl like a healthy machine."""

    def runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if "smartctl" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(smart or HEALTHY_SMART), "")
        if cmd[0] == "journalctl":
            return subprocess.CompletedProcess(cmd, 0, journal, "")
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")

    return runner


def phone_files() -> dict[str, tuple[bytes, int]]:
    """A small but realistic iPhone camera roll."""
    return {
        "DCIM/100APPLE/IMG_0001.HEIC": (b"heic-1" * 1000, 1_700_000_000),
        "DCIM/100APPLE/IMG_0001.MOV": (b"live-1" * 3000, 1_700_000_000),  # Live Photo clip
        "DCIM/100APPLE/IMG_0002.JPG": (b"jpg-2" * 800, 1_700_000_100),
        "DCIM/100APPLE/IMG_0003.MOV": (b"video-3" * 20000, 1_700_000_200),  # real video
        "DCIM/100APPLE/IMG_0003.AAE": (b"<plist/>", 1_700_000_200),
        "DCIM/101APPLE/IMG_0100.MP4": (b"video-100" * 9000, 1_710_000_000),
        "PhotoData/Mutations/DCIM/100APPLE/IMG_0002/Adjustments/FullSizeRender.jpg": (
            b"edited-2" * 700,
            1_700_000_300,
        ),
    }


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("PHOTO_BACKUP_STATE", str(state))
    return state


def make_drive(root: Path, label: str) -> Drive:
    mount = root / label
    mount.mkdir(parents=True, exist_ok=True)
    return init_drive(mount, label, require_mountpoint=False)


@pytest.fixture
def drive(tmp_path: Path) -> Drive:
    return make_drive(tmp_path / "mnt", "test-drive")


@pytest.fixture
def phone() -> FakeSource:
    return FakeSource(phone_files())


def opts(**kw: object) -> BackupOptions:
    gate = GateOptions(seed=1)
    kw.setdefault("retry_delays", (0.0, 0.0))  # keep retries instant in tests
    return BackupOptions(gate=gate, now=kw.pop("now", NOW), **kw)  # type: ignore[arg-type]


def backup(source: Source, drive: Drive, **kw: object) -> tuple[int, list[str]]:
    """Run a backup against a healthy fake disk; return (exit code, output lines)."""
    from photo_backup.backup import run_backup

    lines: list[str] = []
    runner = kw.pop("runner", None) or fake_runner()
    disk = kw.pop("disk", "/dev/sdz")
    code = run_backup(source, drive, opts(**kw), disk=disk, runner=runner, out=lines.append)  # type: ignore[arg-type]
    return code, lines
