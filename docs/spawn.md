# Offline spawn planning (v1)

`connectome-spawn validate|plan|dry-run` is read-only. It does not install,
extract archives, run catalog commands, acquire dependencies, contact providers,
read credentials, inspect target services, apply, seed, or activate anything.
All plans say `activationAllowed: false`. A plan is not authorization, an
idempotent-success receipt, or proof of runtime closure/compatibility.

## One implementation, one catalog

The Python package lives at
`roles/connectome_agent/module_utils/connectome_spawn/`. This is the single
source, not a copied CLI implementation: Ansible can import
`ansible.module_utils.connectome_spawn`; the checkout launcher adds the
`module_utils` directory to its import path; `pyproject.toml` installs that same
package as `connectome_spawn`. Runtime dependencies are Python 3.10+ and PyYAML
6+ only. Setuptools is only an optional installation/build dependency.

This layout permits Ansible's module-utils dependency bundling without requiring
an independently installed target package. The existing deployment module is
**not rewired**: a future frontend must explicitly call this API, without
reimplementing validation or compilation. No variable merging, `become`, ambient
Ansible facts, environment defaults or private inventory enter the CLI.

```python
from connectome_spawn import load_source, load_catalog, make_plan

result = make_plan(
    load_source(file="template.yml"),
    load_source(file="instance.yml"),
    load_catalog("."),
    load_source(file="stores.yml"),
)
```

The same call is available from the Ansible namespace. `template`, `instance`,
`decode`, `canonical`, `digest`, and `require_bound` are also public functions.
Inputs and output are detached; callers must treat a returned plan as immutable.
`SpawnError` messages never include supplied values.

## CLI

No installation is needed from a checkout:

```sh
scripts/connectome-spawn validate --file examples/spawn/template.yml \
  --instance examples/spawn/instance.yml
scripts/connectome-spawn plan --file examples/spawn/template.yml \
  --instance examples/spawn/instance.yml --catalog . --stores stores.yml
scripts/connectome-spawn dry-run --file examples/spawn/template.yml \
  --instance examples/spawn/instance.yml --catalog . --stores stores.yml
```

Replace the generic paths in `examples/spawn/stores.yml` with local object-store
paths in a **private** input file. Each required commit must already exist. The
example uses no connectors or extra runtime artifacts; the three bridge runtime
commits in `versions.yml` still must resolve. It is a planning example, not a
runnable or qualified runtime distribution.

Alternatively, with prerequisites already installed:
`python3 -m pip install --no-index --no-build-isolation .` creates the
`connectome-spawn` console entrypoint. Do not enable package acquisition for
planning. `validate` validates template and instance shapes only; it does not
accept a catalog or promise that artifact resolution will succeed.

Exactly one template source is required:

* `--inline 'JSON or YAML text'`
* `--file template.yml`
* `--repo "$LOCAL_REPOSITORY+templates/example.yml@$FULL_COMMIT"`

The final `+` separates the absolute local repository directory from its relative
blob path; the final `@` separates the full, lowercase 40-hex commit. No remote
URL, branch, tag, abbreviated commit, revision expression, symlink blob,
submodule path or traversal is accepted. Bare repositories work. Only Git
plumbing reads local objects: no fetch, checkout, replacement objects, lazy
promisor fetch, hooks, user/system configuration, or permitted Git transport.
Dirty working-tree bytes do not override committed template bytes.

Output is JSON on stdout; input refusal is a value-free JSON error on stderr,
exit 2. **Plans include system prompt text and draft file contents**; handle
stdout as protected configuration, not as a public receipt. `dry-run` includes
the identical plan plus `wouldWrite`, an explicit list of paths, modes, contents
and tool-computed SHA-256 values. Neither command writes those files.

## Template schema: `connectome.spawn-template/v1`

Every object is closed: unspecified fields are refused, never silently dropped.
There are no template commands, hooks, shell, build instructions, executable
paths, environment variables, credentials, arbitrary source URLs, or digest
inputs. Executables can only be selected by catalog entry name.

Required fields:

| Field | Contract |
| --- | --- |
| `schema` | Exact version above |
| `runtime` | Unique list of catalog names of kind `runtime` or `adapter`; may be empty |
| `connectors` | Unique list of connector catalog names; may be empty |
| `modules` | Unique list of the builtin module names below; enables each with `true` |
| `agent` | Required `provider`, `model`, `systemPrompt`, `strategy` |
| `seedPolicy` | `required`, `clean`, or `optional`; recorded only, no seed behavior |

Optional `preflights` is a unique list of catalog names of kind `preflight`.
`requires` dependencies are expanded transitively by the catalog reader;
unknown dependencies, kind mismatches and cycles fail closed. Template selection
is explicit and independent of descriptor `enabled` (which is role merge metadata).

Builtin modules: `activity`, `channelMode`, `fleet`, `history`, `identity`,
`instructions`, `lessons`, `mcplAdmin`, `notices`, `retrieval`, `subagents`,
`subscriptionGc`, `wake`, `webui`. Arbitrary module options, workspace mounts,
extensions and full parity settings are deliberately not a v1 escape hatch;
they require later typed schema additions.

Supported providers: `anthropic`, `openai-responses`, `openai-codex`, `openrouter`,
`bedrock`, `mock`. Provider names select recipe data, **not** provisioning or
spending. Model identifiers are literal strings. `systemPrompt` must be concrete
and nonempty. Optional agent fields:

* `contextBudgetTokens`, `maxStreamTokens`: positive integers, at most 10,000,000.
* `proseRouting`: `locus`, `explicit`, `hybrid`, `disabled`.
* `responses: {reasoningEffort: ...}`: `none`, `low`, `medium`, `high`, `xhigh`, `max`.
* `codex: {fastMode: boolean}`.

`strategy.type` must be `autobiographical`. Optional strategy fields:

* Positive bounded integers: `recentWindowTokens`, `headWindowTokens`,
  `targetChunkTokens`, `maxMessageTokens`, `maxSpeculativeL1s`, `mergeThreshold`,
  `compressionRecallBudgetTokens`. Tail cannot exceed explicit context budget.
* Booleans: `adaptiveResolution`, `enforceBudget`.
* Numbers in [0,1]: `compressionSlackRatio`, `overBudgetGraceRatio`.
* `compressionModel`: literal model identifier.
* `foldingStrategy`: `kv-unified`, `kv-stable`, `flat-profile`, `oldest-first`.

`agent.name` and `summaryParticipant` are not template inputs. The compiler
always derives them from the instance's exact-case logical identity. Template
values such as empty strings, TODO/TBD/CHANGEME, `${...}`, `{{...}}`, angle-bracket
placeholders, or bare field-name placeholders are refused, including prompt,
identity and home channel values. This is a conservative syntactic check, not
proof that a human approved a prompt or that every possible placeholder was found.

## Instance schema: `connectome.spawn-instance/v1`

All fields in `examples/spawn/instance.yml` are required; no optional fields:

* `instance` and `unixName` are separate lowercase identifiers matching
  `[a-z][a-z0-9-]{0,62}`. No NSS lookup, account creation or collision detection.
* `agent` contains only required `logicalName`, matching
  `[A-Za-z][A-Za-z0-9_-]{0,62}`. There is **no fallback or lowercasing**. Recipe
  `name`, `agent.name` and `agent.strategy.summaryParticipant` use it verbatim.
  Host participant namespace is recorded as `agents/<logicalName>`.
* `homeDirectory` is an explicit literal absolute path, not `$HOME` or an
  Ansible fact. Derived config/state/runtime/workspace/credential paths are
  deterministic and lexical; the planner does not inspect target ancestry.
* `home` is `{transport, channel}`. Transport must be `console` or an explicitly
  selected connector. Channel is a concrete literal identifier (letters,
  digits, `_ . : / -`, at most 256 characters); traversal components are refused.
  This binding is recorded, not provisioned or claimed to route a conversation.
* `rights` contains explicit boolean `administration`. It records a request,
  never grants rights.
* `target` contains `os: linux` and `architecture: x86_64|aarch64`. These are
  declared target facts, not inferred controller facts or an ABI certification.

## Reading the existing role catalog

`load_catalog(root)` reads **only** the existing layout:
`connectors/<name>.yml` (exactly one matching outer name) and `versions.yml`.
There is no second catalog file format. Its `RoleCatalog` result is an internal
Python value, not a new manifest to maintain. Names allow lowercase letters,
digits, underscores and hyphens.

Descriptor contract shared with the role:

* Missing `kind` means `connector`; explicit kinds are `connector`, `runtime`,
  `adapter`, `preflight`. An explicit dispatch registry rejects unknown kinds.
* Connector fields: `repo`, `pin_ref: connectors.NAME`, `settings` (native
  connector launch data); optional `kind`, `enabled`, `build` (argv lists),
  `required_secrets`, `requires`, `exports`.
* Other kinds: `kind`, `pin_ref: artifacts.NAME`; optional `enabled`, `requires`,
  `build`, `exports`, `required_secrets`. Preflight additionally requires `run`
  (argv). Exports map names to safe relative paths.
* `versions.connectors.NAME` is either a full nonzero commit (legacy), or an
  archive pin record with `archive`, `version`, `sha256`, `descriptor_sha256`,
  and full nonzero `commit`.
* `versions.artifacts.NAME` uses those four archive fields, without `commit`.
  `version` is concrete semver; `archive` is a contained relative file path.
* Descriptor SHA is computed from sorted compact JSON, `ensure_ascii=True`,
  UTF-8, excluding **only** `enabled`. Archive SHA is computed from local bytes.
  Both must match the reviewed catalog pins. Users never enter recipe/plan SHA
  values. Placeholder/zero pins are refused when selected; unused unresolved
  descriptors do not block unrelated selections.
* `versions.runtime` retains the bridge's commit pins. `cook`, `connectome-host`
  and `agent-framework` are required; every runtime pin is resolved locally.
  Those legacy records contain no repository metadata: the caller explicitly
  supplies their local Git object stores; no repository URLs are hardcoded.
* `versions.platform` optionally maps exact absolute platform file paths to
  nonzero SHA-256 pins. These are bound in the plan but **not probed or qualified
  against the target**. No directory grants are inferred.

The separate `--stores` input is **only resolution locations**, a closed map
from `pin_ref` to absolute local directory. For legacy commit pins its value is
a Git object store; for archive pins it is the payload root containing
`record.archive`. Archive paths, including ancestors, cannot be symlinks.
Legacy tree digest is SHA-256 of the canonical sorted file inventory
(path, Git mode, byte size, computed SHA-256); symlinks and submodules are refused.
Source documents are limited to 1 MiB/depth 32 and artifact payloads/trees to
64 MiB each. No unpacking, archive safety or executable/ABI qualification is
claimed. Runtime closure larger than that limit needs an explicit future change.

Secret names become recipe requirements, never values. Catalog commands are
trusted policy input and are recorded/compiled but never run. Do not let an
untrusted API caller supply or modify the catalog. Hashes establish consistency,
not catalog authentication. The current default catalog may intentionally fail
planning due to unresolved pins or missing launch metadata.

## Symbolic build binding and hashes

Archive connectors use cook `source: {external: true}`. Catalog references
`{artifact:NAME}` expand to `__CONNECTOME_BUILD_DIR__/catalog/NAME`.
The fixed token represents the **role-owned** future build directory, not a
request hash guessed from the plan hash. `symbolicFields` lists JSON pointers to
every scalar containing it, including serialized draft file contents and export
or preflight paths. These are **draft recipes, not installable files**.

A future apply adapter must bind the real role request directory, call
`require_bound(apply_input)` (which refuses any surviving build token), validate
all concrete paths and recipe fields, and recompute installed-file hashes before
writing or execution. This package contains no apply/bind command. Do not feed
draft files directly to the existing deployment module. The admission-guard test
explicitly proves that an unresolved draft is refused as an apply input.

Canonical v1 encoding: JSON-compatible finite data, sorted object keys, UTF-8,
`ensure_ascii=False`, compact separators, no final newline, no Unicode
normalization. Array order and number types remain significant; this is not
RFC 8785. JSON rejects duplicates/nonfinite numbers/malformed JSON without YAML
fallback. YAML rejects duplicates, aliases/anchors, tags, merges, multiple
documents and non-JSON scalar types; SafeLoader scalar rules otherwise apply.
Quoted YAML scalars avoid unintended booleans or dates.

* `templateHash` hashes validated template data, independent of transport,
  formatting, filename and repository locator. No source locator contaminates
  the semantic hash.
* `planHash` hashes the entire `plan` object (not its wrapper), including
  identities, selected catalog descriptors/pins, computed artifact digests,
  effective recipe settings, paths, seed, home and rights requests, limitations,
  symbolic-field inventory and explicit observation.
* Draft file SHA values hash exactly their UTF-8 `content`, including the final
  newline. They do not claim to hash a future bound file.
* `idempotenceKey` hashes `{schema: connectome.spawn-idempotence/v1, planHash,
  target, observed}`. Optional `--observed` supplies the closed
  `{deploymentHash: null|64-lowercase-hex}` object; omission is explicit unknown
  (`null`), **not** evidence that a deployment is absent or safe to create.

Repeated equivalent inputs produce identical plans. Any meaningful selected
input change changes the hash/key. Apply/reconcile must later bind a reviewed
plan to current target observations and authorization; a key alone does not
make effects safe to retry.

Run `scripts/test.sh` for offline regression tests, including source equivalence,
strict refusals, catalog fixture compatibility, symbolic admission, CLI
read-only behavior and import parity with the Ansible module-utils namespace.
