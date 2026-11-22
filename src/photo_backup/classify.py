"""Classify source paths into :class:`Kind` values.

The one subtle case is the iOS Live Photo: ``IMG_1234.HEIC`` plus ``IMG_1234.MOV`` in the
same folder. The ``.MOV`` is a ~3 s clip that belongs to the photo, so it must never be
reported as a "video" (deleting videos on the phone does not delete these clips).
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable

from photo_backup.model import Kind

PHOTO_EXTS = frozenset(
    {".heic", ".heif", ".jpg", ".jpeg", ".png", ".avif", ".dng", ".gif", ".webp", ".tif", ".tiff"}
)
VIDEO_EXTS = frozenset({".mov", ".mp4", ".m4v", ".3gp", ".mkv", ".avi", ".mts"})
SIDECAR_EXTS = frozenset({".aae", ".xmp"})
EDITED_PREFIX = "PhotoData/Mutations/"


def _split(path: str) -> tuple[str, str, str]:
    folder, name = posixpath.split(path)
    stem, ext = posixpath.splitext(name)
    return folder, stem.lower(), ext.lower()


def classify(paths: Iterable[str]) -> dict[str, Kind]:
    """Return a mapping of path -> Kind for every path given."""
    paths = list(paths)
    photo_keys = set()
    for p in paths:
        folder, stem, ext = _split(p)
        if ext in PHOTO_EXTS:
            photo_keys.add((folder, stem))

    kinds: dict[str, Kind] = {}
    for p in paths:
        folder, stem, ext = _split(p)
        if p.startswith(EDITED_PREFIX):
            kinds[p] = Kind.EDITED
        elif ext in PHOTO_EXTS:
            kinds[p] = Kind.PHOTO
        elif ext in VIDEO_EXTS:
            is_live = ext == ".mov" and (folder, stem) in photo_keys
            kinds[p] = Kind.LIVE_CLIP if is_live else Kind.VIDEO
        elif ext in SIDECAR_EXTS:
            kinds[p] = Kind.SIDECAR
        else:
            kinds[p] = Kind.OTHER
    return kinds
