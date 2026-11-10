"""Local-only source reads: never fetch, checkout, execute hooks or resolve pins through refs."""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

from .data import LIMIT, SpawnError, decode, match, need

COMMIT = r'[0-9a-f]{40}'
RELATIVE = r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*'
ARTIFACT_LIMIT = 64 * 1024 * 1024


def relative(value):
    need(match(value, RELATIVE) and all(p not in ('.', '..', '.git') for p in value.split('/')),
         'literal contained relative path required')
    return value


def absolute(value):
    need(type(value) is str and value.startswith('/') and not value.startswith('//'),
         'absolute literal path required')
    need(match(value[1:], RELATIVE) and all(p not in ('.', '..') for p in value[1:].split('/')),
         'absolute literal path required')
    return value


def read_file(path, limit=LIMIT, *, root=None):
    """Read a bounded, single-link regular file without any symlink component.

    Paths are literal: never normalize away '..'. An explicit root is a
    containment boundary, not a base for interpreting relative input paths.
    Linux dir-fd traversal pins each directory; /proc confirms the opened file
    still resides beneath the boundary before any bytes are read. Refuse hard
    links because their other names may be outside that boundary.
    """
    path = Path(path).absolute()
    boundary = Path(root).absolute() if root is not None else path.parent
    need('..' not in path.parts and '..' not in boundary.parts, 'path traversal refused')
    need(path != boundary and path.is_relative_to(boundary), 'input escapes root')
    directory_fd = None
    try:
        directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            need(stat.S_ISREG(metadata.st_mode), 'regular input file required')
            need(metadata.st_nlink == 1, 'single-link input file required')
            resolved = Path(os.readlink('/proc/self/fd/' + str(stream.fileno())))
            need(resolved.is_absolute() and resolved.is_relative_to(boundary) and
                 not str(resolved).endswith(' (deleted)'), 'input escapes root')
            data = stream.read(limit + 1)
            need(len(data) <= limit, 'input size limit exceeded')
            return data
    except OSError:
        raise SpawnError('unreadable local file') from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def git(repo, *args, limit=LIMIT):
    """Read an explicit local object store, never discover a parent repository.

    Non-bare inputs must be repository tops with a real .git directory; bare
    inputs must declare core.bare. Gitfiles/linked worktrees are not supported.
    Pin no-follow directory traversal with fds, as for local file reads.
    No ambient config, replacements, lazy fetch, filters, hooks or transports.
    """
    absolute(repo)
    env = {'PATH': os.defpath, 'LANG': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1',
           'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_ALLOW_PROTOCOL': ''}
    directory_fd = None
    try:
        directory_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in Path(repo).parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        has_dotgit = os.path.lexists('/proc/self/fd/' + str(directory_fd) + '/.git')
        if has_dotgit:
            next_fd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        argv = ['git', '--git-dir=/proc/self/fd/' + str(directory_fd),
                '-c', 'safe.directory=' + repo,
                '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                '-c', 'protocol.allow=never']

        def run(arguments, bound):
            with subprocess.Popen(argv + list(arguments), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  pass_fds=(directory_fd,)) as child:
                data = child.stdout.read(bound + 1)
                if len(data) > bound:
                    child.kill()
                code = child.wait()
            need(code == 0 and len(data) <= bound, 'missing or oversized local git object')
            return data

        if not has_dotgit:
            need(run(('rev-parse', '--is-bare-repository'), LIMIT) == b'true\n',
                 'repository top or bare object store required')
        return run(args, limit)
    except OSError:
        raise SpawnError('unreadable local repository or git unavailable') from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def commit_tree(repo, commit):
    need(match(commit, COMMIT) and commit != '0' * 40, 'full nonzero commit required')
    refs = git(repo, 'for-each-ref', '--format=%(refname)').splitlines()
    need(not any(ref.endswith(b'/' + commit.encode('ascii')) for ref in refs),
         'commit pin collides with ref name')
    resolved = git(repo, 'rev-parse', '--verify', '--quiet', '--end-of-options', commit + '^{commit}')
    need(resolved == (commit + '\n').encode('ascii'), 'exact commit object required')
    need(git(repo, 'cat-file', '-t', commit) == b'commit\n', 'commit object required')
    raw = git(repo, 'cat-file', 'commit', commit)
    first = raw.split(b'\n', 1)[0].decode('ascii')
    need(re.fullmatch('tree ' + COMMIT, first) is not None, 'invalid commit tree')
    return first[5:]


def entries(repo, commit):
    tree = commit_tree(repo, commit)
    raw = git(repo, 'ls-tree', '-rz', commit)
    result = {}
    for record in raw.split(b'\0'):
        if not record:
            continue
        metadata, path = record.split(b'\t', 1)
        mode, kind, oid = metadata.decode('ascii').split()
        name = path.decode('utf-8')
        relative(name)
        result[name] = (mode, kind, oid)
    return tree, result


def blob(repo, commit, path, limit=LIMIT):
    regular_blob(repo, commit, path)
    return git(repo, 'cat-file', 'blob', commit + ':' + path, limit=limit)


def regular_blob(repo, commit, path):
    relative(path)
    _, inventory = entries(repo, commit)
    entry = inventory.get(path)
    need(entry is not None and entry[0] in ('100644', '100755') and entry[1] == 'blob',
         'pinned regular blob required (no symlinks or submodules)')
    return entry


def pinned_blob(repo, commit, path, limit=LIMIT):
    """Read a pinned regular blob and informational commit provenance locally."""
    entry = regular_blob(repo, commit, path)
    identity = absolute(repo)
    headers = git(repo, 'cat-file', 'commit', commit).split(b'\n\n', 1)[0].split(b'\n')
    provenance = {}
    for key in ('author', 'committer'):
        rows = [line[len(key) + 1:] for line in headers if line.startswith(key.encode() + b' ')]
        need(len(rows) == 1, 'commit author and committer required')
        try:
            provenance[key] = rows[0].decode('utf-8')
        except UnicodeError:
            raise SpawnError('invalid commit provenance') from None
    return git(repo, 'cat-file', 'blob', commit + ':' + path, limit=limit), {
        'kind': 'repo', 'repo': identity, 'commit': commit,
        'path': path, 'blob': entry[2], **provenance}


def parse_repo_source(value):
    try:
        location, commit = value.rsplit('@', 1)
        directory, path = location.rsplit('+', 1)
    except (ValueError, AttributeError):
        raise SpawnError('expected local repo + path @ full-commit') from None
    return {'kind': 'repo', 'repo': directory, 'commit': commit, 'path': path}


def load_source(*, inline=None, file=None, repo=None):
    """Exactly one of inline text, file path, or LOCAL_REPO + PATH @ COMMIT."""
    need(sum(v is not None for v in (inline, file, repo)) == 1, 'exactly one source required')
    if inline is not None:
        return decode(inline)
    if file is not None:
        return decode(read_file(file))
    source = parse_repo_source(repo)
    return decode(blob(source['repo'], source['commit'], source['path']))


def resolve_artifact(entry):
    """Compute hashes from pinned local bytes, never from operator digest fields."""
    from .data import digest, fields
    fields(entry, ('repo', 'commit'), ('path',))
    if 'path' in entry:
        content = blob(entry['repo'], entry['commit'], entry['path'], ARTIFACT_LIMIT)
        return dict(entry, kind='blob', size=len(content), sha256=hashlib.sha256(content).hexdigest())
    tree, inventory = entries(entry['repo'], entry['commit'])
    files = []
    total = 0
    for path, (mode, kind, oid) in sorted(inventory.items()):
        need(mode in ('100644', '100755') and kind == 'blob', 'artifact links and submodules refused')
        content = git(entry['repo'], 'cat-file', 'blob', entry['commit'] + ':' + path,
                      limit=ARTIFACT_LIMIT - total)
        total += len(content)
        need(total <= ARTIFACT_LIMIT, 'artifact size limit exceeded')
        files.append({'path': path, 'mode': mode, 'size': len(content),
                      'sha256': hashlib.sha256(content).hexdigest()})
    return dict(entry, kind='git-tree', tree=tree, size=total, files=files, sha256=digest(files))
