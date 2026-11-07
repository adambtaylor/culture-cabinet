"""Separate experimental runtime; no feedback/recovery lifecycle authority."""
import resource
from django.conf import settings
from django.core.management.base import CommandError
from django.core.management.commands.runserver import Command as Runserver
from shelf.catalogue_server import CatalogueWSGIServer


class Command(Runserver):
    help = "Provider catalogue only, fixed guest 127.0.0.1:8000, temporary intake."
    server_cls = CatalogueWSGIServer

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument('--provider-demo', action='store_true')

    def handle(self, *args, **options):
        if (not options.pop('provider_demo', False) or options.get('addrport')
                or options.get('use_ipv6')
                or settings.ROOT_URLCONF != 'cabinet.provider_urls'):
            raise CommandError('Explicit provider-only runtime required.')
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        options.update(addrport='127.0.0.1:8000', use_reloader=False,
                       use_threading=False, use_ipv6=False)
        return super().handle(*args, **options)

    def inner_run(self, *args, **options):
        if (getattr(self, 'addr', None) != '127.0.0.1'
                or str(getattr(self, 'port', None)) != '8000'
                or getattr(self, 'use_ipv6', True)
                or options.get('use_ipv6', False)
                or options.get('use_reloader') is not False
                or options.get('use_threading') is not False
                or settings.ROOT_URLCONF != 'cabinet.provider_urls'):
            raise CommandError('Fixed IPv4 loopback without reload/threading required.')
        from shelf.weekly_synthesis_runtime import start_clock
        stop, thread = start_clock()
        try:
            return super().inner_run(*args, **options)
        finally:
            stop.set()
            thread.join(timeout=2)
