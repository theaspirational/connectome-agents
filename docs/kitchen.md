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
| `prepare` | runner `prepare` (`apply`, phase prepare: cook) | the save for `rollback`; the new build beside the running one; one entry of `state/ownership.json` | no |
| `verify` | runner `verify` | the log; the comparison and the baseline in the save directory | no |
| `start` | runner `verify`, then `start` (`apply`, both phases) | the environment file, `current`, the unit links, the drop-in | **once** |
| `rollback` | none (`files/kitchen-pointers.sh back`) | `current`, the unit links, the drop-in, `state/ownership.json`, from the save | **once** (stop, start) |
| `readback` | none (`files/kitchen-inspect.py readback`) | nothing | no |

`name` asks the engine what it calls the build of the request and prints it. Every other operation that reaches the
engine needs that name pinned as `connectome_kitchen_build`, and the name of the build that runs now as
`connectome_kitchen_live_build`. The runner refuses when the host says otherwise, so a request that is not the one
that was looked at cannot be built or started under these pins.

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

A request that adds a secret file gives the new process another environment file by design. Name the file's
environment variable in `connectome_kitchen_secret_files_new`: `start` then takes the difference when every other
secret file is what the start would write and none of the named ones is there yet. What it cannot see is whether the
environment file differs in more than those names: the runner answers yes or no for the file as a whole.

## What `start` does after the restart

1. The agent must run the new build in another process (the links, the loaded unit file and environment file).
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
process was started with; one child process below the main process for each MCP server the recipe starts as a process;
of the unit's journal, per MCP server the word of its last connection line, and the `hardBudget` of the last
`[plan-vs-actual]` line against the one `verify` kept from the process before. For a server named in
`connectome_kitchen_servers_connected` that word must be `connected`; for the others no word against them is enough. That line appears only after the first
turn of the new process: until then `readback` says `LOOK` for it.

Both print words, names, counts and numbers. Neither prints a text value of a recipe, a secret or a journal line.

## After a rollback

`state/ownership.json` is the saved one, so the engine does not know the new build directory and refuses it as
unmanaged. To try again: remove `builds/<new>`, `cook-config/<new>` and `cook-credentials/<new>`, then `plan`,
`prepare`, `verify` and `start` as before. To stay: nothing has to be done; the new build is files nothing names.

## By hand

If `rollback` ends with 61 or 62 the agent may be down. Its steps are the last lines of
`files/kitchen-pointers.sh`: stop the unit, three `ln -sfn` from `<save>/links`, the drop-in and `ownership.json`
installed from the save with the account as owner and mode 0600, `daemon-reload`, `start`. If the unit then fails on
the previous build too, the cause is not the build.

## Trying it without a host

`tests/kitchen/trial.py` runs every operation through `ansible-playbook` on a sandbox of kitchen's deploy corpus, with
a stand-in service manager that keeps real processes: the gates, a start, a start the engine takes back, a build that
does not stay up, the way back, and the retry after it. `tests/kitchen/render.yml` words the request without a host,
so that it can be held against a request that is known. Neither is part of `scripts/test.sh`: they need a built
kitchen clone, ansible-core and Node.
