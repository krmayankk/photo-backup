"""The Source interface every device adapter implements."""

from __future__ import annotations

import os
import re
import stat
from abc import ABC, abstractmethod
from collections.abc import Collection
from pathlib import Path
from typing import BinaryIO

from photo_backup.fsutil import sanitize_relpath
from photo_backup.model import SourceFile

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.,_+-]{0,95}$")


class SourceError(Exception):
    """The source cannot be read (not connected, locked, not trusted, ...)."""


def validate_device_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise SourceError(f"invalid device name {name!r}: use letters, digits and . , _ + -")
    return name


class Source(ABC):
    """A read-only place to copy files from.

    Sources are context managers so adapters that need to mount something (iPhone) can
    clean up reliably.
    """

    #: Stable name used as the folder under ``PhotoBackup/devices/``.
    name: str
    #: Free-form provenance recorded in every run summary (model, OS version, path...).
    info: dict[str, str]
    #: Set when the source is a local directory, so a drive is never backed up into itself.
    local_root: Path | None = None

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    @abstractmethod
    def list_files(self) -> list[SourceFile]:
        """Every file to consider, sorted by path."""

    @abstractmethod
    def open(self, path: str) -> BinaryIO:
        """Open a listed file for binary reading."""

    def dest_path(self, path: str) -> str:
        """Where a source path lands inside the run's ``files/`` folder."""
        return sanitize_relpath(path)


def scan_tree(
    root: Path,
    *,
    prefix: str = "",
    skip_dirs: Collection[str] = (),
    name_filter: re.Pattern[str] | None = None,
) -> list[SourceFile]:
    """Walk ``root`` without following symlinks and return regular files.

    ``prefix`` is prepended to the relative paths, ``skip_dirs`` prunes directories by
    name and ``name_filter`` keeps only matching file names.
    """
    out: list[SourceFile] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            if name_filter and not name_filter.search(name):
                continue
            full = here / name
            st = full.lstat()
            if not stat.S_ISREG(st.st_mode):  # skips symlinks, sockets, devices
                continue
            rel = full.relative_to(root).as_posix()
            out.append(SourceFile(f"{prefix}{rel}", st.st_size, int(st.st_mtime)))
    out.sort(key=lambda f: f.path)
    return out
