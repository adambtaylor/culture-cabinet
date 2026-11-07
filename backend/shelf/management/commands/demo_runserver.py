from django.core.management.base import CommandError
from django.core.management.commands.runserver import Command as Runserver


class Command(Runserver):
    help = "Explicit synthetic demo, fixed 127.0.0.1:8000 only, without reload/workers."

    def __init__(self, *args, validated_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        # No production I/O during command discovery, argument parsing or handle.
        if validated_store is not None:
            from shelf.recovery import _sealed_store, RecoveryBlocked
            if not _sealed_store(validated_store) or validated_store.mode != 'fixture':
                raise RecoveryBlocked()
        self.validated_store = validated_store

    def inner_run(self, *args, **options):
        from shelf.recovery import (validated_demo_store, synthetic_demo_lifecycle,
                                    _sealed_store, RecoveryBlocked)
        if (getattr(self, 'addr', None) != '127.0.0.1'
                or str(getattr(self, 'port', None)) != '8000'
                or getattr(self, 'use_ipv6', True)
                or options.get('use_ipv6', False)
                or options.get('use_reloader') is not False
                or options.get('use_threading') is not False):
            raise CommandError("Fixed IPv4 loopback without reload/threading required.")
        store = self.validated_store
        if store is None:
            store = validated_demo_store()
        elif not _sealed_store(store) or store.mode != 'fixture':
            raise RecoveryBlocked()
        with synthetic_demo_lifecycle(validated_store=store):
            return super().inner_run(*args, **options)

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--synthetic-demo", action="store_true")

    def handle(self, *args, **options):
        if (not options.pop("synthetic_demo", False)
                or options.get("addrport") or options.get("use_ipv6")):
            raise CommandError("Explicit synthetic demo on fixed loopback required.")
        options.update(addrport="127.0.0.1:8000", use_reloader=False,
                       use_threading=False, use_ipv6=False)
        return super().handle(*args, **options)

