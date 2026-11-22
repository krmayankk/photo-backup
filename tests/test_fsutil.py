from pathlib import Path

import pytest

from photo_backup.fsutil import (
    FAT_MAX_FILE,
    free_name,
    max_file_size,
    sanitize_component,
    sanitize_relpath,
)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("IMG_0001.HEIC", "IMG_0001.HEIC"),
        ('a:b*c?"d<e>f|g\\h', "a_b_c__d_e_f_g_h"),
        ("trailing dot.", "trailing dot"),
        ("trailing space ", "trailing space"),
        ("CON.txt", "_CON.txt"),
        ("lpt1", "_lpt1"),
        ("tab\there", "tab_here"),
        ("...", "_"),
    ],
)
def test_sanitize_component(raw: str, clean: str) -> None:
    assert sanitize_component(raw) == clean


def test_long_names_are_shortened_deterministically_and_keep_extension() -> None:
    name = "é" * 200 + ".HEIC"  # 405 bytes in UTF-8
    out = sanitize_component(name)
    assert len(out.encode()) <= 255
    assert out.endswith(".HEIC")
    assert out == sanitize_component(name)
    assert out != sanitize_component("è" * 200 + ".HEIC")


def test_sanitize_relpath_drops_traversal() -> None:
    assert sanitize_relpath("../a/./b//c:d") == "a/b/c_d"


def test_free_name_avoids_existing_and_case_variants(tmp_path: Path) -> None:
    (tmp_path / "IMG_1.HEIC").write_bytes(b"x")
    assert free_name(tmp_path, "new.HEIC") == tmp_path / "new.HEIC"
    assert free_name(tmp_path, "IMG_1.HEIC") == tmp_path / "IMG_1.conflict-1.HEIC"
    assert free_name(tmp_path, "img_1.heic") == tmp_path / "img_1.conflict-1.heic"
    (tmp_path / "IMG_1.conflict-1.HEIC").write_bytes(b"y")
    assert free_name(tmp_path, "IMG_1.HEIC") == tmp_path / "IMG_1.conflict-2.HEIC"


def test_free_name_in_missing_directory(tmp_path: Path) -> None:
    assert free_name(tmp_path / "nope", "a.jpg") == tmp_path / "nope" / "a.jpg"


def test_max_file_size() -> None:
    assert max_file_size("vfat") == FAT_MAX_FILE
    assert max_file_size("ntfs3") is None
    assert max_file_size(None) is None
