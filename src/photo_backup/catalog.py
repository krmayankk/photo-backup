"""Per-drive SQLite catalog of every file ever backed up to that drive.

The catalog is an index for speed. The plain-text ``manifest.tsv`` files in each run
folder hold the same information and remain readable without this tool.

``verified_at`` is the single source of truth for "this drive holds a good copy":
it is set when a file is copied (verify-on-write) or re-verified, and cleared to NULL
when ``verify`` or the gate's spot-check finds the copy damaged or missing. Rows with a
NULL ``verified_at`` are neither counted by ``status`` nor skipped by ``backup``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import astuple, dataclass, fields
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
    verified_at TEXT,               -- NULL = copy known damaged/missing
    PRIMARY KEY (device, src_path, size, mtime)
);
CREATE INDEX IF NOT EXISTS files_by_month ON files (device, month);
-- Size + SHA-256 of each append-only record file (manifest.tsv, SHA256SUMS) as of the
-- end of the last run that touched it. Later runs check the file still starts with
-- exactly these bytes, which proves nothing was modified in place.
CREATE TABLE IF NOT EXISTS records (
    path     TEXT PRIMARY KEY,      -- relative to <drive>/PhotoBackup
    size     INTEGER NOT NULL,
    sha256   TEXT    NOT NULL,
    run_id   TEXT    NOT NULL
);
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

    @property
    def is_good(self) -> bool:
        return self.verified_at is not None


@dataclass(frozen=True)
class RunRow:
    run_id: str
    device: str
    month: str
    started: str
    finished: str | None
    result: str | None


# Columns are always named explicitly so the dataclasses never depend on table order.
_FILE_COLS = ", ".join(f.name for f in fields(FileRow))
_RUN_COLS = ", ".join(f.name for f in fields(RunRow))
_KEY = "device=? AND src_path=? AND size=? AND mtime=?"


def _key(row: FileRow) -> tuple[str, str, int, int]:
    return (row.device, row.src_path, row.size, row.mtime)


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
            f"SELECT {_FILE_COLS} FROM files WHERE {_KEY}", (device, f.path, f.size, f.mtime)
        )
        row = cur.fetchone()
        return FileRow(*row) if row else None

    def put(self, row: FileRow) -> None:
        """Insert a row, replacing an existing row for the same source file.

        Replacing is only ever done for a row whose copy was found damaged (the backup
        planner never re-copies a file that has a good row).
        """
        placeholders = ",".join("?" * len(fields(FileRow)))
        self._db.execute(
            f"INSERT OR REPLACE INTO files ({_FILE_COLS}) VALUES ({placeholders})", astuple(row)
        )
        self._db.commit()

    def files(self, device: str | None = None, month: str | None = None) -> Iterator[FileRow]:
        sql = f"SELECT {_FILE_COLS} FROM files"
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

    def dest_paths(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT dest_path FROM files")}

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def mark_verified(self, rows: Iterable[FileRow], when: str) -> None:
        self._db.executemany(
            f"UPDATE files SET verified_at=? WHERE {_KEY}", [(when, *_key(r)) for r in rows]
        )
        self._db.commit()

    def mark_damaged(self, rows: Iterable[FileRow]) -> None:
        """Forget that these copies are good; status stops counting them, backup re-copies."""
        self._db.executemany(
            f"UPDATE files SET verified_at=NULL WHERE {_KEY}", [_key(r) for r in rows]
        )
        self._db.commit()

    # -- append-only record files ---------------------------------------------------

    def set_record(self, path: str, size: int, sha256: str, run_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO records (path, size, sha256, run_id) VALUES (?,?,?,?)",
            (path, size, sha256, run_id),
        )
        self._db.commit()

    def records(self) -> list[tuple[str, int, str]]:
        """``(path, size, sha256)`` for every append-only record file."""
        return [
            (p, int(n), h)
            for p, n, h in self._db.execute("SELECT path, size, sha256 FROM records ORDER BY path")
        ]

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
        sql = f"SELECT {_RUN_COLS} FROM runs"
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
