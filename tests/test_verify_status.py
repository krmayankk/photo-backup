from pathlib import Path

from conftest import FakeSource, backup, make_drive

from photo_backup.drive import Drive
from photo_backup.report import EXIT_OK, EXIT_VERIFY_FAILED
from photo_backup.status import run_status
from photo_backup.verify import run_verify


def _verify(drive: Drive, **kw: object) -> tuple[int, list[str]]:
    out: list[str] = []
    return run_verify(drive, out=out.append, **kw), out  # type: ignore[arg-type]


def _status(**kw: object) -> list[str]:
    out: list[str] = []
    assert run_status(out=out.append, **kw) == EXIT_OK  # type: ignore[arg-type]
    return out


def test_verify_passes_on_a_fresh_backup(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    code, out = _verify(drive)
    assert code == EXIT_OK
    assert out[-1] == "RESULT: PASS"


def test_verify_detects_damage_and_missing_files(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    with drive.open_catalog() as cat:
        rows = list(cat.files())
    damaged: Path = drive.root / rows[0].dest_path
    damaged.write_bytes(b"X" + damaged.read_bytes()[1:])
    (drive.root / rows[1].dest_path).unlink()

    code, out = _verify(drive)
    assert code == EXIT_VERIFY_FAILED
    assert any(line.startswith("  DAMAGED") and rows[0].dest_path in line for line in out)
    assert any(line.startswith("  MISSING") and rows[1].dest_path in line for line in out)


def test_verify_sample_and_filters(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    code, out = _verify(drive, sample_pct=50, seed=3)
    assert code == EXIT_OK
    assert out[0].startswith("verifying 4 files")
    code, out = _verify(drive, month="1999-01")
    assert out == ["nothing to verify"]


def _verdicts(lines: list[str]) -> dict[str, str]:
    rows = {}
    for line in lines:
        parts = line.split()
        if parts and parts[0] in {"video", "photo", "live-clip", "sidecar", "edited"}:
            rows[parts[0]] = line
    return rows


def test_status_requires_two_drives_before_videos_are_safe(
    tmp_path: Path, phone: FakeSource
) -> None:
    first = make_drive(tmp_path / "a", "drive-a")
    second = make_drive(tmp_path / "b", "drive-b")

    backup(phone, first)
    verdicts = _verdicts(_status())
    assert "NOT YET" in verdicts["video"]

    backup(phone, second)
    out = _status()
    verdicts = _verdicts(out)
    assert "SAFE TO DELETE" in verdicts["video"]
    assert "goes with its photo" in verdicts["live-clip"]
    assert any("drive drive-a: last run" in line for line in out)


def test_status_with_one_copy_required(drive: Drive, phone: FakeSource) -> None:
    backup(phone, drive)
    assert "SAFE TO DELETE" in _verdicts(_status(min_copies=1))["video"]


def test_status_without_any_runs() -> None:
    assert _status() == ["no devices recorded yet: run a backup (or a --dry-run) first"]
