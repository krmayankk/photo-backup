from photo_backup.classify import classify
from photo_backup.model import Kind


def test_live_photo_clip_is_not_a_video() -> None:
    kinds = classify(["DCIM/100APPLE/IMG_1.HEIC", "DCIM/100APPLE/IMG_1.MOV"])
    assert kinds["DCIM/100APPLE/IMG_1.HEIC"] is Kind.PHOTO
    assert kinds["DCIM/100APPLE/IMG_1.MOV"] is Kind.LIVE_CLIP


def test_standalone_mov_is_a_video() -> None:
    assert classify(["DCIM/100APPLE/IMG_2.MOV"])["DCIM/100APPLE/IMG_2.MOV"] is Kind.VIDEO


def test_same_stem_in_another_folder_is_not_a_live_photo() -> None:
    kinds = classify(["DCIM/100APPLE/IMG_1.HEIC", "DCIM/101APPLE/IMG_1.MOV"])
    assert kinds["DCIM/101APPLE/IMG_1.MOV"] is Kind.VIDEO


def test_extension_matching_is_case_insensitive() -> None:
    kinds = classify(["a/IMG_3.jpeg", "a/img_3.mov", "a/clip.Mp4"])
    assert kinds["a/IMG_3.jpeg"] is Kind.PHOTO
    assert kinds["a/img_3.mov"] is Kind.LIVE_CLIP
    assert kinds["a/clip.Mp4"] is Kind.VIDEO


def test_mp4_with_photo_stem_stays_a_video() -> None:
    # Only .MOV is used for Live Photos.
    assert classify(["a/IMG_4.JPG", "a/IMG_4.MP4"])["a/IMG_4.MP4"] is Kind.VIDEO


def test_sidecar_edited_and_other() -> None:
    kinds = classify(
        [
            "DCIM/100APPLE/IMG_5.AAE",
            "PhotoData/Mutations/DCIM/100APPLE/IMG_5/Adjustments/FullSizeRender.heic",
            "notes.txt",
        ]
    )
    assert list(kinds.values()) == [Kind.SIDECAR, Kind.EDITED, Kind.OTHER]
