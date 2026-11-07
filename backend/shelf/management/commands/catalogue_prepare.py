"""Explicit operator-only migration, never called by startup or saved views.

Stop the provider runtime first. Take and verify an opaque backup of the whole
existing store (including SQLite sidecars if present) and session key, privately.
From the authorized literal deployment root, inspect --plan, then run this fixed
command with --provider-demo. No reset, grant changes or bootstrap is performed.
Rollback code first with the server stopped; restore the verified opaque backup
to reverse schema changes. Do not use the synthetic feedback recovery workflow
as the provider runtime's backup or promotion authority.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command

from cabinet import boundary


class Command(BaseCommand):
    help = 'Explicit provider schema preparation; requires stopped runtime and verified opaque backup.'

    def add_arguments(self, parser):
        parser.add_argument('--provider-demo', action='store_true')
        parser.add_argument('--preserve-intake', action='store_true')
        parser.add_argument('--plan', action='store_true')
        parser.add_argument('--check', action='store_true')
        parser.add_argument('--noinput', '--no-input', action='store_false', dest='interactive', default=True)

    def handle(self, *args, **options):
        if (not options.get('provider_demo') or not boundary._provider
                or settings.ROOT_URLCONF != 'cabinet.provider_urls'):
            raise CommandError('Explicit fixed provider preparation required.')
        boundary.validate_paths(demo=True)
        call_command('migrate', interactive=options['interactive'], plan=options['plan'],
                     check_unapplied=options['check'], verbosity=options['verbosity'],
                     stdout=self.stdout, stderr=self.stderr)

        if options.get('preserve_intake') and not options['plan'] and not options['check']:
            from shelf.durable_intake import preserve_sessions
            self.stdout.write(f'Preserved intake sessions: {preserve_sessions()}')
