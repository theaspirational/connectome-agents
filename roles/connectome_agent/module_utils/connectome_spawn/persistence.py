"""Local POSIX persistence: locked writers, atomic replacement and fsync.

Trusted same-uid callers only; no protection against concurrent hostile path swaps.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile

from .data import canonical, decode, need
from .sources import read_file


def safe_path(path):
    path = Path(path).absolute()
    need('..' not in path.parts, 'path traversal refused')
    for part in (*reversed(path.parents), path):
        need(not part.is_symlink(), 'symlink persistence path refused')
    return path


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def directory(path):
    path = safe_path(path)
    if not path.exists():
        directory(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            path.chmod(0o700)  # Exact private mode, even under a restrictive umask.
            sync_dir(path.parent)
    need(path.is_dir(), 'directory required')
    return path


def read(path):
    path = safe_path(path)
    return decode(read_file(path))


def write(path, value):
    path = safe_path(path)
    raw = canonical(value) + b'\n'
    need(len(raw) <= 1024 * 1024, 'state size limit exceeded')
    write_bytes(path, raw)


def write_bytes(path, raw):
    """Atomic private byte delivery; no serialized/logged copy of secrets."""
    path = safe_path(path)
    directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.spawn-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_secret_bytes(path, raw):
    """One deterministic private staging inode, recoverable after process death.

    Caller holds its provider lock. Never accumulate random temporary copies of
    a credential on retries. A surviving complete stage is renamed, not copied;
    a partial/conflicting stage is refused for explicit reconciliation.
    """
    path = safe_path(path)
    directory(path.parent)
    staging = safe_path(path.parent / ('.' + path.name + '.pending'))
    if os.path.lexists(staging):
        need(read_file(staging, root=path.parent, private=True) == raw,
             'secret staging conflict; reconciliation required')
        fd = os.open(staging, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            os.fsync(stream.fileno())
    else:
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            staging.unlink()
            sync_dir(path.parent)
            raise
    # If rename/fsync fails or the process dies, retain only this one private
    # inode. A retry either promotes the stage or verifies the delivered file.
    sync_dir(path.parent)
    os.replace(staging, path)
    sync_dir(path.parent)


@contextmanager
def locked(root):
    root = directory(root)
    path = safe_path(root / '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
