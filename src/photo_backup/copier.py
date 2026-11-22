"""Verify-on-write copy: the only code path that creates files on a backup drive."""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
from typing import BinaryIO

from photo_backup.fsutil import CHUNK, fsync_dir, hash_file

PARTIAL_SUFFIX = ".partial"


class CopyError(Exception):
    """A copy could not be completed and verified; nothing was left at the destination."""


def copy_verified(
    src: BinaryIO, dest: Path, *, expected_size: int | None, mtime: int | None
) -> tuple[str, int]:
    """Copy ``src`` to ``dest`` and prove the bytes on disk match what was read.

    Steps: hash while streaming into ``dest.partial`` -> fsync -> drop page cache ->
    re-read and hash -> rename to ``dest``. ``dest`` must not exist; it is never
    overwritten. On any failure the partial file is removed and :class:`CopyError`
    (or the original exception) propagates.

    Returns ``(sha256_hex, size)``.
    """
    if dest.exists():
        raise CopyError(f"refusing to overwrite existing file: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + PARTIAL_SUFFIX)

    try:
        h = hashlib.sha256()
        size = 0
        with open(partial, "wb") as out:
            while chunk := src.read(CHUNK):
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())

        if expected_size is not None and size != expected_size:
            raise CopyError(f"size mismatch: source listed {expected_size} bytes, read {size}")

        digest = h.hexdigest()
        on_disk, _ = hash_file(partial)
        if on_disk != digest:
            raise CopyError(f"verify-on-write mismatch for {dest.name}: read back {on_disk}")

        if mtime is not None:
            with contextlib.suppress(OSError):  # timestamps are nice-to-have, bytes are not
                os.utime(partial, (mtime, mtime))
        if dest.exists():
            raise CopyError(f"destination appeared during copy, refusing to overwrite: {dest}")
        os.rename(partial, dest)
        fsync_dir(dest.parent)
        return digest, size
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        raise


def remove_stale_partials(root: Path) -> int:
    """Delete ``*.partial`` leftovers from an interrupted run. Returns how many."""
    if not root.is_dir():
        return 0
    count = 0
    for p in root.rglob("*" + PARTIAL_SUFFIX):
        if p.is_file():
            p.unlink()
            count += 1
    return count
