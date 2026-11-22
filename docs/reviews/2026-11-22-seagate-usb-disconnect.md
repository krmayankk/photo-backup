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

## Root cause

`sudo smartctl -a -d sat /dev/sda` on the idle, unmounted drive fails cleanly ("Read
Device Identity failed: scsi error unsupported field in scsi command") **without** a
disconnect.

Follow-up research (Claude Fable 5.1, web sources) explains why:

- **SMART is blocked by the kernel, not the drive.** Since Linux 4.15 the `uas` driver sets
  `US_FL_NO_ATA_1X` for *every* Seagate (VID `0x0bc2`) UAS enclosure ("All Seagate disk
  enclosures have broken ATA pass-through support") and answers ATA_12/ATA_16 commands
  locally with "invalid field in CDB". `-T permissive` and other `-d` types cannot help.
  [commit 7fee72d5](https://github.com/torvalds/linux/commit/7fee72d5e8f1e7b8d8212e28291b1a0243ecf2f1),
  [uas-detect.h](https://github.com/torvalds/linux/blob/master/drivers/usb/storage/uas-detect.h),
  [Ubuntu bug 1810215](https://bugs.launchpad.net/ubuntu/+source/linux/+bug/1810215).
- **So the gate's SMART probe never reached the bridge** and almost certainly did not cause
  the disconnect. Seagate UAS enclosures resetting under sustained load is widely reported
  (e.g. [Ubuntu bug 1584557](https://bugs.launchpad.net/ubuntu/+source/linux/+bug/1584557));
  the standard mitigation is to disable UAS for the device. Not confirmed for this exact PID.
- With UAS disabled (`usb-storage` quirk flag `u`), pass-through goes through the older
  usb-storage (BOT) driver and `smartctl -d sat` works (smartmontools drivedb lists
  `0bc2:ab24` "Seagate Backup Plus Slim" under `-d sat`). Cost: lower throughput.
  Seagate's `openSeaChest` (Ubuntu package `openseachest`) hits the same kernel block
  ([Seagate wiki](https://github.com/Seagate/openSeaChest/wiki/Enabling-Access-To-Additional-Commands-In-Linux)).

**Tool change:** the gate recognises the "unsupported field in scsi command" response,
reports SMART as *unknown* with this explanation, and does not retry with `-d sat`.

## Operator fix for this drive

```bash
# disable UAS for this enclosure only (now, and on every boot)
echo 0bc2:ab24:u | sudo tee /sys/module/usb_storage/parameters/quirks
echo "options usb-storage quirks=0bc2:ab24:u" | sudo tee /etc/modprobe.d/seagate-bup-slim-no-uas.conf
# replug the drive; `lsusb -t` should show Driver=usb-storage instead of uas
# clear the dirty flag: chkdsk /f on Windows (preferred) or `sudo ntfsfix -d /dev/sdX1`
sudo smartctl -a -d sat /dev/sdX          # SMART should now be readable
```

Then re-run `tests/integration/real_drive.py` with `--big-mb 2048` while watching
`journalctl -kf` before trusting the drive with backups.
