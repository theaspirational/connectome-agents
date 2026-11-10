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
        path.mkdir(mode=0o700, exist_ok=True)
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
    directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.spawn-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(root):
    root = directory(root)
    path = safe_path(root / '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
