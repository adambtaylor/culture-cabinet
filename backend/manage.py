#!/usr/bin/env python3
"""The only supported entry point for this disposable synthetic demo."""
import os
import sys

sys.dont_write_bytecode = True

from cabinet.boundary import BoundaryError, prepare


def main():
    try:
        prepare(sys.argv[1:], os.environ)
    except BoundaryError:
        raise SystemExit("Refused: fixed synthetic-demo configuration required.") from None
    os.environ["DJANGO_SETTINGS_MODULE"] = "cabinet.settings"
    from django.core.management import execute_from_command_line

    argv = list(sys.argv)
    # prepare has already validated every positional token. Bare test retains
    # its historical app discovery; explicit methods reach Django unchanged.
    if argv[1] == "test" and not any(not arg.startswith("-") for arg in argv[2:]):
        argv.insert(2, "shelf")
    execute_from_command_line(argv)


if __name__ == "__main__":
    main()
