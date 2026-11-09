# Lock v2 contract and cook boundary

Source inspected offline: `anima-research/connectome-cook` PR #14,
`src/lockfile.ts` and `src/lockfile-v2.test.ts` at commit abbreviation `67112f6c`,
and the lifecycle RFC at `fb14e2be`. No upstream compatibility build was run.
The pinned default-branch cook is not asserted to contain that PR.

## What this layer consumes

| Field | Status in inspected #14 | Consumer contract |
| --- | --- | --- |
| `version` | emitted | exactly `2`; v1 and unknown versions refused |
| `backend` | emitted | exactly `host` |
| `build.id` | emitted | 64 lowercase hex, cook's input-derived identity |
| `build.dir` | emitted | absolute materialization directory, must equal the requested fresh directory |
| `connectomeHost.commit` | emitted on host | equals requested full host pin |
| `requirements[]` | emitted | records with exactly string `name`, `envName`, `origin`; never `value` |
| `credentials[]` | emitted | string names only; conservative superset including non-secret/alternative names |
| `launch` | emitted | `{kind: "script", script: "..."}`; script must exist within build directory |
| `units[]` | **planned**, not emitted | optional `{path, sha256}`; relative/contained `.service` paths and matching SHA-256; otherwise render a generic unit for `launch.script` |
| `health` | **planned**, not emitted | optional `{command: [executable, arg, ...]}` after `systemctl --user is-active`; fallback to effective agent `health` |
| `state.path` | optional type, **planned emission** | persistent location within the agent root, outside `build.dir`; absent in #14 tests; adapter receives an explicit state directory |

The inspected source and tests actually emit `build.id/dir`; these are **not**
planned in that revision. Neither the RFC's prospective fields nor optional
TypeScript fields alone are evidence of emission. This layer does not invent
units/health/state fields in real locks. The optional units/health interpretation
above is an explicitly **planned deployment extension**, not a claim of an
upstream schema already agreed. Unknown lock fields are ignored.

Other emitted fields (`cook`, `recipe`, `components`, `outputs`, `createdAt`,
`localExtensions`, etc.) remain upstream provenance and are not lifecycle state.
This layer does not hash secret env files or resolve lock requirement values.
The adapter must verify cook/framework/component provenance; this layer verifies
the host pin and validates all supplied runtime/active connector pins before
invocation, but cannot independently attest an arbitrary installed executable.

## Input, invocation and identities

`connectome_cook_command` is an argv array, with Python format placeholders:

- `{recipe}`: private normalized `deployment.json`, not a stock cook recipe;
- `{build_dir}`: a newly created empty request build directory (apart from spec);
- `{state_dir}`: persistent agent state, outside the build directory.

Example wiring (the adapter is deliberately private, not a claimed stock CLI):

```yaml
connectome_cook_command:
  - python3
  - ./cook-adapter.py
  - --spec
  - '{recipe}'
  - --out
  - '{build_dir}'
  - --state
  - '{state_dir}'
```

Use an absolute adapter path in actual inventory: its working directory is the
build directory. Literal braces in command arguments must be doubled.

The JSON spec is `{config: <merged/pruned agent>, pins: {runtime, connectors}}`.
Adapter obligations:

1. Use the exact pinned cook, host, framework and connector commits. Never scan
   the unfiltered catalog, build disabled entries, or import disabled modules.
2. Translate descriptor `repo`, `build` argv lists, `settings`, and modules to
   that cook version's recipe/host interface. Public connector build commands
   are candidates and have not been verified against upstream source offline.
3. Run cook's one-shot host materialization in the supplied build directory;
   do not start or enable services. Emit its real `connectome.lock` v2 there.
4. Bind all mutable runtime data to `{state_dir}`, not to a build. Do not pass
   actual secret values to cook. Runtime secret names must appear in lock
   requirements/credentials and be explicitly declared in `required_secrets`.
5. Leave `launch.script` runnable within the build. If it sources an upstream
   `.env`, it must not override the deployment-provided environment.

An unset command fails closed. This is an explicit integration boundary because
stock recipe/CLI changes and PR-only lock output cannot be verified offline.
The reusable lifecycle is complete at this boundary; a production cook adapter
and compatibility run are still required. The test stub implements the boundary
without network access, but is not a production cook implementation.

Directory names use a **deployment request ID**: SHA-256 of the canonical merged
spec, resolved pins and adapter argv. That is separate from cook's `build.id`,
which includes cook-specific options and transitive recipe provenance. Both IDs
are retained; the layer never rewrites cook's identity. A validated `.prepared`
marker allows repeated identical requests to reuse manifest-owned builds. Unknown
pre-existing request directories are refused unchanged, even with a matching
marker. Only manifest-owned incomplete, inactive builds without tracked installed
unit files are recursively discarded and materialized fresh. Changing pinned adapter
content requires changing its immutable path/argv (or another request input).

## Secret and unit delivery

The explicit `required_secrets` name set is verified against the union of lock
`credentials` and `requirements[].envName`. Only this explicit set is fetched;
#14's conservative credentials superset must **not** cause lookup of every
alternative name. Missing declared names fail before activation. Declare any
runtime-level secret in the agent's `required_secrets`, not just connectors.
The provider result must have exactly this set, no extra keys.

Each build's `secrets.env` is atomically written mode 0600 within private build
storage. Values are double-quoted, with quote/backslash escaping; multiline,
carriage-return and NUL values are refused. They are environment values, not
shell assignments to execute. A private drop-in adds this env file to every
managed unit. Unit links go into the configured systemd user directory;
unmanaged units are neither overwritten nor disabled/deleted when retired.
The ownership preflight covers all manifest entries and new/retired unit paths
before changing the env file, current pointer or service manager. Exact ownership,
not merely containment in `builds/`, is required. No state lives under `current`.

Activation switches `current` atomically, reloads the manager, enables/restarts
managed units, then checks `is-active` and the optional argv health command.
On failure the previous pointer, unit set and env reference are restored and
restarted. No previous build is rebuilt or deleted. Successful identical
requests with identical env/unit links do not rebuild or restart, but do recheck
health. Secret rotation restarts without changing the build identity.


## Ownership and threat model

Run deployment as the agent's own user, in its own directories. Hostile config
values/names, path escapes and pre-existing files/links not created by the role
are in scope. Same-uid concurrent modification (including directory swaps during
deployment) is out of scope, as with cook. Preflight checks are pathname checks,
not fd/openat race hardening. Adapters, executable health commands and runtime
code are trusted code, not sandboxed inputs.

`state/ownership.json` is a private, atomically replaced version-1 manifest:
`{version: 1, entries: [{path, kind, ...}]}`. Paths are absolute;
links record the exact, un-resolved target string and regular files record their
SHA-256 content hash (`kind: link, target: ...` or `kind: file, sha256: ...`).
Each build directory created by prepare is recorded as `{path, kind: directory}`
before invoking cook; directory entries are restricted to direct request-ID
children of `builds/` and verify a real directory, not its contents. It also
records rendered unit files, unit-directory links, enablement/alias links,
env drop-ins and `current`. Cook-supplied unit sources are validated against lock
digests and copied to `managed-units/` inside the build; these installed copies
and their links are recorded, rather than adopting cook output files. Old
installed unit files are retained and remain tracked.
Secret env contents are not hashed into this manifest.

Every recorded artifact must still exist with the recorded type and exact
content/target before prepare or activation, including rollback, retirement and check mode.
Unknown pre-existing request build directories and files/links at installed unit,
drop-in, enablement/alias and `current` destinations are never adopted, even if
they match the desired content/target. Unknown build directories are left unchanged
on refusal. A link into a never-installed file under `builds/` grants no ownership.
Base/root, build and state directories are checked for symlink aliases and
non-directory parents before prepare writes or cleanup. Unit directories,
drop-in directories and other artifact parents must not be symlinks, including
aliases wholly contained within the unit tree.
The full preflight runs before env, pointer or service-manager changes. Engine
ownership errors are exposed by the Ansible boundary without echoing values.

Enablement is explicit: the role creates and records links for the supported
`[Install]` subset `WantedBy`, `RequiredBy` and `Alias` (simple unquoted target or
service names, no specifiers). Other Install directives/syntax fail closed.
It does not invoke `systemctl enable/disable`, which can mutate untracked links.
Retirement stops only owned units and removes only their recorded matching links
and drop-ins; unrelated aliases and files are left alone. Empty real artifact parent
directories may be removed.

This is not blanket preservation of every pre-existing file or directory. An
owned incomplete build may be recursively deleted, including untracked files
added there; active builds and builds containing manifest-owned unit files are
not rebuilt. Within owned builds, cook outputs, `deployment.json`, `.prepared`
and `secrets.env` have no per-file ownership protection. The serialization lock
and persistent state (apart from manifest validation) are not tracked artifacts;
trusted cook/runtime code may modify state. Existing real unit/drop-in/enablement
parent directories can be reused and removed when empty after retirement, even
if the role did not create them. The manifest itself is validated as a regular
file with a valid schema, not authenticated against an external ownership record.

The manifest is updated atomically after each artifact change, so normal
activation failure can restore the previous unit set using the same ownership
rules. This is not a crash-atomic multi-file transaction: interruption between
an artifact write and manifest update can require manual recovery. Missing,
modified or corrupt manifests/artifacts fail closed. There is no automatic
adoption/migration for older deployments without a manifest or a build-directory
entry. Stop the affected agent and have an operator inspect, back up and remove legacy/conflicting
artifacts before a fresh deployment; do not erase a manifest to bypass drift.
Never edit a live deployment concurrently. Separate agents must use distinct
roots and unique unit names.
