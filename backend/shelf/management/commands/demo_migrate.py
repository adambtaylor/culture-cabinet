from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as Migrate


class Command(Migrate):
    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--synthetic-demo", action="store_true")

    def handle(self, *args, **options):
        if not options.pop("synthetic_demo", False):
            raise CommandError("Explicit synthetic demo required.")
        return super().handle(*args, **options)
