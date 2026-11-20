import uuid

from django.core.management.base import BaseCommand

from shelf.beta_feedback import erase_report


class Command(BaseCommand):
    help = 'Erase one beta problem report from its stored installation.'

    def add_arguments(self, parser):
        parser.add_argument('installation', type=uuid.UUID)
        parser.add_argument('reference', type=uuid.UUID)

    def handle(self, *args, **options):
        erased = erase_report(installation_id=options['installation'],
                              reference=options['reference'])
        self.stdout.write('Erasure recorded.' if erased else 'Not found.')
