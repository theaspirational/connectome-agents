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
connector pins have been replaced with unresolved digest records rather than
presenting unverified upstream commits as releases.

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
  openai-codex-adapter: {enabled: true}
  inference-gate-check: {enabled: true}
  transport-readiness: {enabled: true}
```

Disable unneeded connectors using their existing `enabled: false` overrides.
For each selected connector, supply reviewed native stdio `settings.command` and
`settings.args`; public empty settings intentionally do not guess upstream entry
points. Use the artifact placeholders for all runtimes/scripts. Per-connector
state paths belong in native settings, not cook's `connector_state` mapping:
cook sees these already installed servers as `source.external`.

Supply the agent's native provider configuration with an artifact placeholder
where the reviewed host schema expects the adapter executable. The role does not
invent provider-specific host keys or provision credentials. The adapter payload
contract is `src/index.ts` plus vendored dependencies, built by the pinned Bun into
`dist/adapter.js`; change and re-pin the descriptor if the reviewed build differs.

The cook checkout, local host checkout and local host-dependency archives must
also reside under this agent's root. Existing `cook.host_dependencies` hash and
package-identity checks still apply. Existing `runtime` full-commit pins still
bind cook/host/framework. The reviewed cook source-tree check is unchanged.

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
be granted exceptions. These OS tools are not copied; their bytes are checked
before build and activation/reuse. Cook's build PATH contains only installed
engines and aliases for these exact pinned tools, with private build caches.

The audit refuses undeclared absolute paths in compiled recipe fields (including
nested args/env), health settings, cook lock paths, launcher scripts and exported
units. Every build, launch, health and preflight argv is additionally checked
against its execution cwd, including its executable, relative paths, bare file
names and `--option=value` operands. Symlinks are resolved before the boundary
check. Build argv are rechecked during lock verification, not just installation.
Ambiguous argument syntax (attached short-option values, response files, URLs,
path lists, inline command evaluation, substitutions or encoded forms) fails closed. Use literal argv and
explicit executable paths (build-only `node`/`bun` aliases remain supported).
Exported units must have an explicit literal `WorkingDirectory`; their Exec
commands and the added preflights use that cwd. Launch scripts accept only
literal commands, `exec`, absolute `cd` without `..`, and numeric `exit`, not
arbitrary shell syntax.
The audit also checks the entire installed artifact inventory (file hashes/modes and no links). The only
path roots admitted are the agent deployment tree, its explicitly configured
separate persistent state, and exact platform pins. There is no external-directory
exception for catalog MCP servers. Cook's engine provenance and external server
inventory must exactly match the installed closure. A separate catalog sidecar
preserves cook's raw schema and build identity.

This is a conservative launch-surface audit, not a proof about arbitrary native
binaries/JavaScript, dynamically constructed paths, ELF loaders/shared libraries,
or administrator-created private systemd overrides. The OS ABI and control plane
remain prerequisites. Reviewed payloads must not conceal dependencies; stronger
execution confinement and effective-unit probing belong to later apply/verify
work. Unrelated state and credentials are never copied into archives or locks.

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
private HOME/temp/cache, fixed `LANG=C.UTF-8`, offline/no-notifier settings,
installed runtimes and individually pinned platform aliases on PATH. There is
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

No real artifact pins were fetched or certified. For **each selected** entry:

1. `artifacts.node`, `artifacts.bun`, `artifacts.openai-codex-adapter`,
   `artifacts.inference-gate-check`, `artifacts.transport-readiness`: supply
   **version, archive SHA-256, descriptor SHA-256**, plus the reviewed archive
   bytes at the declared `inputs/NAME.tar.gz` location. Node must be v24.
2. `connectors.slack-mcpl`, `connectors.telegram-bot-mcpl`,
   `connectors.zulip_mcp`, `connectors.github-mcpl`,
   `connectors.terminal-sessions-mcp`: supply **reviewed fork repo/launch/build
   metadata, full commit, version, archive SHA-256, descriptor SHA-256**, and
   corresponding complete archive bytes. The existing upstream repo names are
   candidates, not verified fork recommendations. Hash after finalizing metadata.
3. **Target-platform file hashes** for `/bin/bash`, `/bin/sh`, `/usr/bin/git`, and
   any additional exact OS tools actually required by the reviewed build/launcher.
   Remove unused entries instead of retaining unresolved placeholders.
4. **Reviewed host/cook/framework commits and local payloads**, plus the existing
   `cook.host_dependencies` package identities/hashes (or legacy framework archive
   hash). Recheck bridge-era commit values for the new closure; this change does
   not certify their compatibility with the selected adapter/engines.
5. Instance-only inputs: native provider/connector launch settings, intended
   persistent state, privately provisioned endpoint/token environment names and
   values. These are not release pins and never belong in public archives.

Disabled entries need no pins. Selected placeholders, all-zero hashes/commits,
missing dependencies, wrong descriptor hashes or changed archive bytes fail closed.
