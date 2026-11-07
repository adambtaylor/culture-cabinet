"""Explicit portable preview entry; never imports the legacy/demo settings."""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(description='Culture Cabinet portable setup preview')
    parser.add_argument('--data-root', required=True, help='New or compatible private installation directory')
    parser.add_argument('--port', type=int, default=0, help='Loopback port; 0 chooses a free port')
    args = parser.parse_args()
    # Inherited demo/provider selectors must never silently select another store.
    if any(k.startswith(('DJANGO_', 'CC_', 'CABINET_')) for k in os.environ):
        print('PORTABLE_ENVIRONMENT_REFUSED', file=sys.stderr)
        return 1
    from cabinet.portable_runtime import run
    return run(args.data_root, port=args.port)


if __name__ == '__main__':
    raise SystemExit(main())
