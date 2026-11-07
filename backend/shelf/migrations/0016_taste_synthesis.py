import uuid

from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0015_obtain_completion_status'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='SynthesisRun', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('mode', models.CharField(default='household', max_length=9)),
        ('request_identity', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
        ('input_snapshot', models.JSONField()),
        ('input_digest', models.CharField(max_length=64)),
        ('state', models.CharField(default='queued', max_length=10)),
        ('revision', models.PositiveIntegerField(default=0)),
        ('attempts', models.PositiveSmallIntegerField(default=0)),
        ('output', models.JSONField(default=dict)),
        ('evidence', models.JSONField(default=dict)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('updated_at', models.DateTimeField(auto_now=True)),
        ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
        ('profile', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.profile')),
    ], options={'ordering': ['-created_at', '-id'], 'constraints': [
        models.UniqueConstraint(fields=('actor', 'profile', 'input_digest'), name='synthesis_actor_profile_input'),
        models.CheckConstraint(condition=models.Q(mode='household'), name='synthesis_household_only'),
        models.CheckConstraint(condition=models.Q(state__in=['queued', 'running', 'completed', 'failed', 'unknown']), name='synthesis_state'),
        models.CheckConstraint(condition=models.Q(attempts__lte=1), name='synthesis_one_attempt'),
    ]})]
