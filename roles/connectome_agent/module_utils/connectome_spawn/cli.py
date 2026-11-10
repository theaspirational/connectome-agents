"""Offline JSON-output frontend. No Ansible imports, environment defaults or writes."""
import argparse
import json
import sys

from . import SpawnError, digest, instance, load_catalog, load_source, make_plan, template


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid values, potentially protected text.
        raise SpawnError('invalid CLI arguments; see --help')


def main(argv=None):
    parser = Parser(prog='connectome-spawn', description=__doc__, allow_abbrev=False)
    parser.add_argument('command', choices=('validate', 'plan', 'dry-run'))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--inline', help='inline template JSON or YAML')
    source.add_argument('--file', help='template file')
    source.add_argument('--repo', help='absolute local repository + path @ full-commit')
    parser.add_argument('--instance', required=True, help='instance JSON/YAML file')
    parser.add_argument('--catalog', help='role catalog root with connectors/ and versions.yml; required for planning')
    parser.add_argument('--stores', help='local pin_ref to object-store/payload-root JSON/YAML map')
    parser.add_argument('--observed', help='explicit observed deployment JSON/YAML file')
    try:
        args = parser.parse_args(argv)
        spec = template(load_source(inline=args.inline, file=args.file, repo=args.repo))
        declaration = instance(load_source(file=args.instance))
        if args.command == 'validate':
            if args.catalog or args.stores or args.observed:
                raise SpawnError('validate accepts template and instance only')
            result = {'valid': True, 'templateHash': digest(spec), 'instanceHash': digest(declaration),
                      'catalogResolved': False}
        else:
            if not args.catalog or not args.stores:
                raise SpawnError('planning requires a role catalog and local stores')
            result = make_plan(spec, declaration, load_catalog(args.catalog), load_source(file=args.stores),
                               observed=load_source(file=args.observed) if args.observed else None)
            if args.command == 'dry-run':
                result['wouldWrite'] = result['plan']['files']
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 0
    except (SpawnError, OSError, UnicodeError, RecursionError):
        print(json.dumps({'error': 'spawn input refused', 'filesWritten': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
