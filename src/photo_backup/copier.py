"""Verify-on-write copy: the only code path that creates files on a backup drive."""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Collection
from pathlib import Path
from typing import BinaryIO

from photo_backup.fsutil import CHUNK, fsync_dir, hash_file

#: Prefix of in-flight temp files. Hidden and tool-specific so it cannot be mistaken for
#: (or collide with) a real backed-up file such as ``movie.mp4.partial``.
TMP_PREFIX = ".pbtmp-"


class CopyError(Exception):
    """A copy could not be completed and verified; nothing was left at the destination."""


def copy_verified(
    src: BinaryIO, dest: Path, *, expected_size: int | None, mtime: int | None
) -> tuple[str, int]:
    """Copy ``src`` to ``dest`` and prove the bytes on disk match what was read.

    Steps: hash while streaming into ``.pbtmp-<name>`` -> fsync -> drop page cache ->
    re-read and hash -> rename to ``dest``. ``dest`` must not exist; it is never
    overwritten. On any failure the partial file is removed and :class:`CopyError`
    (or the original exception) propagates.

    Returns ``(sha256_hex, size)``.
    """
    if dest.exists():
        raise CopyError(f"refusing to overwrite existing file: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(TMP_PREFIX + dest.name)
    try:
        out = open(partial, "xb")  # noqa: SIM115 - "x": never truncate an existing file
    except FileExistsError as exc:
        raise CopyError(f"temp file already exists, refusing to touch it: {partial}") from exc

    try:
        h = hashlib.sha256()
        size = 0
        with out:
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


def remove_stale_partials(root: Path, keep: Collection[Path] = ()) -> int:
    """Delete temp files left by an interrupted run. Returns how many.

    Anything listed in ``keep`` (every path the catalog knows) is never deleted, even if
    its name happens to look like a temp file.
    """
    if not root.is_dir():
        return 0
    protected = set(keep)
    count = 0
    for p in root.rglob(TMP_PREFIX + "*"):
        if p.is_file() and p not in protected:
            p.unlink()
            count += 1
    return count
