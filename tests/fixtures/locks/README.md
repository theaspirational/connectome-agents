# Recorded schema cases

These anonymized, synthetic host lock records transcribe the fields written by
`src/lockfile.ts` and `src/backends/host.ts` at cook
`011d73bc6c6103e0124df80191885a6585409c56`. They are **not** claimed to be
captures of an executed compatibility build. Paths and digest/commit bytes are
neutral test placeholders. `v1` includes the legacy answer field; malformed v2
omits `build.dir`. The tests relocate paths but preserve the recorded schema.
Only `RealCookTests` runs the real CLI; an unavailable prerequisite skips it.

The fixture JSON files are unchanged by this repin: v2 records only
`cook.version`, not `cook.commit`, and v1 has no `cook` field. The
reviewed commit's source tree is
`b81301dd3d139b78647860643a24cfde723e56f1`. None of these fixtures contains the
optional `external` list; `tests/test_external.py` adds it to a copy.
