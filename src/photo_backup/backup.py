"""The ``backup`` command: gate -> plan -> verified copy -> reconcile -> records.

Write ordering per file is chosen so that a crash at any point is recoverable by simply
running again:

1. ``copy_verified`` lands the file atomically (``.partial`` -> rename).
2. A line is appended to ``manifest.tsv`` and ``SHA256SUMS`` (flushed).
3. The catalog row is committed.

A crash after (1) but before (3) leaves a file the catalog does not know about. The next
run copies the file again to a ``.conflict-N`` name, sees identical bytes, removes the
extra copy and records the original as a ``duplicate``. Nothing is lost or doubled.
"""

from __future__ import annotations

import csv
import json
import platform
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TextIO

from photo_backup import __version__
from photo_backup.catalog import Catalog, FileRow
from photo_backup.classify import classify
from photo_backup.copier import CopyError, copy_verified, remove_stale_partials
from photo_backup.drive import Drive
from photo_backup.fsutil import free_name, fs_type, fsync_dir, hash_file, max_file_size
from photo_backup.health import GateOptions, run_gate
from photo_backup.model import Kind, SourceFile
from photo_backup.proc import Runner, run
from photo_backup.report import (
    EXIT_GATE_FAILED,
    EXIT_OK,
    EXIT_VERIFY_FAILED,
    human_bytes,
    table,
)
from photo_backup.sources.base import Source, SourceError
from photo_backup.state import mirror_catalog, save_listing

MANIFEST_FIELDS = ["src_path", "dest_path", "size", "mtime", "sha256", "kind", "status", "run_id"]
PROGRESS_EVERY_FILES = 100
PROGRESS_EVERY_BYTES = 1024**3

Out = Callable[[str], None]


@dataclass
class BackupOptions:
    dry_run: bool = False
    limit: int | None = None
    gate: GateOptions = field(default_factory=GateOptions)
    now: datetime | None = None  # injectable clock for tests


@dataclass
class KindStats:
    listed: int = 0
    skipped: int = 0
    deferred: int = 0
    copied: int = 0
    conflicts: int = 0
    duplicates: int = 0
    failed: int = 0
    bytes_listed: int = 0
    bytes_copied: int = 0

    @property
    def accounted(self) -> int:
        return (
            self.skipped
            + self.deferred
            + self.copied
            + self.conflicts
            + self.duplicates
            + self.failed
        )


class RunLog:
    """Prints lines and, once a run folder exists, also appends them to ``run.log``."""

    def __init__(self, out: Out) -> None:
        self._out = out
        self._buffer: list[str] = []
        self._fh: TextIO | None = None

    def __call__(self, line: str = "") -> None:
        self._out(line)
        stamped = f"{datetime.now().isoformat(timespec='seconds')}  {line}"
        if self._fh:
            self._fh.write(stamped + "\n")
            self._fh.flush()
        else:
            self._buffer.append(stamped)

    def attach(self, path: Path) -> None:
        self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 - held open for the whole run, closed in close()
        self._fh.write("\n".join(self._buffer) + "\n")
        self._fh.flush()
        self._buffer.clear()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


@dataclass
class _Outcome:
    status: str  # copied | conflict | duplicate
    dest: Path
    sha256: str
    size: int


def _copy_one(source: Source, f: SourceFile, files_dir: Path, max_bytes: int | None) -> _Outcome:
    if max_bytes is not None and f.size > max_bytes:
        raise CopyError(f"{human_bytes(f.size)} exceeds this filesystem's file size limit")
    target = files_dir / source.dest_path(f.path)
    dest = free_name(target.parent, target.name)
    with source.open(f.path) as fh:
        sha, size = copy_verified(fh, dest, expected_size=f.size, mtime=f.mtime)
    if dest == target:
        return _Outcome("copied", dest, sha, size)
    if target.is_file() and hash_file(target)[0] == sha:
        dest.unlink()  # identical bytes already there (resumed run): keep the original
        fsync_dir(dest.parent)
        return _Outcome("duplicate", target, sha, size)
    return _Outcome("conflict", dest, sha, size)


class _Records:
    """Append-only manifest.tsv + SHA256SUMS for one month folder."""

    def __init__(self, run_dir: Path) -> None:
        manifest = run_dir / "manifest.tsv"
        new = not manifest.exists()
        self._manifest = open(manifest, "a", newline="", encoding="utf-8")  # noqa: SIM115 - held open for the whole run, closed in close()
        self._writer = csv.writer(self._manifest, delimiter="\t", lineterminator="\n")
        if new:
            self._writer.writerow(MANIFEST_FIELDS)
        self._sums = open(run_dir / "SHA256SUMS", "a", encoding="utf-8")  # noqa: SIM115 - held open for the whole run, closed in close()
        self._files_dir = run_dir / "files"

    def add(self, f: SourceFile, o: _Outcome, kind: Kind, run_id: str) -> None:
        rel = o.dest.relative_to(self._files_dir).as_posix()
        self._writer.writerow(
            [f.path, rel, o.size, f.mtime, o.sha256, kind.value, o.status, run_id]
        )
        self._sums.write(f"{o.sha256}  {rel}\n")
        self._manifest.flush()
        self._sums.flush()

    def close(self) -> None:
        self._manifest.close()
        self._sums.close()


def _plan_lines(stats: dict[Kind, KindStats]) -> list[str]:
    rows = []
    for kind in Kind:
        s = stats.get(kind)
        if s and s.listed:
            todo = s.listed - s.skipped - s.deferred
            rows.append([kind.value, s.listed, s.skipped, todo, s.deferred])
    total = [sum(r[i] for r in rows) for i in range(1, 5)]
    rows.append(["total", *total])
    return table(["kind", "on source", "already here", "to copy", "deferred"], rows)


def run_backup(
    source: Source,
    drive: Drive,
    opts: BackupOptions,
    *,
    disk: str | None,
    runner: Runner = run,
    out: Out = print,
) -> int:
    now = opts.now or datetime.now()
    month = now.strftime("%Y-%m")
    log = RunLog(out)
    try:
        with drive.lock(), drive.open_catalog() as catalog:
            run_id = catalog.unique_run_id(now.strftime("%Y%m%dT%H%M%S"))
            return _run(source, drive, catalog, opts, disk, runner, log, now, month, run_id)
    finally:
        log.close()


def _run(
    source: Source,
    drive: Drive,
    catalog: Catalog,
    opts: BackupOptions,
    disk: str | None,
    runner: Runner,
    log: RunLog,
    now: datetime,
    month: str,
    run_id: str,
) -> int:
    fstype = fs_type(drive.mount)
    log(f"photo-backup {__version__}  run {run_id}")
    log(f"source: {source.name}  {source.info}")
    log(f"drive:  {drive.label}  {drive.mount}  fs={fstype}  disk={disk}")

    # 1. list + plan -------------------------------------------------------------------
    files = source.list_files()
    if source.local_root is not None and drive.root.is_relative_to(source.local_root):
        inside = drive.root.relative_to(source.local_root).as_posix() + "/"
        files = [f for f in files if not f.path.startswith(inside)]
    kinds = classify(f.path for f in files)
    stats: dict[Kind, KindStats] = defaultdict(KindStats)
    plan: list[SourceFile] = []
    for f in files:
        s = stats[kinds[f.path]]
        s.listed += 1
        s.bytes_listed += f.size
        if catalog.get(source.name, f):
            s.skipped += 1
        else:
            plan.append(f)
    if opts.limit is not None:
        for f in plan[opts.limit :]:
            stats[kinds[f.path]].deferred += 1
        plan = plan[: opts.limit]
    planned_bytes = sum(f.size for f in plan)
    log()
    for line in _plan_lines(stats):
        log(line)
    log(f"to copy: {len(plan)} files, {human_bytes(planned_bytes)}")

    # 2. gate --------------------------------------------------------------------------
    gate_opts = replace(opts.gate, record=opts.gate.record and not opts.dry_run)
    gate = run_gate(
        drive, catalog, disk=disk, planned_bytes=planned_bytes, opts=gate_opts, runner=runner
    )
    log()
    log("health & sanity gate:")
    for check in gate.checks:
        log(f"  {check}")
    if not gate.ok:
        log("GATE FAILED: nothing was written. Fix the FAIL items above and re-run.")
        return EXIT_GATE_FAILED
    if gate.forced and gate.failed:
        log("!! gate failures overridden by --force-unhealthy")

    save_listing(source.name, files, kinds)
    if opts.dry_run:
        log("dry run: nothing copied.")
        return EXIT_OK

    # 3. copy --------------------------------------------------------------------------
    run_dir = drive.device_dir(source.name) / month
    files_dir = run_dir / "files"
    logs_dir = run_dir / "runs" / run_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    log.attach(logs_dir / "run.log")
    stale = remove_stale_partials(files_dir)
    if stale:
        log(f"removed {stale} .partial leftovers from an interrupted run")

    max_bytes = max_file_size(fstype)
    started = now.isoformat(timespec="seconds")
    catalog.start_run(run_id, source.name, month, started)
    records = _Records(run_dir)
    landed: list[tuple[Path, int]] = []
    log()
    log(f"copying into {run_dir}")
    t0, done_bytes, last_report = time.monotonic(), 0, 0
    try:
        for i, f in enumerate(plan, 1):
            kind = kinds[f.path]
            s = stats[kind]
            try:
                outcome = _copy_one(source, f, files_dir, max_bytes)
            except (CopyError, SourceError, OSError) as exc:
                s.failed += 1
                log(f"  FAILED {f.path}: {exc}")
                continue
            records.add(f, outcome, kind, run_id)
            catalog.add(
                FileRow(
                    device=source.name,
                    src_path=f.path,
                    size=f.size,
                    mtime=f.mtime,
                    sha256=outcome.sha256,
                    kind=kind.value,
                    dest_path=outcome.dest.relative_to(drive.root).as_posix(),
                    month=month,
                    run_id=run_id,
                    copied_at=datetime.now().isoformat(timespec="seconds"),
                    verified_at=datetime.now().isoformat(timespec="seconds"),
                )
            )
            landed.append((outcome.dest, outcome.size))
            if outcome.status == "copied":
                s.copied += 1
                s.bytes_copied += outcome.size
            elif outcome.status == "conflict":
                s.conflicts += 1
                s.bytes_copied += outcome.size
                log(f"  conflict: {f.path} -> {outcome.dest.name} (different bytes, kept both)")
            else:
                s.duplicates += 1
            done_bytes += f.size
            if i % PROGRESS_EVERY_FILES == 0 or done_bytes - last_report >= PROGRESS_EVERY_BYTES:
                last_report = done_bytes
                log(_progress(i, len(plan), done_bytes, planned_bytes, time.monotonic() - t0))
    except KeyboardInterrupt:
        catalog.finish_run(run_id, datetime.now().isoformat(timespec="seconds"), "INTERRUPTED")
        log("interrupted: everything copied so far is verified and recorded; re-run to resume")
        raise
    finally:
        records.close()

    # 4. reconcile ---------------------------------------------------------------------
    problems = _reconcile(stats, landed)
    failed = sum(s.failed for s in stats.values())
    result = "PASS" if not problems and failed == 0 else "FAIL"
    finished = datetime.now().isoformat(timespec="seconds")
    catalog.finish_run(run_id, finished, result)

    summary = {
        "tool": f"photo-backup {__version__}",
        "host": platform.node(),
        "run_id": run_id,
        "month": month,
        "started": started,
        "finished": finished,
        "result": result,
        "source": {"name": source.name, **source.info},
        "drive": {"label": drive.label, "uuid": drive.uuid, "fs": fstype, "disk": disk},
        "gate": {"result": gate.result, "checks": [str(c) for c in gate.checks], **gate.metrics},
        "counts": {k.value: asdict(v) for k, v in stats.items()},
        "problems": problems,
    }
    (logs_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    catalog.snapshot_to(logs_dir / "catalog-snapshot.sqlite")
    mirror_catalog(drive, catalog)

    log()
    log(_result_table(stats))
    for p in problems:
        log(f"  PROBLEM: {p}")
    deferred = sum(s.deferred for s in stats.values())
    tail = f" ({deferred} files deferred by --limit; run again to continue)" if deferred else ""
    log(f"RESULT: {result}{tail}  ->  {logs_dir}")
    return EXIT_OK if result == "PASS" else EXIT_VERIFY_FAILED


def _reconcile(stats: dict[Kind, KindStats], landed: list[tuple[Path, int]]) -> list[str]:
    """Counts must add up per kind, and every landed file must be on disk at full size."""
    problems = [
        f"{kind.value}: listed {s.listed} but accounted for {s.accounted}"
        for kind, s in stats.items()
        if s.accounted != s.listed
    ]
    for path, size in landed:
        if not path.is_file():
            problems.append(f"missing after copy: {path}")
        elif path.stat().st_size != size:
            problems.append(f"size changed after copy: {path}")
    return problems


def _result_table(stats: dict[Kind, KindStats]) -> str:
    rows = [
        [
            k.value,
            s.listed,
            s.skipped,
            s.copied,
            s.conflicts,
            s.duplicates,
            s.failed,
            human_bytes(s.bytes_copied),
        ]
        for k, s in stats.items()
        if s.listed
    ]
    headers = ["kind", "listed", "skipped", "copied", "conflict", "dup", "failed", "written"]
    return "\n".join(table(headers, rows))


def _progress(i: int, n: int, done: int, total: int, elapsed: float) -> str:
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate else 0
    return (
        f"  [{i:>6}/{n}]  {human_bytes(done)} / {human_bytes(total)}  "
        f"{rate / 1024**2:.1f} MiB/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s"
    )
