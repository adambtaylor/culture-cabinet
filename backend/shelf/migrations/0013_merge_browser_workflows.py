"""Join the independently authored additive social and messaging branches."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('shelf', '0011_social_discovery'), ('shelf', '0012_message_draft')]
    operations = []
