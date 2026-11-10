"""Guard the test-only boundary without weakening production ancestor checks."""
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy_fixtures import safe_ancestors, scratch
import test_deploy
from connectome_deploy import AncestorWritable, check_ancestors


class FixtureTests(unittest.TestCase):
    def test_private_root_is_real_and_host_metadata_is_restored(self):
        # Force the fallback used by accounts without a writable home. Ambient
        # TMPDIR/HOME must neither select scratch nor cause permission errors.
        with patch('deploy_fixtures.pwd.getpwuid', return_value=SimpleNamespace(pw_dir='/dev/null')), \
                patch.dict(os.environ, HOME='/dev/null', TMPDIR='/dev/null'), scratch() as tmp:
            root = Path(tmp)
            self.assertEqual(root.parent, Path('/tmp').resolve())
            before = {path: path.lstat() for path in (root, *root.parents)}
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            unsafe = root / 'unsafe'
            unsafe.mkdir(mode=0o700)
            unsafe.chmod(0o777)
            with safe_ancestors(root):
                check_ancestors(root / 'safe')
                info = root.lstat()
                self.assertEqual((info.st_uid, info.st_gid, info.st_mode),
                                 (before[root].st_uid, before[root].st_gid, before[root].st_mode))
                with self.assertRaises(AncestorWritable) as failure:
                    check_ancestors(unsafe / 'leaf')
                self.assertEqual(failure.exception.path, str(unsafe))
                root.chmod(0o777)
                try:
                    with self.assertRaises(AncestorWritable):
                        check_ancestors(root / 'leaf')
                finally:
                    root.chmod(0o700)
            for path, info in before.items():
                after = path.lstat()
                self.assertEqual((after.st_uid, after.st_gid, after.st_mode),
                                 (info.st_uid, info.st_gid, info.st_mode))
            if any(info.st_mode & 0o022 for path, info in before.items() if path != root):
                with self.assertRaises(AncestorWritable):
                    check_ancestors(root / 'safe')
