"""Command-line interface."""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from photo_backup import __version__
from photo_backup.backup import BackupOptions, run_backup
from photo_backup.drive import (
    DriveError,
    block_device,
    candidate_mounts,
    init_drive,
    load_drive,
    resolve_drive,
)
from photo_backup.fsutil import fs_type
from photo_backup.health import GateOptions, read_history, run_gate
from photo_backup.report import EXIT_GATE_FAILED, EXIT_OK, EXIT_USAGE, human_bytes, table
from photo_backup.sources import SourceError, open_source
from photo_backup.status import run_status
from photo_backup.verify import run_verify


def _add_gate_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--allow-unknown-health",
        action="store_true",
        help="continue (with a warning) when SMART data cannot be read",
    )
    p.add_argument(
        "--force-unhealthy",
        action="store_true",
        help="write even if the health gate FAILS (recorded in the run summary)",
    )
    p.add_argument(
        "--sample-pct",
        type=float,
        default=1.0,
        help="percent of existing backup files to re-hash before writing (default 1)",
    )


def _gate_opts(args: argparse.Namespace) -> GateOptions:
    return GateOptions(
        allow_unknown_health=args.allow_unknown_health,
        force_unhealthy=args.force_unhealthy,
        sample_pct=args.sample_pct,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo-backup",
        description="Verified, append-only backups of phone photos to external drives.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="mark a mounted drive as a backup drive")
    p.add_argument("mount", type=Path)
    p.add_argument("--label", required=True, help="short name, e.g. goflex-500")

    sub.add_parser("drives", help="list attached drives")

    p = sub.add_parser("health", help="run the health & sanity gate only")
    p.add_argument("drive", help="mount path or label")
    _add_gate_flags(p)

    p = sub.add_parser("backup", help="copy new files from a source to a drive")
    p.add_argument("--from", dest="source", required=True, help="iphone | dir:<path>")
    p.add_argument("--to", dest="drive", required=True, help="mount path or label")
    p.add_argument("--name", help="device name for dir: sources")
    p.add_argument("--udid", help="pick one iPhone when several are connected")
    p.add_argument("--skip-trash", action="store_true", help="dir: skip .Trashes folders")
    p.add_argument("--dry-run", action="store_true", help="plan and gate only, copy nothing")
    p.add_argument("--limit", type=int, help="copy at most N new files (smoke tests)")
    _add_gate_flags(p)

    p = sub.add_parser("verify", help="re-hash backed-up files against the catalog")
    p.add_argument("drive", help="mount path or label")
    p.add_argument("--device")
    p.add_argument("--month", help="YYYY-MM")
    p.add_argument("--sample-pct", type=float, help="verify a random percentage only")

    p = sub.add_parser("status", help="what is safe to delete from the phone")
    p.add_argument("--device")
    p.add_argument("--min-copies", type=int, default=2)
    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    drive = init_drive(args.mount, args.label)
    print(f"initialised {drive.label} ({drive.uuid}) at {drive.root}")
    return EXIT_OK


def _cmd_drives(_: argparse.Namespace) -> int:
    rows = []
    for mount in candidate_mounts():
        drive = load_drive(mount)
        usage = shutil.disk_usage(mount)
        last = ""
        if drive and drive.catalog_path.is_file():
            with drive.open_catalog() as cat:
                runs = cat.runs()
                if runs:
                    last = f"{runs[-1].run_id} {runs[-1].result or 'unfinished'}"
        rows.append(
            [
                str(mount),
                drive.label if drive else "(not initialised)",
                fs_type(mount) or "?",
                human_bytes(usage.free),
                human_bytes(usage.total),
                last,
            ]
        )
    if not rows:
        print("no mounted drives found under /media, /run/media or /mnt")
        return EXIT_OK
    for line in table(["mount", "label", "fs", "free", "size", "last run"], rows):
        print(line)
    return EXIT_OK


def _cmd_health(args: argparse.Namespace) -> int:
    drive = resolve_drive(args.drive)
    disk = block_device(drive.mount)
    with drive.lock(), drive.open_catalog() as catalog:
        gate = run_gate(drive, catalog, disk=disk, planned_bytes=None, opts=_gate_opts(args))
    print(f"{drive.label}  {drive.mount}  disk={disk}")
    for check in gate.checks:
        print(f"  {check}")
    print(f"RESULT: {gate.result}")
    history = read_history(drive)[-5:]
    if history:
        print("\nrecent history:")
        cols = ["timestamp", "smart", "reallocated", "pending", "temp_c", "read_mb_s", "result"]
        for line in table(cols, [[h.get(c, "") for c in cols] for h in history]):
            print("  " + line)
    return EXIT_OK if gate.ok else EXIT_GATE_FAILED


def _cmd_backup(args: argparse.Namespace) -> int:
    drive = resolve_drive(args.drive)
    opts = BackupOptions(dry_run=args.dry_run, limit=args.limit, gate=_gate_opts(args))
    source = open_source(args.source, name=args.name, udid=args.udid, skip_trash=args.skip_trash)
    with source:
        return run_backup(source, drive, opts, disk=block_device(drive.mount))


def _cmd_verify(args: argparse.Namespace) -> int:
    drive = resolve_drive(args.drive)
    return run_verify(drive, device=args.device, month=args.month, sample_pct=args.sample_pct)


def _cmd_status(args: argparse.Namespace) -> int:
    return run_status(device=args.device, min_copies=args.min_copies)


_COMMANDS = {
    "init": _cmd_init,
    "drives": _cmd_drives,
    "health": _cmd_health,
    "backup": _cmd_backup,
    "verify": _cmd_verify,
    "status": _cmd_status,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except (DriveError, SourceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\ninterrupted: verified files are kept; re-run to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
