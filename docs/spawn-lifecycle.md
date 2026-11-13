# Seed modes, clean-start initiation and explicit providers

Plan/dry-run remain offline and always say `activationAllowed: false`. The
Python API now includes an explicitly configured **Zulip identity provider**,
local AI-access providers and a file-secret provider. Only explicit provider
calls perform effects; merely importing the package or planning never contacts
Zulip or reads credentials. There is an **executor-only Python lifecycle backend**, staged verification and
an audited recovery CLI. The default executor only records proposed actions;
it cannot acknowledge deployment. There is **no real executor, apply/start CLI,
automatic role wiring or chat frontend**. Tests use temporary paths and fakes.
This is not a qualified service deployment path.

A new agent requires only **its own identity (a Zulip bot)** and **AI access
(an inference-gate agent token OR a direct provider key)**. Runtime integrations,
extra secrets, connectors, subscriptions, seed import and other platform actions
are optional; choosing one may require its adapter, but none is an additional
mandatory provider for agent creation. No orchestration service is a spawn
backend dependency. Only Zulip transport provisioning is implemented.

## Modes

Template v2 requires `seed: {mode: required|clean|optional}`. There is no default.
V1 is refused with a migration message: replace `seedPolicy` with `seed.mode`
and change the template schema to `connectome.spawn-template/v2`.

* **required**: supply opaque seed bytes (`--seed-file` for planning). Steps are
  build, hold **without activation**, ingest, durably write `state/seed.consumed`
  containing computed SHA-256 `digest` and UTC ISO-8601 `timestamp`, then start
  and pass the journaled serviceReady gate before recording `active`.
  Activation checks concrete name, summaryParticipant and home
  channel, plus a concrete systemPrompt when present. Nothing starts the seeded agent before the receipt is committed.
* **clean**: rejects seed bytes; follows the initiation flow below.
* **optional**: explicitly reports `branch: seeded` when seed bytes are supplied,
  otherwise `branch: clean`. Seeded takes the required ordering; clean takes
  initiation. The branch and digest are bound into the plan and persisted state.

The local seed input is a nonempty, nonsymlink file up to 1 MiB. Payload bytes
are opaque; `SeedImporter.ingest(payload, store, operation_id)` defines the
pluggable interface. The test fake writes bytes to a fixture store. Anthropic
and OpenAI import formats and implementations are **adapter hooks**, not
implemented here. Importer completion must mean durable ingest, not scheduling.

No re-ingest: an existing store, even an empty one, is never a fresh import.
An existing store without a consumption receipt is specifically reported as
recovery. A receipt without matching lifecycle intent is also refused. Retrying
an already completed operation returns current status without effects. A new
operation cannot import an already consumed seed, even with identical bytes.
Deleting receipts or resetting lifecycle files is not a recovery procedure.

## Clean initiation

The normal clean path is `planned -> initiation -> named -> active`:

1. Build using the concrete bootstrap identity/prompt in the plan. Create a
   new temporary transport identity **owned only by this agent**. No shared pool
   exists: different agents never reuse a bot's message history.
2. Join the home channel in topic **`initiation`**, then start on its own
   temporary identity. Any members of the home channel may participate in the
   dialogue, including the creator; there is no creator-only speaker restriction.
3. Normally the **initiator agent** writes the name record and commits it in the
   agents config repository. The new agent may write its own record **only when
   explicitly asked during initiation and granted write access to that repo**.
   Write access is what makes self-naming possible; there is **no extra approval
   step by design**. The role/frontend reads a specific pinned commit through
   shared module-utils `read_name_record(source)` (also used by the lifecycle
   transition), never the repository working tree. `named` persists the validated
   record and provenance, not a dependency on later repo/file availability.
4. Check the provider's `supports_rename` capability:
   * **replace** (`false`): provision a distinct permanent identity using the
     chosen name, redeploy with the same memory store, pass serviceReady,
     close initiation, then retire the temporary identity. It is never reused
     or returned to a pool.
   * **rename** (`true`): rename the temporary identity using the chosen name
     record. The same handle becomes permanent; redeploy with memory kept.
     Do **not** retire that handle as temporary cleanup.
   Both paths require serviceReady (health plus every selected connector
   handshake) before closing initiation or recording `active`. Unknown/failed
   probes keep promotion pending; no temporary identity is deactivated.

Optional template configuration (the shown value is the default):

```yaml
identity:
  temporaryNameTemplate: ConnectomeNewborn{timestamp}
```

The template must contain exactly one `{timestamp}` token, with safe ASCII
identifier literals; a rendered name must start with a letter and contain only
letters, digits, underscore or hyphen, at most 256 characters. UTC timestamps
are compact and sortable: `YYYYMMDDTHHMMSSffffffZ`, for example
`ConnectomeNewborn20260102T030405123456Z`. The timestamp/name is generated once
at lifecycle creation and persisted, never regenerated on retry. Read-only
plans record the convention, not a wall-clock value, keeping hashes deterministic.
Before any provisioning, the rendered name is checked against the provider's
`max_name_length` and `name_pattern` (full-match charset); names are refused,
**never truncated**. Name records are also checked against provider capabilities
before entering `named`. The fake defaults to 63 ASCII letters/digits/underscore/
hyphen, starting with a letter. A rename-capability change during initiation is
refused. Name collisions within the provider's ledger and owned identities are refused,
never resolved by sharing an account or silently changing the name. Realm-level
uniqueness is limited to what the transport enforces.
Plans list conditional rename/replace steps; no provider capability is assumed
by the offline planner (`identity.supportsRename: null`).

The closed name record requires name, displayName and summaryParticipant;
systemPrompt is optional (JSON or strict YAML):

```json
{
  "name": "ExampleAgent",
  "displayName": "Example Agent",
  "systemPrompt": "Help the creator with their work.",
  "summaryParticipant": "ExampleAgent"
}
```

Name and summaryParticipant use `[A-Za-z][A-Za-z0-9_-]{0,62}`. Display name is
at most 128 characters, letters/digits/underscore, spaces, periods and hyphens.
If present, the prompt is at most 65,536 characters, may include newline/tab,
and is data, never executed. **Absent means no system prompt**: no placeholder,
null value, empty string or bootstrap-prompt fallback is inserted. All supplied
fields reject empty/trimmed-away values, template placeholders,
control/format characters (except prompt newline/tab); identifiers reject path
traversal, shell punctuation and unsafe separators. Unknown fields or missing
required fields,
symlink files, oversized input, and malformed/duplicate-key input are refused.
Placeholder detection is conservative syntax, not semantic approval of a prompt.
The future runtime adapter must enforce config-repo write access. Commit author
and committer are informational provenance, **not** authentication or a permission
check; neither a commit identity nor a plan grants write access.

### Recording and pinning a name

The initiator writes the above JSON (omit systemPrompt entirely when not wanted)
to, for example, `names/example-agent.json` in its local agents config repo, then:

```sh
git -C "$CONFIG_REPO" add -- names/example-agent.json
git -C "$CONFIG_REPO" commit -m 'Record agent name'
git -C "$CONFIG_REPO" rev-parse HEAD
```

Pass that full lowercase 40-hex commit, not HEAD or a branch/tag, to the role's
single name-record ingress:

```python
source = {"kind": "repo", "repo": "/absolute/agents-config",
          "commit": full_commit, "path": "names/example-agent.json"}
record = read_name_record(source)
lifecycle.transition("example-agent", "named", "record-name", name_source=source)
```

The commit must already exist locally. Only local Git plumbing reads the commit,
tree and blob: no checkout, network, fetch, filters or working-tree reads. Missing
commits/paths, non-commit pins, non-blob entries, symlink mode 120000, submodules,
non-full SHAs and paths containing `..` components are refused. Repo identity is
the absolute local repository top, not a remote URL or subdirectory. Every path
component must be nonsymlink; non-bare repos require a real `.git` directory.
Gitfiles and linked worktrees are refused; bare repos with `core.bare=true` work.
Git uses only explicit `--git-dir`, never repository discovery. A pin matching
any ref basename is refused even when that ref targets the same commit. The receipt
`nameRecord` contains `recordHash` (canonical validated-record SHA-256) and
`source: {kind, repo, commit, path, blob, author, committer}`. Author/committer
are the commit headers, including identity and timestamp/offset. Source metadata
is bound into operation intent separately from the content-only record hash;
identical file and repo records have identical recordHash values.

`read_name_record(source, with_provenance=True)` returns `{record, recordHash,
source}`. The existing `read_name_record(file_path)` and transition `name_file=`
remain supported; explicit `{kind: "file", path: ...}` works too. File provenance
has only kind and absolute path. `ingress_root` applies to file inputs; a repo
source with a file ingress root is refused, not silently authorized by that root.
Callers must explicitly select trusted repo locations.

For plan/dry-run, pass `--name-repo "$CONFIG_REPO+names/example-agent.json@$FULL_COMMIT"`
(or `--name-file PATH`), or `make_plan(..., name_source=source)`. The optional
`plan.nameRecord` shows the pinned record, recordHash and provenance. It describes
the future named input, **not** a replacement of the bootstrap recipe or an
activation grant. Dry-run never proposes writing a fabricated name record.
Status shows the persisted receipt even if the original repository is unavailable.

Seeded agents instead use `planned -> active`, with the required seed sequence.
Any nonterminal state except `failed` can be marked `failed`; any state except
`retired` can transition to `retired`. Failure is an explicit local record, not
proof that a running service has stopped. Retirement requires the adapter to
stop/retire the agent, then retires current owned identities and closes any open
initiation. Explicit terminal retirement is separate from promotion: rename promotion never retires
the now-permanent handle.
`failed` cannot resume directly; `retired` is terminal. Other transitions fail.

## Persistence, operations and recovery

`Lifecycle(state_root, adapter=..., identity_provider=..., importer=...)` exposes:

* `create(agent, seed=..., identity=..., home=..., operation_id=..., payload=None, identity_config=None)`
  persists `planned`; identity uses the name-record shape (optional systemPrompt);
  identity_config is the validated template `identity` object (or its default).
* `transition(agent, target, operation_id, name_file=None, name_source=None, payload=None)`
  enforces ordering. Only `named` accepts/requires exactly one name source.
  Seeded `active` requires bytes matching the planned digest. Clean activation does not accept a seed.
* `status(agent)` reads without creating anything.

The same functions are importable as `ansible.module_utils.connectome_spawn`.
The existing role deployment engine is **not automatically wired** to these
APIs. `ExecutionBackend` is the explicit Python frontend, using `read_name_record`
for naming. The executor boundary carries thin role-engine requests; it does
not bypass validation or install planner draft recipes.

Per-agent files are `ROOT/AGENT/lifecycle.json`, `ROOT/AGENT/state/store/`, and
`ROOT/AGENT/state/seed.consumed`. The latter is the required `state/seed.consumed`;
`ExecutionBackend` binds this exact external store without copying/resetting memory. Completed
operation ids and input digests live in the lifecycle file. Same id plus same
input is a no-op, even after restart or later transitions; id reuse with changed
input is refused. IDs use 1–128 ASCII letters, digits, underscore or hyphen.
Agent identifiers use the existing lowercase instance-name grammar.

Writers hold per-agent POSIX file locks. Private 0600 temporary files are fsynced,
atomically replaced, and their parent directory fsynced; newly created state
directories are 0700. Intent is persisted **before** every adapter/identity/import
step. A crash before final transition commit leaves a pending operation, even
if an effect or its receipt completed. Status reports recovery; **no pending
operation is automatically replayed**, with either the same or a different id.
This includes a crash after seed import or marker write but before activation.
No exactly-once external effect guarantee is claimed.

An ordinary explicit failed transition is supported, but pending uncertain
operations block every transition, including failed/retired: an operator must
first reconcile service, memory and identity-provider reality. Recovery now resolves a checkpoint only after an explicit operator statement
(see below); it never assumes an interrupted effect did not happen. Symlink persistence paths and
corrupt state are refused. Same-uid hostile path races and state tampering are
outside the trusted-caller boundary. The journal is bounded to 1 MiB; no automatic
history pruning or identity tombstone reclamation is implemented.

```sh
scripts/connectome-spawn status example-agent --state-root /absolute/spawn-state
```

Status includes state, seed intent, pending operation, recovery reasons and owned
identity notice. Missing state is refused, never initialized. Like plans, status
contains protected prompt/identity data: keep its output private.

## Identity-provider contract and T4 work

`IdentityProvider` declares `supports_rename: bool`, `max_name_length: int` and
`name_pattern: str`, plus these operations (all take an explicit operation id):

* `provision_temporary(agent, name, operation_id) -> handle`
* `provision_permanent(agent, name_record, operation_id) -> handle`
* `rename(handle, name_record, operation_id) -> handle` (same handle)
* `retire(handle, operation_id)`
* `list() -> list` (credential-free ownership/liveness observations)

Handles are closed `{id, agent}` data: a stable provider-local id (1–128 ASCII
letters/digits/underscore/hyphen) and the owning agent identifier. They contain
no credentials. Providers must isolate native account/message history per agent,
refuse ownership collisions and name collisions within their ledger and owned
identities (realm-level uniqueness follows transport rules), persist operation
receipts, and make identical operation retries idempotent. Reusing an operation id with changed intent fails.
No implicit reclaim, reuse or transfer of temporary identities is allowed.

`FileIdentityProvider(root, supports_rename=False)` initializes the offline fake;
`FileIdentityProvider(root)` reopens its persisted capabilities, identities and
receipts. Optional `max_name_length`/`name_pattern` configure fake capability
checks. The fake uses UUID ids, locked atomic writes and agent-scoped operation
ids. Its `list()` reports every owned identity as liveness unknown and requiring
reconciliation if the agent crashed. Retired records are retained, never leased
or reused. Uncertain real effects still require recovery through the lifecycle
journal; the fake's atomic local operation is not proof of provider atomicity.

### T4 slice 1: providers now available

`ZulipIdentityProvider(root, zuliprc=..., secrets_root=...)` implements the exact
identity interface. Configuration is role-only Python API input, **not** a
public template, CLI credential ingress, environment default or plan field.
The operator supplies a dedicated human administrator's zuliprc-style file:
`[api]` with exactly `email`, `key`, `site`. It must belong to the executing uid
and have mode **0600**; the shared reader also refuses symlinks in every
component, hard links and non-regular files. Credential bytes, their input path,
and the raw operator email/site never enter handles, journals, plans or status.
The adapter requires a **human organization owner (role 100) or administrator
(role 200)**, not a normal member or a bot. The account must also be allowed by
the realm's bot-creation policy (`can_create_bots_group`). Use a dedicated admin
with minimal stream subscriptions, not a personal administrator: its bots can
post wherever their owner is subscribed, including private streams. This slice
does not support a lower-privilege service owner and creates no subscriptions.

Capabilities are `supports_rename=True`, `max_name_length=63`,
`name_pattern=[A-Za-z][A-Za-z0-9_-]+` (minimum length **2**). Rename is supported by Zulip's
`PATCH /api/v1/bots/{id}` with `full_name`; retirement uses
`DELETE /api/v1/bots/{id}` (deactivation, not deletion/recycling).
**63 and this ASCII grammar are conservative adapter-supported limits, not
assertions of Zulip's universal server maxima/complete charset.** The reference
API client does not establish universal realm/version limits. Stricter server
policy can still refuse a name; names are never truncated. Provision/rename
set `full_name` to the exact logical `name` (temporary name or name-record name),
not `displayName`. Native email/short name remains unchanged on promotion.

Create uses `POST /api/v1/bots` (`bot_type=1`), `GET /api/v1/bots`,
`GET /api/v1/users/me` and individual `GET /api/v1/users/{email-or-id}` reads.
There is **no whole-realm `GET /users` dependency**. Zulip's `get_bots_backend`
already filters to caller-owned active bots. Its entries contain `username`,
`full_name`, `api_key`, `avatar_url` and `default_*` settings, **not** `owner_id`,
`user_id` or `is_active` (do not confuse these with `/register` `realm_bots`).
Discovery uses the listed username to fetch that single user; the returned
`user_id`, `email`, `is_bot`, `bot_owner_id`, `full_name`, `is_active` must match
the reservation/operator. Later observations use its native id. No user-endpoint
`username` fallback is assumed. POST response fields are ignored; PATCH/DELETE
need only the success envelope. Unsupported/incomplete responses fail closed.
Production transport is HTTPS with normal certificate verification, no proxy
inheritance and no redirects. `allow_loopback_http=True` permits only the IPv4
loopback literal for offline tests; it does not enable arbitrary plaintext sites.
Errors are value-free and never include response bodies, headers or credentials.

The provider serializes writers and journals a random stable handle and its
native short name (`spawn` plus handle id) **before POST**. This opaque native
address is not a renamed display persona. Preflight checks case-insensitive
names against **all ledger tombstones** and the caller's active owned bots, and
checks the native prefix in that owned listing. It does not enumerate foreign
or inactive realm accounts. Zulip display names are not globally unique; this
provider does **not** promise realm-wide full_name uniqueness. Zulip only
enforces exact full_name uniqueness among active users; deactivated and
case-variant duplicates are possible. Routing and mentions must address agents
by native user id/email (for example, `@**Name|id**`), never a bare full_name.
Native-address collisions, including foreign/inactive accounts, are rejected
by Zulip at POST;
a refusal is never permission to adopt the colliding account. A matching owned
short-name prefix with a different full_name or a different per-user owner is
always refused. No unjournaled existing bot is adopted.
After POST (including a lost response), owned-bot listing and an individual user
read reconcile the reservation. Retries after process death find that same bot,
not a second POST. Only HTTP 400–499 with a bounded, parsed Zulip JSON
`result:error` body means creation was refused: the reservation and pending
intent are atomically released. The value-free error labels only 408/429
retryable and other 4xx refusals terminal (correct the request/policy first).
There is no automatic retry; a later explicit call may use the same or a new id.
Any 2xx with `result:error`, every 5xx (even parsed JSON errors), truncated,
malformed/oversized responses, redirects, network loss and timeouts remain
ambiguous: Zulip can commit a bot before returning an error. If no committed bot
is observable, the operation stays pending and POST is **never blindly repeated**. This
also covers a crash just before POST: an operator must reconcile the no-effect
case. New operation ids cannot bypass pending work for that agent. Observed,
owned rename/deactivate retries are idempotent assignments. After a parsed 4xx
refusal, an individual-user read verifies the real bot state: an unchanged
rename on its **first attempt**, or still-active retirement, releases only
that intent with a retryable/terminal label. A rename with any earlier ambiguous
attempt stays pending: that earlier PATCH may still land after a later refusal;
an already-applied effect completes normally. A subsequent terminal refusal
reports its HTTP status/category and directs the operator to `recover_rename`.
Failed or inconsistent observation keeps the intent pending, as do ambiguous mutation responses with no observed
effect. Completed operation ids bind exact intent, including the entire
validated name record.

For an ambiguous rename followed by a terminal refusal, retrying the identical
rename re-observes a late commit. To resolve the no-effect case without editing
JSON, use the explicit Python API:

```python
provider.recover_rename(handle, operation_id, observed="not-applied",
    reason="All earlier requests settled; unchanged owned bot observed",
    evidence_reference="operator-maintained-case-reference", settled=True)
```

Before asserting `settled=True`, the operator must settle/cancel **every earlier
in-flight PATCH** using provider-side request tracing/administrative evidence.
An unchanged GET alone is insufficient. If that cannot be established, keep the
intent pending. Recovery requires a nonblank evidence reference and reason,
re-observes ownership/name/activity, and atomically journals the original intent,
uid/euid, time, statement and observation before releasing it. `observed="applied"`
resolves an observed late commit to the original permanent handle instead.
Neither branch sends PATCH/DELETE or replays a rename. A separate subsequent
provider operation is explicit. This is an audited operator assertion, **not**
an automated remote-quiescence guarantee or a new CLI/provider connection.
Reasons/references must not include credentials.

Provider receipts are `root/zulip.json`, locked and atomically fsynced, with
retired tombstones kept permanently. Realm/operator/delivery-root binding is a
non-secret digest; changing that binding is refused, while operator key rotation
is allowed. Bot API keys are delivered only to
`secrets_root/AGENT/HANDLE_ID.key`: a single atomic **0600** byte file in a newly
created **0700** directory. No key is serialized into a receipt or handle, and
conflicting or unsafe existing key files are refused rather than overwritten.
Retirement first verifies remote deactivation, then validates and unlinks the
delivered key file and fsyncs its directory before committing the receipt. A
crash between unlink and receipt is safely retryable. A completed create cannot
redeliver a retired identity's key. Deactivation does not rotate the remote key:
external copies may work if an administrator reactivates the bot; do not do so.
Delivery uses one deterministic private staging inode (`.HANDLE_ID.key.pending`);
a retry promotes that same inode after an interruption, never accumulates extra
key copies. Partial/conflicting staging bytes require operator reconciliation.
Completed create retries also refuse a missing/unsafe delivery file. `list()`
observes only reserved/owned identities, reports
activity and pending reconciliation, and never returns API keys. Lost create
receipts are discoverable via listing, but this is **not** automatic replay or
recovery of a pending `Lifecycle` journal.

`AIAccessProvider` declares `kind`, `plan(agent)` and
`provision(agent, operation_id)`. Built-ins:

* `DirectKeyProvider(root, key_file=...)`: the operator supplies a file; provision
  validates it locally and records only its absolute path and SHA-256 digest,
  never its bytes. No provider request or authorization/quota check is performed.
* `InferenceGateProvider(root, token_file=..., pool=..., limit=...)`: plan returns
  an **operator-required privileged action** with agent, pool and limit. Pool
  uses a safe ASCII identifier (1–63 characters); limit is a positive integer
  up to 10,000,000, an opaque planning input, not a claimed enforcement unit.
  The provider never runs a command, invokes gate tooling or contacts the gate.
  Until the operator provisions the agent token and supplies the file, provision
  returns **`awaiting-provisioning`**, never ready/success. Ready means safe local
  bytes exist, not proof of their validity, agent binding or applied pool/limit.

Both use `root/access.json` for agent-scoped operation receipts, bind exact
intent/path, and allow an awaiting operation to become ready on the same id.
They refuse unsafe input or a changed/disappeared ready file. Direct keys also
report awaiting-provisioning if absent. All key/token files require current-uid
ownership, exact 0600, no symlink components and a single regular-file link.
The operator is responsible for access-token authenticity and per-agent binding.

`FileSecretProvider(secrets_root).resolve(agent, declared_names)` maps each
explicit uppercase secret name to `secrets_root/AGENT/NAME`. It returns only
path/digest metadata, reads with the same private-file rules, and never searches
the environment or creates/copies secrets. Requesting an absent/unsafe file is
refused; omitting an optional integration never requires that file.

Secret-file metadata currently uses unsalted SHA-256 content fingerprints. This
is appropriate for random API keys/tokens but reveals offline-guessable hashes
of low-entropy optional secrets. Keep receipts private; keyed/salted digests
remain a follow-up, not a confidentiality guarantee of this slice.

### Plan declarations and remaining frontend work

Plans include structured `providerActions` and `executorActions` distinguishing
`automatic` from `operator-required`. `lifecycle[].needsProvider` lists real
provider/adapter kinds. Mandatory capabilities are identity and AI access;
optional integrations need their own explicitly selected adapter. Deployment
steps now describe automatic **executor requests**, with a dry-run default;
production seed-format and conversation hooks remain unavailable. Planning
never instantiates providers, grants permissions or sends requests.
AI access uses `providerSelection: one-of`; unselected access reports
`selectionRequired: true`. Public policy accepts no credential paths/values.
Conditional identity steps retain `when` tied to `provider.supports_rename`;
offline plans assume no capability (`identity.supportsRename: null`).

Still required in later owner-approved windows: a real executor and translation
to the role engine, role/frontend authorization, production seed-format import,
initiator commits/self-naming write-access enforcement, real conversation routing
and subscriptions, runtime qualification, real health/rollback validation and
the first real spawn. The executor-only core and audited checkpoint recovery
below do not claim these integrations. No apply/start CLI is supplied.

Offline tests use local files and an in-process loopback fake HTTP server;
no real credentials, provider traffic or privileged platform commands are used.

### Local input path boundary

`Lifecycle(..., ingress_root=...)` and
`read_name_record(path, ingress_root=...)` optionally restrict name records to
an explicitly configured ingress directory. Paths keep their existing meaning
(relative to cwd, or absolute); an absolute path outside that root is refused,
not rebased. Without an ingress root, the caller still selects the file as
before, with its literal parent as the read boundary. Never derive the configured root from agent-provided input.

All local file reads reject `..`, symlinks in **every** component (including
the ingress root itself and its ancestors), non-regular files, and regular files
whose opened-fd `st_nlink` is not exactly one. Hard links are refused because
another name for the same inode may be outside the read boundary. This applies
to every input served by the shared reader, including role-written catalog
payloads: tools must materialize single-link files, not hard-link extractions.
Legitimate single-link nested paths work. Directory-fd traversal uses `O_NOFOLLOW`; Linux
`/proc/self/fd` confirms containment of the opened regular file before reading.
Symlinked roots are deliberately unsupported, even when their targets are
inside the configured tree.

This shared rule covers name records; opaque seed files; lifecycle/status
`lifecycle.json` and `state/seed.consumed`; identity-provider state (`identities.json`,
`zulip.json`) and AI-access receipts; private operator/key/token/declared-secret
files; and file-backed template, instance, stores, observed-state,
catalog descriptor/versions and payload inputs. State-root/store path checks
also continue to reject symlinks. Store contents belong to the injected adapter
and are not read by the spawn core. Persistence writes retain the documented
trusted same-uid caller assumption; this is not hostile concurrent-writer
isolation or an authenticity check on regular-file contents.

## Executor-only apply, verify and reconcile

`ExecutionBackend(root, validated_plan, private_repo=..., build_root=...,
executor=..., supports_rename=False)` accepts the **trusted output of make_plan**,
not arbitrary public JSON authenticated by its self-hash. It checks the envelope
hash and instance/home binding, and persists the complete execution binding in
`ROOT/AGENT/execution.json`. Changing that binding is refused. The private repo
and build root are explicit operator inputs, never template commands or ambient
host defaults. `supports_rename` declares the injected provider's capability;
the default models offline replacement, not a discovery of Zulip capabilities.
No production provider is selected or instantiated by this backend.

```python
backend = ExecutionBackend(state_root, validated_plan,
    private_repo=config_repo, build_root=build_root, executor=offline_fixture)
backend.apply("initiate")
backend.apply("record-name", target="named", name_source=pinned_name_source)
backend.apply("promote", target="active")
backend.verify("observe-runtime")
backend.reconcile()
# Retirement is explicit; there is no destructive remove shortcut:
backend.apply("retire", target="retired")
```

For a seeded plan pass the matching payload to `apply`; its target defaults to
active. A provider-owned permanent identity precedes build. Build/hold do not
start anything; ingest and the durable seed receipt precede activation. Seed
formats still need an executor-owned importer; no production importer ships.
The existing low-level Lifecycle API remains usable with explicit test adapters.

| Lifecycle step | Executor request / acknowledgement |
| --- | --- |
| build | local AI-access input/ready check; selected optional secrets; Unix user with collision refusal; private directories; agent layer in private repo (executor must enforce write access); fresh cook build, install-only/no-start |
| hold | install user unit without start, from the **same** prepared build |
| join-initiation | join the agent's owned identity to home channel/topic initiation, home-channel members may participate |
| start-initiation / activate | install-only then switch/start the prepared build, supplying the current owned transport identity |
| redeploy-keep-memory | render chosen identity (absent prompt stays absent), fresh build, switch/start with the **same** external store; acknowledge, then pass journaled serviceReady before closing initiation or temporary retirement |
| serviceReady | journal service-health and an MCP handshake for every selected connector; require evidenced success before active/topic closure/temporary retirement |
| close-initiation | close topic while participating identities are still live; no identity recycling |
| retire | stop user unit; close any still-open initiation **before** provider deactivation of current owned identities; retain store and journals |

Each executor request receives explicit inputs and a stable
`operation:step:action` id; identity/import steps use `operation:step`. The
lifecycle fsyncs each individual action intent **before** execute, then retains
its credential-free receipt in `receipts`. Completed subactions are not repeated
on an explicitly recovered continuation. A missing/unknown acknowledgement
leaves pending intent, including with the default `DryRunExecutor`; it does not
advance state or pretend the recorder deployed anything. Build directories are
operation/phase-specific. Symbolic paths are bound and `require_bound` runs;
recipe/layout files and their hashes are regenerated after binding. Runtime
state is never copied into builds. Executor inputs/outputs and status contain
protected configuration; evidence must never contain keys or raw provider bodies.

Actions: `ai-access`, `validate-optional-secrets`, `create-unix-user`, `create-directories`,
`render-agent-layer`, `cook-build`, `install-user-unit`, `switch-start`,
`join-initiation`, `close-initiation`, `stop-unit`, `provision-temporary`,
`provision-permanent`, `rename`, `retire-identity`, `ingest`, `mcp-handshake`.
Planning lists automatic executor requests separately from operator-only work;
automatic means supported request orchestration, **not available real execution**.
AI-access acknowledgement means safe supplied local input, not authentication.
Privileged gate token minting/limits, credential supply, config-repo permissions,
Unix user/state deletion, user-manager provisioning and a real executor/first
spawn remain operator-only.
No useradd, systemctl, loginctl, cook, gate or Zulip command is run by this backend.

The role engine currently performs filesystem/subprocess effects directly and
consumes normalized deployment specs rather than spawn envelopes. Sharing it by
calling it here would violate the executor-only boundary. Requests therefore
name `Deployment.prepare`, `Deployment.activate` and `connectome_cook.adapt` as
the intended engine, instead of forking their build/switch/rollback/health logic.
A later owner-approved real executor must translate these typed requests into
that engine, re-resolve/verify local runtime and artifact inputs, enforce path,
ownership and launch audits, and durably track its internal effects. This slice
does not claim that translation or production runtime qualification is complete.

### Honest verification stages

`verify(operation_id)` is allowed only in `initiation` and `active` (never
retired/failed/planned/named). It separately journals observations/probes and records:

* `serviceReady`: executor service-health evidence **and** an MCP probe for each
  selected connector: start connector, initialize MCP, `tools/list`, close.
  The executor uses the installed launch, not planner drafts. With no selected
  connectors there is no connector handshake to claim.
* `providerAuthenticated`: independent provider-authentication evidence, not a
  safe key file or running service.
* `firstRealReply`: independent evidence of a real reply, not a synthetic
  readiness check or authentication alone.

Only an applied receipt with nonempty structured evidence and the stage's
explicit true result qualifies as success. Missing/incomplete/negative evidence
is conservatively unknown, never success. Verification does not send a test chat
message or authenticate through a real service here.

Promotion and seeded activation run the same checks as a **journaled gate**,
not a prior verify receipt: `serviceReady:service-health`, then
`serviceReady:mcp-handshake:CONNECTOR`. Only successful gate probes complete;
unknown/failed probes leave an explicit pending serviceReady status, keep the
temporary bot live (a renamed handle stays live as permanent), and prevent
initiation closure and `active`. Provider authentication and real reply remain
independent observations, not activation prerequisites. To retry a closed,
unsuccessful probe, first reconcile its exact descriptor/id and establish no
in-flight probe remains; explicitly recover that checkpoint as `not-applied`
when the observer permits it, then continue the original operation. A lost
successful probe receipt can instead be recovered from the observer's original
result, without re-running the connector. Recovery never invents probe success.
Probes also have durable intent; lost acknowledgements block blind re-probing. Distinct verification ids
request fresh observations; an identical completed id is a no-op.

`reconcile()` only asks the executor to observe the exact pending effect, or
current deployment reality when nothing is pending. It journals the observation,
never creates/restarts/replays, clears intent, or changes lifecycle state.
Without an observer, CLI reconciliation reports unknown, never absence/success.

### Audited recovery, not reset

```sh
scripts/connectome-spawn reconcile example-agent --state-root /absolute/spawn-state
scripts/connectome-spawn recover example-agent --state-root /absolute/spawn-state \
  --operation initiate --observed not-applied --reason 'Inspected the target and confirmed no effect'
```

`recover` requires a nonblank bounded reason, matching pending operation, and
`applied` or `not-applied`. It observes first (unknown is retained as unknown),
refuses a statement contradicting available evidence, and appends the uid/euid,
UTC timestamp, operation, checkpoint, statement, reason and observation to the
private journal. It resolves **one checkpoint**, not the whole transition, and
returns without executing anything. A separate explicit API call with the same
operation id and exact original inputs re-observes immediately before continuing
(and blocks if reality contradicts the statement): applied checkpoints are skipped;
not-applied checkpoints can be attempted again. Other ids remain refused.
The operator is responsible for resolving an in-flight attempt before asserting
not-applied; an unknown observation alone is never retry authorization.

A handle-producing operation requires the observer's original owned handle;
the CLI cannot fabricate one. Such lost-handle recovery needs the Python API
with an injected observer. Promotion/activation switch-start and ingest also
require an **applied observer result with nonempty evidence**; an operator's
reason alone is insufficient, so the observer-less CLI refuses `--observed applied`
for these unresolved checkpoints. Use the bound Python backend with a trusted
observer that checks the exact deployment or durably completed import. For an
import, first validate the full expected store against the planned seed digest;
a partial store is not applied, and must never receive `seed.consumed`. Leave
unknown imports pending rather than deleting stores/receipts or asserting success.
The same evidence rule applies to gate probes. Durable completed checkpoints
cannot be rolled back
by saying not-applied. Legacy pending journals without checkpoint history are
refused for continuation and need an external migration/reconciliation procedure;
there is no unsafe automatic migration, deletion/reset or auto-replay command.
Recovery statements/observations may write the local journal even on failure;
CLI errors for these commands report filesWritten as unknown rather than false.
Known lifecycle failures include fixed, value-free `code`/`detail` fields (unknown
agent, pending mismatch, contradiction, completed checkpoint, missing evidence).
Unclassified/parser errors remain generic; input values are never echoed.
A misspelled agent in status/reconcile/recover creates no directory or lock.

Residual audit limits: journals are private atomic JSON, not hash-chained,
signed or append-only; same-uid edits are outside the trust boundary. Reconcile
and receipt/audit history still grow without compaction. The 1 MiB journal cap
fails closed and can stop progress; rotation/archival needs a separately designed
operator procedure, not deletion of evidence. uid/euid do not identify the
original login/sudo user. These are not tamper-evidence or unbounded-liveness
claims.

Retirement deactivates rather than deletes/recycles the provider identity,
stops the unit and **keeps memory, seed receipts, config and ownership evidence**.
Deleting the Unix account, state, build history, credentials outside provider
retirement, or private repo records is explicitly operator-only. No remove
command silently turns retirement into data deletion.
