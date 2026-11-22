# Review 2026-11-22 — independent code & spec review (Claude Fable 5.1)

**Scope:** `SPEC.md`, `README.md`, `src/photo_backup/`, `tests/` at commit `7078a1e`
(first public version). Read-only review by a separate agent running Claude Fable 5.1
with no access to the author's reasoning; findings marked *reproduced* were confirmed with
throwaway scripts in temp dirs. All findings below were addressed in the `review-fixes`
branch; each has a regression test in `tests/test_safety_regressions.py` unless noted.

## 1. Data-loss / wrong-safety risks

| # | Finding | Status / fix | Test |
|---|---|---|---|
| 1.1 | `status` kept counting a copy as good after `verify` (or the gate's spot-check) found it damaged or missing → could say SAFE TO DELETE with only one real copy. *Reproduced.* | `verify` and the spot-check set `verified_at = NULL`; status only counts non-NULL; backup re-copies such files. | `test_damaged_copy_stops_counting_in_status`, `test_backup_recopies_a_damaged_file_and_status_recovers`, `test_gate_spot_check_marks_damage` |
| 1.2 | Temp files used a `.partial` suffix; cleanup `rglob("*.partial")` deleted real backed-up files with that name, and `open(..., "wb")` could truncate an existing `X.partial` when `X` was copied. *Reproduced.* | Temp files are `.pbtmp-<name>`, opened with `"xb"` (never truncate); cleanup skips every path in the catalog. | `test_source_files_named_like_temp_files_survive_reruns`, `test_existing_temp_file_is_never_truncated`, `test_remove_stale_partials_spares_real_files`, `test_leftover_temp_file_from_a_crash_is_cleaned` |
| 1.3 | Re-initialising a drive (new uuid, same label) made one physical drive count twice in `status`; a lost drive counted forever. *Reproduced.* | Mirrors are de-duplicated by label (newest wins); status warns about drives unseen for 180 days; new `forget <label>` command. | `test_reinitialised_drive_counts_once`, `test_forget_and_stale_drive_warning` |
| 1.4 | Verdicts were per kind: a photo could be "safe" while its Live clip / rendered edit was not backed up, but deleting the photo in the Photos app deletes those too. *Reproduced.* | `classify.asset_key` groups photo, clip, `.AAE` and rendered edit; an item is covered only if all its files are. | `test_photo_is_not_safe_while_its_live_clip_is_missing` |
| 1.5 | `manifest.tsv` / `SHA256SUMS` were flushed but not fsynced while the catalog was `synchronous=FULL`. | Both are fsynced per file before the catalog commit; a crash can leave at most one harmless duplicate line. Additionally (owner request) the files are now **proven append-only**: size + hash are sealed in the catalog after each run and the gate / `verify` FAIL if a file shrank or its prefix changed. | `test_resume_leaves_records_consistent`, `test_records_are_append_only_across_runs`, `test_in_place_changes_to_records_are_caught` |
| 1.6 | On NTFS/exFAT a case-variant duplicate recorded the source's case, not the on-disk name, breaking `sha256sum -c` after copying to ext4. | `NameIndex.existing` returns the real on-disk name. | `test_case_variant_duplicate_records_real_on_disk_name` |
| 1.7 | `POSIX_FADV_DONTNEED` may not force a device read on FUSE/exFAT. *Unconfirmed.* | Documented as best-effort in SPEC invariant 3. `O_DIRECT` considered for phase 2. | — |

## 2. Spec / code mismatches

| Finding | Resolution |
|---|---|
| SPEC promised `--rehash`, not implemented | Moved to phase 2 in SPEC. |
| SPEC said gate runs before listing | SPEC now: list → plan → gate (gate needs the planned byte count). |
| SPEC said manifest written at the end | SPEC now: appended + fsynced per file. |
| `verify --run/--sample` vs CLI `--month/--sample-pct`; report file name | SPEC fixed to match CLI and `status-<device>-<date>.tsv`. |
| Sanitized renames "logged" only in the manifest | Each rename is now printed and written to `run.log`. |
| Read-speed "up to 256 MB" | SPEC now describes the actual measurement (spot-check sample ≥ 64 MiB). |
| `OSError` / read-only lock file escaped as a traceback | CLI catches `OSError`, prints a one-line error, exits 1. |

## 3. Test gaps

All gaps listed in the review now have tests (see table above). `tests/integration/
real_drive.py` additionally exercises `status` and checks that a damaged copy flips the
verdict to NOT YET and back after repair.

## 4. Code quality

| Finding | Resolution |
|---|---|
| `free_name` listed the directory for every file (O(n²)) | Cached `NameIndex`, one listing per directory per run. |
| `FileRow(*row)` from `SELECT *` | Columns named explicitly from the dataclass fields. |
| `status` used list indices for totals | `_KindTotals` dataclass. |
| Two `datetime.now()` calls per row; handle leak in `_Records`; `noqa`s | One timestamp per row; `RunRecords` is a context manager that closes on partial init. |
| Docstring silent on manifest duplicate lines | `backup.py` and `records.py` docstrings explain it. |

## Added in the same change (owner requests, not review findings)

- Per-file retries (2 s, 5 s), stop after 10 consecutive failures ("source disconnected").
- Stall watchdog: warns when no bytes move for 3 minutes.
- Live one-line progress bar for `backup` and `verify` in interactive terminals.
