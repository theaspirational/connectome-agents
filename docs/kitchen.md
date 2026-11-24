# The thin layer over connectome-kitchen

`roles/connectome_kitchen` runs one operation of connectome-kitchen on one agent. It is the entry point for
deployments **without a catalog**: a pinned recipe file, cooked by connectome-cook, started as one user unit.
Catalog deployments and spawn stay with `roles/connectome_agent`, the Python role; kitchen has no port of them.

The role does not build, link or restart. Kitchen does, through its runner `scripts/takeover.mjs` over its engine
`src/engine.ts` (contract v1 `plan` and `apply`), as the account of the agent. The role gathers the request, hands it
over, reads the answer, and keeps the gates of a window.

## Never both roles on one agent

A kitchen names a build by its adapter contract (`kitchen-cook-adapter/<n>`); the Python role names it by the path of
its interpreter and adapter script. The same request has two names. **Run the Python role on an agent a kitchen
deployed, and it finds no build under its own name, cooks one and restarts the agent onto it.** Nothing in the Python
role refuses that: its task file is held byte for byte to a baseline (`tests/test_compatibility.py`), so no guard was
added there. The rule is the operator's: an agent that has `<home>/kitchen-takeover/<name>/` is kitchen's, and only
this role is run on it. This role never calls the `connectome_release` module (`tests/test_kitchen_role.py` holds that).

## Operations

One run is one operation: `-e connectome_kitchen_operation=<name>`. Ansible's check mode is refused; `plan` is the dry run.

| Operation | Kitchen | Writes | Restarts |
| --- | --- | --- | --- |
| `name` | `plan` through `files/kitchen-name.mjs` | the request and the recipe copy in the account's `kitchen-takeover/<name>/inputs` | no |
| `plan` | runner `check` | the same, and the runner's log and receipt | no |
| `prepare` | runner `prepare` (`apply`, phase prepare: cook); with an agreed drift, runner `check` first | the save for `rollback` (and, with an agreed drift, what kitchen agreed); the new build beside the running one; one entry of `state/ownership.json` | no |
| `verify` | runner `verify` | the log; the comparison and the baseline in the save directory | no |
| `start` | runner `verify`, then `start` (`apply`, both phases) | the environment file, `current`, the unit links, the drop-in | **once** |
| `rollback` | none (`files/kitchen-pointers.sh back`) | `current`, the unit links, the drop-in, `state/ownership.json`, from the save | **once** (stop, start) |
| `readback` | none (`files/kitchen-inspect.py readback`) | nothing | no |
| `clear` | none (`files/kitchen-pointers.sh clear`) | removes the secret files named in `connectome_kitchen_secret_files_new`, after a rollback, if this window introduced them | no |
| `remove` | none (`files/kitchen-first.sh remove`) | a first install only: removes the unit links, the drop-in and the deployment root | no (**one stop**) |

`name` asks the engine what it calls the build of the request and prints it. Every other operation that reaches the
engine needs that name pinned as `connectome_kitchen_build`, and the name of the build that runs now as
`connectome_kitchen_live_build`. The runner refuses when the host says otherwise, so a request that is not the one
that was looked at cannot be built or started under these pins.

These are the operations on an agent that runs: a takeover. An agent that does not exist yet is installed by the same
operations with `connectome_kitchen_first_install` and no live build: see [A first install](#a-first-install).

## An agreed drift of the running build

A build that runs is read again by kitchen whenever a new one is prepared or started beside it, and its installed
recipe (`builds/<live>/cook-recipe.json`) must still be the bytes its lock and its `deployment.json` recorded. Where
that file was edited by hand after cook, `prepare` is refused (`E_LOCK_INVALID`, "invalid operator recipe
provenance"). Do not put the old bytes back and do not look for a way around the check; name the edit:

```yaml
connectome_kitchen_agreed_drift:
  - {path: cook-recipe.json, sha256: <sha256 of the bytes builds/<live>/cook-recipe.json has now>}
```

The role puts it in the request as `agreed_drift` with `connectome_kitchen_live_build` as its build, and the same list
is handed to every operation of the window that reaches kitchen (`plan`, `prepare`, `verify`, `start`, and `name`).
Kitchen (`src/drift.ts`; a kitchen install without it refuses the key, so `connectome_kitchen_commit` must name one that has it) then reads the running build with
the agreed digest in place of the recorded one for exactly the files named, and as before in everything else. It refuses:
a file whose bytes are not exactly the agreed digest; a declared drift that is not there (the bytes are the recorded
ones, or the agreed digest is the recorded one); a path that is not a file it holds to a digest, which today is
`cook-recipe.json` alone (an absolute path, `..`, a subdirectory, the app, `connectome.lock`, a unit file: all
refused); any other difference it checks (the lock's own digest, a unit's digest, the mode and owner of the recipe);
and a drift of any build but the one `current` names. The new build is never read against it.

`prepare` with a drift first runs kitchen's `check`, which holds the running build to it and writes nothing but the
runner's log and receipt, then keeps what kitchen agreed in `<save dir>/agreed-drift.<build>.json` (operator, 0600):
`{build, files: [{path, locked_sha256, actual_sha256}]}`. Only then is the build made; its own `plan` and `apply`
hold the drift again and carry the same record in their receipts. What it does not cover: files kitchen does not hold
to a digest of the build (the `outputs` digests of the lock, the app with an uncommitted patch, files added to the
app that no lock names, `framework.lock.json`) do not refuse in the first place and cannot be named: a host bridge
patched into `app/` needs no entry, and the recipe entry is all a build with both drifts needs (kitchen's tests run a
window with both); the comparison `verify` makes (`files/kitchen-inspect.py`) is
not changed and shows the edited recipe as it is. Empty (the default), the request has no such key and nothing differs.
There is no switch that accepts anything else.

## What the role keeps, and what kitchen owns

| Kept in Ansible | Why |
| --- | --- |
| Reading `defaults/agent.yml`, the connector descriptors, the versions file and the three private layers | YAML, Jinja and inventory are Ansible's; kitchen takes the four layers as data |
| `public_catalog` (through a filter that loads `connectome_config.py` of the Python role) | catalog knowledge stays in Python for now |
| Reading the recipe on the controller, holding it to its pin, handing it to the account as a 0600 file | the recipe may be unreadable for the account; it does not leave the host |
| The four engine paths out of `connectome_cook_command` | so one inventory serves both roles; kitchen takes them as the closed `engines.cook` record |
| The gates of a restart, the save, the watch after the start, the way back, the readback | kitchen has no operation for them (below) |

| Owned by kitchen, not repeated here | Where |
| --- | --- |
| Merging the layers, pruning disabled entries | `src/config.ts` |
| Pins, the request identity, the plan | `src/deploy.ts` |
| Operator-file checks, the cook adapter, the lock reader, archives | `src/inputs.ts`, `src/cook.ts`, `src/lock.ts`, `src/archive.ts` |
| Build, ownership bookkeeping, units, drop-in, pointer, secrets from the target file, health, taking a failed start back | `src/deploy.ts` |
| The per-agent lock, refusing every service-manager verb but the ones a step needs, the measurement of the builds a step must not touch | `src/engine.ts`, `scripts/takeover.mjs`, `scripts/takeover/systemctl-guard` |

Not carried over from the Python role, on purpose: controller-side secret providers (`connectome_secret_lookup`). The
engine reads the target secrets file in place; an agent whose secrets come from a controller plugin is not served.

## Deviations, stated

1. **The role calls kitchen's runner, not `engine.ts` directly.** `src/cli.ts` still refuses every operational command;
   `scripts/takeover.mjs` is the only reviewed entry that runs the engine on a real account. `name` is the one
   place the role calls the engine itself (`plan`, which writes nothing), behind the same read-only guard.
2. **`rollback` is not the engine.** An engine of one adapter contract cannot ask for a build named under another, so
   after its own start succeeded it cannot switch back to the build that ran before a contract change. While its
   start is failing the engine does put the previous build back itself. `rollback` moves the same five things, to
   where `prepare` found them: code and configuration only. The agent's state directory, its secrets file and both
   build directories are not written; the old code starts on the state as the new build left it.
3. **Controller and target are one host.** The role refuses any connection but `local`. Every kitchen process is
   started as `runuser -u <account> -- env -i <seven names>`: nothing of the controller's environment reaches cook.
4. **The request is worded for the runner** (`connectome.kitchen-takeover/v1`): the contract request plus the pinned
   inputs, the recipe's source and the two build names.

## The gates of a restart

`start` and `rollback` restart the agent and refuse unless both hold.

- **The agent's own IDLE, for this run.** `-e connectome_kitchen_idle="IDLE <name> <UTC time, YYYY-MM-DDTHH:MMZ>"`:
  the agent's word that it is idle, with the time it gave it, no older than `connectome_kitchen_idle_max_age_minutes`
  (15). The role cannot know that an agent is idle; it can refuse to restart one without a fresh, named statement of
  it. For `rollback` of an agent that is down there is nobody to ask: `-e connectome_kitchen_agent_is_down=true`.
- **The executor.** The play must not run inside the agent's unit: the restart would end it half way, between the
  switch and the checks. The role reads its own control group and refuses. An agent that runs its own window starts
  the play as a transient unit of the system manager and reads the outcome from the log afterwards:

      systemd-run --unit kitchen-window-<name> --collect --property=StandardOutput=append:<log> --property=StandardError=append:<log> <the ansible-playbook command>

`start` also refuses unless `verify` ran for this build and `state/ownership.json` is what it was then, the agent
runs the saved build, a fresh `verify` says "build prepared, activation due", and the secrets file gives the new
process the environment the running one has (`connectome_kitchen_secrets_differ_ok` to start all the same).

The runner answers yes or no for the environment file as a whole. The role asks further, name by name
(`files/kitchen-secrets.mjs`, as the account, with kitchen's own reading of the layers): the environment file of the
running build against the one a start would write from the secrets file now. It prints which names are the same,
changed, added or removed. It prints names only: no value, no length and no digest of a value leaves it.

A request that adds a secret file gives the new process another environment file by design. Name the file's
environment variable in `connectome_kitchen_secret_files_new`: `start` then takes the difference only when

- no name of the environment file is changed and none is removed;
- the names added are exactly the named ones, and each is a secret file of the request;
- every other secret file is what the start would write, and none of the named ones is there yet.

Naming a new file authorizes that file and nothing beside it. A direct value that changed in the secrets file since
the running process was started (a rotated `ANTHROPIC_API_KEY`, another `ANTHROPIC_BASE_URL`) refuses the start, with
or without a named new file, and `verify` says which name it is. The one way to start all the same is the separate
`connectome_kitchen_secrets_differ_ok=true`, which takes every difference.

The engine reads the secrets file again when it starts. So after the restart the role holds the environment file the
engine wrote against the previous build's in the same way: it must differ in exactly the names that were held before
the start. If the secrets file changed in between, that is a failed start (below).

## What `start` does after the restart

1. The agent must run the new build in another process (the links, the loaded unit file and environment file), and
   the environment file the engine wrote must differ from the previous build's in exactly the names held before.
2. The same process, with the same restart count, must still run after `connectome_kitchen_watch_seconds` (120). The
   engine's own health check is one question right after the restart; a process that ends ten seconds later passes it.
3. If the start or one of these fails and the agent does not run the previous build, the previous build is put back
   (`rollback`), unless `connectome_kitchen_auto_rollback=false`. The play ends failed and says where the agent is:
   `NOT STARTED` (it runs the previous build; in the same process, or in a new one where the engine took the start
   back), `ROLLED BACK`, or `FAILED` with nothing put back.
4. Then `verify` again and `readback`. Neither changes anything or puts anything back.

## `verify` and `readback`

`verify` holds the new build against the running one (`files/kitchen-inspect.py compare`): the unit, the launcher,
`bunfig.toml` and `app/bun.lock` equal but for the build's name; the staged recipe and cook's copy of it differing in
exactly the leaves named in `connectome_kitchen_recipe_changes`; the lock differing in no leaf of the external
servers, engines, requirements, credentials, host or pinned packages but the ones named in
`connectome_kitchen_lock_changes`. The lock's lists of named things are read by name, so one server more is
`/external/<name>` and one credential more `/credentials/<NAME>`. The lock names its recipe by digest; that leaf is
held to the staged recipe of each build and is not counted. `verify` refuses a build that is not prepared (a refused
or failed `prepare` may leave an unfinished directory under the build's name). Under adapter contract `/2` Bun installs from
an empty cache of the build's own and may resolve an unpinned range to a newer version: the `bun-lock` line says
whether it did. A difference is a `LOOK` line, not a refusal; `start` does not read them, the operator does.

`readback` reads what the agent shows after the start: recipe keys present or absent
(`connectome_kitchen_recipe_absent`, `connectome_kitchen_recipe_present`) in the staged recipe and in the copy the
process was started with; one child process below the main process for each MCP server the recipe starts as a process
(its command line holds every path of the server's entry; or, where the entry names a wrapper that replaces itself
with the server, only paths of that entry, one of them no other server's);
of the unit's journal, per MCP server the word of its last connection line, and the `hardBudget` of the last
`[plan-vs-actual]` line against the one `verify` kept from the process before. With
`connectome_kitchen_budget_reported_only` (a window that changes the recipe's budget) both numbers are said and none
is held: what the planner works with also depends on settings the process keeps in its own state, which this role
neither reads nor writes. For a server named in
`connectome_kitchen_servers_connected` that word must be `connected`; for the others no word against them is enough. That line appears only after the first
turn of the new process: until then `readback` says `LOOK` for it.

Both print words, names, counts and numbers. Neither prints a text value of a recipe, a secret or a journal line.

## When a rollback is done

`rollback` ends with 0 only when the three links, the drop-in **and `state/ownership.json`** are the saved ones and
the agent runs the saved build in the same process ten seconds later, or when nothing had to be put back. While it
puts things back it keeps `<save>/back.incomplete`. If a step fails (exit 62) that file stays, and the next `rollback`
puts all five back again before it says anything is done, also where the links and the drop-in already name the saved
build: a way back that stopped before `ownership.json` is not taken for a finished one.

That file is also what tells an unfinished way back from a build that was prepared and never started. There the links
and the drop-in are the saved ones too, `ownership.json` rightly holds what `prepare` added, and `rollback` says
`Nothing to put back`, with which of the two `ownership.json` is.

## After a rollback

`state/ownership.json` is the saved one, so the engine does not know the new build directory and refuses it as
unmanaged. And a secret file the start wrote for the new build (`connectome_kitchen_secret_files_new`) is still in
`state/secret-files`, where the saved deployment does not declare it: the engine refuses to verify or start beside it
(`secret file must be declared`).

To try again:

1. remove `builds/<new>`, `cook-config/<new>` and `cook-credentials/<new>`;
2. `clear`. It removes each named new secret file, and only if the way back is complete and the agent runs the saved
   build, the file was not there when `prepare` saved (the save lists the names), the saved build's environment file
   has no line for it, and it is a regular file. It removes nothing else, restarts nothing and needs no IDLE. Without
   named new files it says so and changes nothing;
3. `plan`, `prepare`, `verify` and `start` as before. The save is kept: the way back stays the same.

No bypass is part of this: `connectome_kitchen_secrets_differ_ok` is not needed for a retry and would not help.

To stay: nothing has to be done for the build; the new build is files nothing names. `clear` removes the introduced
secret file there too.

## A first install

`-e connectome_kitchen_first_install=true`, with `connectome_kitchen_live_build` left empty, installs an agent from
nothing: no deployment root contents, no unit, no build, nothing running. It needs a kitchen whose runner takes
`expect.live_build: null` (the commit after `e62b4cf` that adds the first install). The account, its linger, its
secrets file, the state directory and whatever the recipe's servers need are the operator's and are there before.
The operations are `name`, `plan`, `prepare`, `verify`, `start`, `readback` and `remove`; `rollback` and `clear` are
refused, since there is nothing to go back to. Everything of a takeover that is held against a running build has
another thing in its place:

| Operation | A takeover | A first install |
| --- | --- | --- |
| the request | names the live build | `expect.live_build` is `null`: the runner holds that `builds` has no build but the new one, that `current` is not there, and that the account has no unit link, enable link or drop-in directory of this deployment |
| `prepare`, first | saves the links, the drop-in and `ownership.json` | `files/kitchen-first.sh hold`: requires a fresh empty root (or a matching first-install marker), no activation artifacts, and an unloaded unit. Binds `<save>/first-install` to the account, paths and expected build, and records directory identities, including a Linux filesystem export handle for the root. Refuses a takeover save or an overlapping layout |
| `verify` | compares with the running build, keeps a baseline | `files/kitchen-inspect.py alone`: the build is prepared and has its launcher, unit, recipe and lock; the lock names the staged recipe; the recipe keys; each server of `connectome_kitchen_servers_connected` is one of the recipe. No comparison, no baseline |
| the environment | name by name against the running build's file | from the runner alone: measures the pinned build’s environment even when `current` is absent, and checks the declared secret files. Missing, matching and different/unreadable are distinguished without printing contents. Start requires absence |
| the gate of `start` | the agent's own IDLE | the operator's own word: `-e connectome_kitchen_operator_word="FIRST-START <name> <UTC time, YYYY-MM-DDTHH:MMZ>"`, as fresh as an IDLE must be; and the executor gate as before |
| before the start | the agent runs the saved build | nothing is installed and the unit is down; `verify` ran for this build. Before apply, the effective activation must be exactly this unit from `managed-units` (not a generated script-only launcher), with its `default.target` enable link and no additional boot links or aliases |
| a start that fails | the engine puts the previous build back | the engine stops the unit and removes what it installed (the guard's `first-start` mode lets that one `stop` through). The play ends `NOT STARTED`: nothing is installed, the prepared build stays; `verify` reports any retained environment; if present, explicitly remove and prepare again before retrying |
| after the start | another process on the new build; the written environment against the previous one's | the unit runs the new build; the same process after `connectome_kitchen_watch_seconds`; then `verify` and readback. Diagnostic LOOK lines may require an operator follow-up even on a successful start |
| a failed watch or fatal post-activation check | `rollback` for a failed watch | guarded cleanup removes boot activation, always preserving state. Default: `REMOVED`; with `connectome_kitchen_auto_rollback=false`: `DEACTIVATED`, retaining the failed build for inspection |
| `readback` | holds the process and the planner's budget to the baseline | says `first install: no process ran before this one`; the planner's `hardBudget` is reported and held to nothing |

`remove` is the way out, at any time while the deployment still is what a first install leaves:

    -e connectome_kitchen_operation=remove -e connectome_kitchen_operator_word="REMOVE <name> <UTC time, YYYY-MM-DDTHH:MMZ>"

It requires the original build pin and a matching operator-owned marker. It validates disjoint deployment, state,
secrets, unit, and protected paths, then checks the expected build and engine-owned links and drop-in before any
stop or unlink. It refuses redirected unit paths and leaves unrelated unit files in place. The dedicated root is
removed with its build, engine records, secret copies and workspace. The source secrets file is never removed.
The role never deletes agent state. Unset `connectome_kitchen_remove_state` even if it was set to false; the role
rejects this retired option. State removal is a separate manual operator action. Repeat prepare refuses a missing
or distinguishably replaced state directory rather than updating the original binding. State first created during
the initial prepare can be bound only by that invocation. To retry with restored state, first run guarded remove,
then hold anew.

Root removal requires the same held directory, not just matching device and inode. The deployment filesystem must
provide Linux export handles with incarnation evidence (for example ext4). If the evidence is unavailable or no
longer matches, cleanup refuses rather than deleting the root. Old markers without this evidence are not upgraded
or reused. A successful remove retires the marker; an already-absent target can be a no-op.

Exit 60 means cleanup was refused before a mutation was attempted. Exit 62 means cleanup was incomplete
(including a validation failure after a stop or another mutating step was attempted): inspect and correct the cause, then retry the same guarded operation with
the original pins and a fresh word. Do not bypass the validator with raw deletion commands. A refusal means
ownership could not be proved; escalate if the original layout cannot be verified. After a successful remove,
`prepare`, `verify` and `start` install from nothing again.

Once the agent runs, its next window is a takeover: `connectome_kitchen_first_install` off, the build of the first
install as `connectome_kitchen_live_build`, and a save directory of its own (the first install's holds its marker and
is refused for a save). `plan` and `prepare` of the first-install request are refused on the running agent, by the
runner and by the role.

What a first install does not do: it creates no account, writes no secret, installs no other unit (a server the recipe
reaches over a socket has to listen before the first start), sets nothing in the agent's own settings, and cannot say
that a state directory is fresh: `prepare` prints the names of what is in it, for the operator to read.

## By hand

If `rollback` ends with 61 or 62 the agent may be down. Its steps are the last lines of
`files/kitchen-pointers.sh`: stop the unit, three `ln -sfn` from `<save>/links`, the drop-in and `ownership.json`
installed from the save with the account as owner and mode 0600, `daemon-reload`, `start`. Then run `rollback` once
more: it finds all five in place and the agent running, removes `back.incomplete`, and changes and restarts nothing;
or it says what is still not the saved one. If the unit fails on the previous build too, the cause is not the build.

## Trying it without a host

`tests/kitchen/trial.py` runs every operation through `ansible-playbook` on a sandbox of kitchen's deploy corpus, with
a stand-in service manager that keeps real processes: the gates, a start, a start the engine takes back, a build that
does not stay up, the way back, and the retry after it through `clear`, `prepare`, `verify` and a second real
start; a way back that fails at `ownership.json` and the next one; a direct secret that changed, with and without
a named new file, before the start and (`--scenario rotated-at-start`) between the check and the start.
`--scenario first-install` begins where nothing is deployed: the refusals, a first start the engine takes back, the
first start, `readback`, `remove`, the install again from nothing, a build that does not stay up with and without
`connectome_kitchen_auto_rollback`, and the removal with the state directory. `tests/kitchen/render.yml` words the request without a host,
so that it can be held against a request that is known. Neither is part of `scripts/test.sh`: they need a built
kitchen clone, ansible-core and Node.
