"""Core value types shared across modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    """What a file is, for reporting and safe-to-delete decisions."""

    PHOTO = "photo"
    VIDEO = "video"
    LIVE_CLIP = "live-clip"  # the short .MOV half of an iOS Live Photo
    SIDECAR = "sidecar"  # .AAE / .XMP edit metadata
    EDITED = "edited"  # rendered edit (iOS PhotoData/Mutations)
    OTHER = "other"


@dataclass(frozen=True)
class SourceFile:
    """A file as listed by a source. ``path`` is POSIX-style, relative to the source root."""

    path: str
    size: int
    mtime: int


class Level(str, Enum):
    """Outcome of a single health/sanity check."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str

    def __str__(self) -> str:
        return f"[{self.level.value:<4}] {self.name}: {self.detail}"
