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

`connectome_secret_lookup` is an Ansible lookup plugin name;
`connectome_secret_lookup_options` supplies its keyword arguments. Production
can use an installed Vault lookup (for example
`community.hashi_vault.hashi_vault`) or a private wrapper accepting a secret
**name**. Supply any private store addressing and auth outside the submodule.
This repository does not install a Vault collection or store credentials.

The bundled `connectome_file` test lookup accepts `path` to a private JSON object
of name-to-string values. It opens only that controller file and returns only
requested names. Keep it outside version control with restrictive permissions.
The role resolves only explicit `required_secrets` from the effective agent,
connectors and modules after the lock confirms their names. It never enumerates
a secret store. All lookup/activation tasks are `no_log`; values never enter
cook argv, the normalized spec, a lock, or the Ansible result.

## Operations

The adapter receives stable state storage outside all builds. Keep old builds
until the deployment succeeds; garbage collection is intentionally manual.
No database migration or state rollback is attempted. Rollback restores the
previous unit links, env-file reference and `current` pointer, restarts previous
units and checks their health. A first-deployment failure stops/disables the new
units and removes `current`. A failed recovery is reported, not claimed healthy.

Check mode validates pins and available locks, predicts build/pointer changes,
and does not run cook, systemctl or secret lookups or write files. It cannot
predict secret rotation or runtime health without executing private lookups and
checks. The role serializes each mutation with a per-agent file lock. Separate
agents must not share unit names. Deployment is recoverable on task failure,
not a crash-consistent multi-file transaction; after process/power loss, rerun
and inspect the user service manager before removing any builds.
