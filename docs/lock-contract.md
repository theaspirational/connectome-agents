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
| `hostDependencies` | emitted with local inputs | archive hash/version/size/snapshot and package.json policy delta; adapter adds closed provenance for list/legacy-marked inputs |
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

The framework archive must be a cook-compatible **local** gzip/ustar npm archive,
pinned by the required SHA-256. When `package.json` contains `gitHead`, it must
match the requested framework commit, including for registry releases. A present
but empty, null or mismatched `gitHead` never falls back to release identity.
Without `gitHead`, the default still refuses. For already-provisioned tarballs
that omit it, the historical flag below remains accepted in a private/group/agent
layer. Despite its legacy spelling, it now maps to **artifact**, never a registry
claim (it provides neither registry origin nor registry integrity):

```yaml
cook:
  framework_archive:
    path: /absolute/inputs/agent-framework.tgz
    sha256: REPLACE_WITH_REVIEWED_ARCHIVE_SHA256
    registry_release: true
    name: "@animalabs/agent-framework"
    version: '0.32.0'
```

`registry_release` must be the boolean `true`, with both expected `name` and
concrete `version`. They must exactly match the archive's package.json, and the
name must be the framework package. This declares an **operator-supplied prebuilt
payload, verified out of band**. It does **not** verify a registry origin or the
commit by other means: the archive is pinned by its hash, not authenticated as that commit. No
registry lookup, Git fetch or other acquisition is performed by this check.
Supply a trusted, authenticated archive/hash pair; even a matching `gitHead` is
an artifact assertion, **not source authentication**.

The compatibility plan field `framework_archive` and the role-owned
`BUILD/framework.lock.json` record schema `connectome.framework-archive/v1`,
SHA-256, package name/version, `requested_commit`, `registry_release` and
`commit_verifiable`. Absent gitHead is explicitly `commit_verifiable: false`;
the requested pin is never presented as verified. Here `registry_release` is
only the historical config flag, not an origin assertion. The sidecar remains
compatible; normalized plan `host_dependencies` and new lock provenance label
the marked input `kind: artifact`, `commit_verifiable: false` and
`payload: operator-supplied prebuilt payload, verified out of band`. The operator
is responsible for out-of-band verification. The role itself checks only local
bytes and manifest declarations, without authenticating their source. Prepared builds validate
it against the request and cook's dependency hash/name/version; retained builds
do not need the original input archive. Legacy gitHead-only builds without a
sidecar remain usable, with plan provenance `not_recorded_legacy_build`; legacy-
marked builds require the sidecar. Change the immutable adapter path/argv to
materialize a new request after upgrading the adapter.

### Additional or generic host inputs

`cook.host_dependencies` is an optional **replacement-merged list** of closed
records. It can supplement `framework_archive`, or replace that legacy field
with an explicit framework record. Unknown keys and duplicate package names
(including a duplicate of the legacy framework input) are refused before any
subprocess. Each record requires `name`, `archive`, `sha256`, `kind`, and
`version`; only `git_commit` also requires `commit`. Only `registry_release`
requires `registry_url` and `registry_integrity`; these keys are forbidden for
other kinds. Names must be lowercase,
URL-safe npm names (scoped names supported); versions must be concrete, not
ranges. Archives are absolute local regular files, never symlinks. No registry
lookup, download, pack, build, extraction or lifecycle script is added by the
adapter. Cook's existing install policy applies; see the script policy below.
This is not a new script sandbox or a frozen transitive dependency closure.

For example, retain the legacy framework declaration above and add:

```yaml
cook:
  host_dependencies:
    - name: "@animalabs/context-manager"
      archive: /absolute/inputs/context-manager-prebuilt.tgz
      sha256: REPLACE_WITH_REVIEWED_ARCHIVE_SHA256
      kind: artifact
      version: '1.0.0'
```

Kinds have deliberately different provenance claims:

- `git_commit`: requires a nonzero full lowercase 40-hex `commit` and exactly
  matching package.json `gitHead`; records `commit_verifiable: true`.
- `registry_release`: additionally requires a credential-free HTTPS registry
  **origin** URL (no path except `/`, query or fragment) in `registry_url`, and a
  canonical SRI `sha512-BASE64` digest in `registry_integrity`. The local bytes
  must match both configured SHA-256 and SHA-512. Plan and lock record
  `registry_comparison: bytes were not compared against the registry`. This is
  operator-supplied registry metadata, not evidence of an online comparison or
  a Git commit claim. No registry connection is made.
- `artifact`: **operator-supplied prebuilt payload, verified out of band**, not
  source acquisition or a build performed by the adapter. The operator must
  obtain/build and authenticate it separately; the role cannot attest that
  out-of-band verification itself occurred.

Both release and artifact allow absent `gitHead` and record
`commit_verifiable: false`, even if the manifest contains a syntactically valid
40-hex gitHead. Empty/null/malformed present gitHead is refused. `commit` is
forbidden for these kinds. Every archive must match the declared SHA-256 and
package.json name/version. The legacy framework rules above remain unchanged,
including checking a legacy-marked artifact's present gitHead against the runtime
framework pin.
The generic framework `git_commit` declaration's explicit commit is its authority;
`versions.yml` remains required for compatibility but does not override that
explicit declaration.

Plans expose all resolved inputs as `host_dependencies` when the list or the
legacy release mark is used.
Each cook v2 `hostDependencies` entry retains cook's original range, version,
hash, snapshot, size and policy delta, and gains a closed `provenance` record:
the exact configured fields plus `commit_verifiable`, and, for `artifact`,
`payload: operator-supplied prebuilt payload, verified out of band`. Registry
records also retain the exact origin, expected integrity and no-comparison statement. A legacy framework combined with
the list is normalized into the same inventory (its inferred version and
sidecar attestation are retained). List-enabled and legacy-marked builds add this metadata
to cook's lock; cook's build identity and generated output digests are not
rewritten. Unmarked legacy-only configs preserve cook argv and lock bytes exactly
(the baseline compatibility test checks both byte-for-byte). The historical
marked configs keep the same argv, but new locks gain the artifact provenance.

The lock reader cross-checks the complete ordered inventory and provenance
against the saved config: missing, extra, duplicate, reordered, changed identity,
hash, kind, archive path, commit, registry origin/integrity or provenance records
fail closed. Prepare,
activation, retained-build plans and rollback all use this validation. Retained
builds use the recorded inventory/legacy sidecar, not the original archive
paths, so removing provisioned input archives does not break reuse. Existing
legacy-only gitHead builds keep their documented sidecar compatibility.

#### Install script policy (reviewed cook tree)

The role preserves cook's install script policy; it does not invoke a separate
package installer, `npm pack`, or archive lifecycle/build command. In this
adapter cook runs the selected Bun executable as `bun install` for the host
checkout. It does **not** pass a global `--ignore-scripts`: host/root lifecycle
scripts and Bun's ordinarily trusted/default-trusted transitive dependencies
remain subject to Bun's existing policy. Do not treat the host pin as untrusted
code or infer that all scripts are blocked.

For each selected local input cook refuses a host `trustedDependencies` entry
naming that input (`E_HOST_DEPENDENCY_TRUSTED`) before host installation; malformed
trust lists also refuse. For other inputs Bun applies its existing trust rules:
untrusted packages' lifecycle scripts remain blocked, while Bun's default trust
policy is not replaced. The real-cook synthetic packages exercise a blocked
postinstall marker and a trusted-input refusal. Connector descriptor `build`
argv are still quoted into cook's explicit custom shell build commands: they
are trusted operator code, not archive lifecycle opt-ins.

The closed dependency and cook schemas provide **no script-enabling options**;
`allow_scripts`, `ignore_scripts`, `scripts`, `trustedDependencies` and other
unknown options are refused. The adapter never adds `--trust`, changes a host
trust list or relaxes cook's trusted-input refusal. This refuses config opt-ins
for local dependency lifecycle scripts without pretending to sandbox cook's
existing trusted host/connector execution.

Cook also checks name/version against the host range and binds all same-name Bun
lock entries to the hash-pinned archive before recording `hostDependencies`.
Other transitive dependencies are not a frozen closure. Build tools and archives
remain trusted.

Enabled connector descriptors need native cook MCP settings (`command`, `args`,
`env`, etc.); the public catalog's empty candidate settings deliberately fail
until supplied. `repo` and the resolved full commit become `source.url/ref`;
`build` argv lists are individually shell-quoted and joined with `&&` into cook's
custom install command. Each source has an explicit unique `/connectors/NAME`
materialization path. `pin_ref` must be `connectors.NAME`. Module `settings` map
to native host module settings (empty settings enable the module); source-backed
modules/extensions are refused rather than silently ignored. No catalog scan,
disabled import, fleet walk, inferred launch command, credential file or sidecar
is added. Nonempty step/cost budgets are refused because this host recipe schema
has no corresponding fields; see [state and budgets](layering.md#external-runtime-state-and-budgets).

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

Agent-level `state_dir` is required, canonical and absolute, outside the deployment
root/builds, with no symlink aliases. It need not be under HOME. Existing state
must be mode 0700 owned by the agent account; the role creates only a missing
directory and never repairs, moves, deletes or writes its contents. Its separate
bookkeeping remains ROOT/state/ownership.json. Rebuild and rollback never touch
runtime data. Trusted cook/runtime code can still modify state.

Split layout binds host state/CWD to `{state_dir}` and workspace to ROOT/workspace; per-request config and reserved empty credentials
roots live under ROOT/cook-config/REQUEST and ROOT/cook-credentials/REQUEST.
The reviewed `run.sh` explicitly changes to `layout.state` before exec, regardless
of its initial cwd. Thus cwd-relative `mcpl-servers.json` and agent overlay files
resolve in `state_dir`. HOME supplied by a separate private runtime
`EnvironmentFile=` passes through unchanged: no unit or launcher HOME assignment
is emitted. This does not make HOME a supported cook requirement/secret name;
see [runtime HOME and cwd](layering.md#external-runtime-state-and-budgets).
The adapter output must be ROOT/builds/REQUEST. The external state directory's
parent need not be writable by the agent account. All are outside the
immutable build. These layout paths must be private, distinct per agent and non-overlapping.
Declare connector file state explicitly in `connector_state`;
other application paths are not guessed. Cook requires private, non-overlapping
roots with safe ancestors; consult its layout documentation. Runtime launches,
child Bun autoload and operator absolute overrides need separate audits. The
adapter checks returned layout, framework hash and exact component commits.

## Safe failure diagnostics

The adapter emits only a closed JSON error envelope on stderr: `error_code`,
`msg` (code plus a fixed message), and optionally `cook_error_code`. The prepare
Ansible task displays that code/message directly, for example:
`E_ARCHIVE_GITHEAD: Archive gitHead is missing, invalid or differs from the requested commit.`
Raw stderr/stdout, exception text, argv and config values are never relayed.
Failure results also suppress Ansible's implicit invocation and debug traceback.
Unknown/malformed subprocess output becomes `E_COOK_FAILED`, not a guessed
explanation. CLI argument errors also use a fixed envelope without echoing argv.

| Code | Meaning / operator action |
| --- | --- |
| `E_ARCHIVE_GITHEAD` | Missing, malformed or mismatched gitHead; inspect kind and commit declaration. |
| `E_ARCHIVE_IDENTITY` | Invalid declaration or package name/version mismatch. |
| `E_ARCHIVE_HASH` | Archive SHA-256 check failed. |
| `E_ADAPTER_INPUT` | Invalid adapter input or unavailable local prerequisite. |
| `E_LOCK_INVALID` | Cook lock, dependency inventory or framework provenance failed validation. |
| `E_LOCK_STATE_PATH` | Returned persistent state/layout differs from the request. |
| `E_ANCESTOR_WRITABLE` | Existing group/other-writable ancestor; the deployment result names `ancestor_path`. No chmod or writes occur. |
| `E_SYSTEMD_PATH` | Use literal absolute paths without whitespace, specifiers, globs or escapes. |
| `E_ENVIRONMENT_FILE` | Manager did not accept the required non-optional environment file. |
| `E_UNIT_ACTIVE` | Install-only would replace an active/transitioning unit; names `unit` and `scope`. Stop it explicitly first. |
| `E_UNIT_EXCLUSIVE` | Start conflicts with a listed unit; names `unit` and `scope`. Stop the old instance explicitly first. |
| `E_SECRETS_MODE` | Require owned directory 0700/file 0600 without symlinks; never repaired automatically. |
| `E_BUDGETS_UNSUPPORTED` | Remove nonempty budgets explicitly; no enforcement is implied. |
| `E_COOK_FAILED` | Child command failed; raw output remains redacted. |
| `E_DEPLOY_FAILED` | Other deployment failure; retained/restored where possible. |

For `E_COOK_FAILED`, a typed code printed by the reviewed cook is included only
if it belongs to the literal allowlist in `connectome_errors.py` (host engine,
layout, unit, private-directory and dependency codes). Only the code survives, never its surrounding
message, paths or values. Ansible accepts only the exact closed envelope and
regenerates the diagnostic locally; tool output cannot supply arbitrary messages.
The deployment's local ancestor preflight is the one path-bearing exception:
`E_ANCESTOR_WRITABLE` adds `ancestor_path`, a directory inspected locally with
lstat, not a path or message supplied by a subprocess. Existing writable ancestors
(including sticky writable directories) are refused, not chmodded. All directories
the role creates receive exact modes independent of umask: missing ancestors
0755, private roots/builds/state/bookkeeping and artifact parents 0700. Existing
directories are never chmodded. This preflight also runs in check mode and before
the Ansible boundary creates its serialization root/lock.
Activation and provider tasks remain `no_log` to protect secret-bearing inputs.

## Offline tests

Schema fixtures are recorded synthetic/anonymized cases, not claimed live-build
captures. Unit doubles test invocation separately. `RealCookTests` uses the exact
reviewed cook tree, real Git, real Bun/Node and real dependency installation for a
tiny all-local host recipe, covering matching gitHead, explicit registry releases
without gitHead, and two dependencies (framework plus a synthetic context-manager
prebuilt artifact), through both combined legacy and generic-only declarations; it never replaces those tools. Set
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
prerequisites followed by a failed build are a failure, not a skip. Schema/adapter
unit scratch remains under the ignored worktree `.cache/`.
The private-HOME integration case checks the installed cook unit and all test
drop-ins for HOME assignments, then executes the real exported launcher with a
synthetic private EnvironmentFile's HOME (including a space). Its tiny local host
records the observed HOME, cwd and DATA_DIR; both the normal unit cwd and a
different initial cwd must yield `layout.state`. EnvironmentFile delivery is
simulated, not qualified against a live user manager. Cook source is not patched.
Deployment lifecycle fixtures use private `TMPDIR` (default: the account home),
not group-writable worktree ancestors. Real-cook checkout, empty HOME and
split-layout roots also use `TMPDIR`; all temporary fixtures are cleaned up.

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
The provider result must have exactly this set, no extra keys. Default delivery
reads only these names on the target from the agent account's
`~/.config/connectome-secrets/NAME.json`; an agent-level absolute `secrets_file`
override is shown in check plans. Before start, directory 0700/file 0600, uid and
non-symlink checks fail closed without repair. See [secret providers](layering.md#secret-providers).

The adapter previously emitted `requirements: {}` even without secrets; it now
omits that empty cook-only key, eliminating the host's ignored-key warning in
no-secret deployments. With declared secrets it must retain nonempty cook
`requirements` declarations (`required: false`, no values) so cook records even
runtime-only secret names in the lock. The reviewed cook's `lowerToConfiguration`
preserves this build-only metadata, so the host may still warn in that case. It
is ignored at runtime; the role enforces the required names and supplies values.
The adapter does not patch cook output or rewrite its lock identity/digests to
silence that upstream lowering limitation.

Each build's `secrets.env` is atomically written mode 0600 within private build
storage. Values are double-quoted, with quote/backslash escaping; multiline,
carriage-return and NUL values are refused. They are environment values, not
shell assignments to execute. A private drop-in adds this env file to every
managed unit with a bare absolute `EnvironmentFile=` path (no quotes).
Path directives are not shell commands: the role refuses whitespace, quotes,
backslashes, `%` specifiers, wildcard syntax and other ambiguous characters
instead of escaping; accepted paths use only ASCII letters/digits and `_./:@+-`.
`Environment=DATA_DIR=...` and fallback unit paths use these same literal paths.
Unit links go into the configured systemd user directory;
unmanaged units are neither overwritten nor disabled/deleted when retired.
The ownership preflight covers all manifest entries and new/retired unit paths
before changing the env file, current pointer or service manager. Exact ownership,
not merely containment in `builds/`, is required. No state lives under `current`.

Activation switches `current` atomically, reloads the manager, enables/restarts
managed units, then checks `is-active` and the optional argv health command.
When the build declares required secrets, each unit must also report the exact
`secrets.env` path with `ignore_errors=no` via
`systemctl --user show -p EnvironmentFiles --value UNIT`. This checks the loaded
manager state after reload/restart, not just on-disk text; missing/rejected files
or failed inspection fail health and trigger rollback. Identical-request health
checks and rollback health use the respective build's declarations too. This
proves acceptance, not application-level consumption of each secret. The directive
and `show` semantics were checked against local systemd.exec(5), systemctl(1)
and org.freedesktop.systemd1(5) (systemd 255).
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

Every recorded artifact must still match its recorded type and exact
content/target before prepare or activation, including rollback, retirement and
check mode, with one narrow absence exception: `systemctl disable` may remove
recorded unit/enablement symlinks under unit_dir. Apply recreates those absent
links without rebuilding; update/rollback also tolerate them. Check mode reports
the repair as a change without writes. A replacement file, dangling/retargeted
link, missing drop-in/generated regular unit or missing `current` still fails
closed; nothing unrecorded is adopted.
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
and external runtime state are not tracked artifacts;
trusted cook/runtime code may modify state. Existing real unit/drop-in/enablement
parent directories can be reused and removed when empty after retirement, even
if the role did not create them. The manifest itself is validated as a regular
file with a valid schema, not authenticated against an external ownership record.

The manifest is updated atomically after each artifact change, so normal
activation failure can restore the previous unit set using the same ownership
rules. This is not a crash-atomic multi-file transaction: interruption between
an artifact write and manifest update can require manual recovery. Missing
(except the unit-link case above), modified or corrupt manifests/artifacts fail
closed. There is no automatic
adoption/migration for older deployments without a manifest or a build-directory
entry. Stop the affected agent and have an operator inspect, back up and remove legacy/conflicting
artifacts before a fresh deployment; do not erase a manifest to bypass drift.
Never edit a live deployment concurrently. Separate agents must use distinct
roots and unique unit names.

## Operator-supplied recipe (opt-in)

For an existing hand-built agent, effective agent config may supply this **closed**
record instead of generator settings:

```yaml
recipe_file:
  path: /absolute/controller/recipe.json
  sha256: REPLACE_WITH_REVIEWED_RECIPE_SHA256
```

`path` is controller-local, canonical and absolute. The controller lookup rejects
symlinks (including ancestor aliases), non-regular files, hash mismatches and
non-JSON input. JSON must be an object, without duplicate keys or nonfinite
numbers. The lookup transports bytes separately from the normalized deployment
spec; the target rechecks the hash/JSON and installs `BUILD/cook-recipe.json`
verbatim, owned by the agent, mode 0600. No JSON reserialization, templating or
placeholder substitution occurs. **Paths inside the recipe must already be the
target's paths**, not controller paths. Only this one file is copied; any child
recipes, source directories or other referenced files must already be available
on the target. Secret references such as `${EXAMPLE_FILE}` retain cook's semantics;
never put secret values in the recipe.

Connector and module generation, including `agent` generation, is skipped.
Connector descriptors therefore need no `pin_ref` or connector source pins;
the resolved connector pin map is empty. Runtime pins, local host dependencies,
budget refusal, cook's normal recipe validation, v2 lock validation and state
layout checks still apply. The operator recipe owns its component/source
metadata: the role does not assert that these components match ignored connector
descriptors. Declare all required runtime environment names in the recipe's cook
requirements, without values. The role never injects requirements into this file.

The plan records `recipe_file.path` and `recipe_file.sha256`; cook's existing
`recipe.sha256` in `connectome.lock` must match those exact installed bytes.
Prepared/retained builds revalidate the installed bytes, owner/mode and lock;
rollback does not need the original controller file. A fresh Ansible apply still
reads/verifies the controller input. Build, unit export, EnvironmentFile delivery,
current switch, health and rollback remain unchanged: **ExecStart continues to
use cook's generated run.sh**. No alternate start command or extra unit directives
are introduced. Absent these new inputs, recipe rendering, adapter/deployment
argv, units and lock bytes are compared directly against baseline `06af58c` in
the offline compatibility test.

## File-only secrets (opt-in)

The effective agent config accepts a **closed list**, replacement-merged like any
other list, with only `secret` and `env` per entry:

```yaml
secret_files:
  - secret: EXAMPLE_SECRET
    env: EXAMPLE_FILE
```

`secret` names a string in the agent's secrets JSON (or the selected existing
secret provider). `env` names the environment variable that the application uses
to locate that secret's file, for example `GITHUB_TOKEN_FILE`. Both names use
`[A-Z_][A-Z0-9_]*`. Duplicate destination env names, collisions with direct secret
env delivery, state env names and adapter infrastructure env names are refused.
Multiple file variables may refer to the same source secret. File-only source
names are fetched but are **not** also exported as raw environment values. If a
source is also explicitly declared in `required_secrets` (including a descriptor),
that explicit direct delivery still applies. Existing secret provider selection,
JSON permission checks and EnvironmentFile handling are unchanged.

Files live at `ROOT/state/secret-files/ENV`, beside deployment bookkeeping, **not
inside external runtime state**. The directory is agent-owned mode 0700; each file
is agent-owned mode 0600. Existing files/directories must already have these exact
modes and ownership; symlinks, writable ancestors and undeclared files are refused,
not adopted or repaired. Values are written as exact UTF-8 string bytes, without
an added newline; multiline file-only values are allowed. Each apply atomically
rewrites declared files, even when unchanged. Changed bytes restart the service;
unchanged bytes only recheck health. Removing an entry deletes its managed file
on successful activation. Neither a new recipe nor a file-secret value rotates
cook's launcher away from run.sh.

`BUILD/secrets.env` sets each destination env variable to its absolute file path,
using the existing EnvironmentFile drop-in. Plans contain only source/destination
names and paths; locks cover runtime env **names**, not source values or file
contents/hashes. Generated recipes declare destination env names, not file-only
source names. With `recipe_file`, declare these destination names yourself as
optional unresolved cook requirements (`exposeAs`, `required: false`). Values
never enter the deployment spec, plan, lock, ownership manifest or diagnostics.
Only the secret provider's input, the private secret files, and any explicitly
requested direct EnvironmentFile delivery contain them.

Activation snapshots the prior file set privately in memory. Failed health
restores prior declarations **and bytes**, removes newly introduced files, restores
the previous EnvironmentFile/unit/current references, and rechecks prior health.
Same-request rotation failures restore the previous file bytes as well. This is
normal failure rollback, not crash-atomic recovery or protection against trusted
same-uid runtime code. Do not modify this private directory concurrently.

The real-cook offline test builds a verbatim recipe and activates file-secret
delivery, executes the real generated run.sh against a tiny local host, checks a
synthetic canary for leaks, and rolls back a changed declaration. Systemctl remains
an explicitly identified offline double; this is not live systemd qualification.
