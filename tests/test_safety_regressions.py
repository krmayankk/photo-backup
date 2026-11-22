"""Regression tests for the data-safety findings of the first external review."""

from __future__ import annotations

import io
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import NOW, FakeSource, backup, make_drive, phone_files

from photo_backup.catalog import Catalog
from photo_backup.drive import Drive, init_drive
from photo_backup.progress import CountingReader, Progress, StallWatchdog
from photo_backup.report import EXIT_OK, EXIT_VERIFY_FAILED
from photo_backup.state import forget_drive, mirrored_drives
from photo_backup.status import run_status
from photo_backup.verify import run_verify

MONTH_DIR = "devices/fake-phone/2026-11"


def _status(**kw: object) -> dict[str, str]:
    out: list[str] = []
    run_status(out=out.append, **kw)  # type: ignore[arg-type]
    verdicts = {}
    for line in out:
        parts = line.split()
        if parts and parts[0] in {"video", "photo", "live-clip", "sidecar", "edited"}:
            verdicts[parts[0]] = line
    verdicts["_all"] = "\n".join(out)
    return verdicts


def _quiet_verify(drive: Drive) -> int:
    return run_verify(drive, out=lambda _l: None)


def _two_drives(tmp_path: Path, phone: FakeSource) -> tuple[Drive, Drive]:
    a, b = make_drive(tmp_path / "a", "drive-a"), make_drive(tmp_path / "b", "drive-b")
    assert backup(phone, a)[0] == EXIT_OK
    assert backup(phone, b)[0] == EXIT_OK
    return a, b


# 1.1 ----------------------------------------------------------------------------------


def test_damaged_copy_stops_counting_in_status(tmp_path: Path, phone: FakeSource) -> None:
    a, _ = _two_drives(tmp_path, phone)
    assert "SAFE TO DELETE" in _status()["video"]

    video = a.root / MONTH_DIR / "files/DCIM/100APPLE/IMG_0003.MOV"
    video.write_bytes(b"rot" + video.read_bytes()[3:])
    assert _quiet_verify(a) == EXIT_VERIFY_FAILED
    assert "NOT YET" in _status()["video"]


def test_backup_recopies_a_damaged_file_and_status_recovers(
    tmp_path: Path, phone: FakeSource
) -> None:
    a, _ = _two_drives(tmp_path, phone)
    (a.root / MONTH_DIR / "files/DCIM/100APPLE/IMG_0003.MOV").unlink()
    assert _quiet_verify(a) == EXIT_VERIFY_FAILED

    code, out = backup(phone, a, now=NOW + timedelta(days=1))
    assert code == EXIT_OK
    assert any("1 re-copies of damaged files" in line for line in out)
    assert _quiet_verify(a) == EXIT_OK
    assert "SAFE TO DELETE" in _status()["video"]


def test_gate_spot_check_marks_damage(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    with Catalog(drive.catalog_path) as cat:
        rows = list(cat.files())
    for row in rows:  # damage everything so any sample hits it
        p = drive.root / row.dest_path
        p.write_bytes(b"\0" * p.stat().st_size)
    phone.files["DCIM/new.jpg"] = (b"new", 5)
    code, _ = backup(phone, drive, now=NOW + timedelta(days=1))
    assert code != EXIT_OK  # gate fails on the damaged sample
    with Catalog(drive.catalog_path) as cat:
        assert any(not r.is_good for r in cat.files())


# 1.2 ----------------------------------------------------------------------------------


def test_source_files_named_like_temp_files_survive_reruns(drive: Drive) -> None:
    phone = FakeSource({"dl/movie.mp4.partial": (b"real data", 1), "dl/.pbtmp-x": (b"y", 1)})
    assert backup(phone, drive)[0] == EXIT_OK
    assert backup(phone, drive, now=NOW + timedelta(days=1))[0] == EXIT_OK
    assert _quiet_verify(drive) == EXIT_OK


def test_leftover_temp_file_from_a_crash_is_cleaned(drive: Drive, phone: FakeSource) -> None:
    leftover = drive.root / MONTH_DIR / "files/DCIM/100APPLE/.pbtmp-IMG_0003.MOV"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"half a video")
    code, out = backup(phone, drive)
    assert code == EXIT_OK
    assert not leftover.exists()
    assert any("temp files left by an interrupted run" in line for line in out)


# 1.3 ----------------------------------------------------------------------------------


def test_reinitialised_drive_counts_once(tmp_path: Path, phone: FakeSource) -> None:
    a = make_drive(tmp_path / "a", "drive-a")
    backup(phone, a)
    # the same physical drive is wiped and initialised again under the same label
    import shutil

    shutil.rmtree(a.root)
    again = init_drive(a.mount, "drive-a", require_mountpoint=False)
    backup(phone, again, now=NOW + timedelta(days=1))
    assert [d.label for d in mirrored_drives()] == ["drive-a"]
    assert "NOT YET" in _status()["video"]


def test_forget_and_stale_drive_warning(tmp_path: Path, phone: FakeSource) -> None:
    _two_drives(tmp_path, phone)
    later = datetime.now() + timedelta(days=400)
    assert "not seen since" in _status(now=later)["_all"]
    assert forget_drive("drive-b") == 1
    assert "NOT YET" in _status()["video"]


# 1.4 ----------------------------------------------------------------------------------


def test_photo_is_not_safe_while_its_live_clip_is_missing(
    tmp_path: Path, phone: FakeSource
) -> None:
    _two_drives(tmp_path, phone)
    # A new Live Photo whose clip was not readable when both drives were backed up:
    # the HEIC is on 2 drives, the clip on none.
    phone.files["DCIM/100APPLE/IMG_0009.HEIC"] = (b"heic-9", 1_700_000_900)
    clip = (b"live-9", 1_700_000_900)
    for d in mirrored_drives():
        drive = make_drive(tmp_path / "x" / d.label, d.label)  # fresh mounts, same labels
        backup(phone, drive, now=NOW + timedelta(days=1))
    phone.files["DCIM/100APPLE/IMG_0009.MOV"] = clip
    backup(phone, make_drive(tmp_path / "y", "drive-c"), dry_run=True)  # refresh listing only

    verdicts = _status()
    assert "NOT YET" in verdicts["photo"]
    assert "NOT YET" in verdicts["live-clip"]


# 1.5 / 1.6 ----------------------------------------------------------------------------


def test_resume_leaves_records_consistent(drive: Drive, phone: FakeSource, monkeypatch) -> None:
    real_put = Catalog.put
    calls = {"n": 0}

    def crash_once(self: Catalog, row: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        real_put(self, row)  # type: ignore[arg-type]

    monkeypatch.setattr(Catalog, "put", crash_once)
    with pytest.raises(KeyboardInterrupt):
        backup(phone, drive)
    monkeypatch.setattr(Catalog, "put", real_put)
    assert backup(phone, drive)[0] == EXIT_OK

    month = drive.root / MONTH_DIR
    sums = (month / "SHA256SUMS").read_text().splitlines()
    manifest = (month / "manifest.tsv").read_text().splitlines()[1:]
    # at most one harmless duplicate line, and every file is covered
    assert len(phone_files()) <= len(sums) <= len(phone_files()) + 1
    assert len(sums) == len(manifest)
    assert {line.split("  ", 1)[1] for line in sums} == {
        p.relative_to(month / "files").as_posix()
        for p in (month / "files").rglob("*")
        if p.is_file()
    }


def test_case_variant_duplicate_records_real_on_disk_name(drive: Drive) -> None:
    existing = drive.root / MONTH_DIR / "files/DCIM/IMG_1.HEIC"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"same bytes")
    phone = FakeSource({"DCIM/img_1.heic": (b"same bytes", 1)})
    assert backup(phone, drive)[0] == EXIT_OK
    with Catalog(drive.catalog_path) as cat:
        [row] = list(cat.files())
    assert row.dest_path.endswith("DCIM/IMG_1.HEIC")


# retries, abort, stall ----------------------------------------------------------------


class _Flaky(FakeSource):
    def __init__(self, failures: int) -> None:
        super().__init__({"DCIM/a.jpg": (b"aaa", 1)})
        self.failures = failures

    def open(self, path: str):  # type: ignore[override]
        if self.failures:
            self.failures -= 1
            raise OSError(5, "transient USB error")
        return super().open(path)


def test_transient_errors_are_retried(drive: Drive) -> None:
    code, out = backup(_Flaky(failures=2), drive)
    assert code == EXIT_OK
    assert sum("retry in" in line for line in out) == 2


def test_persistent_errors_fail_after_three_attempts(drive: Drive) -> None:
    code, out = backup(_Flaky(failures=99), drive)
    assert code == EXIT_VERIFY_FAILED
    assert sum("retry in" in line for line in out) == 2


def test_disconnected_source_stops_the_run(drive: Drive) -> None:
    files = {f"DCIM/{i}.jpg": (b"x", 1) for i in range(30)}
    phone = FakeSource(files)
    phone.broken = set(files)
    code, out = backup(phone, drive)
    assert code == EXIT_VERIFY_FAILED
    assert any("source looks disconnected" in line for line in out)
    summary = json.loads(next((drive.root / MONTH_DIR / "runs").glob("*/summary.json")).read_text())
    assert summary["counts"]["photo"]["aborted"] == 20


def test_stall_watchdog_warns_once_per_stall() -> None:
    progress, warnings = Progress(), []
    with StallWatchdog(progress, warnings.append, after=0.05, poll=0.01):
        time.sleep(0.2)
    assert len(warnings) == 1 and "no data" in warnings[0]
    reader = CountingReader(io.BytesIO(b"12345"), progress)
    assert reader.read(3) == b"123" and progress.bytes == 3


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_live_bar_renders_and_stays_out_of_pipes() -> None:
    from photo_backup.progress import LiveBar

    progress = Progress()
    progress.start_file("DCIM/100APPLE/IMG_0001.HEIC")
    progress.advance(512 * 1024**2)
    progress.finish_file()
    tty = _Tty()
    bar = LiveBar(progress, total_bytes=1024**3, total_files=4, stream=tty, interval=0.01)
    assert bar.enabled
    line = bar.render()
    assert "50%" in line and "[1/4]" in line and "IMG_0001.HEIC" in line
    printed: list[str] = []
    with bar:
        bar.wrap(printed.append)("a log line")
        time.sleep(0.05)
    assert printed == ["a log line"]
    assert "\r" in tty.getvalue()

    piped = LiveBar(progress, total_bytes=1, total_files=1, stream=io.StringIO())
    assert not piped.enabled
    assert piped.wrap(print) is print


# append-only records ------------------------------------------------------------------


def test_records_are_append_only_across_runs(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    month = drive.root / MONTH_DIR
    before = {n: (month / n).read_bytes() for n in ("manifest.tsv", "SHA256SUMS")}
    phone.files["DCIM/100APPLE/IMG_0500.HEIC"] = (b"later", 1_700_100_000)
    assert backup(phone, drive, now=NOW + timedelta(hours=1))[0] == EXIT_OK
    for name, old in before.items():
        new = (month / name).read_bytes()
        assert new.startswith(old) and len(new) > len(old), name
    with Catalog(drive.catalog_path) as cat:
        names = [p.rsplit("/", 1)[1] for p, _, _ in cat.records()]
    assert names == ["SHA256SUMS", "manifest.tsv"]


@pytest.mark.parametrize("damage", ["edit", "truncate", "delete"])
def test_in_place_changes_to_records_are_caught(
    drive: Drive, phone: FakeSource, damage: str
) -> None:
    backup(phone, drive)
    sums = drive.root / MONTH_DIR / "SHA256SUMS"
    data = sums.read_bytes()
    if damage == "edit":
        sums.write_bytes(b"0" + data[1:])  # same length, first byte changed
    elif damage == "truncate":
        sums.write_bytes(data[: len(data) // 2])
    else:
        sums.unlink()

    out: list[str] = []
    assert run_verify(drive, out=out.append) == EXIT_VERIFY_FAILED
    assert any("append-only records changed" in line for line in out)
    code, lines = backup(phone, drive, now=NOW + timedelta(hours=1))
    assert code != EXIT_OK
    assert any("GATE FAILED" in line for line in lines)
