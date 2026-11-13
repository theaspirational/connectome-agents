# Connectome agents: deployment layer

Reusable, public Ansible role for cook materialization and transactional systemd
**user** deployment. Agent configuration, inventories and secret values belong
in a separate private repository. The only example agent is `example`.

## Stage 2 status

- Four-layer recursive merge with null deletion and disabled-entry removal.
- Immutable request directories, validated host lock v2, exact-name secret lookup,
  private env files, user units, atomic `current` switch, health and rollback.
- Offline lifecycle tests use a mock secret provider and fake systemctl.
  Adapter unit tests and a prerequisite-gated real-cook local build are separate.
- The catalog pins offline-built Linux x86_64 Node/Bun and six connector
  payloads; these are **not compatibility-tested deployment releases**. See
  [catalog provenance and remaining inputs](docs/catalog.md#offline-pins-round).
- A bundled production adapter translates the normalized spec to the reviewed
  cook fork's host CLI and validates its real v2 lock. Configure executable paths,
  native launch settings and a hash-pinned framework archive explicitly. Registry
  archives without gitHead require an explicit release declaration and matching
  package name/version; plans record that the commit was not verifiable. Safe
  typed adapter diagnostics are visible in the prepare task. See the
  [contract](docs/lock-contract.md); absent offline prerequisites skip the real
  build test, not a claim of compatibility qualification.

## Threat model

Deployment runs as the agent's own user in its own directories, never as a
privileged account. We defend against hostile configuration values/identifiers.
Unknown pre-existing request build directories are refused unchanged. Build/state
parents and unit/drop-in parents must not be symlink aliases, even within the
deployment tree. An atomic `state/ownership.json` records created build directories
and installed unit files, drop-ins and links by exact path and target/content hash.
Unrecorded unit destinations and `current` are not adopted or overwritten; recorded
files/links must still match before replacement or retirement, except absent
recorded unit/enablement symlinks removed by `systemctl disable`, which apply
recreates without rebuilding. Incomplete, inactive
owned build directories may be recursively cleaned, including untracked contents;
this is not protection for arbitrary files placed inside them. Runtime state is external and never rewritten, moved or deleted by the role;
trusted cook/runtime code can modify it. Secret env files and cook outputs are
not individually ownership-protected. See
the contract for the exact boundary; pointing into `builds/` is not ownership.

Concurrent modification by an attacker with the **same uid**, including swapping
directories mid-deploy, is **out of scope** (the same boundary as cook). This is
not a sandbox for same-user code; cook adapters, health commands and runtime code
must be trusted. There is no fd/openat race-hardening claim. See the
[ownership and recovery contract](docs/lock-contract.md#ownership-and-threat-model).

## Use

1. Pin this repository as a submodule; follow [layering](docs/layering.md).
2. Install Python 3.9+ on the target and ansible-core on the controller. Tests
   and the publication checker need the already-installed PyYAML. No collections
   are vendored.
3. Supply `connectome_cook_command` as an argv list for the bundled pinned cook adapter.
   Set agent-level `state_dir` to an absolute external directory (no HOME assumption),
   and provision the agent account's `~/.config/connectome-secrets/NAME.json`
   (directory 0700, file 0600, both owned by that account, no symlinks).
   Agent-level `secrets_file` is an explicit absolute-path override, shown in
   the check plan. Never put secret values in role vars.
   **Step/cost budgets are unsupported by the inspected host schema.** They
   appear in `--check` plans but the production adapter refuses nonempty budgets;
   explicitly set `budgets: null` in the highest layer to deploy without them.
   No budget enforcement is implemented here.
4. Run the private playbook as the service account, not as a privileged account.
   Its user service manager must be available (session/lingering is an operator
   prerequisite). Each agent needs its own root and unique unit names.
   Optional agent-level `activation: install_only` stages an inactive release
   without enabling or starting it; `activation: start` (default) promotes it
   without rebuilding. Use `exclusive_with: [{scope: system, unit: old.service}]`
   to prevent start while a migration's old instance is running. See
   [activation and migration staging](docs/layering.md#activation-and-migration-staging).
   `connectome_root` must resolve within `connectome_base` (default: the account's
   `.local/share/connectome`). Override both when relocating deployments. Bases
   and their ancestors must not be symlinks. Existing ancestors of root,
   state and secrets must not be group/other-writable: preflight fails with
   `E_ANCESTOR_WRITABLE` and `ancestor_path`, never repairs their permissions.
   The role sets exact modes on directories it creates (0755 missing ancestors,
   0700 private directories), independent of umask. Systemd paths accept only
   absolute literal ASCII paths using letters, digits and `_./:@+-`; whitespace,
   quotes, specifiers, globs and escapes are refused, not escaped.
   Agent and connector identifiers use
   `[a-z0-9][a-z0-9-]{0,62}` and are checked before any deployment writes.

```sh
scripts/test.sh
scripts/check-public.sh
scripts/check-public.sh --range <initial-commit>..HEAD
scripts/syntax-check.sh
```

The syntax script explicitly skips when Ansible is unavailable; the Python
suite then exercises the same deployment engine and a merge fallback, not an
Ansible execution. No gate downloads dependencies or contacts a remote.

Deployment unit tests inject offline cook, service-manager and static-verifier
commands. Their private scratch ignores `TMPDIR`/`HOME`; a test-only metadata
boundary models root-owned 0755 ancestors **above** the scratch root. Files and
metadata inside it remain real, including unsafe-ancestor refusal cases. No host
permissions are changed. Unprivileged-account tests explicitly skip as root.
The real static verifier is opt-in with `CONNECTOME_TEST_SYSTEMD_ANALYZE=1`;
it skips when systemd tools, a user runtime directory or user manager are absent.
Historical compatibility tests skip if their local Git baseline is unavailable.

Publication: publish only the clean branch descending from the initial commit;
do not mirror unrelated refs. The public checker scans tracked and nonignored
untracked working-tree files, including paths. With `--range A..B` it also scans
commit subjects/bodies and raw author/committer identities for that range, **not
every ref**. Public config must be plain scalars; the checker refuses the rest.
YAML/JSON string keys and values must be unescaped and single-line (simple quote
delimiters are allowed). Only two exact-path Ansible expressions may be multiline;
each line is still checked with its parent key. One rule table checks raw text in
all files and key-context views of accepted config scalars. Markdown is raw text
only, with no decoding claim. See [exact publication coverage and limits](docs/publication.md).
Generic rules allow attribution in Git identities with an approved email domain;
external denylist terms still apply to those identities.
Review all publication content as well; this denylist-based gate is not a secret
or personal-name detector with perfect recall.

The **private repository** owns the denylist and runs the check before publishing.
Never copy private terms into this public tree, including tests or encoded forms.
Supply its external UTF-8 file with `--deny-file PATH` and/or the environment
variable `CONNECTOME_PUBLIC_DENYLIST` (a file path). Use one term per line;
blank lines and lines beginning with `#` are ignored. Publication automation must
use `--require-denylist` to fail closed when no terms are loaded:

```sh
scripts/check-public.sh --require-denylist --deny-file <external-policy-file> --range <initial-commit>..HEAD
```

Without a denylist the checker warns clearly and runs **generic rules only**;
that mode cannot check private identifiers. See the coverage document for policy
matching details and limits.

Hash-pinned local runtime packages can be supplied through the closed
[`cook.host_dependencies` list](docs/lock-contract.md#additional-or-generic-host-inputs),
including operator-supplied prebuilt artifacts. The legacy framework archive
configuration remains supported.

License: [MIT](LICENSE).

Existing hand-built agents can opt into a hash-pinned, verbatim controller-local
[`recipe_file`](docs/lock-contract.md#operator-supplied-recipe-opt-in) without
regenerating connector/module recipes. Optional
[`secret_files`](docs/lock-contract.md#file-only-secrets-opt-in) deliver selected
secret values through private files and EnvironmentFile path variables. A supplied
recipe may use cook's host-installed [`source: {external: true}`](docs/lock-contract.md#host-installed-external-mcp-servers)
MCP servers; their commands and path args must be root-owned, non-writable
(`E_EXTERNAL_COMMAND`) and usable by the agent account (`E_EXTERNAL_ACCESS`).
Agent-level [`external_data_args`](docs/lock-contract.md#host-installed-external-mcp-servers)
is a closed list, one entry per server:
```yaml
external_data_args:
  - server: alpha
    command: /usr/bin/node
    args: [/opt/example/mcp.js, /absolute/agent-home/registry.json]
    path: /absolute/agent-home/registry.json
    kind: file
```
Required `command` (absolute) and the full string-list `args` must match the
recipe invocation byte-for-byte; any mismatch refuses the whole server with
`E_EXTERNAL_DATA_ARG`. Required `kind` is `file` or `dir`: the exception permits
an agent-owned file 0600 or directory 0700 under its home, with safe ancestry.
Without optional `flag`, data must be positional, never first or immediately
after a flag. With `flag: --state`, only exact `--state PATH` or `--state=PATH`
is allowed; code-loading flags are denied. Both forms require `args[0]` to be
an absolute root-owned regular file with **no execute bits** (`mode & 0o111 == 0`,
e.g. a 0644 script), passing the normal ancestry/readability checks. No options
may precede the script, not even `--`. `env /usr/bin/sh FILE` is refused.
The command's fully resolved target basename must be exactly `node`, `python3`,
`bun`, or match `^python3\.[0-9]+$`; this allowlist is not configurable.
Symlink aliases do not count: `node -> /usr/bin/find` is refused, as are
`find`, `env`, `sh`, `dash`, `busybox` and `node2`. Together these rules put the
script immediately after a permitted interpreter, with no interpreter options.
Only declare paths the script reads as data, never agent-owned code; what the
root script does with the data remains the operator's responsibility. Commands
and all other args remain unchanged. Declarations appear in the role lock view, plan and status;
file contents are never recorded. Recipe/file-secret delivery are
off by default; cook's run.sh, health checks and transactional rollback remain.

Offline template planning is available through
[`connectome-spawn validate|plan|dry-run|status`](docs/spawn.md). It shares one Python
package with the role's module-utils namespace, reads the existing catalog, and
never applies or activates a deployment. Start with `examples/spawn/`.

[Seed modes and clean-start initiation](docs/spawn-lifecycle.md) define the offline
persisted lifecycle, pinned config-repo name record, and per-agent identity ownership.
Explicit provider APIs now support Zulip bots, direct-key or inference-gate token
files, and optional file secrets. Deployment adapters, role wiring and the chat
frontend remain later work; plans still never activate anything.
