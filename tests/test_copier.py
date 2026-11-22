import hashlib
import io
import os
from pathlib import Path

import pytest

from photo_backup import copier
from photo_backup.copier import CopyError, copy_verified, remove_stale_partials


def test_copy_returns_hash_and_preserves_mtime(tmp_path: Path) -> None:
    data = os.urandom(10_000)
    dest = tmp_path / "sub" / "IMG.HEIC"
    sha, size = copy_verified(io.BytesIO(data), dest, expected_size=len(data), mtime=1_600_000_000)
    assert dest.read_bytes() == data
    assert (sha, size) == (hashlib.sha256(data).hexdigest(), len(data))
    assert int(dest.stat().st_mtime) == 1_600_000_000
    assert not list(tmp_path.rglob("*.partial"))


def test_never_overwrites(tmp_path: Path) -> None:
    dest = tmp_path / "IMG.HEIC"
    dest.write_bytes(b"original")
    with pytest.raises(CopyError, match="refusing to overwrite"):
        copy_verified(io.BytesIO(b"new"), dest, expected_size=3, mtime=None)
    assert dest.read_bytes() == b"original"


def test_short_read_is_rejected_and_cleaned_up(tmp_path: Path) -> None:
    dest = tmp_path / "IMG.HEIC"
    with pytest.raises(CopyError, match="size mismatch"):
        copy_verified(io.BytesIO(b"only half"), dest, expected_size=100, mtime=None)
    assert list(tmp_path.iterdir()) == []


def test_readback_mismatch_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copier, "hash_file", lambda _p: ("0" * 64, 3))
    with pytest.raises(CopyError, match="verify-on-write mismatch"):
        copy_verified(io.BytesIO(b"abc"), tmp_path / "x", expected_size=3, mtime=None)
    assert list(tmp_path.iterdir()) == []


class _Exploding(io.BytesIO):
    def read(self, n: int | None = -1) -> bytes:
        raise OSError(5, "device unplugged")


def test_source_error_mid_copy_leaves_nothing(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="unplugged"):
        copy_verified(_Exploding(), tmp_path / "x", expected_size=None, mtime=None)
    assert list(tmp_path.iterdir()) == []


def test_remove_stale_partials(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "IMG.HEIC.partial").write_bytes(b"x")
    (tmp_path / "a" / "IMG.HEIC").write_bytes(b"keep")
    assert remove_stale_partials(tmp_path) == 1
    assert [p.name for p in (tmp_path / "a").iterdir()] == ["IMG.HEIC"]
    assert remove_stale_partials(tmp_path / "missing") == 0
