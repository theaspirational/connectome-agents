# Recorded schema cases

These anonymized, synthetic host lock records transcribe the fields written by
`src/lockfile.ts` and `src/backends/host.ts` at cook
`28236dd51045c99604ddc7ba6a2eb0c02679fd3d`. They are **not** claimed to be
captures of an executed compatibility build. Paths and digest/commit bytes are
neutral test placeholders. `v1` includes the legacy answer field; malformed v2
omits `build.dir`. The tests relocate paths but preserve the recorded schema.
Only `RealCookTests` runs the real CLI; an unavailable prerequisite skips it.

The fixture JSON files are unchanged by this repin: v2 records only
`cook.version`, not `cook.commit`, and v1 has no `cook` field. The
reviewed public commit has the identical source tree
`188047b296b03341926c22881a45f903861773a4`.
