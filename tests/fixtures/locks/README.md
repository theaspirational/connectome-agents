# Recorded schema cases

These anonymized, synthetic host lock records transcribe the fields written by
`src/lockfile.ts` and `src/backends/host.ts` at cook
`7955a3132cb54ee975e5a672578cf8922abc6e22`. They are **not** claimed to be
captures of an executed compatibility build. Paths and digest/commit bytes are
neutral test placeholders. `v1` includes the legacy answer field; malformed v2
omits `build.dir`. The tests relocate paths but preserve the recorded schema.
Only `RealCookTests` runs the real CLI; an unavailable prerequisite skips it.
