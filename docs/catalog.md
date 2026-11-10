# Agent-local, digest-pinned catalog

The role has two deliberately separate paths:

* `recipe_file`: the existing byte-preserving recipe + `source.external` bridge.
  Catalog descriptors and artifact pins are not resolved or installed. Existing
  external-server validation, runtime arguments and cook lock behavior remain.
* No `recipe_file`, with selected runtime entries: compile the connector catalog,
  verify and install its complete local artifact closure, invoke cook using the
  installed Node/Bun, and validate both cook's lock and `catalog.lock.json` before
  prepare, activation or reuse. No external connector staging directory is used.

## Shared descriptor and pin format

All descriptors remain `connectors/<name>.yml`, with one outer name key.
`versions.yml` is the pin file. The four-layer merge remains the only merge site.
`public_catalog` splits explicit non-connector kinds into `config.catalog`;
existing connector descriptors remain `config.connectors`. Disabled entries are
pruned, including their requirements. An empty `catalog` key is removed, preserving
legacy request identities. Select every dependency explicitly in role configuration;
the `requires` graph rejects missing dependencies and cycles.

| Kind | Fields (besides `enabled`, `pin_ref`, `required_secrets`) |
| --- | --- |
| `connector` (default if `kind` absent) | `repo`, `build` argv lists, native cook `settings`; optional `requires`, `exports` |
| `runtime` | `kind`, `requires`, `exports`; optional `build` |
| `adapter` | `kind`, `requires`, `build`, `exports` |
| `preflight` | `kind`, `requires`, `run` argv; optional `build`, `exports` |

`exports` maps names to relative files, not shell commands. Node must export
`node`, Bun must export `bun`. Node's pinned version must have major **24**.
Connector dependencies default to `[node, bun]`; other kinds declare `requires`.
`{artifact:NAME}/relative/file` expands to
`ROOT/builds/REQUEST/catalog/NAME/relative/file` in commands and recipe settings.
REQUEST is the role's existing request hash, **not** a compiler plan hash.

New entries use `pin_ref: artifacts.NAME`. Existing connectors retain
`pin_ref: connectors.NAME` and their repository/build meaning. The old commit-only
private connector format still works with the legacy compiler path. Public
connector pins bind the reviewed offline-built payloads described below; they
are not a claim of deployed runtime compatibility.

```yaml
artifacts:
  node:
    archive: inputs/node.tar.gz
    version: PLACEHOLDER
    sha256: PLACEHOLDER
    descriptor_sha256: PLACEHOLDER
connectors:
  example-connector:
    archive: inputs/example-connector.tar.gz
    version: PLACEHOLDER
    sha256: PLACEHOLDER
    descriptor_sha256: PLACEHOLDER
    commit: PLACEHOLDER
```

These records are closed: the four fields shown for artifacts, plus `commit`
for connectors. `version` is semver; `sha256` hashes the archive bytes;
`descriptor_sha256` hashes **the effective reviewed descriptor excluding only
`enabled`**, using Python `json.dumps(..., sort_keys=True, separators=(',', ':'),
ensure_ascii=True, allow_nan=False).encode('utf-8')`. Any change to build commands,
launch metadata, exports, dependencies or secret names needs a newly reviewed
pin. A connector's full nonzero commit is provenance supplied by the publisher;
the archive hash proves byte identity, **not** that those bytes were built from
that commit. Repository identity remains in the descriptor and is hash-bound.

`archive` is relative to the agent deployment root and must start with `inputs/`.
It cannot be a URL, another account's file, symlink or traversal path. Stage the
reviewed tar files under that agent's own `ROOT/inputs/` before prepare. No archive
fetcher, shared store or cross-agent copying is implemented. Tar entries are
relative files/directories; links, devices, duplicates and traversal are refused.
Unpacked size is limited to 1 GiB and 100,000 entries per archive. Package links
as real files when assembling a payload. Bundles must include their complete
vendored build dependency closure. Descriptor build commands run in dependency
order, with no secret environment, agent-local HOME/temp/cache, installed
runtimes on PATH, and offline npm configuration. Bare `bun`/`node` in legacy
connector build argv resolve to the installed exports, never ambient binaries.

Build commands are reviewed executable policy, **not untrusted templates**.
Offline environment settings are not a network sandbox. In particular, a default
connector's install command must be reviewed/overridden for the supplied complete
payload; the role does not certify arbitrary package-manager lifecycle scripts.

## Role selection and inputs

For example, in the highest layer:

```yaml
budgets: null
catalog:
  node: {enabled: true}
  bun: {enabled: true}
  inference-gate-check: {enabled: true}
  transport-readiness: {enabled: true}
```

Disable unneeded connectors using their existing `enabled: false` overrides.
Public connector descriptors now supply reviewed native stdio `settings.command`
and `settings.args` matching the built payloads. Review these for the instance;
use artifact placeholders for all runtimes/scripts. Per-connector
state paths belong in the explicit `settings.data_args` fields described below,
not ordinary args/env or cook's `connector_state` mapping:
cook sees these already installed servers as `source.external`.

Supply the agent's native provider configuration through the reviewed host/framework
schema. The OpenAI provider belongs to host/framework; no separate adapter artifact
is supplied or required by this catalog. The role does not invent provider-specific
host keys or provision credentials.

The cook checkout, local host checkout and local host-dependency archives must
also reside under this agent's root. Existing `cook.host_dependencies` hash and
package-identity checks still apply. Existing `runtime` full-commit pins still
bind cook/host/framework. The role copies the cook and host checkouts into the build before executing
code from them; links/special files are refused. The reviewed cook source-tree
check is performed on that staged copy. The operator-supplied role/Python adapter
bootstrap remains a trusted control-plane prerequisite, not catalog code.

The catalog adapter command only needs `--spec`, `--out`, `--state`, and
`--entrypoint ROOT/inputs/cook/bin/cook`. Its Python interpreter and the role are
control-plane prerequisites. `--runtime`, `--bun-path`, `--node-path` remain
required in effect for the recipe-file/legacy path; when provided in catalog
mode they are ignored in favor of installed pinned engines.

## Platform and hidden-path boundary

`versions.platform` is an explicit mapping of exact OS executable paths to
SHA-256 hashes (initial examples: `/bin/bash`, `/bin/sh`, `/usr/bin/git`). It is
not a directory allowlist. Only files directly under `/bin`, `/usr/bin`, or
`/usr/local/bin` can be platform tools; application installation trees cannot
be granted exceptions. These OS tool bytes and agent-nonwritability (including ancestors) are checked
before build and activation/reuse. Cook's build PATH contains only installed
engines and role-written copies of these exact pinned tools, with private build
caches; no outward platform-bin symlinks are created.

The audit refuses undeclared absolute paths in compiled recipe fields (including
nested args/env), health settings, cook lock paths, launcher scripts and exported
units. Every build, launch, health and preflight argv is additionally checked
against its execution cwd, including its executable, relative paths, bare file
names and `--option=value` operands. Symlinks are resolved before the boundary
check. Build argv are rechecked during lock verification, not just installation.
Ambiguous argument syntax (attached short-option values, response files, URLs,
path lists, inline command evaluation, substitutions or encoded forms) fails closed. Use literal argv and
explicit executable paths (build-only `node`/`bun` aliases remain supported).
Exported units must have an explicit literal build-local `WorkingDirectory`; their Exec
commands and the added preflights use that cwd. Launch scripts accept only
literal commands, `exec`, absolute `cd` without `..`, and numeric `exit`, not
arbitrary shell syntax.
The audit also checks the entire installed artifact inventory (file hashes and
final read-only modes). **Code paths must be inside this build or exactly name a
pinned platform tool**: not another build, inputs, workspace, or persistent state.
This applies to executable argv, interpreter scripts/shebangs, preload arguments,
build/preflight/health commands, exported Exec targets, recipe commands and env.
Relative code and environment paths resolve from a build-local cwd. All build
symlinks (including inward links), hardlinked files and special files are refused
at sealing/verification. There is no external-directory exception for catalog MCP servers. Cook's engine provenance and external server
inventory must exactly match the installed closure. A separate catalog sidecar
preserves cook's raw schema and build identity.

This is a conservative launch-surface audit, not a proof about arbitrary native
binaries/JavaScript, dynamically constructed paths, ELF loaders/shared libraries,
or administrator-created private systemd overrides. The OS ABI and control plane
remain prerequisites. Reviewed payloads must not conceal dependencies; stronger
execution confinement and effective-unit probing belong to later apply/verify
work. Unrelated state and credentials are never copied into archives or locks.

## Build sealing, starts and the same-UID limit

After cook finishes and the role materializes managed units and `.prepared`, it
removes **all write bits** (`mode & 0555`) from every build file and directory,
including the build root. No agent-owned writable directory remains inside it.
Verification checks the whole tree, not just executable files; write bits, links,
special files and build aliases fail closed. This also removes the POSIX ACL
write mask. The role does not silently repair an already prepared writable build.
The inventory records final sealed modes. The internal pre-seal assembly audit
is not a reusable/activation verification.

Every generated Exec bootstrap (preflights and all exported Exec directives)
walks the entire tree with `lstat` before `execve`, checking modes, links and file
types, and rechecks platform writability. Thus systemd automatic restarts also
check the boundary, without rehashing all files. Role-requested restarts perform
full verification again immediately before restart. HOME and module/package
caches stay in the read-only build; runtime TMPDIR and DATA_DIR refer to data
state. Applications requiring writable code caches must be prebuilt, not granted
a writable module search path. Runtime secrets are written to role bookkeeping
`ROOT/state/REQUEST.env`, **outside the sealed build**; secret rotation and
activation do not reopen it. This is not the agent's persistent `state_dir`.

**Same-UID policy: read-only modes plus start-time detection, not kernel-enforced
immutability.** A malicious process sharing the deploying UID can chmod owned
files/directories, replace writable ancestors, tamper with units/the bootstrap,
or race a check and restore read-only modes. The pinned Node bootstrap must
itself load before it can check the tree. Modes do not authenticate changed bytes
on automatic restart. These residual risks are NOT solved by this role. For a
hostile agent, use a separate deployment owner and non-agent-writable ancestors,
units and runtime, or a read-only mount/confinement boundary maintained outside
the agent UID. No such ownership/mount isolation is provisioned here. OS ABI,
loader/shared-library dependencies and reviewed application behavior remain
trusted. Native payloads must not interpret data files as code.

## Explicit state data arguments

Only `data_args` in a native command object (for example connector `settings`)
allows catalog-supplied state paths. It maps **application `--data-*` options** to
literal absolute paths inside the configured state directory, resolving links:

```yaml
settings:
  command: '{artifact:node}/bin/node'
  args: ['{artifact:example-connector}/server.js']
  data_args:
    --data-file: /example/state/records.json
```

The compiler appends `--data-file=/example/state/records.json` to native cook
`args` and removes `data_args`; no cook/host schema extension is required. The
role rebinds these operands to the reviewed command/declaration at install and
verify. Positional data exceptions, interpreter flags (`--require`, `--import`,
`--eval`, etc.), state cwd, and state references in ordinary args or env are
refused. A bare state executable or interpreter script is refused even before
it exists. Use an application that supports a reviewed `--data-*` option (or a
reviewed build-local adapter); do not disguise a code-loading option as data.
The descriptor is trusted executable policy: these fields are not permission for
application code to execute/evaluate a state file. Built-in DATA_DIR/TMPDIR and
private secret transport are role-constructed data channels, not catalog code
exceptions. The recipe-file bridge's separate `external_data_args` semantics are
unchanged and do not create catalog exceptions.

## Closed catalog environment

This tightening applies **only to catalog mode**. The `recipe_file` bridge is
out of scope: its input bytes, generated recipe, invocation and environment
behavior remain unchanged (covered by the baseline golden comparison).

Catalog environment assignments are executable policy too. We **refuse**, rather
than silently strip, these names, including declared-secret destinations and
exported unit `Environment=` assignments (matching names case-insensitively):

* `PATH`, `MANPATH`, `CDPATH`, and every name ending in `PATH` (including
  `*_PATH`): search-path policy belongs to the role, not the descriptor;
* `NODE_OPTIONS`, `NODE_PATH`, `NODE_REPL_EXTERNAL_MODULE`;
* `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, all `DYLD_*`;
* `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `PYTHONINSPECT`;
* `PERL5LIB`, `PERL5OPT`, `PERLLIB`, `RUBYOPT`, `RUBYLIB`;
* `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`;
* `GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_EXEC_PATH`, `GIT_CONFIG`, all
  `GIT_CONFIG_*`, `GIT_EDITOR`, `GIT_SEQUENCE_EDITOR`, `GIT_ASKPASS`, `SSH_ASKPASS`;
* `JAVA_TOOL_OPTIONS`, `JDK_JAVA_OPTIONS`, `_JAVA_OPTIONS`;
* `OPENSSL_CONF`, `OPENSSL_MODULES` (configuration can load native providers);
* all names ending in `_PRELOAD`; `BUN_*` names containing `PRELOAD`, or
  ending in `CONFIG`, or containing `CONFIG` followed by an optional
  underscore-prefixed suffix and `PATH`, `FILE`, or `DIR`;
* names beginning with `NODE`, `NODEJS`, `PYTHON` (optionally digits), `PERL`,
  `RUBY`, `BUN`, `BASH`, `SH`, `ZSH`, `DENO`, `LUA`, `PHP`, `JAVA`, `JVM`, or
  `JDK`, followed by `_OPTIONS` or an underscore-delimited suffix ending in
  `_OPTIONS`.

These names load code, redirect interpreter/module resolution, or enable
interpreter control. `NODE_EXTRA_CA_CERTS` is **not** denied: certificates are
data, but their paths are audited. Every other nonempty catalog env value is
checked as a potential path against its execution cwd, resolving symlinks,
including bare words and literal words containing spaces. Colon lists are split
and **every** element is checked; empty list elements fail closed. Whole values
are also checked before option/list interpretation. Literal
`--option=value`/`option=value` operands receive the same checks. Bare non-path
words remain valid when their cwd-relative resolution stays inside the closure.
URLs, tilde expansion, attached short options, substitutions and other
unclassified syntax fail closed. Use literal local paths and literal non-path
strings. Only exact `${DECLARED_SECRET}` references for the matching declared destination are
accepted; private runtime secret values are not catalog literals and are never
written into the generated command payload. All descriptor settings (including
non-connector settings), nested recipe env mappings and reused locks are checked.
Per-build/per-preflight env overrides are not a descriptor feature: command
objects or additional descriptor fields fail closed instead of creating another
environment channel. Exported units cannot supply `EnvironmentFile`,
`PassEnvironment`, `UnsetEnvironment`, or PAM environment indirection.

Builds/cook receive a constructed environment, never `os.environ` plus removals:
private build HOME/temp/cache, fixed `LANG=C.UTF-8`, offline/no-notifier settings,
installed runtimes and individually pinned platform copies on PATH. There is
no ambient locale or shell PATH inheritance. Only the control-plane Python
adapter bootstrap uses fixed `/usr/bin:/bin`; application PATH is stricter and
contains no unpinned system directories. Health commands use the same clean
catalog environment. Secret values are never passed to builds.

For service preflights and every exported Exec command, the role writes a
closed-environment exec bootstrap using the pinned Node v24 `process.execve`.
This preserves the command's PID, signals and exit status. Simply resetting
systemd `Environment=`/`PassEnvironment=` is insufficient for user services:
they also inherit their manager's environment. The drop-in resets those lists
and EnvironmentFile inputs, removes the explicit loader/control names above
before the Node bootstrap starts, then that bootstrap constructs the **target**
environment from fixed role values, audited unit literals, DATA_DIR and exactly
the declared secrets from the role-owned secrets file. Other manager/operator
variables never reach the target. Secret values remain in memory/environment,
not command-line arguments. Pattern-denied names cannot enter through catalog
assignments; ambient names irrelevant to the Linux Node bootstrap (such as
`DYLD_*` or `BUN_*`) are not copied to targets either. This bootstrap relies on
the Linux/systemd OS boundary, not a shell wrapper or a general code sandbox.


## Opt-in preflights

Each selected `preflight.run` becomes a quoted `ExecStartPre` in the role-owned
unit drop-in. It executes on every service start with the same EnvironmentFile
as the agent; a nonzero exit prevents start. Prepare/install-only renders and
validates it but does not run authenticated probes or assert readiness.

Source payloads in `catalog_payloads/` can be packaged as `check.mjs` in the
corresponding reviewed archives:

* `inference-gate-check`: GET `INFERENCE_GATE_URL` with the
  `INFERENCE_GATE_TOKEN` bearer credential. The operator must select a genuinely
  token-authenticating endpoint; a public health endpoint proves no authentication.
* `transport-readiness`: GET `TRANSPORT_READINESS_URL`, require a successful HTTP
  response. This is **not** an MCP handshake, provider authentication, or a first
  real reply.

URLs are privately delivered environment names, not literal public settings.
Both checks fail on missing/invalid inputs, non-success responses, redirects or
five-second timeout; credentials/responses are not printed. HTTPS is required
except `http://localhost` for local endpoints/tests. No accounts or tokens are
minted by these checks.

## Exact coordinator supply checklist

The offline pins round supplies the engine and connector release records below.
For **each selected** entry, the deployment coordinator must still supply/review:

1. Stage the reviewed `artifacts.node` and `artifacts.bun` archive bytes at their
   declared `inputs/NAME.tar.gz` locations. Both digests and versions are pinned.
   `artifacts.inference-gate-check` and `artifacts.transport-readiness` remain
   disabled placeholders: generic versions and reproducible archives are a later
   task; selecting them still requires version and both digests. Node must be v24.
2. `connectors.slack-mcpl`, `connectors.telegram-bot-mcpl`,
   `connectors.zulip_mcp`, `connectors.github-mcpl`,
   `connectors.terminal-sessions-mcp`, `connectors.heartbeat-mcpl`: stage the
   corresponding complete archive bytes. Reviewed repo/launch/build metadata,
   full commits, versions and both digests are pinned. Instance overrides change
   effective descriptors and therefore need newly reviewed descriptor hashes.
3. **Target-platform file hashes** for `/bin/bash`, `/bin/sh`, `/usr/bin/git`, and
   any additional exact OS tools actually required by the reviewed build/launcher.
   Remove unused entries instead of retaining unresolved placeholders.
4. **Reviewed host/cook/framework commits and local payloads**, plus the existing
   `cook.host_dependencies` package identities/hashes (or legacy framework archive
   hash). Recheck bridge-era commit values for the new closure; this change does
   not certify their compatibility with the selected provider/engines.
5. Instance-only inputs: native provider/connector launch settings, intended
   persistent state, privately provisioned endpoint/token environment names and
   values. These are not release pins and never belong in public archives.

Disabled entries need no pins. Selected placeholders, all-zero hashes/commits,
missing dependencies, wrong descriptor hashes or changed archive bytes fail closed.

## Offline pins round

The release records in `versions.yml` bind Node **24.14.0**, Bun **1.4.4** and
six source-archive commits. Package versions come from each archived
`package.json` (they do not imply a published npm release). Source provenance
was supplied by the coordinator; git archives contain no `.git/config` remotes.
Telegram, Slack, Zulip and terminal-sessions use the coordinator-selected
`theaspirational` forks. Heartbeat's URL comes from its archived
`package.json` `repository.url` (with `git+` removed). GitHub's URL is retained
from the pre-existing descriptor, not inferred from an absent remote.

The coordinator verified Node's upstream signature. Bun's checksum was verified;
**Bun's signature was NOT verified**. The supplied checksum manifest was itself
SHA-256 checked before every listed file was checked. No network fetching was
performed. Builds used the supplied Node 24.14.0, a writable copy of the supplied
npm cache, `NO_UPDATE_NOTIFIER=1`, and `npm ci --offline --cache <copy>` with
lifecycle scripts enabled, followed by each package's normal `npm run build`
(`tsc`). Build processes ran in a 4 GiB scope with non-Unix sockets denied.

Terminal-sessions initially stopped because Puppeteer wanted Chrome
major 142 (build 7444, patch 61), absent from the offline inputs. With coordinator approval, both
clean builds used `PUPPETEER_SKIP_DOWNLOAD=1` and
`PUPPETEER_SKIP_CHROME_HEADLESS_SHELL_DOWNLOAD=1` during npm installation. These
source-assembly environment settings are recorded in its descriptor comments;
they are not unsupported per-command environment fields. Browser payloads and
screenshot functionality are **not shipped or qualified**. The stdio launch
includes `--disable-screenshot`; it connects to a separately managed session
backend and does not forward that flag to it. That backend must independently
launch `dist/src/server/start-session-server.js --headless --disable-screenshot`
with the pinned Node and instance-reviewed access/authentication settings.

### Payload and rebuild contract

Stage each archive as `inputs/NAME.tar.gz`; extract its contents directly into
the artifact root, without an enclosing NAME directory. Node contains `bin/node`
and the upstream `LICENSE`; Bun contains `bin/bun`. Connector payloads contain
source, package/lock/config files, compiled output and the complete npm-installed
dependency closure, including development compilers and platform packages.
No reference tree was used as a build or packaging input.

To assemble a payload from either independently installed source tree:

* Omit npm's `node_modules/.bin` links: descriptors invoke the real compiler
  through the explicit pinned Node interpreter instead. Other payload entries
  are regular files/directories, never links or hardlink records.
* Omit native node-pty build-host records (`build/Makefile`, `config.gypi`,
  `pty.target.mk`, `binding.Makefile`); retain `build/Release/pty.node` built for
  Node 24. Do not rebuild native dependencies during deployment.
* Omit paths outside the catalog's literal filename alphabet (the terminal
  demonstration GIF with spaces). These are not build/runtime dependencies.
* Replace initial script shebang lines with blank lines, retaining line numbers.
  This avoids undeclared platform interpreters in the sealed closure. Reviewed
  JavaScript entry points and compilers use explicit Node argv; incidental
  dependency/tool scripts are not supported standalone executable entry points.
  Recompile normalized sources with `node node_modules/typescript/bin/tsc`,
  removing old `*.tsbuildinfo` first. Do not run npm install at deployment.
* Enumerate all entries in bytewise path order, excluding a `.` root entry. Use
  GNU tar format, no recursive duplicate entries, mtime fixed to Unix epoch zero,
  `--owner=0 --group=0 --numeric-owner --mode=u+rwX,go+rX,go-w
  --hard-dereference`, then `gzip -n -9`.

Two fresh source extractions, offline installations, builds and normalized
payload assemblies produced identical SHA-256 values for **all eight archives**.
Descriptor build argv was also executed against both vendored payload trees.
The archive bytes, detailed transformation lists and checksum lists are retained
as run evidence, not in this public repository.

### Reference comparison and limits

Comparison is of the original built trees, before packaging normalization.
The supplied references used Node 22.23.x; this build used Node 24.14.0.
Differences are classified as (a) Node-version effects or (b) other causes:

| Connector | Result and examples |
| --- | --- |
| telegram-bot-mcpl | Expected (b): the reference omits source/test/docs, e.g. `src/config.ts`, `test/`, `docs/AGENT-GUIDE.md`. Every shared file, including `dist/`, is identical. |
| slack-mcpl | Expected (b): this complete build closure retains dev/optional tools absent in the reference, e.g. `node_modules/typescript/`, the scoped TypeScript Linux x64 package, `.bin/tsc`, and the corresponding `.package-lock.json` entries. Shared compiled output is identical. |
| heartbeat-mcpl | Expected (b): retained dev tooling, e.g. Node type declarations, `typescript/`, `.bin/tsc`, and `.package-lock.json` entries. Shared compiled output is identical. |
| terminal-sessions-mcp | Expected (a): `node_modules/node-pty/build/Release/pty.node` and Node configuration (ABI 127 to 137). Expected (b): gyp Makefile/tool/header paths, added docs/tests and xterm source data files (e.g. the scoped xterm package's `src/common/data/Charsets.ts`). Shared `dist/` is identical. Build-host gyp records are omitted from the payload. |
| zulip_mcp | Comparison unavailable: reference not readable by the worker. Sorted built `build/` path/hash manifest and package/lock hashes retained for coordinator comparison. |
| github-mcpl | No reference supplied. |

Other-cause differences above were inspected; none changed shared connector
application output. **Node 22 to Node 24 runtime behaviour is not qualified by
this round**. Offline compilation and archive reproducibility are not deployment,
authentication, persistent-state, OS-ABI or native-feature qualification. In
particular, GitHub expects instance-selected App credentials or `GITHUB_TOKEN_FILE`
and a repository allow-list, not the old descriptor's unused `GITHUB_TOKEN`.
Connector state paths, Zulip identity/realm, Telegram policy and optional helpers,
and the terminal backend remain instance-review inputs. Connectors whose state
interfaces cannot satisfy the catalog's explicit data-path contract need a
separately reviewed adaptation before deployment; writable code-tree defaults
are not permission to weaken sealing.

No entry depends on a separate OpenAI adapter artifact: its descriptor and pin
were removed. The two preflight placeholders remain disabled for later generic
releases. No control-plane MCP server is added: its source remains unresolved.
Target-platform hashes and compatible host/cook/framework closure payloads are
still coordinator inputs. Build-host executable hashes belong only in the run
report, never in public pins.
