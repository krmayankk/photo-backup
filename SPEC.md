# photo-backup — spec

A small, boring, paranoid tool that copies photos and videos from phones (iPhone and
Android, old and new) and whole folders/old disks onto external drives **without ever
losing anything**, proves every copy with SHA-256, checks drive health before writing,
and tells you when it is safe to delete things from the phone.

Runs on Linux. Python ≥ 3.10, standard library only.

## Goals

1. Back up everything on a phone to **N ≥ 2** drives, organised by device and year-month.
2. Report which phone items (videos first) are verified on ≥ 2 drives, so they can be
   deleted on the phone to free space.
3. Copy whole folders / old disks onto new drives with the same guarantees.
4. Be repeatable: re-run any time; only new files are copied.
5. Never write to a drive that looks unhealthy.

## Non-goals

- **Deleting anything from a phone.** Deleting files via USB behind the iOS Photos app
  corrupts its library. The tool *reports*; the user deletes in the Photos/Gallery app.
- Viewing, editing, converting or content-level de-duplication. Bytes are copied as-is.
- Cloud sync.

## Hard invariants (each has a test)

1. **Never overwrite.** An existing destination path with different content → the new
   file gets a suffix (`IMG_1234.conflict-1.HEIC`); the conflict is logged.
2. **Never delete from a backup** except via an explicit command that lists what would
   be removed and requires a typed confirmation (phase 2).
3. **Verify on write.** Hash while reading the source → write `<name>.partial` → fsync →
   re-read from the destination and hash → rename only if hashes match.
4. **Interrupt-safe.** A crash/unplug leaves only `.partial` files; the next run removes
   them and redoes the work. Re-running is always safe.
5. **Sources are read-only.** iPhone is mounted `-o ro`; Android is read via `adb exec-out cat`.
6. **Loud failure.** Any mismatch, missing file or count difference → exit code 1 and a
   `FAIL` summary. Success is printed only when every check passed.
7. **Safe-to-delete = verified copy on ≥ 2 distinct drives.**

## Sources (device adapters)

| `--from`        | Transport | Reads | Notes |
|---|---|---|---|
| `iphone`        | `libimobiledevice` + `ifuse -o ro` | `DCIM/` (originals, Live Photo clips, `.AAE`) and `PhotoData/Mutations/**/FullSizeRender.*` (rendered edits) | Any iPhone that can pair with libimobiledevice (iOS 7 → current). Picks the only connected phone or `--udid`. |
| `android` *(phase 2)* | `adb` (USB debugging on) | `DCIM/`, `Pictures/`, `Movies/`, WhatsApp media folders; override with `--include` | Android 6+ (toybox `find`/`stat`). Until then, an MTP-mounted phone works via `dir:`. |
| `dir:<path>`    | local filesystem | everything under `<path>` | For old disks, folders, or an Android/camera mounted via MTP/gvfs (fallback for phones without adb). Skips `System Volume Information`, `$RECYCLE.BIN`, `.Spotlight-V100`, `.fseventsd`. `.Trashes` included unless `--skip-trash`. Requires `--name`. |

Each source has a stable **device name** used in the layout:
`iphone-<ProductType>-<last 4 of UDID>`, `android-<model>-<last 4 of serial>`, or the
`--name` given for `dir:`. Model/OS version are recorded in every run summary.

**Skip rule (re-runs):** a source file is skipped when this drive's catalog already has the
same device + source path + size + mtime with a verified hash. `--rehash` re-reads and
compares hashes instead (slow, paranoid).

## Drives

Any mounted filesystem (ext4, NTFS, exFAT, FAT32, APFS/HFS+ read-write drivers…).
A drive is initialised once with `photo-backup init <mountpoint> --label <name>`, which
writes `PhotoBackup/.photo-backup/drive.json` (uuid, label, created). Commands accept the
mountpoint path **or** the label (found by scanning `/media/$USER/*` and `/mnt/*`).

Filesystem limits are enforced per file before copying: FAT32 4 GiB max file size;
characters illegal on NTFS/exFAT/FAT (`<>:"\|?*`, trailing dot/space) are replaced and the
mapping logged; case-only collisions on case-insensitive filesystems get a suffix.

### Layout

```
<drive>/PhotoBackup/
  devices/
    iphone-iPhone15,4-5F6A/
      2026-11/                    # run month; contains only files NEW to this drive
        files/DCIM/100APPLE/...   # original source tree preserved
        files/Edited/...
        manifest.tsv              # src_path dest_path size mtime sha256 kind status run_id
        SHA256SUMS                # `cd files && sha256sum -c ../SHA256SUMS` works without this tool
        runs/<run_id>/
          summary.json            # counts/bytes per kind, gate results, PASS/FAIL, provenance
          run.log
          catalog-snapshot.sqlite
    old-goflex-disk/
      2026-12/files/<original folders>...
  .photo-backup/
    drive.json
    catalog.sqlite                # every file ever backed up here
    health.tsv                    # one row per gate run (trend over years)
    lock                          # one writer at a time
```

A second run in the same month appends to that month's `files/`, `manifest.tsv` and
`SHA256SUMS`, and gets its own `runs/<run_id>/` folder.
After each run the drive catalog is mirrored to
`~/.local/share/photo-backup/drives/<uuid>.sqlite` so `status` works with drives unplugged.

## Health & sanity gate (start of every write)

FAIL → refuse to write (override `--force-unhealthy`, logged). WARN → continue, reported.

| Check | How | FAIL | WARN |
|---|---|---|---|
| SMART | `sudo -n smartctl -j -a` (auto `-d sat` for USB bridges) | overall failed; pending or offline-uncorrectable sectors > 0 | reallocated > 0 or increased since last run; temp > 50 °C; power-on > 40 000 h |
| SMART unavailable | no smartctl / no sudo rule / bridge blocks it | FAIL unless `--allow-unknown-health` | — |
| Kernel log | `journalctl -k` since boot, lines for this block device | I/O / medium errors | USB resets |
| Writable | create + fsync + delete a probe file (catches dirty-NTFS read-only mounts) | not writable | — |
| Free space | vs planned bytes | < planned × 1.1 | < 10 % free after run |
| Read speed | read up to 256 MB of existing backup data | — | > 30 % below this drive's median |
| Spot check | re-hash a random sample of existing files (default 1 % capped at 2 GB) | any mismatch | — |
| Catalog | `PRAGMA integrity_check` | not ok | — |

`photo-backup health <drive>` runs the gate alone and prints the history.

## Commands

Phase 1 (iPhone + local folders → any mounted drive; enough to back up a phone to two
drives and free space):

```
photo-backup init    <mountpoint> --label <name>
photo-backup drives                                   # attached drives, label, free, last run
photo-backup health  <drive>
photo-backup backup  --from iphone|dir:<path> [--name N] --to <drive> [--dry-run] [--limit N]
photo-backup verify  <drive> [--device D] [--run YYYY-MM] [--sample PCT]
photo-backup status  [--device D] [--min-copies 2]    # safe-to-delete report
```

Phase 2: Android adapter, `replicate --from <drive> --to <drive>`, `prune` (typed confirmation),
`restore-test`, `rebuild-catalog` (from `manifest.tsv` files), iOS completeness check
(compare `Photos.sqlite` asset count to `DCIM` to detect iCloud-only originals),
`systemd-inhibit` wrapping, desktop notification.

**Exit codes:** 0 pass · 1 verification/count failure · 2 preflight/gate failure · 3 usage.

### `backup` flow

1. Resolve drive, take lock, run gate.
2. Connect source, list files, classify, plan (skip / copy) → print counts and GB by kind.
   `--dry-run` stops here.
3. Copy with verify-on-write; record each file in the catalog as it lands (resumable).
4. Reconcile: `listed = skipped + copied + conflicts + failed`, per kind; re-list the run's
   `files/` from disk and compare with the manifest.
5. Write manifest, SHA256SUMS, summary.json; mirror catalog; release lock; print PASS/FAIL.

### `status` output

```
iphone-iPhone15,4-5F6A   (last listed 2026-11-22)
  kind        files      GB   on>=2 drives   safe to delete
  video         165    20.3        165        YES (all)
  photo       3,610     8.8      3,610        yes (optional)
  live-clip   1,719     6.7      1,719        delete with its photo
  drives: goflex (2026-11-22 PASS)  seagate-1tb (2026-11-23 PASS)
  -> safe-to-delete-2026-11-23.tsv
```

## Classification

- `video`: `.mov .mp4 .m4v .3gp .mkv .avi .mts` with **no** same-stem photo in the same folder.
- `live-clip`: a `.mov` with a same-stem `.heic/.jpg` (iOS Live Photo). Never counted as `video`.
- `photo`: `.heic .heif .jpg .jpeg .png .avif .dng .gif .webp .tif .tiff`.
- `sidecar`: `.aae .xmp`. `edited`: anything from iOS `Mutations`. `other`: everything else.

## Repository quality bar

- `pyproject.toml`, `src/` layout, type hints, ruff-clean, no runtime dependencies.
- pytest suite with fake device adapters: every invariant, resume after a mid-copy
  crash, corruption detection, conflict suffixing, FS-limit handling, classification,
  gate decisions from recorded `smartctl -j` JSON, multi-drive `status`.
- GitHub Actions: ruff + pytest on Python 3.10–3.12.
- README: what it does, the flow, quick start, safety guarantees, recovering without the tool.

## Testing before trusting it with real data

1. **Unit tests** (`pytest`): invariants, classification, gate decisions, catalog, status.
2. **End-to-end tests** (`pytest`, temp dirs): full `backup → verify → status` with a fake
   phone, including an interrupted run, a corrupted destination and a same-name conflict.
3. **Integration on the real drive** (`tests/integration/run_real_drive.sh <mount>`):
   a synthetic source (random files incl. Live-Photo pairs, odd filenames, a 300 MB file)
   is backed up into a scratch folder on the real drive, verified, re-run (must copy 0),
   checked with plain `sha256sum -c`, then the scratch folder is removed.
4. **Phone smoke test:** `backup --from iphone --dry-run`, then `--limit 20` into the real
   drive, then `verify`.
5. Only then the full run, followed by `verify` and an independent `sha256sum -c`.

`--limit N` copies at most N new files (for smoke tests); later runs pick up the rest.

## Operational notes

- **iCloud:** deleting a photo on an iPhone with iCloud Photos on also deletes it in
  iCloud. To move to a new iPhone without a paid plan use Quick Start (phone-to-phone),
  or Settings → General → Transfer or Reset → "Prepare for New iPhone" (temporary free
  iCloud storage).
- **Amazon/Google Photos:** deleting on the phone does not delete there; but free tiers
  may not keep full-quality video, so the drives are the real video backup.
- **Shared albums / "Shared with You"** are not on the device filesystem; out of scope.
- **HEIC on Windows** needs Microsoft's free "HEIF Image Extensions".
- **Drive buying:** for cold backups prefer HDD (cheap per TB, retains data unpowered);
  power each drive up and run `verify` a few times a year. Keep one copy off-site.
