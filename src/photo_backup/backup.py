"""The ``backup`` command: list -> plan -> gate -> verified copy -> reconcile -> records.

Write ordering per file is chosen so that a crash at any point is recoverable by simply
running again:

1. ``copy_verified`` lands the file atomically (``.pbtmp-<name>`` -> rename).
2. A line is appended to ``manifest.tsv`` and ``SHA256SUMS`` and fsynced.
3. The catalog row is committed.

A crash after (1) but before (3) leaves a file the catalog does not know about. The next
run copies the file again to a ``.conflict-N`` name, sees identical bytes, removes the
extra copy and records the original as a ``duplicate``. Nothing is lost; at worst the
manifest and SHA256SUMS hold one harmless duplicate line.
"""

from __future__ import annotations

import json
import platform
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from photo_backup import __version__
from photo_backup.catalog import Catalog, FileRow
from photo_backup.classify import classify
from photo_backup.copier import CopyError, copy_verified, remove_stale_partials
from photo_backup.drive import Drive
from photo_backup.fsutil import (
    NameIndex,
    fs_type,
    fsync_dir,
    hash_file,
    max_file_size,
    sanitize_relpath,
)
from photo_backup.health import GateOptions, run_gate
from photo_backup.model import Kind, SourceFile
from photo_backup.proc import Runner, run
from photo_backup.progress import CountingReader, LiveBar, Progress, StallWatchdog
from photo_backup.records import RunLog, RunRecords
from photo_backup.report import EXIT_GATE_FAILED, EXIT_OK, EXIT_VERIFY_FAILED, human_bytes, table
from photo_backup.sources.base import Source, SourceError
from photo_backup.state import mirror_catalog, save_listing

PROGRESS_EVERY_FILES = 100
PROGRESS_EVERY_BYTES = 1024**3
RETRY_DELAYS = (2.0, 5.0)  # seconds before the 2nd and 3rd attempt of a file
MAX_CONSECUTIVE_FAILURES = 10  # then assume the source is gone and stop

_RETRYABLE = (CopyError, SourceError, OSError)


@dataclass
class BackupOptions:
    dry_run: bool = False
    limit: int | None = None
    gate: GateOptions = field(default_factory=GateOptions)
    retry_delays: Sequence[float] = RETRY_DELAYS
    stall_after: float = 180.0
    live_progress: bool = True  # one-line progress bar when stderr is a terminal
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
    aborted: int = 0  # not attempted because the run stopped early
    repaired: int = 0  # informational: re-copied after verify found the old copy damaged
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
            + self.aborted
        )


@dataclass(frozen=True)
class _Outcome:
    status: str  # copied | conflict | duplicate
    dest: Path
    sha256: str
    size: int


class _FileCopier:
    """Copies single files into one month's ``files/`` folder.

    Handles name collisions (case-insensitively), identical-bytes duplicates left by an
    interrupted run, filesystem size limits and retries with backoff.
    """

    def __init__(
        self,
        source: Source,
        files_dir: Path,
        *,
        max_bytes: int | None,
        progress: Progress,
        retry_delays: Sequence[float],
        log: RunLog,
    ) -> None:
        self._source = source
        self._files_dir = files_dir
        self._max_bytes = max_bytes
        self._progress = progress
        self._retry_delays = retry_delays
        self._log = log
        self._names = NameIndex()

    def copy(self, f: SourceFile) -> _Outcome:
        if self._max_bytes is not None and f.size > self._max_bytes:
            raise CopyError(f"{human_bytes(f.size)} exceeds this filesystem's file size limit")
        last: Exception | None = None
        for delay in (0.0, *self._retry_delays):
            if last is not None:
                self._log(f"  retry in {delay:.0f}s: {f.path} ({last})")
                time.sleep(delay)
            try:
                return self._attempt(f)
            except _RETRYABLE as exc:
                last = exc
        assert last is not None
        raise last

    def _attempt(self, f: SourceFile) -> _Outcome:
        self._progress.start_file(f.path)
        target = self._files_dir / self._source.dest_path(f.path)
        dest = self._names.free(target.parent, target.name)
        with self._source.open(f.path) as raw:
            reader = CountingReader(raw, self._progress)
            sha, size = copy_verified(reader, dest, expected_size=f.size, mtime=f.mtime)  # type: ignore[arg-type]
        self._names.add(dest)
        if dest == target:
            return _Outcome("copied", dest, sha, size)
        existing = self._names.existing(target.parent, target.name)  # real on-disk case
        if existing is not None and existing.is_file() and hash_file(existing)[0] == sha:
            dest.unlink()  # identical bytes already there (resumed run): keep the original
            self._names.discard(dest)
            fsync_dir(dest.parent)
            return _Outcome("duplicate", existing, sha, size)
        return _Outcome("conflict", dest, sha, size)


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
    out: Callable[[str], None] = print,
) -> int:
    now = opts.now or datetime.now()
    log = RunLog(out)
    try:
        with drive.lock(), drive.open_catalog() as catalog:
            run_id = catalog.unique_run_id(now.strftime("%Y%m%dT%H%M%S"))
            return _BackupRun(source, drive, catalog, opts, disk, runner, log, now, run_id).run()
    finally:
        log.close()


class _BackupRun:
    def __init__(
        self,
        source: Source,
        drive: Drive,
        catalog: Catalog,
        opts: BackupOptions,
        disk: str | None,
        runner: Runner,
        log: RunLog,
        now: datetime,
        run_id: str,
    ) -> None:
        self.source, self.drive, self.catalog, self.opts = source, drive, catalog, opts
        self.disk, self.runner, self.log = disk, runner, log
        self.now, self.run_id, self.month = now, run_id, now.strftime("%Y-%m")
        self.stats: dict[Kind, KindStats] = defaultdict(KindStats)

    # -- phases ------------------------------------------------------------------------

    def run(self) -> int:
        fstype = fs_type(self.drive.mount)
        log = self.log
        log(f"photo-backup {__version__}  run {self.run_id}")
        log(f"source: {self.source.name}  {self.source.info}")
        log(f"drive:  {self.drive.label}  {self.drive.mount}  fs={fstype}  disk={self.disk}")

        files, kinds, plan, repairs = self._plan()
        planned_bytes = sum(f.size for f in plan)

        gate_opts = replace(self.opts.gate, record=self.opts.gate.record and not self.opts.dry_run)
        gate = run_gate(
            self.drive,
            self.catalog,
            disk=self.disk,
            planned_bytes=planned_bytes,
            opts=gate_opts,
            runner=self.runner,
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

        save_listing(self.source.name, files, kinds)
        if self.opts.dry_run:
            log("dry run: nothing copied.")
            return EXIT_OK

        run_dir = self.drive.device_dir(self.source.name) / self.month
        logs_dir = run_dir / "runs" / self.run_id
        logs_dir.mkdir(parents=True, exist_ok=True)
        log.attach(logs_dir / "run.log")
        started = self.now.isoformat(timespec="seconds")
        self.catalog.start_run(self.run_id, self.source.name, self.month, started)

        landed = self._copy(run_dir, plan, kinds, repairs, planned_bytes, fstype)
        self._seal_records(run_dir)

        problems = self._reconcile(landed)
        failed = sum(s.failed + s.aborted for s in self.stats.values())
        result = "PASS" if not problems and failed == 0 else "FAIL"
        finished = datetime.now().isoformat(timespec="seconds")
        self.catalog.finish_run(self.run_id, finished, result)
        summary = {
            "tool": f"photo-backup {__version__}",
            "host": platform.node(),
            "run_id": self.run_id,
            "month": self.month,
            "started": started,
            "finished": finished,
            "result": result,
            "source": {"name": self.source.name, **self.source.info},
            "drive": {
                "label": self.drive.label,
                "uuid": self.drive.uuid,
                "fs": fstype,
                "disk": self.disk,
            },
            "gate": {
                "result": gate.result,
                "checks": [str(c) for c in gate.checks],
                **gate.metrics,
            },
            "counts": {k.value: asdict(v) for k, v in self.stats.items()},
            "problems": problems,
        }
        (logs_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        self.catalog.snapshot_to(logs_dir / "catalog-snapshot.sqlite")
        mirror_catalog(self.drive, self.catalog)

        log()
        for line in self._result_lines():
            log(line)
        for p in problems:
            log(f"  PROBLEM: {p}")
        deferred = sum(s.deferred for s in self.stats.values())
        tail = f" ({deferred} files deferred by --limit; run again to continue)" if deferred else ""
        log(f"RESULT: {result}{tail}  ->  {logs_dir}")
        return EXIT_OK if result == "PASS" else EXIT_VERIFY_FAILED

    def _plan(
        self,
    ) -> tuple[list[SourceFile], dict[str, Kind], list[SourceFile], set[str]]:
        files = self.source.list_files()
        root = self.source.local_root
        if root is not None and self.drive.root.is_relative_to(root):
            inside = self.drive.root.relative_to(root).as_posix() + "/"
            files = [f for f in files if not f.path.startswith(inside)]
        kinds = classify(f.path for f in files)
        plan: list[SourceFile] = []
        repairs: set[str] = set()
        for f in files:
            s = self.stats[kinds[f.path]]
            s.listed += 1
            s.bytes_listed += f.size
            row = self.catalog.get(self.source.name, f)
            if row is not None and row.is_good:
                s.skipped += 1
                continue
            if row is not None:
                repairs.add(f.path)  # known copy was found damaged: copy again
            plan.append(f)
        if self.opts.limit is not None:
            for f in plan[self.opts.limit :]:
                self.stats[kinds[f.path]].deferred += 1
            plan = plan[: self.opts.limit]
        self.log()
        for line in _plan_lines(self.stats):
            self.log(line)
        extra = f" ({len(repairs)} re-copies of damaged files)" if repairs else ""
        planned = human_bytes(sum(f.size for f in plan))
        self.log(f"to copy: {len(plan)} files, {planned}{extra}")
        return files, kinds, plan, repairs

    def _copy(
        self,
        run_dir: Path,
        plan: list[SourceFile],
        kinds: dict[str, Kind],
        repairs: set[str],
        planned_bytes: int,
        fstype: str | None,
    ) -> list[tuple[Path, int]]:
        log, catalog = self.log, self.catalog
        files_dir = run_dir / "files"
        known = {self.drive.root / p for p in catalog.dest_paths()}
        stale = remove_stale_partials(files_dir, keep=known)
        if stale:
            log(f"removed {stale} temp files left by an interrupted run")

        progress = Progress()
        copier = _FileCopier(
            self.source,
            files_dir,
            max_bytes=max_file_size(fstype),
            progress=progress,
            retry_delays=self.opts.retry_delays,
            log=log,
        )
        landed: list[tuple[Path, int]] = []
        log()
        log(f"copying into {run_dir}")
        t0, done_bytes, last_report, consecutive_failures = time.monotonic(), 0, 0, 0
        bar = LiveBar(
            progress,
            total_bytes=planned_bytes,
            total_files=len(plan),
            enabled=self.opts.live_progress,
        )
        with (
            RunRecords(run_dir) as records,
            bar,
            StallWatchdog(progress, log, after=self.opts.stall_after),
        ):
            plain_out = log.swap_out(lambda _line: None)
            log.swap_out(bar.wrap(plain_out))  # log lines print above the live bar
            try:
                for i, f in enumerate(plan, 1):
                    kind = kinds[f.path]
                    s = self.stats[kind]
                    if sanitize_relpath(f.path) != f.path:
                        log(f"  renamed for portability: {f.path} -> {sanitize_relpath(f.path)}")
                    try:
                        outcome = copier.copy(f)
                    except _RETRYABLE as exc:
                        progress.finish_file()
                        s.failed += 1
                        consecutive_failures += 1
                        log(f"  FAILED {f.path}: {exc}")
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            self._abort_rest(plan[i:], kinds)
                            break
                        continue
                    consecutive_failures = 0
                    progress.finish_file()
                    stamp = datetime.now().isoformat(timespec="seconds")
                    records.add(
                        src_path=f.path,
                        dest=outcome.dest,
                        size=outcome.size,
                        mtime=f.mtime,
                        sha256=outcome.sha256,
                        kind=kind.value,
                        status=outcome.status,
                        run_id=self.run_id,
                    )
                    catalog.put(
                        FileRow(
                            device=self.source.name,
                            src_path=f.path,
                            size=f.size,
                            mtime=f.mtime,
                            sha256=outcome.sha256,
                            kind=kind.value,
                            dest_path=outcome.dest.relative_to(self.drive.root).as_posix(),
                            month=self.month,
                            run_id=self.run_id,
                            copied_at=stamp,
                            verified_at=stamp,
                        )
                    )
                    landed.append((outcome.dest, outcome.size))
                    self._count(s, outcome, f, repairs)
                    done_bytes += f.size
                    if (
                        i % PROGRESS_EVERY_FILES == 0
                        or done_bytes - last_report >= PROGRESS_EVERY_BYTES
                    ):
                        last_report = done_bytes
                        elapsed = time.monotonic() - t0
                        log(_progress(i, len(plan), done_bytes, planned_bytes, elapsed))
            except KeyboardInterrupt:
                now = datetime.now().isoformat(timespec="seconds")
                catalog.finish_run(self.run_id, now, "INTERRUPTED")
                log("interrupted: everything copied so far is verified and recorded; re-run")
                raise
            finally:
                log.swap_out(plain_out)
        return landed

    def _seal_records(self, run_dir: Path) -> None:
        """Remember size + hash of the append-only files so later runs can prove that
        nothing rewrote them (see ``health.records_check``)."""
        for name in ("manifest.tsv", "SHA256SUMS"):
            path = run_dir / name
            if path.is_file():
                sha, size = hash_file(path)
                rel = path.relative_to(self.drive.root).as_posix()
                self.catalog.set_record(rel, size, sha, self.run_id)

    def _count(self, s: KindStats, o: _Outcome, f: SourceFile, repairs: set[str]) -> None:
        if f.path in repairs:
            s.repaired += 1
        if o.status == "copied":
            s.copied += 1
            s.bytes_copied += o.size
        elif o.status == "conflict":
            s.conflicts += 1
            s.bytes_copied += o.size
            self.log(
                f"  conflict: {f.path} -> {o.dest.name} (name taken by other bytes; kept both)"
            )
        else:
            s.duplicates += 1

    def _abort_rest(self, rest: list[SourceFile], kinds: dict[str, Kind]) -> None:
        for f in rest:
            self.stats[kinds[f.path]].aborted += 1
        self.log(
            f"  !! {MAX_CONSECUTIVE_FAILURES} files in a row failed: the source looks "
            f"disconnected. Stopped; {len(rest)} files not attempted. Reconnect and re-run."
        )

    def _reconcile(self, landed: list[tuple[Path, int]]) -> list[str]:
        """Counts must add up per kind, and every landed file must be on disk at full size."""
        problems = [
            f"{kind.value}: listed {s.listed} but accounted for {s.accounted}"
            for kind, s in self.stats.items()
            if s.accounted != s.listed
        ]
        for path, size in landed:
            if not path.is_file():
                problems.append(f"missing after copy: {path}")
            elif path.stat().st_size != size:
                problems.append(f"size changed after copy: {path}")
        return problems

    def _result_lines(self) -> list[str]:
        rows = [
            [
                k.value,
                s.listed,
                s.skipped,
                s.copied,
                s.conflicts,
                s.duplicates,
                s.failed + s.aborted,
                human_bytes(s.bytes_copied),
            ]
            for k, s in self.stats.items()
            if s.listed
        ]
        headers = ["kind", "listed", "skipped", "copied", "conflict", "dup", "failed", "written"]
        return table(headers, rows)


def _progress(i: int, n: int, done: int, total: int, elapsed: float) -> str:
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate else 0
    return (
        f"  [{i:>6}/{n}]  {human_bytes(done)} / {human_bytes(total)}  "
        f"{rate / 1024**2:.1f} MiB/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s"
    )
