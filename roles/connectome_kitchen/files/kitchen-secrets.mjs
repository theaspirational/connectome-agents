#!/usr/bin/env node
// In which names the environment file of a start differs from the one the running build has. Reads only; writes nothing.
//
//   kitchen-secrets.mjs <kitchen install> <request document> proposed|written
//
// The request document is the one the role writes for kitchen's runner (connectome.kitchen-takeover/v1). The environment
// file of the build that runs (`expect.live_build`) is held, name by name, against
//   proposed   the file the engine would write now: the direct names with their values of the secrets file, and each
//              secret file's name with its path (src/deploy.ts `activate`; the runner words it the same way);
//   written    the file the engine wrote for the new build (`expect.request_id`).
// Kitchen's runner answers yes or no for the file as a whole. This says which names: the same, changed, added, removed,
// and which of the names are secret files of the request.
//
// Prints one JSON object of names, counts and yes or no. NO VALUE, no length and no digest of a value leaves this
// process, and no text of an exception does: a JSON parser quotes what it could not read.
// Exit status: 0 compared; 2 unusable invocation; 3 could not compare (the reason is one closed line).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/** This helper's own refusal: exit status 3. The message is closed text: never a value, never an exception's text. */
export class Refusal extends Error {}
const refuse = message => { throw new Refusal(message); };
const quiet = (work, message) => { try { return work(); } catch (error) { if (error instanceof Refusal) throw error; return refuse(message); } };

const NAME = /^[A-Z_][A-Z0-9_]*$/u;
const HEX = /^[a-f0-9]{64}$/u;

/** The lines of an environment file as the engine writes one, by name. Refuses a file that is not worded that way. */
export function lines(text, what) {
  if (text !== '' && !text.endsWith('\n')) refuse(what + ' does not end with a line end');
  const found = new Map();
  for (const line of text === '' ? [] : text.slice(0, -1).split('\n')) {
    const name = /^([A-Z_][A-Z0-9_]*)="(?:[^"\\]|\\["\\])*"$/u.exec(line)?.[1];
    if (name === undefined || found.has(name)) refuse(what + ' has a line that is not one NAME="value" of the engine, or a name twice');
    found.set(name, line);
  }
  return found;
}

/** One line as `activate` words it (src/deploy.ts). */
const worded = (name, value) => name + '="' + value.replaceAll('\\', '\\\\').replaceAll('"', '\\"') + '"';

export async function compare(home, file, mode) {
  const load = leaf => import(pathToFileURL(path.join(home, 'dist', leaf)).href);
  const { merge_layers } = await load('config.js');
  const deploy = await load('deploy.js');
  const { loads } = await load('pylike/pyjson.js');
  const input = quiet(() => loads(fs.readFileSync(file)), 'the request document cannot be read');
  const [effective, root, live, wanted] = quiet(() => [merge_layers(input.get('request').get('layers')), input.get('request').get('root'), input.get('expect').get('live_build'), input.get('expect').get('request_id')],
    'the request document is not one of the role');
  if (typeof root !== 'string' || !path.isAbsolute(root) || !HEX.test(live) || !HEX.test(wanted) || live === wanted) refuse('the request names no root or not two builds');
  const direct = quiet(() => deploy.direct_secret_names(effective), 'the request declares an unusable secret name');
  const files = quiet(() => (effective.get('secret_files') ?? []).map(record => [record.get('env'), record.get('secret')]), 'the request declares unusable secret files');
  if (files.some(([env, secret]) => typeof env !== 'string' || !NAME.test(env) || typeof secret !== 'string' || direct.includes(env)) || new Set(files.map(([env]) => env)).size !== files.length) {
    refuse('the request declares unusable secret files');
  }
  const mappings = files.map(([env]) => env).sort();
  const before = lines(quiet(() => fs.readFileSync(path.join(root, 'builds', live, 'secrets.env'), 'utf8'), 'the environment file of the running build cannot be read'), 'the environment file of the running build');
  let after;
  if (mode === 'written') {
    after = lines(quiet(() => fs.readFileSync(path.join(root, 'builds', wanted, 'secrets.env'), 'utf8'), 'the environment file of the new build cannot be read'), 'the environment file of the new build');
  } else {
    const secretsFile = effective.get('secrets_file');
    if (typeof secretsFile !== 'string' || !path.isAbsolute(secretsFile)) refuse('the request names no secrets file');
    const values = quiet(() => JSON.parse(fs.readFileSync(secretsFile, 'utf8')), 'the secrets file cannot be read as JSON');
    if (values === null || typeof values !== 'object' || Array.isArray(values)) refuse('the secrets file is not an object of names');
    if (direct.some(name => !Object.hasOwn(values, name) || typeof values[name] !== 'string' || /[\u0000\n\r]/u.test(values[name]))) refuse('the secrets file does not hold every direct name as one line of text');
    after = new Map(direct.map(name => [name, worded(name, values[name])]));
    for (const env of mappings) after.set(env, worded(env, quiet(() => deploy.Deployment._systemd_path(path.join(root, 'state', 'secret-files', env)), 'a secret file has a path the engine would not write')));
  }
  const names = [...new Set([...before.keys(), ...after.keys()])].sort();
  const pick = held => names.filter(held);
  return {
    mode, against: live, compared: true,
    same: pick(name => before.has(name) && before.get(name) === after.get(name)),
    changed: pick(name => before.has(name) && after.has(name) && before.get(name) !== after.get(name)),
    added: pick(name => !before.has(name)),
    removed: pick(name => !after.has(name)),
    secret_files: mappings,
  };
}

/** The command line: prints the line, returns the exit status. */
export async function main(argv) {
  const [home, file, mode, ...rest] = argv;
  if (!home || !file || rest.length || !path.isAbsolute(home) || !path.isAbsolute(file) || !['proposed', 'written'].includes(mode)) {
    process.stderr.write('usage: kitchen-secrets.mjs <absolute kitchen install> <absolute request document> proposed|written\n');
    return 2;
  }
  try {
    process.stdout.write(JSON.stringify(await compare(home, file, mode)) + '\n');
    return 0;
  } catch (error) {
    // Only this helper's own closed words. Anything else may quote a value.
    process.stdout.write(JSON.stringify({ mode, compared: false, reason: error instanceof Refusal ? error.message : 'the comparison failed (the text of the failure is not shown)' }) + '\n');
    return 3;
  }
}

if (process.argv[1] && fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) process.exitCode = await main(process.argv.slice(2));
