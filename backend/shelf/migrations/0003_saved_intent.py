"""Add local saved intent and unconfigured acquisition requests; no data operations."""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shelf", "0002_feedback")]
    operations = [
        migrations.CreateModel(
            name='SavedItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
                ('profile', models.ForeignKey('shelf.profile', on_delete=models.PROTECT)),
                ('provider', models.CharField(max_length=16)),
                ('medium', models.CharField(max_length=5)),
                ('source_id', models.PositiveIntegerField()),
                ('list_kind', models.CharField(max_length=9)),
                ('title', models.CharField(max_length=300)),
                ('url', models.URLField(max_length=300)),
                ('fetched_at', models.DateTimeField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['id'], 'constraints': [models.UniqueConstraint(fields=['actor', 'profile', 'provider', 'medium', 'source_id', 'list_kind'], name='saved_identity_list'), models.CheckConstraint(condition=models.Q(provider='tmdb', medium__in=['movie', 'tv']) | models.Q(provider='hardcover', medium='book'), name='saved_provider_medium'), models.CheckConstraint(condition=models.Q(list_kind='favorites') | models.Q(list_kind='watch', medium__in=['movie', 'tv']) | models.Q(list_kind='read', medium='book'), name='saved_list_medium'), models.CheckConstraint(condition=models.Q(source_id__gte=1, source_id__lte=2147483647), name='saved_source_id')]},
        ),
        migrations.CreateModel(
            name='AcquisitionJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
                ('profile', models.ForeignKey('shelf.profile', on_delete=models.PROTECT)),
                ('saved_item', models.ForeignKey('shelf.saveditem', null=True, on_delete=models.SET_NULL)),
                ('request_id', models.UUIDField()),
                ('active_key', models.CharField(max_length=64, null=True, unique=True)),
                ('provider', models.CharField(max_length=16)),
                ('medium', models.CharField(max_length=5)),
                ('source_id', models.PositiveIntegerField()),
                ('title', models.CharField(max_length=300)),
                ('scope', models.CharField(max_length=14)),
                ('edition', models.CharField(max_length=200)),
                ('storage', models.CharField(max_length=24)),
                ('authorized_content', models.BooleanField()),
                ('storage_check', models.BooleanField()),
                ('status', models.CharField(max_length=24, default='blocked_configuration')),
                ('reason', models.CharField(max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('cancelled_at', models.DateTimeField(null=True)),
            ],
            options={'ordering': ['id'], 'constraints': [models.UniqueConstraint(fields=['actor', 'profile', 'request_id'], name='acquisition_request_receipt'), models.CheckConstraint(condition=models.Q(provider='tmdb', medium='movie', scope='film') | models.Q(provider='tmdb', medium='tv', scope='series_s01e01') | models.Q(provider='hardcover', medium='book', scope='audiobook'), name='acquisition_exact_scope'), models.CheckConstraint(condition=models.Q(authorized_content=True, storage_check=True, storage='local_library'), name='acquisition_confirmed_checks'), models.CheckConstraint(condition=models.Q(status='blocked_configuration', active_key__isnull=False, cancelled_at__isnull=True) | models.Q(status='cancelled', active_key__isnull=True, cancelled_at__isnull=False), name='acquisition_local_state')]},
        ),
    ]
