"""Add local preview receipts and selection bindings; no legacy/data operations."""
import uuid
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0004_durable_intake')]
    operations = [
        migrations.CreateModel(name='ObtainPreview', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('saved_identity', models.PositiveBigIntegerField()),
            ('provider', models.CharField(max_length=16)),
            ('medium', models.CharField(max_length=5)),
            ('source_id', models.PositiveIntegerField()),
            ('title', models.CharField(max_length=300)),
            ('scope', models.CharField(max_length=14)),
            ('nonce', models.UUIDField(default=uuid.uuid4, editable=False)),
            ('version', models.PositiveIntegerField(default=1)),
            ('state', models.CharField(max_length=24, default='searching')),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('expires_at', models.DateTimeField()),
            ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
            ('profile', models.ForeignKey('shelf.profile', on_delete=models.PROTECT)),
        ]),
        migrations.CreateModel(name='ObtainResult', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('guid', models.CharField(max_length=20)),
            ('metadata', models.JSONField()),
            ('digest', models.CharField(max_length=64)),
            ('preview', models.ForeignKey('shelf.obtainpreview', related_name='results', on_delete=models.CASCADE)),
        ], options={'constraints': [models.UniqueConstraint(fields=['preview', 'guid'], name='obtain_preview_guid')]}),
        migrations.CreateModel(name='ObtainSelection', fields=[
            ('job', models.OneToOneField('shelf.acquisitionjob', primary_key=True,
                serialize=False, on_delete=models.PROTECT)),
            ('result', models.ForeignKey('shelf.obtainresult', on_delete=models.PROTECT)),
        ]),
    ]
