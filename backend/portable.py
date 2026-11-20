"""Explicit portable preview entry; never imports the legacy/demo settings."""
import argparse
import os
import sys
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description='Culture Cabinet portable setup preview')
    parser.add_argument('--data-root', required=True, help='Private installation directory')
    parser.add_argument('--port', type=int, help='Loopback port; 0 chooses a free port')
    parser.add_argument('--erase-beta-report', nargs=2, type=uuid.UUID,
        metavar=('INSTALLATION_UUID', 'REPORT_UUID'))
    args = parser.parse_args(argv)
    # Inherited demo/provider selectors must never silently select another store.
    if any(k.startswith(('DJANGO_', 'CC_', 'CABINET_')) for k in os.environ):
        print('PORTABLE_ENVIRONMENT_REFUSED', file=sys.stderr)
        return 1
    if args.erase_beta_report:
        if args.port is not None:
            print('PORTABLE_ERASURE_OPTIONS_REFUSED', file=sys.stderr)
            return 1
        from cabinet.portable_runtime import RuntimeFailure, erase_existing
        try:
            erased = erase_existing(args.data_root, *args.erase_beta_report)
        except RuntimeFailure as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print('REPORT_ERASURE_RECORDED' if erased else 'REPORT_NOT_FOUND')
        return 0
    from cabinet.portable_runtime import run
    return run(args.data_root, port=0 if args.port is None else args.port)


if __name__ == '__main__':
    raise SystemExit(main())
