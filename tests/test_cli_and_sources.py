from pathlib import Path

import pytest

from photo_backup import cli
from photo_backup.drive import init_drive, load_drive
from photo_backup.report import EXIT_GATE_FAILED, EXIT_OK, EXIT_USAGE
from photo_backup.sources import open_source
from photo_backup.sources.base import SourceError
from photo_backup.sources.iphone import IPhoneSource, device_name


def test_iphone_edits_land_under_edited() -> None:
    src = IPhoneSource.__new__(IPhoneSource)  # dest_path needs no connected phone
    assert (
        src.dest_path("PhotoData/Mutations/DCIM/100APPLE/IMG_1/Adjustments/FullSizeRender.heic")
        == "Edited/DCIM/100APPLE/IMG_1/Adjustments/FullSizeRender.heic"
    )
    assert src.dest_path("DCIM/100APPLE/IMG_1.HEIC") == "DCIM/100APPLE/IMG_1.HEIC"


def test_iphone_device_name() -> None:
    assert device_name("iPhone15,4", "00008120-000A1B2C3D4E5F6A") == "iphone-iPhone15,4-5F6A"


def test_open_source_validation(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="--name is required"):
        open_source(f"dir:{tmp_path}")
    with pytest.raises(SourceError, match="unknown source"):
        open_source("android")
    with pytest.raises(SourceError, match="invalid device name"):
        open_source(f"dir:{tmp_path}", name="bad name")


def test_init_refuses_non_mountpoint_and_reinit(tmp_path: Path, capsys) -> None:
    assert cli.main(["init", str(tmp_path), "--label", "x"]) == EXIT_USAGE
    assert "not a mount point" in capsys.readouterr().err
    init_drive(tmp_path, "x", require_mountpoint=False)
    with pytest.raises(Exception, match="already initialised"):
        init_drive(tmp_path, "y", require_mountpoint=False)
    assert load_drive(tmp_path).label == "x"


def test_cli_backup_verify_status_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli, "block_device", lambda _m: None)  # no real SMART in tests
    src = tmp_path / "src"
    (src / "DCIM").mkdir(parents=True)
    (src / "DCIM" / "IMG_1.HEIC").write_bytes(b"photo")
    (src / "DCIM" / "IMG_2.MOV").write_bytes(b"video")
    mount = tmp_path / "drive"
    mount.mkdir()
    init_drive(mount, "itest", require_mountpoint=False)

    base = ["backup", "--from", f"dir:{src}", "--name", "cam", "--to", str(mount)]
    assert cli.main(base) == EXIT_GATE_FAILED  # unknown health is a FAIL by default
    assert cli.main([*base, "--allow-unknown-health"]) == EXIT_OK
    assert cli.main(["verify", str(mount)]) == EXIT_OK
    assert cli.main(["status", "--min-copies", "1"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    assert "SAFE TO DELETE" in out


def test_cli_turns_a_vanished_drive_into_a_clean_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import sqlite3

    mount = tmp_path / "drive"
    mount.mkdir()
    init_drive(mount, "gone", require_mountpoint=False)

    def explode(*_a: object, **_k: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cli, "run_verify", explode)
    assert cli.main(["verify", str(mount)]) == 1
    err = capsys.readouterr().err
    assert "drive I/O error" in err and "Traceback" not in err


def test_cli_reports_unknown_drive(capsys) -> None:
    assert cli.main(["verify", "no-such-label"]) == EXIT_USAGE
    assert "no attached drive" in capsys.readouterr().err
