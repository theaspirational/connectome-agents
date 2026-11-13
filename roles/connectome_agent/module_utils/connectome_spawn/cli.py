"""Offline JSON frontend. Recovery/reconcile write only the private local journal."""
import argparse
import json
import sys

from .lifecycle import Lifecycle
from .seed import seed_payload
from .sources import parse_repo_source
from . import SpawnError, digest, instance, load_catalog, load_source, make_plan, template


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid values, potentially protected text.
        raise SpawnError('invalid CLI arguments; see --help')


def main(argv=None):
    parser = Parser(prog='connectome-spawn', description=__doc__, allow_abbrev=False)
    parser.add_argument('command', choices=('validate', 'plan', 'dry-run', 'status', 'recover', 'reconcile'))
    parser.add_argument('agent', nargs='?', help='persisted agent identifier for status')
    parser.add_argument('--state-root', help='explicit lifecycle state root for status')
    parser.add_argument('--operation', help='pending operation id for recovery')
    parser.add_argument('--reason', help='explicit operator recovery statement')
    parser.add_argument('--seed-file', help='opaque local seed payload for planning')
    naming = parser.add_mutually_exclusive_group()
    naming.add_argument('--name-file', help='local name record for planning')
    naming.add_argument('--name-repo', help='name record: absolute local repository + path @ full-commit')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--inline', help='inline template JSON or YAML')
    source.add_argument('--file', help='template file')
    source.add_argument('--repo', help='absolute local repository + path @ full-commit')
    parser.add_argument('--instance', help='instance JSON/YAML file')
    parser.add_argument('--catalog', help='role catalog root with connectors/ and versions.yml; required for planning')
    parser.add_argument('--stores', help='local pin_ref to object-store/payload-root JSON/YAML map')
    parser.add_argument('--observed', help='planning: deployment observation file; recovery: applied or not-applied')
    mutation_attempted = False
    try:
        args = parser.parse_args(argv)
        if args.command in ('status', 'recover', 'reconcile'):
            if not args.agent or not args.state_root or any(value is not None for value in (args.inline, args.file, args.repo,
                    args.instance, args.catalog, args.stores, args.seed_file, args.name_file, args.name_repo)):
                raise SpawnError('status requires agent and state root only')
            life = Lifecycle(args.state_root)
            if args.command == 'recover':
                mutation_attempted = True
                result = life.recover(args.agent, operation_id=args.operation, observed=args.observed, reason=args.reason)
            else:
                if args.operation or args.reason or args.observed:
                    raise SpawnError('unexpected lifecycle arguments')
                mutation_attempted = args.command == 'reconcile'
                result = life.status(args.agent) if args.command == 'status' else life.reconcile(args.agent)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0
        if args.agent or args.state_root or args.operation or args.reason or not args.instance:
            raise SpawnError('template command requires instance, not status arguments')
        spec = template(load_source(inline=args.inline, file=args.file, repo=args.repo))
        declaration = instance(load_source(file=args.instance))
        if args.command == 'validate':
            if args.catalog or args.stores or args.observed or args.seed_file or args.name_file or args.name_repo:
                raise SpawnError('validate accepts template and instance only')
            result = {'valid': True, 'templateHash': digest(spec), 'instanceHash': digest(declaration),
                      'catalogResolved': False}
        else:
            if not args.catalog or not args.stores:
                raise SpawnError('planning requires a role catalog and local stores')
            result = make_plan(spec, declaration, load_catalog(args.catalog), load_source(file=args.stores),
                               observed=load_source(file=args.observed) if args.observed else None,
                               seed_payload=seed_payload(args.seed_file) if args.seed_file else None,
                               name_source=parse_repo_source(args.name_repo) if args.name_repo is not None else args.name_file)
            if args.command == 'dry-run':
                result['wouldWrite'] = result['plan']['files']
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 0
    except (SpawnError, OSError, UnicodeError, RecursionError) as exc:
        error = {'error': 'spawn input refused', 'filesWritten': None if mutation_attempted else False}
        if isinstance(exc, SpawnError) and str(exc).startswith('migrate template v1'):
            error['migration'] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
