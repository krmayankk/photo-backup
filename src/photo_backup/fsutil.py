"""Filesystem helpers: portable filenames, hashing that really hits the disk, fs limits."""

from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import subprocess
from pathlib import Path

CHUNK = 4 * 1024 * 1024

# Characters that NTFS / exFAT / FAT cannot store, plus Windows reserved device names.
_ILLEGAL = frozenset('<>:"\\|?*')
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_MAX_NAME_BYTES = 255

FAT_FS_TYPES = frozenset({"vfat", "msdos", "fat", "fat32"})
FAT_MAX_FILE = 4 * 1024**3 - 1


def sanitize_component(name: str) -> str:
    """Make one path component storable on NTFS, exFAT and FAT, deterministically."""
    out = "".join("_" if (c in _ILLEGAL or ord(c) < 32) else c for c in name)
    out = out.rstrip(" .") or "_"
    if out.split(".", 1)[0].upper() in _RESERVED:
        out = "_" + out
    encoded = out.encode()
    if len(encoded) > _MAX_NAME_BYTES:
        digest = hashlib.sha1(encoded).hexdigest()[:8]
        root, ext = posixpath.splitext(out)
        budget = _MAX_NAME_BYTES - len(ext.encode()) - len(digest) - 1
        root = root.encode()[:budget].decode("utf-8", "ignore")
        out = f"{root}~{digest}{ext}"
    return out


def sanitize_relpath(path: str) -> str:
    """Sanitize every component of a relative POSIX path; drop empty/dot/dot-dot parts."""
    parts = [p for p in path.split("/") if p not in ("", ".", "..")]
    return "/".join(sanitize_component(p) for p in parts)


def drop_page_cache(fd: int) -> None:
    """Ask the kernel to forget cached pages so the next read comes from the device."""
    with contextlib.suppress(AttributeError, OSError):  # not available on every platform
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def hash_file(path: Path) -> tuple[str, int]:
    """SHA-256 and size of ``path``, read from the device rather than the page cache."""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        drop_page_cache(fh.fileno())
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def fsync_dir(path: Path) -> None:
    """Persist a directory entry (rename/create) to disk."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - some FUSE filesystems refuse O_RDONLY on dirs
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - not supported everywhere (e.g. some FUSE mounts)
        pass
    finally:
        os.close(fd)


def free_name(directory: Path, name: str) -> Path:
    """Return ``directory/name`` or a ``.conflict-N`` variant that is not taken.

    The comparison is case-insensitive so the result is also safe on NTFS/exFAT when the
    drive is later used from Windows or macOS.
    """
    taken = {n.lower() for n in os.listdir(directory)} if directory.is_dir() else set()
    if name.lower() not in taken:
        return directory / name
    root, ext = posixpath.splitext(name)
    i = 1
    while f"{root}.conflict-{i}{ext}".lower() in taken:
        i += 1
    return directory / f"{root}.conflict-{i}{ext}"


def fs_type(path: Path) -> str | None:
    """Filesystem type of the mount containing ``path`` (e.g. ``ntfs3``), if known."""
    try:
        out = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "--target", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = out.stdout.strip()
    return value or None


def max_file_size(fstype: str | None) -> int | None:
    """Largest file the filesystem can hold, or None if effectively unlimited."""
    if fstype and fstype.lower() in FAT_FS_TYPES:
        return FAT_MAX_FILE
    return None
