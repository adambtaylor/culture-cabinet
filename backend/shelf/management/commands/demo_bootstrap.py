from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from shelf.models import Grant, Profile


class Command(BaseCommand):
    help = "Create only fictional demo slots and a disposable operator-password user."

    def add_arguments(self, parser):
        parser.add_argument("--synthetic-demo", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        if not options.get("synthetic_demo"):
            raise CommandError("Explicit synthetic demo required.")
        User = get_user_model()
        if User.objects.exists() or Profile.objects.exists() or Grant.objects.exists():
            raise CommandError("Bootstrap requires an empty synthetic store; no reset is provided.")
        password = getpass("Disposable fictional-demo password: ")
        confirmation = getpass("Confirm disposable password: ")
        if not password or password != confirmation:
            raise CommandError("Disposable passwords must match and be nonempty.")
        user = User(username="demo-curator", first_name="Juniper", last_name="Lantern")
        try:
            validate_password(password, user=user)
        except ValidationError:
            raise CommandError("Use a unique disposable password of at least 12 characters.") from None
        user.set_password(password)
        user.save()
        owner = Profile.objects.create(kind="owner", label="Juniper Lantern", active=True)
        Profile.objects.create(kind="partner", label="Clover Comet", active=False)
        Profile.objects.create(kind="family", label="Pebble Chorus", active=False)
        Grant.objects.create(user=user, profile=owner, can_view=True, can_select=True, can_mutate=True, can_export=True)
        self.stdout.write("Created three fictional slots; only owner is active. Login: demo-curator")
