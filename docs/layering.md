# Public library, private deployment

A private repository pins this library as a submodule called `public`:

```sh
git submodule add <public-repository-url> public
git -C public checkout --detach <reviewed-full-commit-sha>
git add .gitmodules public
```

These are operator instructions, not commands run by the offline gates. Updating
the gitlink is an explicit reviewed change; do not follow a floating branch.
Copy `examples/private-skeleton/` into the private repository root. Its layout:

```text
public/                        # pinned submodule
inventory/hosts.yml
agents/example/agent.yml
overrides/agent.yml
connectors/                    # private descriptors, same schema as public
deploy.yml                     # playbook
```

The playbook loads the role at `public/roles/connectome_agent`. The explicitly named
`connectome_agent` mapping is what the role consumes. Mirror `overrides/` in the
private repo; never edit files inside the public submodule to customize an agent.

## Exactly four layers

1. Public `defaults/agent.yml` plus the public connector catalog.
2. `connectome_private` (typically `overrides/agent.yml`).
3. `connectome_group` (a mapping supplied by private group vars).
4. `connectome_agent` (the named agent mapping).

The only production merge site is `merge_layers` in the role's module utility:
a **left fold** of Ansible `combine(recursive=True)`, then null pruning and
connector/module filtering. Lists replace, not append. Inputs are not mutated.
The left fold matters: merging all four arguments with Ansible's reverse fold
can resurrect keys across a null tombstone.

```yaml
# public defaults
budgets: {max_steps: 100, max_cost: 5.0}
modules: {memory: {enabled: true, settings: {mode: compact}}}
# private layer
budgets: {max_steps: 50}
# group layer
budgets: {max_cost: 3.0}
# agent layer
budgets: {max_cost: null}
modules: {memory: {enabled: false}}
```

Result: `budgets: {max_steps: 50}`, `modules: {}`. Null removes a mapping key;
it is not a literal YAML null in the output. Deleting a whole mapping and then
reintroducing it in a higher layer starts from that higher layer, not the lower
mapping. Null list elements remain list elements. A higher `enabled: true` can
re-enable an entry disabled by a lower layer. Quote neither boolean.

An entry with effective `enabled: false` or null is absent from the adapter
input, pin resolution, requirements and imports. The adapter must build/import
**only** entries present in that input; no discovery or catalog scan is allowed.

## Private connectors and pins

Load a private descriptor file (the outer key is the connector name) into
`connectome_private.connectors`. For multiple descriptors, assemble a mapping
with distinct keys; this is catalog construction, not another layer merge.
Private overrides of public connector fields use that same mapping. For example:

```yaml
connectors:
  github-mcpl:
    enabled: false
  extra:
    repo: https://example.invalid/connector.git
    pin_ref: connectors.extra
    enabled: true
    build: [[make, build]]
    settings: {}
    required_secrets: [EXTRA_API_KEY]
```

Keep an entire private `versions.yml` containing the public pins plus private
keys and set `connectome_versions_file` to its controller path. Pin lookups use
dotted mapping keys. Only enabled connectors need resolvable nonzero full SHAs.
Do not put actual values or secret-store paths in the public catalog.

## Secret providers

By default `connectome_secret_lookup: connectome_file` selects **target-side**
JSON loading, not an Ansible controller lookup. Its path is the agent account's
passwd home plus `.config/connectome-secrets/NAME.json` (not controller HOME or
XDG overrides). Agent-level `secrets_file: /absolute/private/example.json` overrides
it. The plan shows the effective path; `connectome_secret_lookup_options.path`
is not the override for this mode.

Before prepare and again before activation, the role requires a real directory
0700 and regular file 0600, both owned by the effective agent uid; symlinked
files/directories/ancestors, missing paths and mismatches fail closed with a clear
message. It never fixes permissions/ownership or creates this file. Provision it
privately, outside version control, even for an agent with no declared secrets
(an empty JSON object suffices). Check mode validates metadata, but does not read
values. Target-side lookup returns only declared `required_secrets`; extra keys
are ignored, missing/non-string declared values fail. It runs after cook lock
validation, and values never enter cook, the spec, lock or result.

For an existing external provider, `connectome_secret_lookup` may instead name
an installed controller lookup plugin; `connectome_secret_lookup_options` supplies
its keyword arguments. This repository installs no collections. Only declared
names are requested, under `no_log`, and facts are cleaned afterward. The target
secrets-file metadata convention still applies before starting. The standalone
bundled controller lookup remains a legacy helper; the role's default bypasses it.

## External runtime state and budgets

Supply agent-level `state_dir: /absolute/persistent/example/data`. There is no
HOME-derived default: a StateDirectory-style path is valid too. It must be outside
and non-overlapping with the deployment root, including `builds/` and `current/`.
Symlink paths/ancestors (including links into builds) are refused. The role creates
a missing directory as the agent uid, mode 0700. An existing directory must already
have that mode/owner; it is never repaired, moved, copied, rewritten or deleted.
The role's bookkeeping stays at ROOT/state/ownership.json, **not** in `state_dir`.
Back up and select existing live state explicitly; no automatic migration occurs.
Cook binds DATA_DIR and host CWD through its split layout; no guessed host flags
or recipe fields are used. The rendered unit starts in `state_dir`, and cook's
`run.sh` itself changes to `layout.state` (the same `state_dir`) before executing
the host. Changing only systemd's `WorkingDirectory=` does not change the host's
effective cwd. Host cwd-relative files, including `mcpl-servers.json` and the
agent overlay, therefore resolve under `state_dir`, not the deployment root or
HOME. Back up/audit those files there when migrating; the role does not discover
or relocate them. There is no per-agent `working_directory` option.

HOME is independent of cwd and DATA_DIR. An operator-provisioned private systemd
`EnvironmentFile=` can set HOME for the runtime process; neither the reviewed
cook unit/launcher nor the role's environment drop-in assigns HOME. For example,
an additional private drop-in can reference a private mode-0600 file containing
`HOME="/absolute/persistent/example/home"`. Keep that drop-in separate from the
role-owned `90-connectome-env.conf` and its generated `secrets.env`. HOME remains
an adapter infrastructure variable: do **not** declare it in `required_secrets`
or `secret_files`. Its build-time value is not a runtime HOME setting. The
offline real-cook test simulates EnvironmentFile delivery and observes HOME,
cwd and DATA_DIR inside the launched process; it is not a live-systemd test.

Use distinct non-overlapping state/layout paths per
agent. Rebuild cleanup is restricted to owned build directories; rollback only
restores code/units/env, never data. Runtime/schema migrations remain operator risk.

Effective `budgets` from all four layers are shown in the nonsecret `--check`
plan, alongside `state_dir` and `secrets_file`. Host `src/recipe.ts` at the pinned
`cef933981dc0699fc00e5189b129d854ff8d43f6` has no step/cost-limit fields (nor does
the inspected local host); cook `011d73bc` exposes none. The adapter refuses any
nonempty budgets with `not supported by host recipe schema at <host-pin>` before
spawning tools. No fields are invented and no enforcement is implemented here.
Public defaults retain example budgets for merge/plan visibility; deploying with
this host requires an explicit highest-layer `budgets: null` tombstone. An empty
mapping alone does not clear lower-layer values. Check is a plan, not a promise
that adapter validation or runtime health will succeed.

## Operations

### Activation and migration staging

Two optional **per-agent** policy fields are closed schemas:

```yaml
activation: install_only  # start (default) | install_only
exclusive_with:           # default: []; exactly scope + unit per entry
  - scope: system         # system | user
    unit: old.service     # literal NAME.service, no paths or patterns
```

Omitted fields and explicit `activation: start` / `exclusive_with: []` preserve
the existing render, adapter argv, lock bytes and role task sequence. Policy is
excluded from cook input and the release request identity. Changing only policy
does not rebuild: a manifest-owned, matching `.prepared` request and validated
lock are reused (including verification of installed operator recipe bytes).

`install_only` builds/verifies the release, renders and installs unit files and
their environment drop-ins, writes `secrets.env` and declared secret files, and
atomically switches `current`. It installs the unit-name link, **not** `[Install]`
enablement/alias/target links. It never calls enable, start, restart, stop or
disable, including on failure rollback. Previously owned enablement links are
removed only after the associated units have passed the inactivity guard.
Before any deployment mutation (including the Ansible root/serialization lock),
the target and any previous units must be inactive; active or transitioning
units produce typed `E_UNIT_ACTIVE` with `unit` and `scope`. A renamed agent
cannot silently retire a running previous unit. Inspection failure fails closed.

Static health checks validate the installed lock, `current` target, unit-file
presence, and the manager's exact required `EnvironmentFiles` path via
`systemctl --user show`, after daemon-reload. When `systemd-analyze` is installed,
`systemd-analyze --user verify` must pass. Runtime health commands are **not** run.
The activation result reports `activation: install_only`, `unit_state: inactive`,
and each configured `exclusive_with` record with its observed `state`.
The check plan identifies the activation policy but does not query systemd or
promise runtime health. A user manager is still required for real staging.

`exclusive_with` applies only to starting, not staging. Start checks each listed
unit with `systemctl --system is-active NAME.service` or
`systemctl --user is-active NAME.service`; reading system scope needs no privilege.
An active or activating unit produces typed `E_UNIT_EXCLUSIVE`, naming its
validated `unit` and `scope`, before enabling/starting. Inactive/failed/unknown
units permit start; transitions such as reloading/deactivating are refused
conservatively. In install-only mode these units are reported, even if active,
and never cause a conflict refusal. Unavailable/malformed inspection is reported
as `unavailable` during staging, but is an error rather than permission to start.
No listed unit is stopped by this policy.

For a migration, stage with `install_only` while the old **different** system
unit runs. Stop that old unit explicitly, then apply the same private inputs
with `activation: start`: the role reuses the installed release, installs its
owned enablement links and starts it using the existing restart operation.
`ROOT/state/activation` records a successful staging, so promotion also works
for units without an `[Install]` section. This is deployment bookkeeping, not
runtime state or a cook-lock format change. Keep activation policy consistent
across the role's prepare/activate tasks. As with the existing threat model,
do not race external unit starts or other same-user writers against deployment;
the per-agent lock serializes role operations, not external systemd clients.

The adapter receives stable state storage outside all builds. Keep old builds
until the deployment succeeds; garbage collection is intentionally manual.
No database migration or state rollback is attempted. Rollback restores the
previous unit links, env-file reference and `current` pointer, restarts previous
units and checks their health. A first-deployment failure stops/disables the new
units and removes `current`. A failed recovery is reported, not claimed healthy.

### Operator rollback to a recorded request

Restore the **exact previous private layers, versions file and immutable adapter
argv**, keeping the same deployment root, state_dir and unit_dir. Run the normal
private deployment playbook (use `--check` first). Its request ID identifies the
retained manifest-owned build; prepare reuses its validated `.prepared` marker,
and activation restores its units/current pointer, restarts and health-checks it.
There is no cook rebuild, state copy, migration or data rollback. Current secret
values are resolved normally, so this is code/config rollback, not secret rollback.
Select `activation: install_only` to roll back the pointer, units and environment
without starting anything; all affected units must first be inactive. Failure
recovery in install-only mode follows the same no-start rule and uses static
health checks for the restored release.
Keep the previous private inputs and build; an unknown/unowned/modified build is
still refused. If the old build/marker is missing, check predicts a new build:
stop rather than claim this is rollback. A new build-ID selector is not provided.

Check mode validates pins, state/secrets metadata and available locks, predicts build/pointer changes,
and does not run cook, systemctl or secret lookups or write files. It cannot
predict secret rotation or runtime health without executing private lookups and
checks. The role serializes each mutation with a per-agent file lock. Separate
agents must not share unit names. Deployment is recoverable on task failure,
not a crash-consistent multi-file transaction; after process/power loss, rerun
and inspect the user service manager before removing any builds.
