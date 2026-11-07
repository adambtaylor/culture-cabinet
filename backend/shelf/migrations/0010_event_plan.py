from django.conf import settings
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    dependencies = [('shelf', '0009_album_postprocess_files'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='EventPlan', fields=[
        ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
        ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
        ('profile', models.ForeignKey('shelf.profile', on_delete=models.PROTECT)),
        ('mode', models.CharField(max_length=12, default='sample')),
        ('active_key', models.CharField(max_length=64, unique=True)),
        ('version', models.PositiveIntegerField(default=0)),
        ('nonce', models.CharField(max_length=64)),
        ('data', models.JSONField(default=dict)),
        ('receipts', models.JSONField(default=dict)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('updated_at', models.DateTimeField(auto_now=True)),
    ], options={'ordering': ['-created_at'], 'constraints': [models.CheckConstraint(
        condition=models.Q(mode__in=['sample', 'household']), name='event_plan_mode')]})]
