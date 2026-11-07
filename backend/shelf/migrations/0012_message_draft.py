from django.conf import settings
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    # Parallel to the social worker's 0011; integration must merge both leaves.
    dependencies = [('shelf', '0010_event_plan'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='MessageDraft', fields=[
        ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
        ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
        ('profile', models.ForeignKey('shelf.profile', on_delete=models.PROTECT)),
        ('mode', models.CharField(max_length=12, default='sample')),
        ('active_key', models.CharField(max_length=64, unique=True)),
        ('state', models.CharField(max_length=12, default='draft')),
        ('version', models.PositiveIntegerField(default=0)),
        ('nonce', models.CharField(max_length=64)),
        ('data', models.JSONField(default=dict)),
        ('receipts', models.JSONField(default=dict)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('updated_at', models.DateTimeField(auto_now=True)),
    ], options={'ordering': ['-created_at'], 'constraints': [
        models.CheckConstraint(condition=models.Q(mode='sample'), name='message_sample_only'),
        models.CheckConstraint(condition=models.Q(state__in=[
            'draft', 'queued', 'sending', 'sent', 'failed', 'unknown']), name='message_state'),
    ]})]
