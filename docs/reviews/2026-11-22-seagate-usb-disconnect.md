# Incident 2026-11-22 — USB drive dropped off during the real-drive integration test

**Drive:** Seagate Backup Plus Slim 1 TB, USB bridge `0bc2:ab24` ("BUP Slim SL"),
`uas` driver, NTFS (`ntfs3`), Ubuntu 24.04 / kernel 6.8.
**What ran:** `tests/integration/real_drive.py` with the `review-fixes` branch.

## Timeline

| Time | Event |
|---|---|
| 14:34:56 | Integration step 1 (backup, 37 files / 323 MiB incl. a 300 MiB file) → PASS. Gate SMART: "not readable through this USB enclosure". |
| 14:35:1x | Step 2 (`verify`) → PASS. |
| 14:35:15.356 | Step 3 starts a new backup run. Kernel: `[sda] FAILED Result: hostbyte=DID_NO_CONNECT`, write and read `I/O error`s, `usb 4-1: USB disconnect`. |
| 14:35:15.38 | `ntfs3: sda1: Mark volume as dirty due to NTFS errors`. |
| 14:35:15.75 | Drive re-enumerates as `sda` and logs `[sda] Attached SCSI disk`. Remount refused: `volume is dirty and "force" flag is not set`. |
| 14:35:15 | Gate spot-check hits I/O errors → marks the sampled rows damaged → `catalog.commit()` raises `sqlite3.OperationalError: disk I/O error` (traceback). |

## What the tool did right

- The spot-check refused to trust unreadable files and marked them damaged (safe direction).
- No backup data was involved: only the scratch folder of the integration test.

## What it did wrong → fixed

| Problem | Fix |
|---|---|
| Disk vanishing mid-run surfaced as a Python traceback. | CLI catches `sqlite3.OperationalError` like `OSError` and prints a "drive I/O error — unplugged / dropped off USB?" message, exit 1. |
| `kernel-log` only looked after the *last* `Attached` line, so a drop-off + re-attach hid the errors. | Errors since the last attach FAIL; errors earlier in the same boot WARN ("dropped off USB?"). Regex also matches `device offline error` and `DID_NO_CONNECT`. Test replays the real log lines. |
| Integration script printed "removed scratch folder … exists now: False" when the drive was gone. | It now checks the mount and says the folder was NOT removed. |

## Root cause (not yet confirmed)

`sudo smartctl -a -d sat /dev/sda` on the idle, unmounted drive fails cleanly ("Read
Device Identity failed: scsi error unsupported field in scsi command") **without** a
disconnect, so the SMART probe alone does not reset the bridge. Leading hypothesis: the
known instability of some Seagate UAS bridges under sustained load on Linux. Next steps:
disable UAS for this device only (`usb-storage` quirk `0bc2:ab24:u`), clear the dirty flag
(`chkdsk /f` on Windows, or `ntfsfix -d`), re-run the integration test with a 2 GiB file
while watching `journalctl -k`. Research into SMART access for this bridge is ongoing.
