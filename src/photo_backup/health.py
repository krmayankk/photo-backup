"""Health & sanity gate run before every write to a drive.

Each check returns a :class:`Check`. Any FAIL blocks writing unless the user forces it.
Results are appended to ``health.tsv`` on the drive so trends (growing reallocated
sectors, falling read speed) are visible across years.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import random
import re
import shutil
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from photo_backup.catalog import Catalog, FileRow
from photo_backup.drive import Drive
from photo_backup.fsutil import hash_file, hash_prefix
from photo_backup.model import Check, Level
from photo_backup.proc import Runner, run

GIB = 1024**3
MIB = 1024**2

TEMP_WARN_C = 50
POWER_ON_WARN_H = 40_000
SPEED_WARN_RATIO = 0.7
SPEED_MIN_SAMPLE_BYTES = 64 * MIB
FREE_MARGIN = 1.1

SMARTCTL_HINT = "install smartmontools and allow passwordless smartctl (see README, 'Drive health')"

_KERNEL_ERROR_RE = re.compile(
    r"I/O error|Medium Error|Unrecovered read error|critical medium error|Buffer I/O error",
    re.IGNORECASE,
)

HEALTH_FIELDS = [
    "timestamp",
    "label",
    "disk",
    "smart",
    "reallocated",
    "pending",
    "uncorrectable",
    "temp_c",
    "power_on_h",
    "read_mb_s",
    "sample_files",
    "sample_bad",
    "result",
]


@dataclass
class GateOptions:
    allow_unknown_health: bool = False
    force_unhealthy: bool = False
    sample_pct: float = 1.0
    sample_cap_bytes: int = 2 * GIB
    record: bool = True
    seed: int | None = None


@dataclass
class GateResult:
    checks: list[Check]
    metrics: dict[str, Any] = field(default_factory=dict)
    forced: bool = False

    @property
    def failed(self) -> bool:
        return any(c.level is Level.FAIL for c in self.checks)

    @property
    def ok(self) -> bool:
        return not self.failed or self.forced

    @property
    def result(self) -> str:
        if self.failed:
            return "FORCED" if self.forced else "FAIL"
        return "WARN" if any(c.level is Level.WARN for c in self.checks) else "OK"


# -- SMART ---------------------------------------------------------------------------


def _raw(attrs: dict[int, dict[str, Any]], attr_id: int) -> int | None:
    attr = attrs.get(attr_id)
    return int(attr["raw"]["value"]) if attr else None


def evaluate_smart(
    data: dict[str, Any], prev_reallocated: int | None
) -> tuple[list[Check], dict[str, Any]]:
    """Turn ``smartctl -j -a`` output into checks. Pure function (unit-tested)."""
    attrs = {a["id"]: a for a in data.get("ata_smart_attributes", {}).get("table", [])}
    nvme = data.get("nvme_smart_health_information_log", {})
    passed = data.get("smart_status", {}).get("passed")
    metrics: dict[str, Any] = {
        "smart": {True: "PASSED", False: "FAILED"}.get(passed, "unknown"),
        "reallocated": _raw(attrs, 5),
        "pending": _raw(attrs, 197),
        "uncorrectable": _raw(attrs, 198),
        "temp_c": data.get("temperature", {}).get("current"),
        "power_on_h": data.get("power_on_time", {}).get("hours"),
    }
    checks: list[Check] = []

    def fail(detail: str) -> None:
        checks.append(Check("smart", Level.FAIL, detail))

    def warn(detail: str) -> None:
        checks.append(Check("smart", Level.WARN, detail))

    if passed is False:
        fail("drive reports overall SMART health FAILED: replace it")
    if (metrics["pending"] or 0) > 0:
        fail(f"{metrics['pending']} pending sectors (unreadable areas): drive is failing")
    if (metrics["uncorrectable"] or 0) > 0:
        fail(f"{metrics['uncorrectable']} offline-uncorrectable sectors: drive is failing")
    if nvme.get("media_errors", 0) > 0 or nvme.get("critical_warning", 0) != 0:
        fail(f"NVMe media errors / critical warning: {nvme}")

    realloc = metrics["reallocated"]
    if realloc:
        if prev_reallocated is not None and realloc > prev_reallocated:
            warn(f"reallocated sectors grew {prev_reallocated} -> {realloc}: watch this drive")
        else:
            warn(f"{realloc} reallocated sectors (bad sectors remapped)")
    if metrics["temp_c"] is not None and metrics["temp_c"] > TEMP_WARN_C:
        warn(f"temperature {metrics['temp_c']} C (> {TEMP_WARN_C} C)")
    if metrics["power_on_h"] is not None and metrics["power_on_h"] > POWER_ON_WARN_H:
        warn(f"{metrics['power_on_h']} power-on hours: old drive, plan a replacement")

    if not checks:
        checks.append(
            Check(
                "smart",
                Level.OK,
                f"passed; reallocated={realloc} pending={metrics['pending']} "
                f"temp={metrics['temp_c']}C power_on={metrics['power_on_h']}h",
            )
        )
    return checks, metrics


def smart_checks(
    disk: str | None, runner: Runner, unknown: Level, prev_reallocated: int | None
) -> tuple[list[Check], dict[str, Any]]:
    if disk is None:
        return [Check("smart", unknown, "could not find the drive's block device")], {}
    # USB enclosures often need SAT pass-through; try plain first, then -d sat.
    for extra in ([], ["-d", "sat"]):
        p = runner(["sudo", "-n", "smartctl", "-j", "-a", *extra, disk])
        if "command not found" in p.stderr:
            return [Check("smart", unknown, f"smartctl not installed; {SMARTCTL_HINT}")], {}
        if "password is required" in p.stderr or "terminal is required" in p.stderr:
            return [Check("smart", unknown, f"no sudo rule for smartctl; {SMARTCTL_HINT}")], {}
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            continue
        if "smart_status" in data or "ata_smart_attributes" in data:
            return evaluate_smart(data, prev_reallocated)
    return [Check("smart", unknown, "SMART not readable through this USB enclosure")], {}


# -- other checks ------------------------------------------------------------------


def kernel_log_check(disk: str | None, runner: Runner) -> Check:
    """Look for I/O errors logged for this disk since it was last attached."""
    if disk is None:
        return Check("kernel-log", Level.SKIP, "no block device")
    p = runner(["journalctl", "-k", "-b", "--no-pager", "-o", "cat"])
    if p.returncode != 0:
        return Check("kernel-log", Level.SKIP, "journalctl not readable")
    name = Path(disk).name
    lines = p.stdout.splitlines()
    attached = [i for i, line in enumerate(lines) if f"[{name}] Attached" in line]
    since = lines[attached[-1] :] if attached else lines
    errors = [ln for ln in since if name in ln and _KERNEL_ERROR_RE.search(ln)]
    if errors:
        return Check("kernel-log", Level.FAIL, f"{len(errors)} I/O errors, last: {errors[-1]}")
    return Check("kernel-log", Level.OK, f"no I/O errors for {name} since attach")


def writable_check(drive: Drive) -> Check:
    """Write, fsync, read back and delete a 1 MiB probe."""
    probe = drive.meta / f".probe-{os.getpid()}"
    payload = os.urandom(MIB)
    try:
        drive.meta.mkdir(parents=True, exist_ok=True)
        with open(probe, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if probe.read_bytes() != payload:
            return Check("writable", Level.FAIL, "probe file read back differently")
    except OSError as exc:
        hint = " (NTFS marked dirty? run chkdsk /f on Windows)" if exc.errno == 30 else ""
        return Check("writable", Level.FAIL, f"cannot write to drive: {exc}{hint}")
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return Check("writable", Level.OK, "write/read-back probe succeeded")


def space_checks(drive: Drive, planned_bytes: int | None) -> list[Check]:
    usage = shutil.disk_usage(drive.mount)
    free_gb = usage.free / GIB
    if planned_bytes is None:
        return [Check("free-space", Level.OK, f"{free_gb:.1f} GiB free")]
    need = planned_bytes * FREE_MARGIN
    if usage.free < need:
        return [
            Check(
                "free-space",
                Level.FAIL,
                f"need {need / GIB:.1f} GiB (incl. 10% margin), only {free_gb:.1f} GiB free",
            )
        ]
    after = (usage.free - planned_bytes) / usage.total
    level = Level.WARN if after < 0.10 else Level.OK
    return [
        Check(
            "free-space",
            level,
            f"{free_gb:.1f} GiB free, {planned_bytes / GIB:.1f} GiB planned, "
            f"{after:.0%} free after",
        )
    ]


def sample_check(
    drive: Drive, catalog: Catalog, pct: float, cap_bytes: int, rng: random.Random
) -> tuple[Check, dict[str, Any], list[FileRow]]:
    """Re-hash a random sample of good backed-up files; also measures read speed.

    Returns the check, metrics, and the rows found damaged or missing.
    """
    rows = [r for r in catalog.files() if r.is_good]
    if not rows:
        return Check("spot-check", Level.SKIP, "nothing backed up yet"), {}, []
    rng.shuffle(rows)
    want = max(1, round(len(rows) * pct / 100))
    sample, total = [], 0
    for row in rows:
        if len(sample) >= want or total >= cap_bytes:
            break
        sample.append(row)
        total += row.size

    bad: list[FileRow] = []
    start = time.monotonic()
    for row in sample:
        try:
            digest, _ = hash_file(drive.root / row.dest_path)
        except OSError:
            bad.append(row)
            continue
        if digest != row.sha256:
            bad.append(row)
    elapsed = time.monotonic() - start
    metrics: dict[str, Any] = {"sample_files": len(sample), "sample_bad": len(bad)}
    if total >= SPEED_MIN_SAMPLE_BYTES and elapsed > 0:
        metrics["read_mb_s"] = round(total / MIB / elapsed, 1)
    if bad:
        names = [r.dest_path for r in bad[:3]]
        detail = f"{len(bad)}/{len(sample)} files damaged or missing: {names}; run verify"
        return Check("spot-check", Level.FAIL, detail), metrics, bad
    detail = f"{len(sample)} files ({total / MIB:.0f} MiB) re-hashed ok"
    return Check("spot-check", Level.OK, detail), metrics, []


def speed_check(speed: float | None, history: list[float]) -> Check:
    if speed is None:
        return Check("read-speed", Level.SKIP, "sample too small to measure")
    if len(history) < 3:
        return Check("read-speed", Level.OK, f"{speed} MB/s (building history)")
    median = statistics.median(history)
    if speed < median * SPEED_WARN_RATIO:
        return Check(
            "read-speed", Level.WARN, f"{speed} MB/s vs usual {median:.1f} MB/s: drive slowing?"
        )
    return Check("read-speed", Level.OK, f"{speed} MB/s (usual {median:.1f})")


def records_check(drive: Drive, catalog: Catalog) -> Check:
    """manifest.tsv / SHA256SUMS must be append-only: each must still start with exactly
    the bytes recorded at the end of the last run that wrote it."""
    records = catalog.records()
    if not records:
        return Check("records", Level.SKIP, "no record files yet")
    broken = []
    for rel, size, sha in records:
        path = drive.root / rel
        if not path.is_file():
            broken.append(f"{rel} missing")
        elif path.stat().st_size < size:
            broken.append(f"{rel} shrank")
        elif hash_prefix(path, size) != sha:
            broken.append(f"{rel} modified in place")
    if broken:
        return Check("records", Level.FAIL, "append-only records changed: " + "; ".join(broken))
    return Check("records", Level.OK, f"{len(records)} manifest/checksum files intact")


def catalog_check(catalog: Catalog) -> Check:
    result = catalog.integrity()
    if result != "ok":
        return Check("catalog", Level.FAIL, f"SQLite integrity_check: {result}")
    return Check("catalog", Level.OK, f"{catalog.count()} files indexed, integrity ok")


# -- history -----------------------------------------------------------------------


def read_history(drive: Drive) -> list[dict[str, str]]:
    if not drive.health_path.is_file():
        return []
    with open(drive.health_path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _append_history(drive: Drive, disk: str | None, metrics: dict[str, Any], result: str) -> None:
    new = not drive.health_path.is_file()
    row = {k: metrics.get(k, "") for k in HEALTH_FIELDS}
    row.update(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        label=drive.label,
        disk=disk or "",
        result=result,
    )
    with open(drive.health_path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEALTH_FIELDS, delimiter="\t", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)


def _last_int(history: list[dict[str, str]], key: str) -> int | None:
    for row in reversed(history):
        if row.get(key, "").strip():
            return int(row[key])
    return None


# -- entry point -------------------------------------------------------------------


def run_gate(
    drive: Drive,
    catalog: Catalog,
    *,
    disk: str | None,
    planned_bytes: int | None,
    opts: GateOptions,
    runner: Runner = run,
) -> GateResult:
    history = read_history(drive)
    unknown = Level.WARN if opts.allow_unknown_health else Level.FAIL

    checks, metrics = smart_checks(disk, runner, unknown, _last_int(history, "reallocated"))
    checks.append(kernel_log_check(disk, runner))
    checks.append(writable_check(drive))
    checks.extend(space_checks(drive, planned_bytes))
    sample, sample_metrics, damaged = sample_check(
        drive, catalog, opts.sample_pct, opts.sample_cap_bytes, random.Random(opts.seed)
    )
    if damaged and opts.record:
        catalog.mark_damaged(damaged)  # status must stop counting these copies
    checks.append(sample)
    metrics.update(sample_metrics)
    speeds = [float(r["read_mb_s"]) for r in history if r.get("read_mb_s", "").strip()]
    checks.append(speed_check(metrics.get("read_mb_s"), speeds))
    checks.append(catalog_check(catalog))
    checks.append(records_check(drive, catalog))

    result = GateResult(checks, metrics, forced=opts.force_unhealthy)
    if opts.record:
        _append_history(drive, disk, metrics, result.result)
    return result
