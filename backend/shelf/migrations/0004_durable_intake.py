"""Add isolated human intake storage; no startup or data migration."""
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0003_saved_intent')]
    operations = [migrations.CreateModel(
        name='IntakeRun',
        fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('revision', models.PositiveIntegerField(default=0)),
            ('nonce', models.CharField(max_length=64)),
            ('selector_version', models.CharField(default='intake-v2.1', max_length=32)),
            ('history', models.JSONField(default=list)),
            ('receipts', models.JSONField(default=dict)),
            ('imported_session', models.CharField(max_length=64, null=True, unique=True)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('archived_at', models.DateTimeField(null=True)),
            ('actor', models.ForeignKey(on_delete=models.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('profile', models.ForeignKey(on_delete=models.PROTECT, to='shelf.profile')),
        ], options={'ordering': ['-created_at', '-id']},
    )]
