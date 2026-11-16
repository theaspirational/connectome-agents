#!/usr/bin/env node
// roles/connectome_kitchen/files/kitchen-name.mjs on a sandbox of kitchen's deploy corpus. Test only; driven by trial.py.
//
//   name_seam.mjs <kitchen clone> <request document> <sandbox> <account>
//
// The helper is the one the role runs, behind the host model of the corpus (kitchen's test/corpus/takeover_driver.mjs):
// who owns a path under the sandbox, and where the account's home is. On a real account it has neither.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { main } from '../../roles/connectome_kitchen/files/kitchen-name.mjs';

const [kitchen, request, sandbox, account] = process.argv.slice(2);
const load = leaf => import(pathToFileURL(path.join(kitchen, 'dist', leaf)).href);
const { realHost } = await load('io/host.js');
const pathio = await load('pylike/pathio.js');
const pathlib = await load('pylike/pathlib.js');
const above = new Set(pathio.parents(sandbox));
const asRoot = ['opt', 'srv', 'usr'].map(name => sandbox + '/' + name);
const host = {
  ...realHost,
  lstat(file) {
    const info = realHost.lstat(file);
    if (above.has(file)) return { mode: fs.constants.S_IFDIR | 0o755, uid: 0, gid: 0 };
    if (file === sandbox || file === sandbox + '/home' || asRoot.some(top => file === top || pathlib.isRelativeTo(file, top))) return { mode: info.mode, uid: 0, gid: 0 };
    return info;
  },
  home: uid => (uid === process.geteuid() ? sandbox + '/home/' + account : realHost.home(uid)),
};
process.exitCode = await main([kitchen, request], { host });
