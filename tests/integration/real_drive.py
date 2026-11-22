#!/usr/bin/env python3
"""Integration test against a REAL mounted drive.

Writes only inside ``<mount>/photo-backup-itest-<timestamp>/`` and removes it afterwards
(unless ``--keep``). Uses the drive's real filesystem, block device and health gate.

    python tests/integration/real_drive.py /media/$USER/MyDrive [--allow-unknown-health]

Steps (each must pass):
  1. build a synthetic camera roll (Live Photo pairs, videos, odd names, one big file)
  2. backup  -> PASS
  3. verify  -> PASS
  4. backup again -> copies 0 files
  5. `sha256sum -c SHA256SUMS` (independent of this tool)
  6. byte-for-byte compare every source file with its copy
  7. status (1 copy required) -> SAFE TO DELETE
  8. corruption drill: damage one copy -> verify FAIL and status NOT YET;
     restore -> verify PASS and status SAFE again
"""

from __future__ import annotations

import argparse
import csv
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from photo_backup.backup import BackupOptions, run_backup
from photo_backup.drive import block_device, init_drive
from photo_backup.health import GateOptions
from photo_backup.sources.directory import DirectorySource
from photo_backup.status import run_status
from photo_backup.verify import run_verify

DEVICE = "itest-camera"


def build_source(root: Path, big_mb: int) -> int:
    """Create a fake camera roll; returns the number of files."""
    files: dict[str, bytes] = {}
    for i in range(1, 21):
        files[f"DCIM/100APPLE/IMG_{i:04d}.HEIC"] = os.urandom(200_000 + i * 997)
        if i % 4 == 0:  # Live Photo pair
            files[f"DCIM/100APPLE/IMG_{i:04d}.MOV"] = os.urandom(900_000)
    for i in range(1, 6):
        files[f"DCIM/101APPLE/IMG_{500 + i}.MOV"] = os.urandom(3_000_000 + i)
    files["DCIM/101APPLE/IMG_0505.AAE"] = b"<plist>edit</plist>"
    files["DCIM/102APPLE/what?.jpg"] = os.urandom(1000)
    files["DCIM/102APPLE/colon:name.png"] = os.urandom(1000)
    files["DCIM/102APPLE/trailing."] = os.urandom(1000)
    files["DCIM/102APPLE/café ☕.heic"] = os.urandom(1000)
    files["DCIM/102APPLE/empty.jpg"] = b""
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    big = root / "DCIM/103APPLE/BIG_VIDEO.MOV"
    big.parent.mkdir(parents=True)
    with open(big, "wb") as fh:
        for _ in range(big_mb):
            fh.write(os.urandom(1024 * 1024))
    return len(files) + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mount", type=Path)
    ap.add_argument("--allow-unknown-health", action="store_true")
    ap.add_argument("--big-mb", type=int, default=300)
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder")
    args = ap.parse_args()

    if not os.path.ismount(args.mount):
        print(f"{args.mount} is not a mount point", file=sys.stderr)
        return 2

    results: list[tuple[str, bool]] = []

    def step(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(f"\n===> {'PASS' if ok else 'FAIL'}: {name}\n", flush=True)

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    scratch = args.mount / f"photo-backup-itest-{stamp}"
    with tempfile.TemporaryDirectory(prefix="photo-backup-itest-") as tmp:
        os.environ["PHOTO_BACKUP_STATE"] = str(Path(tmp) / "state")  # keep real state clean
        src_root = Path(tmp) / "camera"
        n = build_source(src_root, args.big_mb)
        print(f"synthetic source: {n} files incl. one {args.big_mb} MiB file")

        scratch.mkdir()
        try:
            drive = init_drive(scratch, "itest", require_mountpoint=False)
            disk = block_device(args.mount)
            gate = GateOptions(allow_unknown_health=args.allow_unknown_health)
            source = DirectorySource(src_root, DEVICE)

            t0 = time.monotonic()
            code = run_backup(source, drive, BackupOptions(gate=gate), disk=disk)
            step(f"backup ({time.monotonic() - t0:.0f}s)", code == 0)
            if code != 0:
                return 1

            step("verify", run_verify(drive) == 0)

            code = run_backup(source, drive, BackupOptions(gate=gate), disk=disk)
            month_dir = next(drive.device_dir(DEVICE).iterdir())
            with open(month_dir / "manifest.tsv", newline="") as fh:
                rows = list(csv.DictReader(fh, delimiter="\t"))
            step("re-run copies nothing", code == 0 and len(rows) == n)

            p = subprocess.run(
                ["sha256sum", "--quiet", "-c", "../SHA256SUMS"],
                cwd=month_dir / "files",
                capture_output=True,
                text=True,
                check=False,
            )
            print(p.stdout + p.stderr)
            step("independent sha256sum -c", p.returncode == 0)

            mismatched = [
                r["src_path"]
                for r in rows
                if not filecmp.cmp(
                    src_root / r["src_path"], month_dir / "files" / r["dest_path"], shallow=False
                )
            ]
            step(f"byte-for-byte compare of {len(rows)} files", not mismatched)

            def status_text() -> str:
                lines: list[str] = []
                run_status(device=DEVICE, min_copies=1, out=lines.append)
                return "\n".join(lines)

            step("status says SAFE TO DELETE", "SAFE TO DELETE" in status_text())

            victim = month_dir / "files" / rows[0]["dest_path"]
            original = victim.read_bytes()
            victim.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
            detected = run_verify(drive) == 1
            blocked = "NOT YET" in status_text()
            victim.write_bytes(original)
            # The damaged copy stays NOT YET until verified good again.
            recovered = run_verify(drive) == 0 and "SAFE TO DELETE" in status_text()
            step(
                "corruption drill (verify FAIL + status NOT YET, then recovers)",
                detected and blocked and recovered,
            )
        finally:
            if args.keep:
                print(f"kept scratch folder: {scratch}")
            else:
                if not os.path.ismount(args.mount):
                    # The drive vanished (e.g. dropped off USB): "not exists" would be a lie.
                    print(f"!! drive no longer mounted; scratch folder NOT removed: {scratch}")
                else:
                    shutil.rmtree(scratch, ignore_errors=True)
                    state = "still exists!" if scratch.exists() else "removed"
                    print(f"scratch folder {state}: {scratch}")

    print("\nINTEGRATION SUMMARY")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
