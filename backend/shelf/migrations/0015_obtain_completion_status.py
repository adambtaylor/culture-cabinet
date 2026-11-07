import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0014_obtain_reviewed_nzb')]
    operations = [migrations.CreateModel(name='ObtainCompletionStatus', fields=[
        ('transfer', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
            primary_key=True, serialize=False, to='shelf.obtaintransfer')),
        ('completion', models.JSONField()),
        ('completion_sha256', models.CharField(max_length=64)),
        ('observed_at', models.DateTimeField(default=django.utils.timezone.now)),
        ('delivery_state', models.CharField(default='not_reported', max_length=16)),
        ('delivery_receipt', models.JSONField(default=dict)),
        ('delivery_sha256', models.CharField(blank=True, default='', max_length=64)),
        ('delivery_at', models.DateTimeField(null=True)),
        ('revision', models.PositiveIntegerField(default=0)),
    ], options={'constraints': [models.CheckConstraint(condition=models.Q(
        delivery_state__in=['not_reported', 'blocked', 'unknown', 'imported']),
        name='obtain_completion_delivery_state')]})]
