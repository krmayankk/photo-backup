"""Source adapters and the ``--from`` parser."""

from __future__ import annotations

from pathlib import Path

from photo_backup.sources.base import Source, SourceError
from photo_backup.sources.directory import DirectorySource
from photo_backup.sources.iphone import IPhoneSource

__all__ = ["DirectorySource", "IPhoneSource", "Source", "SourceError", "open_source"]


def open_source(
    spec: str, *, name: str | None = None, udid: str | None = None, skip_trash: bool = False
) -> Source:
    """Build a source from a ``--from`` value: ``iphone`` or ``dir:<path>``."""
    if spec == "iphone":
        return IPhoneSource(udid)
    if spec.startswith("dir:"):
        if not name:
            raise SourceError("--name is required with --from dir:<path>")
        return DirectorySource(Path(spec[4:]), name, skip_trash=skip_trash)
    raise SourceError(f"unknown source {spec!r}: use 'iphone' or 'dir:<path>'")
