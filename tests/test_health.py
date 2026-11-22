import copy
import random
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import HEALTHY_SMART, backup, fake_runner

from photo_backup.drive import Drive
from photo_backup.health import (
    GateOptions,
    evaluate_smart,
    kernel_log_check,
    read_history,
    run_gate,
    sample_check,
    smart_checks,
    space_checks,
    speed_check,
)
from photo_backup.model import Level


def smart_with(**attrs: int) -> dict:
    data = copy.deepcopy(HEALTHY_SMART)
    ids = {"reallocated": 5, "pending": 197, "uncorrectable": 198}
    for name, value in attrs.items():
        for row in data["ata_smart_attributes"]["table"]:
            if row["id"] == ids[name]:
                row["raw"]["value"] = value
    return data


def levels(checks: list) -> list[Level]:
    return [c.level for c in checks]


def test_healthy_smart_is_ok() -> None:
    checks, metrics = evaluate_smart(HEALTHY_SMART, None)
    assert levels(checks) == [Level.OK]
    assert metrics["smart"] == "PASSED"


def test_pending_sectors_fail() -> None:
    checks, _ = evaluate_smart(smart_with(pending=8), None)
    assert Level.FAIL in levels(checks)


def test_overall_failed_fails() -> None:
    data = copy.deepcopy(HEALTHY_SMART)
    data["smart_status"]["passed"] = False
    checks, _ = evaluate_smart(data, None)
    assert Level.FAIL in levels(checks)


def test_growing_reallocated_sectors_warn() -> None:
    checks, _ = evaluate_smart(smart_with(reallocated=12), prev_reallocated=4)
    assert levels(checks) == [Level.WARN]
    assert "grew 4 -> 12" in checks[0].detail


def test_hot_and_old_drive_warn() -> None:
    data = copy.deepcopy(HEALTHY_SMART)
    data["temperature"]["current"] = 58
    data["power_on_time"]["hours"] = 50_000
    checks, _ = evaluate_smart(data, None)
    assert levels(checks) == [Level.WARN, Level.WARN]


def _runner(responses: list[tuple[int, str, str]]):
    calls: list[list[str]] = []

    def runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        rc, out, err = responses.pop(0)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    return runner, calls


def test_usb_bridge_retries_with_sat() -> None:
    import json

    runner, calls = _runner(
        [
            (1, json.dumps({"smartctl": {"messages": ["Unknown USB bridge"]}}), ""),
            (0, json.dumps(HEALTHY_SMART), ""),
        ]
    )
    checks, _ = smart_checks("/dev/sdz", runner, Level.FAIL, None)
    assert levels(checks) == [Level.OK]
    assert calls[1][-3:] == ["-d", "sat", "/dev/sdz"]


@pytest.mark.parametrize(
    ("stderr", "text"),
    [
        ("sudo: a password is required", "no sudo rule"),
        ("sudo: smartctl: command not found", "not installed"),
    ],
)
def test_unreadable_smart_uses_unknown_level(stderr: str, text: str) -> None:
    runner, _ = _runner([(1, "", stderr)])
    checks, _ = smart_checks("/dev/sdz", runner, Level.WARN, None)
    assert levels(checks) == [Level.WARN]
    assert text in checks[0].detail


def test_kernel_blocked_pass_through_is_reported_once_without_retry() -> None:
    import json

    blocked = json.dumps(
        {
            "smartctl": {
                "messages": [
                    {
                        "string": "Read Device Identity failed: scsi error "
                        "unsupported field in scsi command"
                    }
                ]
            }
        }
    )
    runner, calls = _runner([(4, blocked, "")])
    checks, _ = smart_checks("/dev/sdz", runner, Level.WARN, None)
    assert len(calls) == 1  # no second probe with -d sat
    assert levels(checks) == [Level.WARN]
    assert "uas driver" in checks[0].detail


def test_kernel_log_ok_when_clean() -> None:
    journal = "sd 1:0:0:0: [sdz] Attached SCSI disk\nusb 3-2: new high-speed USB device"
    assert kernel_log_check("/dev/sdz", fake_runner(journal=journal)).level is Level.OK


def test_errors_before_a_reattach_still_warn() -> None:
    # Real sequence from a USB drive that dropped off mid-write and re-enumerated.
    journal = "\n".join(
        [
            "sd 0:0:0:0: [sdz] Attached SCSI disk",
            "sd 0:0:0:0: [sdz] tag#4 FAILED Result: hostbyte=DID_NO_CONNECT driverbyte=DRIVER_OK",
            "I/O error, dev sdz, sector 8460640 op 0x1:(WRITE) flags 0x4800",
            "usb 4-1: USB disconnect, device number 5",
            "device offline error, dev sdz, sector 7799512 op 0x1:(WRITE)",
            "sd 0:0:0:0: [sdz] Attached SCSI disk",
        ]
    )
    check = kernel_log_check("/dev/sdz", fake_runner(journal=journal))
    assert check.level is Level.WARN
    assert "before it re-attached" in check.detail


def test_errors_since_the_last_attach_fail() -> None:
    journal = "sd 1:0:0:0: [sdz] Attached SCSI disk\nBuffer I/O error on dev sdz1, logical block 7"
    assert kernel_log_check("/dev/sdz", fake_runner(journal=journal)).level is Level.FAIL


def test_space_check_requires_margin(drive: Drive) -> None:
    assert space_checks(drive, 10**18)[0].level is Level.FAIL
    assert space_checks(drive, 1)[0].level in (Level.OK, Level.WARN)


def test_spot_check_detects_damage(drive: Drive, phone) -> None:
    assert backup(phone, drive)[0] == 0
    with drive.open_catalog() as cat:
        check, _, _ = sample_check(drive, cat, 100, 10**12, random.Random(0))
        assert check.level is Level.OK
        victim = next(cat.files())
        path: Path = drive.root / victim.dest_path
        path.write_bytes(b"bit rot" + path.read_bytes()[7:])
        check, metrics, damaged = sample_check(drive, cat, 100, 10**12, random.Random(0))
    assert check.level is Level.FAIL
    assert metrics["sample_bad"] == 1
    assert [r.dest_path for r in damaged] == [victim.dest_path]


def test_speed_check_warns_when_much_slower() -> None:
    assert speed_check(None, []).level is Level.SKIP
    assert speed_check(10.0, [30.0]).level is Level.OK  # not enough history yet
    assert speed_check(10.0, [30.0, 31.0, 29.0]).level is Level.WARN
    assert speed_check(28.0, [30.0, 31.0, 29.0]).level is Level.OK


def test_gate_records_history_and_tracks_reallocation(drive: Drive) -> None:
    with drive.open_catalog() as cat:
        run_gate(
            drive,
            cat,
            disk="/dev/sdz",
            planned_bytes=1,
            opts=GateOptions(),
            runner=fake_runner(smart_with(reallocated=2)),
        )
        gate = run_gate(
            drive,
            cat,
            disk="/dev/sdz",
            planned_bytes=1,
            opts=GateOptions(),
            runner=fake_runner(smart_with(reallocated=9)),
        )
    assert any("grew 2 -> 9" in c.detail for c in gate.checks)
    history = read_history(drive)
    assert [h["reallocated"] for h in history] == ["2", "9"]
    assert [h["result"] for h in history] == ["WARN", "WARN"]


def test_unknown_health_fails_unless_allowed(drive: Drive) -> None:
    with drive.open_catalog() as cat:
        strict = run_gate(
            drive, cat, disk=None, planned_bytes=1, opts=GateOptions(), runner=fake_runner()
        )
        lenient = run_gate(
            drive,
            cat,
            disk=None,
            planned_bytes=1,
            opts=GateOptions(allow_unknown_health=True),
            runner=fake_runner(),
        )
    assert not strict.ok
    assert lenient.ok
    assert lenient.result == "WARN"


def test_force_unhealthy_overrides_but_reports(drive: Drive) -> None:
    with drive.open_catalog() as cat:
        gate = run_gate(
            drive,
            cat,
            disk="/dev/sdz",
            planned_bytes=1,
            opts=GateOptions(force_unhealthy=True),
            runner=fake_runner(smart_with(pending=3)),
        )
    assert gate.failed and gate.ok
    assert gate.result == "FORCED"
