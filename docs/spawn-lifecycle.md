# Seed modes and clean-start initiation (offline core)

This package supplies **no real providers, no network calls and no apply CLI**.
Plan/dry-run describe intent; they still say `activationAllowed: false`. Only
explicitly injected adapters can perform effects through the Python lifecycle
API. The included identity provider is a local file fake; tests supply fake
deployment and seed adapters. This is not a qualified service deployment path.

## Modes

Template v2 requires `seed: {mode: required|clean|optional}`. There is no default.
V1 is refused with a migration message: replace `seedPolicy` with `seed.mode`
and change the template schema to `connectome.spawn-template/v2`.

* **required**: supply opaque seed bytes (`--seed-file` for planning). Steps are
  build, hold **without activation**, ingest, durably write `state/seed.consumed`
  containing computed SHA-256 `digest` and UTC ISO-8601 `timestamp`, then activate.
  Activation checks concrete name, summaryParticipant, systemPrompt and home
  channel. Nothing starts the seeded agent before the receipt is committed.
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
3. The **agent itself** writes `name-record.json`. The role/frontend reads it
   using shared module-utils `read_name_record(path)` (also used by the lifecycle
   transition). No extra owner approval is required. `named` persists the validated
   record, rather than depending on later changes to that file.
4. Check the provider's `supports_rename` capability:
   * **replace** (`false`): provision a distinct permanent identity using the
     chosen name, redeploy with the same memory store, then retire the temporary
     identity. It is never reused or returned to a pool.
   * **rename** (`true`): rename the temporary identity using the chosen name
     record. The same handle becomes permanent; redeploy with memory kept.
     Do **not** retire that handle as temporary cleanup.
   Close the initiation topic. State becomes `active` only after all steps.

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
refused. Name collisions are refused, never resolved by sharing an account or silently changing the name.
Plans list conditional rename/replace steps; no provider capability is assumed
by the offline planner (`identity.supportsRename: null`).

The closed name record has exactly these fields (JSON or strict YAML):

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
Prompt is at most 65,536 characters, may include newline/tab, and is data, never
executed. All fields reject empty/trimmed-away values, template placeholders,
control/format characters (except prompt newline/tab); identifiers reject path
traversal, shell punctuation and unsafe separators. Unknown/missing fields,
symlink files, oversized input, and malformed/duplicate-key input are refused.
Placeholder detection is conservative syntax, not semantic approval of a prompt.
The agent's write path must be authenticated by the future runtime adapter;
this offline API does not authenticate who authored a file.

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
  persists `planned`; identity uses the four-field name-record shape;
  identity_config is the validated template `identity` object (or its default).
* `transition(agent, target, operation_id, name_file=None, payload=None)` enforces
  ordering. Only `named` accepts/requires a name file. Seeded `active` requires
  bytes matching the planned digest. Clean activation does not accept a seed.
* `status(agent)` reads without creating anything.

The same functions are importable as `ansible.module_utils.connectome_spawn`.
The existing role deployment engine is **not automatically wired** to these
APIs; T4 must explicitly connect its frontend/role execution to them and use
`read_name_record`, not bypass validation or install planner draft recipes.

Per-agent files are `ROOT/AGENT/lifecycle.json`, `ROOT/AGENT/state/store/`, and
`ROOT/AGENT/state/seed.consumed`. The latter is the required `state/seed.consumed`;
T4 must bind this external store without copying/resetting memory. Completed
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
first reconcile service, memory and identity-provider reality. This version
deliberately has no recovery/reset CLI. T4 must supply an audited recovery mechanism rather than
assuming an interrupted effect did not happen. Symlink persistence paths and
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

Handles are closed `{id, agent}` data: a stable provider-local id (1–128 ASCII
letters/digits/underscore/hyphen) and the owning agent identifier. They contain
no credentials. Providers must isolate native account/message history per agent,
refuse ownership/name collisions, persist operation receipts, and make identical
operation retries idempotent. Reusing an operation id with changed intent fails.
No implicit reclaim, reuse or transfer of temporary identities is allowed.

`FileIdentityProvider(root, supports_rename=False)` initializes the offline fake;
`FileIdentityProvider(root)` reopens its persisted capabilities, identities and
receipts. Optional `max_name_length`/`name_pattern` configure fake capability
checks. The fake uses UUID ids, locked atomic writes and agent-scoped operation
ids. Its `list()` reports every owned identity as liveness unknown and requiring
reconciliation if the agent crashed. Retired records are retained, never leased
or reused. Uncertain real effects still require recovery through the lifecycle
journal; the fake's atomic local operation is not proof of provider atomicity.

T4 must supply, **Zulip first**:

* A real Zulip identity-provider adapter and credentials. Provision a new bot
  identity per newborn; declare actual rename support and name limits. Preserve
  exclusive ownership, durable operation receipts and idempotent retirement.
* `LifecycleAdapter.perform(step, context, operation_id)` implementations for
  build/hold, join/start initiation, redeployment with memory kept, activation,
  topic closure and retirement. Each receives a distinct `operation_id:step`;
  context includes chosen identity, home, store, owned identity handles, literal
  topic `initiation` and `initiationParticipants: home-channel-members`.
  Never activate on build/hold; never retire temporary identity before permanent
  redeploy acknowledges completion; never retire a successfully renamed identity
  as part of promotion. Identity creation/rename/retirement use the separate
  IdentityProvider interface, not deployment callback return values.
* Authenticated agent name-record production during home-channel dialogue,
  routing and subscriptions open to channel members, runtime memory binding,
  external observations/reconciliation, and durable seed import adapters.
* Existing role safeguards: concrete symbolic build binding, authorization,
  provider-safe retries/reconciliation, secret handling, health and rollback.
  A lifecycle journal or plan hash alone grants none of these permissions.

All provider requirements appear in plan/dry-run `lifecycle[].needsProvider` and
`providerActions`; conditional steps have `when` tied to `provider.supports_rename`.
Only intent/record bookkeeping is local; the fake is not a real transport.
Tests exercise transitions, refusals, concurrent per-agent provisioning, restarts,
process death, interrupted effects and atomic persistence without any service.

### Local input path boundary

`Lifecycle(..., ingress_root=...)` and
`read_name_record(path, ingress_root=...)` optionally restrict name records to
an explicitly configured ingress directory. Paths keep their existing meaning
(relative to cwd, or absolute); an absolute path outside that root is refused,
not rebased. Without an ingress root, the caller still selects the file as
before, with its literal parent as the read boundary. Never derive the configured root from agent-provided input.

All local file reads reject `..`, symlinks in **every** component (including
the ingress root itself and its ancestors), and non-regular files. Legitimate
nested paths work. Directory-fd traversal uses `O_NOFOLLOW`; Linux
`/proc/self/fd` confirms containment of the opened regular file before reading.
Symlinked roots are deliberately unsupported, even when their targets are
inside the configured tree.

This shared rule covers name records; opaque seed files; lifecycle/status
`lifecycle.json` and `state/seed.consumed`; the fake identity provider's
`identities.json`; and file-backed template, instance, stores, observed-state,
catalog descriptor/versions and payload inputs. State-root/store path checks
also continue to reject symlinks. Store contents belong to the injected adapter
and are not read by the spawn core. Persistence writes retain the documented
trusted same-uid caller assumption; this is not hostile concurrent-writer
isolation or an authenticity check on regular-file contents.
