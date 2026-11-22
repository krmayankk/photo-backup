"""iPhone source via libimobiledevice + ifuse, mounted read-only.

What is copied:

* ``DCIM/**``: originals, Live Photo clips and ``.AAE`` edit sidecars.
* ``PhotoData/Mutations/**/FullSizeRender.*``: the rendered result of edits made in the
  Photos app. Without these, a backup of DCIM alone silently loses every crop/filter/trim.
  They land under ``Edited/`` in the backup.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from photo_backup.fsutil import sanitize_relpath
from photo_backup.model import SourceFile
from photo_backup.proc import Runner, run
from photo_backup.sources.base import Source, SourceError, scan_tree, validate_device_name

MUTATIONS = "PhotoData/Mutations/"
_RENDER_RE = re.compile(r"^FullSizeRender\.", re.IGNORECASE)
_REQUIRED_TOOLS = ("idevice_id", "ideviceinfo", "ifuse", "fusermount")
_LOCKED_HINT = "unlock the iPhone, tap 'Trust' if asked, then retry"


def device_name(product_type: str, udid: str) -> str:
    """``iphone-iPhone15,4-5F6A``: model plus the last 4 UDID characters."""
    return validate_device_name(f"iphone-{product_type}-{udid.replace('-', '')[-4:]}")


class IPhoneSource(Source):
    def __init__(self, udid: str | None = None, *, runner: Runner = run) -> None:
        missing = [t for t in _REQUIRED_TOOLS if shutil.which(t) is None]
        if missing:
            raise SourceError(
                "missing tools: "
                + ", ".join(missing)
                + " (sudo apt install libimobiledevice-utils ifuse)"
            )
        self._run = runner
        ids = self._run(["idevice_id", "-l"]).stdout.split()
        if not ids:
            raise SourceError(f"no iPhone found over USB; connect it and {_LOCKED_HINT}")
        if udid is None:
            if len(ids) > 1:
                raise SourceError("several iPhones connected; pass --udid " + " | ".join(ids))
            udid = ids[0]
        elif udid not in ids:
            raise SourceError(f"iPhone {udid} not connected (found: {', '.join(ids)})")
        self.udid = udid
        product = self._info("ProductType")
        self.name = device_name(product, udid)
        self.info = {
            "type": "iphone",
            "product_type": product,
            "ios_version": self._info("ProductVersion"),
            "udid_suffix": udid[-4:],
        }
        self._mnt: Path | None = None

    def _info(self, key: str) -> str:
        p = self._run(["ideviceinfo", "-u", self.udid, "-k", key])
        value = p.stdout.strip()
        if p.returncode != 0 or not value:
            raise SourceError(f"cannot read iPhone info ({p.stderr.strip()}); {_LOCKED_HINT}")
        return value

    def __enter__(self) -> IPhoneSource:
        mnt = Path(tempfile.mkdtemp(prefix="photo-backup-iphone-"))
        p = self._run(["ifuse", "-u", self.udid, "-o", "ro", str(mnt)])
        if p.returncode != 0 or not (mnt / "DCIM").is_dir():
            self._unmount(mnt)
            raise SourceError(f"could not mount the iPhone ({p.stderr.strip()}); {_LOCKED_HINT}")
        self._mnt = mnt
        return self

    def __exit__(self, *exc: object) -> None:
        if self._mnt is not None:
            self._unmount(self._mnt)
            self._mnt = None

    def _unmount(self, mnt: Path) -> None:
        self._run(["fusermount", "-u", str(mnt)])
        with contextlib.suppress(OSError):  # still mounted or not empty: leave it alone
            mnt.rmdir()

    @property
    def mount(self) -> Path:
        if self._mnt is None:
            raise SourceError("iPhone is not mounted (use the source as a context manager)")
        return self._mnt

    def list_files(self) -> list[SourceFile]:
        files = scan_tree(self.mount / "DCIM", prefix="DCIM/")
        mutations = self.mount / MUTATIONS
        if mutations.is_dir():
            files += scan_tree(mutations, prefix=MUTATIONS, name_filter=_RENDER_RE)
        files.sort(key=lambda f: f.path)
        return files

    def open(self, path: str) -> BinaryIO:
        return open(self.mount / path, "rb")

    def dest_path(self, path: str) -> str:
        if path.startswith(MUTATIONS):
            path = "Edited/" + path[len(MUTATIONS) :]
        return sanitize_relpath(path)
