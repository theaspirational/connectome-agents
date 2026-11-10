"""Test-only filesystem boundary; never imported by the deployment engine."""
import os
from pathlib import Path
import pwd
import re
import stat
import tempfile
import unittest
from unittest.mock import patch


def scratch():
    """Private real files, independent of TMPDIR, HOME and checkout location.

    Prefer the account home, but support accounts without a writable home.
    No existing directory is chmod'ed or chown'ed by this fixture.
    """
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        raise unittest.SkipTest('deployment fixtures require a local account record')
    for directory in dict.fromkeys((home, '/tmp')):
        try:
            directory = str(Path(directory).resolve())
            if not re.fullmatch(r'[A-Za-z0-9_./:@+-]+', directory):
                continue
            return tempfile.TemporaryDirectory(prefix='deploy-test-', dir=directory)
        except OSError:
            continue
    raise unittest.SkipTest('no writable location for private deployment fixtures')


def safe_ancestors(root):
    """Model only the host-owned boundary above a private fixture as root:0755.

    All lstat calls still hit real files. The private root and everything in it
    retain real metadata, including deliberately unsafe ancestors, symlinks,
    owners and modes. This seam lets security tests run under sticky /tmp or a
    group-writable checkout without relaxing any production path validation.
    """
    parents = set(Path(root).resolve().parents)
    original = Path.lstat

    def metadata(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path in parents:
            fields = list(info)
            fields[0] = stat.S_IFDIR | 0o755
            fields[4] = fields[5] = 0
            return os.stat_result(fields)
        return info

    return patch.object(Path, 'lstat', metadata)


def private_scratch(test):
    tmp = scratch()
    test.addCleanup(tmp.cleanup)
    boundary = safe_ancestors(tmp.name)
    boundary.start()
    test.addCleanup(boundary.stop)
    return tmp
