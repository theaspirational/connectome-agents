#!/usr/bin/env node
// The name kitchen gives the build of a request, asked of the engine itself: contract v1 `plan`, which writes nothing.
//
//   kitchen-name.mjs <kitchen install> <request document>
//
// The request document is the one the role writes for kitchen's runner (connectome.kitchen-takeover/v1); `expect` is
// not read, since the name is what is being asked. The service manager is reached through kitchen's guard in its
// read-only mode, as the runner reaches it, and no other tool is started. Prints one JSON object: the exit status of
// `plan`, the build's name and directory, and the engine's closed answer. Exit status: 0 named; 1 the engine refused;
// 2 unusable invocation; 3 this helper refused.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { fileURLToPath } from 'node:url';

/** This helper's own refusal: exit status 3. */
export class Refusal extends Error {}

/**
 * Asks the engine of the kitchen installed at `home` for its plan of the request in `file`. Returns the exit status of
 * `plan` and the line to print. `options` are the engine's (src/engine.ts `EngineOptions`); a test hands in a model of a
 * host, the command line never does.
 */
export async function name(home, file, options = {}) {
  const refuse = message => { throw new Refusal(message); };
  if (process.geteuid() === 0) refuse('never as root: the engine runs as the account of the agent');
  const load = leaf => import(pathToFileURL(path.join(home, 'dist', leaf)).href);
  const { dispatch } = await load('engine.js');
  const { merge_layers } = await load('config.js');
  const { canonical, dict } = await load('pylike/pydump.js');
  const { loads } = await load('pylike/pyjson.js');
  const text = fs.readFileSync(file);
  const input = loads(text);
  const request = input.get('request'), real = input.get('systemctl'), recipe = input.get('recipe');
  const agent = merge_layers(loads(text).get('request').get('layers')).get('name');
  if (typeof agent !== 'string' || !/^[a-z0-9][a-z0-9-]{0,62}$/u.test(agent)) refuse('the deployment has no usable name');
  const unit = 'connectome-' + agent + '.service';
  const keep = path.join(os.homedir(), 'kitchen-takeover', agent);
  if (!fs.existsSync(keep)) refuse('the directory of the takeover is not there: ' + keep);
  const guard = [path.join(home, 'scripts', 'takeover', 'systemctl-guard'), 'readonly', path.join(keep, 'systemctl-calls.log'), unit, real[0], '--user'];
  request.set('systemctl', guard);
  if (recipe !== null) request.set('operator_files', new Map([['cook-recipe.json', fs.readFileSync(recipe.get('source')).toString('base64')]]));
  // `plan` starts nothing. Whatever it would start is refused here.
  const run = argv => { throw new Error('kitchen-name starts no tool: ' + String(argv[0])); };
  const [receipt, status] = dispatch(dict({ contract: 'connectome.kitchen/v1', command: 'plan', request }), null, { ...options, run });
  const core = JSON.parse(canonical(receipt.get('core')));
  const directory = core.prepare?.build_dir ?? null;
  return [status === 0 ? 0 : 1, JSON.stringify({ status, build: directory === null ? null : path.basename(directory), build_dir: directory, core })];
}

/** The command line: prints the line, returns the exit status. */
export async function main(argv, options = {}) {
  const [home, file, ...rest] = argv;
  if (!home || !file || rest.length || !path.isAbsolute(home) || !path.isAbsolute(file)) {
    process.stderr.write('usage: kitchen-name.mjs <absolute kitchen install> <absolute request document>\n');
    return 2;
  }
  process.umask(0o077);
  try {
    const [status, line] = await name(home, file, options);
    process.stdout.write(line + '\n');
    return status;
  } catch (error) {
    if (error?.constructor?.name === 'ContractError') { process.stderr.write('kitchen-name: the request is not a contract v1 request\n'); return 2; }
    process.stderr.write('kitchen-name: ' + String(error?.message ?? error) + '\n');
    return 3;
  }
}

if (process.argv[1] && fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) process.exitCode = await main(process.argv.slice(2));
