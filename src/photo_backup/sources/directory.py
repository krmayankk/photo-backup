"""A local folder or a whole old disk as a backup source."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from photo_backup.model import SourceFile
from photo_backup.sources.base import Source, SourceError, scan_tree, validate_device_name

# OS bookkeeping folders that are never user data.
SYSTEM_DIRS = frozenset(
    {
        "System Volume Information",
        "$RECYCLE.BIN",
        ".Spotlight-V100",
        ".fseventsd",
        ".DocumentRevisions-V100",
        ".TemporaryItems",
    }
)


class DirectorySource(Source):
    def __init__(self, path: Path, name: str, *, skip_trash: bool = False) -> None:
        path = path.expanduser().resolve()
        if not path.is_dir():
            raise SourceError(f"{path} is not a directory")
        self.root = path
        self.local_root = path
        self.name = validate_device_name(name)
        self.skip_dirs = SYSTEM_DIRS | ({".Trashes"} if skip_trash else set())
        self.info = {"type": "dir", "path": str(path)}

    def list_files(self) -> list[SourceFile]:
        return scan_tree(self.root, skip_dirs=self.skip_dirs)

    def open(self, path: str) -> BinaryIO:
        return open(self.root / path, "rb")
