# Lock v2 contract and cook boundary

Source inspected offline: [the public cook fork](https://github.com/theaspirational/connectome-cook),
commit `28236dd51045c99604ddc7ba6a2eb0c02679fd3d` (public fork main,
tree `188047b296b03341926c22881a45f903861773a4`). In particular: `src/lockfile.ts`, `src/cli.ts`,
`src/backends/host.ts`, and the engine/layout/unit/dependency documentation.
This is a source compatibility target, **not** a claim of a completed live-host
qualification. The offline real-cook test reports whether prerequisites let it run.

## What cook emits and this layer consumes

| Field | Emission at inspected commit | Consumer contract |
| --- | --- | --- |
| `version` | emitted | exactly integer `2`; v1 and unknown versions refused |
| `backend` | emitted | exactly `host` |
| `cook` | `{version}` | package version; optional commit type exists but writer does not emit it |
| `recipe`, `recipePath`, `createdAt` | emitted | root path and exact-source SHA-256, original recipe path, timestamp |
| `build.id`, `build.dir` | emitted | 64 lowercase hex identity; absolute directory must equal requested fresh build |
| `connectomeHost` | emitted | URL/ref/path/actual full commit; commit must equal requested host pin |
| `components[]` | emitted | discriminated Git/npm provenance; adapter accepts only requested pinned Git MCP sources |
| `requirements[]` | emitted | exactly string `name`, `envName`, `origin`; no resolved answers or `value` |
| `credentials[]` | emitted | names only; conservative superset including provider alternatives and file-field names |
| `outputs[]` | emitted | path/mode, optional SHA-256; sensitive outputs have no hash; matched requirement answers use `sha256Omitted: contains-requirement-value` |
| `localExtensions[]` | emitted | source directory/entry inventory, not lifecycle state |
| `launch` | emitted | `{kind: script, script: absolute-path, userUnit?: absolute-path}` |
| `launch.userUnit` | emitted when exporting | contained `.service` file; adapter/role maps it to a path/SHA-256 unit record without rewriting the lock |
| `engines` | emitted with selected engines | Bun/Node path, realPath and observed version |
| `layout`, `state.path` | emitted with split layout | explicit roots/bindings and persistent state outside build |
| `hostDependencies` | emitted with local inputs | archive hash/version/size/snapshot and package.json policy delta |
| `incomplete` | emitted on interrupted/failed reconcile | always refused for deployment |
| `units[]` | **planned** deployment extension, not emitted | optional contained path/SHA-256 records; mutually exclusive with exported unit hint |
| `health` | **planned** deployment extension, not emitted | optional command argv; otherwise effective agent health |

Cook itself reads v1 and v2; **this layer accepts v2 only**. The shared reader
rejects missing required fields, invalid types, requirement answer fields,
credential objects/assignments, credential-bearing URLs, recognizable token/key
values, incomplete installs and unknown top-level fields. Errors never echo the
suspected value. Required secret names must be covered by credentials or
requirement env names. No generic string detector can prove an arbitrary string
is not a secret: trusted configuration/provenance must never contain literals.
The schema checks and isolated build environment are the primary boundary.

Cook's outputs are an explicit inventory, not a filesystem attestation.
`.env` is mode-only, never hashed. Build identity includes recipe bytes, transitive
recipe provenance and component pins/options, but excludes the new engine/unit/
layout/dependency extensions. The role's request identity includes the full
normalized configuration and adapter argv instead. Neither identity is rewritten.

## Production adapter

`scripts/cook-adapter.py` is the bundled adapter. Configure **all** executable
paths; there are no host-specific defaults:

```yaml
connectome_cook_command:
  - python3
  - /absolute/public/scripts/cook-adapter.py
  - --runtime
  - /absolute/bin/bun
  - --entrypoint
  - /absolute/cook/bin/cook
  - --bun-path
  - /absolute/bin/bun
  - --node-path
  - /absolute/bin/node
  - --spec
  - '{recipe}'
  - --out
  - '{build_dir}'
  - --state
  - '{state_dir}'
```

Use immutable absolute paths on the target (the command's CWD is the build).
The entrypoint must be a clean source checkout's `bin/cook`, at the requested
cook pin and with the reviewed tree. Install its dependencies separately; the
adapter does not bootstrap/download tools. Runtime must execute this source
entrypoint (normally Bun). Selected Bun and Node are separately passed to cook
and verified by cook against the acquired host's engine requirements.

The normalized input remains `{config: <merged/pruned agent>, pins: {runtime,
connectors}}`. The role's four-layer merge is unchanged. Supply these additional
nonsecret effective agent fields:

```yaml
agent:
  systemPrompt: Example agent instructions.
  provider: openai-codex
cook:
  host_repo: https://github.com/anima-research/connectome-host.git
  framework_archive:
    path: /absolute/inputs/agent-framework.tgz
    sha256: REPLACE_WITH_REVIEWED_ARCHIVE_SHA256
  connector_state: {}
```

The framework archive must be a cook-compatible local gzip/ustar npm archive,
with the framework package name and `package.json.gitHead` equal to the requested
framework commit. Supply a trusted, authenticated archive/hash pair; `gitHead`
is an artifact provenance assertion, **not source authentication**. Cook checks
its name/version against the host range and binds all same-name Bun lock entries
to the hash-pinned archive before recording `hostDependencies`. Other transitive
dependencies are not a frozen closure. Build tools and archives remain trusted.

Enabled connector descriptors need native cook MCP settings (`command`, `args`,
`env`, etc.); the public catalog's empty candidate settings deliberately fail
until supplied. `repo` and the resolved full commit become `source.url/ref`;
`build` argv lists are individually shell-quoted and joined with `&&` into cook's
custom install command. Each source has an explicit unique `/connectors/NAME`
materialization path. `pin_ref` must be `connectors.NAME`. Module `settings` map
to native host module settings (empty settings enable the module); source-backed
modules/extensions are refused rather than silently ignored. No catalog scan,
disabled import, fleet walk, inferred launch command, credential file or sidecar
is added. Budgets remain role request metadata, not a claimed upstream mapping.

The actual invocation is `RUNTIME ENTRYPOINT install RECIPE --out BUILD
--no-prompts --yes --strict --ch-repo REPO --ch-ref HOST_SHA --bun-path BUN
--node-path NODE --layout LAYOUT --host-dependency NAME=ARCHIVE#sha256=HASH
--user-unit connectome-NAME.service`.
The exported unit uses cook's headless launcher; only the role installs/activates it.
No `--env-file`, service start, enable or prompt is used. Full component refs
are supplied directly; `--pin-refs` is unnecessary. Only PATH/HOME/locale/temp
infrastructure variables reach cook; arbitrary inherited env, COOK_PASS_ENV,
preloads, Git overrides and secret values are excluded. Tool output is redacted.

Every explicit `required_secrets` name becomes an optional, unresolved cook
requirement with `exposeAs` (optional only at build time). Runtime secret env
settings must be `${NAME}`, with no literal or default. No secret provider is
called until the role validates the returned lock. The adapter refuses unexpected
nonempty generated `.env`, so cook's sourcing cannot override runtime secret
delivery. Operators must also keep literals out of prompts/settings/provenance.

Split layout binds host state/CWD to `{state_dir}` and workspace to its sibling
`workspace/`; per-request config and reserved empty credentials roots live under
sibling `cook-config/REQUEST` and `cook-credentials/REQUEST`. All are outside the
immutable build. Declare connector file state explicitly in `connector_state`;
other application paths are not guessed. Cook requires private, non-overlapping
roots with safe ancestors; consult its layout documentation. Runtime launches,
child Bun autoload and operator absolute overrides need separate audits. The
adapter checks returned layout, framework hash and exact component commits.

## Offline tests

Schema fixtures are recorded synthetic/anonymized cases, not claimed live-build
captures. Unit doubles test invocation separately. `RealCookTests` uses the exact
reviewed cook tree, real Git, real Bun/Node and real dependency installation for a
tiny all-local host/framework recipe; it never replaces those tools. Set
`CONNECTOME_TEST_COOK_SOURCE` to a local clone containing the reviewed commit,
`CONNECTOME_TEST_COOK_DEPS` to an already-installed cook `node_modules` directory,
and `CONNECTOME_TEST_BUN` to an installed Bun binary. Optional
`CONNECTOME_TEST_NODE` selects Node; discovery also checks PATH and
`/opt/node/bin/node`. Dependencies (including transitives) are copied, not
symlinked, into a clean temporary checkout at the pinned commit. Tests never
install cook dependencies or download runtimes. Run with a private 0700 `TMPDIR`
under non-group-writable ancestors owned by the current user (or root), not the
worktree hierarchy: cook rejects unsafe ancestors of split-layout roots.
Missing prerequisites produce an explicit unittest skip reason. Available
prerequisites followed by a failed build are a failure, not a skip. Unit scratch remains under the ignored worktree `.cache/`; the real-cook
checkout, empty HOME and split-layout roots use `TMPDIR` and are cleaned up.

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
Cook's conservative credentials superset must **not** cause lookup of every
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
