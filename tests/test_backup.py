"""End-to-end tests of the backup command against a fake phone and a temp drive."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FakeSource, backup, make_drive, phone_files

from photo_backup import backup as backup_mod
from photo_backup.catalog import Catalog
from photo_backup.drive import Drive, DriveError
from photo_backup.report import EXIT_GATE_FAILED, EXIT_OK, EXIT_VERIFY_FAILED
from photo_backup.sources.directory import DirectorySource
from photo_backup.state import load_listing

MONTH = "2026-11"


def run_dir(drive: Drive, device: str = "fake-phone", month: str = MONTH) -> Path:
    return drive.device_dir(device) / month


def summary(drive: Drive, device: str = "fake-phone") -> dict:
    [path] = sorted((run_dir(drive, device) / "runs").glob("*/summary.json"))[-1:]
    return json.loads(path.read_text())


def manifest(drive: Drive) -> list[dict[str, str]]:
    with open(run_dir(drive) / "manifest.tsv", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def check_sums(drive: Drive) -> None:
    """Independent check equivalent to `sha256sum -c SHA256SUMS` from files/."""
    files = run_dir(drive) / "files"
    for line in (run_dir(drive) / "SHA256SUMS").read_text().splitlines():
        digest, rel = line.split("  ", 1)
        assert hashlib.sha256((files / rel).read_bytes()).hexdigest() == digest, rel


def test_first_backup_copies_everything_and_records_it(drive: Drive, phone: FakeSource) -> None:
    code, out = backup(phone, drive)
    assert code == EXIT_OK, "\n".join(out)

    files = run_dir(drive) / "files"
    for path, (data, mtime) in phone_files().items():
        assert (files / path).read_bytes() == data
        assert int((files / path).stat().st_mtime) == mtime

    assert len(manifest(drive)) == len(phone_files())
    check_sums(drive)
    s = summary(drive)
    assert s["result"] == "PASS"
    assert s["counts"]["video"]["copied"] == 2
    assert s["counts"]["live-clip"]["copied"] == 1
    with Catalog(drive.catalog_path) as cat:
        assert cat.count() == len(phone_files())
    assert (run_dir(drive) / "runs").glob("*/run.log")


def test_rerun_copies_nothing(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    code, _ = backup(phone, drive, now=datetime(2026, 11, 23))
    assert code == EXIT_OK
    counts = summary(drive)["counts"]
    assert sum(c["copied"] for c in counts.values()) == 0
    assert sum(c["skipped"] for c in counts.values()) == len(phone_files())


def test_new_files_land_in_a_new_month_folder(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    phone.files["DCIM/102APPLE/IMG_0200.HEIC"] = (b"new", 1_720_000_000)
    code, _ = backup(phone, drive, now=datetime(2027, 3, 1))
    assert code == EXIT_OK
    march = run_dir(drive, month="2027-03") / "files"
    assert [p.name for p in march.rglob("*") if p.is_file()] == ["IMG_0200.HEIC"]


def test_phone_deletions_never_touch_the_backup(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    phone.files.clear()
    code, _ = backup(phone, drive, now=datetime(2027, 1, 1))
    assert code == EXIT_OK
    assert len(list((run_dir(drive) / "files").rglob("*.*"))) == len(phone_files())


def test_dry_run_writes_nothing_but_records_listing(drive: Drive, phone: FakeSource) -> None:
    code, out = backup(phone, drive, dry_run=True)
    assert code == EXIT_OK
    assert not (drive.root / "devices").exists()
    assert not drive.health_path.exists()
    assert any("dry run" in line for line in out)
    listing = load_listing("fake-phone")
    assert listing is not None and len(listing[0]) == len(phone_files())


def test_limit_defers_the_rest(drive: Drive, phone: FakeSource) -> None:
    code, out = backup(phone, drive, limit=2)
    assert code == EXIT_OK
    assert "deferred by --limit" in out[-1]
    assert len(manifest(drive)) == 2
    backup(phone, drive)
    assert len(manifest(drive)) == len(phone_files())


def test_resume_after_crash_between_copy_and_catalog(
    drive: Drive, phone: FakeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_put = Catalog.put
    calls = {"n": 0}

    def crashing_put(self: Catalog, row: object) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt  # simulates power loss after the file landed
        real_put(self, row)  # type: ignore[arg-type]

    monkeypatch.setattr(Catalog, "put", crashing_put)
    with pytest.raises(KeyboardInterrupt):
        backup(phone, drive)
    monkeypatch.setattr(Catalog, "put", real_put)

    code, _ = backup(phone, drive)
    assert code == EXIT_OK
    counts = summary(drive)["counts"]
    assert sum(c["duplicates"] for c in counts.values()) == 1
    assert sum(c["conflicts"] for c in counts.values()) == 0
    landed = [p for p in (run_dir(drive) / "files").rglob("*") if p.is_file()]
    assert len(landed) == len(phone_files())
    assert not [p for p in landed if "conflict" in p.name or p.name.startswith(".pbtmp-")]


def test_different_bytes_at_same_path_are_kept_side_by_side(
    drive: Drive, phone: FakeSource
) -> None:
    squatter = run_dir(drive) / "files" / "DCIM/100APPLE/IMG_0002.JPG"
    squatter.parent.mkdir(parents=True)
    squatter.write_bytes(b"something else")
    code, _ = backup(phone, drive)
    assert code == EXIT_OK
    assert squatter.read_bytes() == b"something else"
    kept = squatter.with_name("IMG_0002.conflict-1.JPG")
    assert kept.read_bytes() == phone_files()["DCIM/100APPLE/IMG_0002.JPG"][0]
    assert summary(drive)["counts"]["photo"]["conflicts"] == 1


def test_unreadable_file_fails_the_run_but_keeps_the_rest(drive: Drive, phone: FakeSource) -> None:
    phone.broken.add("DCIM/100APPLE/IMG_0003.MOV")
    code, out = backup(phone, drive)
    assert code == EXIT_VERIFY_FAILED
    assert out[-1].startswith("RESULT: FAIL")
    assert len(manifest(drive)) == len(phone_files()) - 1

    phone.broken.clear()
    code, _ = backup(phone, drive)
    assert code == EXIT_OK
    assert len(manifest(drive)) == len(phone_files())


def test_truncated_source_read_is_caught(drive: Drive, phone: FakeSource) -> None:
    phone.lie_about_size["DCIM/100APPLE/IMG_0002.JPG"] = 10**6
    code, out = backup(phone, drive)
    assert code == EXIT_VERIFY_FAILED
    assert any("size mismatch" in line for line in out)
    assert not (run_dir(drive) / "files/DCIM/100APPLE/IMG_0002.JPG").exists()


def test_failed_gate_writes_nothing(drive: Drive, phone: FakeSource) -> None:
    code, out = backup(phone, drive, disk=None)  # SMART unknown -> FAIL by default
    assert code == EXIT_GATE_FAILED
    assert any("GATE FAILED" in line for line in out)
    assert not (drive.root / "devices").exists()


def test_files_too_big_for_the_filesystem_fail_loudly(
    drive: Drive, phone: FakeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_mod, "max_file_size", lambda _fs: 100_000)
    code, out = backup(phone, drive)
    assert code == EXIT_VERIFY_FAILED
    assert any("file size limit" in line for line in out)


def test_second_concurrent_run_is_refused(drive: Drive, phone: FakeSource) -> None:
    with drive.lock(), pytest.raises(DriveError, match="another photo-backup run"):
        backup(phone, drive)


def test_directory_source_never_backs_up_the_drive_into_itself(tmp_path: Path) -> None:
    disk = tmp_path / "olddisk"
    (disk / "photos").mkdir(parents=True)
    (disk / "photos" / "a.jpg").write_bytes(b"a")
    (disk / "System Volume Information").mkdir()
    (disk / "System Volume Information" / "junk").write_bytes(b"j")
    drive = make_drive(tmp_path, "olddisk")  # the drive *is* the source disk
    (drive.root / "devices").mkdir(parents=True)
    (drive.root / "devices" / "x.jpg").write_bytes(b"x")

    code, _ = backup(DirectorySource(disk, "old-disk"), drive)
    assert code == EXIT_OK
    landed = sorted(
        p.relative_to(run_dir(drive, "old-disk") / "files").as_posix()
        for p in (run_dir(drive, "old-disk") / "files").rglob("*")
        if p.is_file()
    )
    assert landed == ["photos/a.jpg"]


def test_awkward_filenames_are_sanitized_and_logged(drive: Drive) -> None:
    phone = FakeSource({"DCIM/what?.jpg": (b"q", 1), "DCIM/CON.png": (b"c", 2)})
    code, _ = backup(phone, drive)
    assert code == EXIT_OK
    dests = sorted(row["dest_path"] for row in manifest(drive))
    assert dests == ["DCIM/_CON.png", "DCIM/what_.jpg"]
    src = sorted(row["src_path"] for row in manifest(drive))
    assert src == ["DCIM/CON.png", "DCIM/what?.jpg"]
