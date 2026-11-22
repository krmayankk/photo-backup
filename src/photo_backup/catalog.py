"""Per-drive SQLite catalog of every file ever backed up to that drive.

The catalog is an index for speed. The plain-text ``manifest.tsv`` files in each run
folder hold the same information and remain readable without this tool.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from photo_backup.model import SourceFile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    device      TEXT    NOT NULL,
    src_path    TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    mtime       INTEGER NOT NULL,
    sha256      TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    dest_path   TEXT    NOT NULL,   -- relative to <drive>/PhotoBackup
    month       TEXT    NOT NULL,   -- YYYY-MM run folder
    run_id      TEXT    NOT NULL,
    copied_at   TEXT    NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (device, src_path, size, mtime)
);
CREATE INDEX IF NOT EXISTS files_by_month ON files (device, month);
CREATE TABLE IF NOT EXISTS runs (
    run_id   TEXT PRIMARY KEY,
    device   TEXT NOT NULL,
    month    TEXT NOT NULL,
    started  TEXT NOT NULL,
    finished TEXT,
    result   TEXT
);
"""


@dataclass(frozen=True)
class FileRow:
    device: str
    src_path: str
    size: int
    mtime: int
    sha256: str
    kind: str
    dest_path: str
    month: str
    run_id: str
    copied_at: str
    verified_at: str | None


@dataclass(frozen=True)
class RunRow:
    run_id: str
    device: str
    month: str
    started: str
    finished: str | None
    result: str | None


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- files -----------------------------------------------------------------------

    def get(self, device: str, f: SourceFile) -> FileRow | None:
        cur = self._db.execute(
            "SELECT * FROM files WHERE device=? AND src_path=? AND size=? AND mtime=?",
            (device, f.path, f.size, f.mtime),
        )
        row = cur.fetchone()
        return FileRow(*row) if row else None

    def add(self, row: FileRow) -> None:
        self._db.execute(
            "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.device,
                row.src_path,
                row.size,
                row.mtime,
                row.sha256,
                row.kind,
                row.dest_path,
                row.month,
                row.run_id,
                row.copied_at,
                row.verified_at,
            ),
        )
        self._db.commit()

    def files(self, device: str | None = None, month: str | None = None) -> Iterator[FileRow]:
        sql = "SELECT * FROM files"
        clauses, args = [], []
        if device:
            clauses.append("device=?")
            args.append(device)
        if month:
            clauses.append("month=?")
            args.append(month)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY device, dest_path"
        for row in self._db.execute(sql, args):
            yield FileRow(*row)

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def mark_verified(self, rows: list[FileRow], when: str) -> None:
        self._db.executemany(
            "UPDATE files SET verified_at=? WHERE device=? AND src_path=? AND size=? AND mtime=?",
            [(when, r.device, r.src_path, r.size, r.mtime) for r in rows],
        )
        self._db.commit()

    # -- runs ------------------------------------------------------------------------

    def unique_run_id(self, base: str) -> str:
        """``base``, or ``base-2``, ``base-3``... if runs started within the same second."""
        taken = {
            r[0]
            for r in self._db.execute("SELECT run_id FROM runs WHERE run_id LIKE ?", (base + "%",))
        }
        run_id, n = base, 1
        while run_id in taken:
            n += 1
            run_id = f"{base}-{n}"
        return run_id

    def start_run(self, run_id: str, device: str, month: str, started: str) -> None:
        self._db.execute(
            "INSERT INTO runs (run_id, device, month, started) VALUES (?,?,?,?)",
            (run_id, device, month, started),
        )
        self._db.commit()

    def finish_run(self, run_id: str, finished: str, result: str) -> None:
        self._db.execute(
            "UPDATE runs SET finished=?, result=? WHERE run_id=?", (finished, result, run_id)
        )
        self._db.commit()

    def runs(self, device: str | None = None) -> list[RunRow]:
        sql = "SELECT run_id, device, month, started, finished, result FROM runs"
        args: tuple[str, ...] = ()
        if device:
            sql += " WHERE device=?"
            args = (device,)
        return [RunRow(*r) for r in self._db.execute(sql + " ORDER BY run_id", args)]

    # -- maintenance -----------------------------------------------------------------

    def integrity(self) -> str:
        return str(self._db.execute("PRAGMA integrity_check").fetchone()[0])

    def snapshot_to(self, dest: Path) -> None:
        """Consistent copy of the catalog to ``dest`` (atomic replace)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        if tmp.exists():
            tmp.unlink()
        out = sqlite3.connect(tmp)
        try:
            self._db.backup(out)
        finally:
            out.close()
        os.replace(tmp, dest)
