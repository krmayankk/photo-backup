# photo-backup

Verified, append-only backups of phone photos and videos to external drives — with a
drive health gate before every write and a report of what is safe to delete from the phone.

```
iPhone ──ifuse (read-only)──▶ photo-backup ──verify-on-write──▶ Drive A ─┐
                                   │                                     ├─▶ status: "videos on 2 drives → SAFE TO DELETE"
                                   └─────────────────────────────────▶ Drive B ─┘
```

Built for one job: get a phone's camera roll onto two (or more) drives, **prove** every
byte landed, and only then free space on the phone. See [SPEC.md](SPEC.md) for the full
design.

## Guarantees

| | |
|---|---|
| **Never overwrites** | A different file at the same path is kept side by side as `name.conflict-N.ext`. |
| **Never deletes from a backup** | Deleting a photo on the phone never touches the drives. |
| **Verify on write** | Hash while reading → write `.partial` → fsync → drop page cache → re-read and hash → rename. |
| **Interrupt-safe** | Ctrl-C or unplugging leaves only `.partial` files; re-running resumes cleanly. |
| **Read-only sources** | The iPhone is mounted `ifuse -o ro`. |
| **Health gate** | SMART, kernel I/O errors, a write probe, free space, read-speed trend and a random re-hash of old backups — before anything is written. |
| **Loud failure** | Any mismatch or count difference → exit 1 and `RESULT: FAIL`. |
| **Two-copy rule** | `status` calls an item safe to delete only when ≥ 2 drives hold a verified copy. |

## How a backup run flows

1. **List & plan** — list every file on the source, classify it (photo / video /
   Live Photo clip / edit sidecar / rendered edit), and skip anything this drive already
   holds (same device, path, size and mtime).
2. **Gate** — run the health & sanity checks; any FAIL stops the run before a byte is written.
3. **Copy** — each file is copied with verify-on-write into
   `PhotoBackup/devices/<device>/<YYYY-MM>/files/`, then recorded in `manifest.tsv`,
   `SHA256SUMS` and the drive's catalog — in that order, so a crash is always recoverable.
4. **Reconcile** — per kind, `listed = skipped + copied + conflicts + duplicates + failed`;
   every copied file is re-checked on disk.
5. **Record** — `summary.json`, `run.log` and a catalog snapshot per run; the catalog is
   mirrored to `~/.local/share/photo-backup/` so `status` works with drives unplugged.

## Quick start

```bash
sudo apt install libimobiledevice-utils ifuse smartmontools
pipx install .            # or: python -m venv .venv && .venv/bin/pip install .

photo-backup drives                                   # what is attached
photo-backup init /media/$USER/MyDrive --label drive-a
photo-backup backup --from iphone --to drive-a --dry-run
photo-backup backup --from iphone --to drive-a --limit 20   # smoke test
photo-backup backup --from iphone --to drive-a              # the rest
photo-backup verify drive-a
# ...repeat for a second drive, then:
photo-backup status
```

Unlock the iPhone and tap **Trust** when you connect it. Folders and old disks work too:
`--from dir:/media/$USER/OldDisk --name old-disk`.

### Drive health (SMART)

Reading SMART needs root. Allow exactly one command without a password:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/smartctl" | sudo tee /etc/sudoers.d/photo-backup-smartctl
sudo chmod 440 /etc/sudoers.d/photo-backup-smartctl
```

Without it the gate FAILS (`--allow-unknown-health` turns that into a warning). Some old
USB enclosures do not pass SMART through at all. `photo-backup health <drive>` runs the
gate alone and shows the drive's history from `health.tsv`.

## What ends up on a drive

```
PhotoBackup/
  devices/iphone-iPhone15,4-5F6A/2026-11/
    files/DCIM/100APPLE/IMG_0001.HEIC     original bytes, original mtime
    files/Edited/DCIM/.../FullSizeRender.jpg   rendered edits from the Photos app
    manifest.tsv                          src_path dest_path size mtime sha256 kind status run_id
    SHA256SUMS                            cd files && sha256sum -c ../SHA256SUMS
    runs/20261122T140038/                 summary.json, run.log, catalog-snapshot.sqlite
  .photo-backup/                          drive.json, catalog.sqlite, health.tsv, lock
```

**Recovering without this tool:** everything is plain files. Copy `files/` anywhere;
check integrity with `sha256sum -c`; `manifest.tsv` maps every file back to its original
path on the phone.

## Commands

| Command | Purpose | Exit codes |
|---|---|---|
| `init <mount> --label L` | mark a drive | |
| `drives` | attached drives, free space, last run | |
| `health <drive>` | gate only + history | 0 ok · 2 fail |
| `backup --from iphone\|dir:<path> --to <drive>` | `--dry-run`, `--limit N`, `--allow-unknown-health`, `--force-unhealthy`, `--sample-pct` | 0 pass · 1 verify fail · 2 gate fail |
| `verify <drive>` | re-hash files vs catalog; `--device`, `--month`, `--sample-pct` | 0 pass · 1 damaged/missing |
| `status` | copies per item, safe-to-delete verdicts, per-file TSV | |

Usage errors (unknown drive, phone not connected) exit 3.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest            # unit + end-to-end tests (fake phone, temp drives)
.venv/bin/ruff check . && .venv/bin/ruff format --check .

# integration test on a real drive; writes only to a scratch folder it removes
.venv/bin/python tests/integration/real_drive.py /media/$USER/MyDrive
```

Python ≥ 3.10, standard library only. Linux.

## Roadmap

Android adapter (adb), `replicate` drive→drive, `prune` with typed confirmation,
`restore-test`, `rebuild-catalog` from manifests, iCloud-only-original detection via
`Photos.sqlite`. See [SPEC.md](SPEC.md).

## License

MIT
