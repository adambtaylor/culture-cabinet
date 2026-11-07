from django.conf import settings
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('shelf', '0008_obtain_local_metadata'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.RemoveConstraint(model_name='acquisitionjob', name='acquisition_exact_scope'),
        migrations.AddConstraint(model_name='acquisitionjob', constraint=models.CheckConstraint(
            condition=(models.Q(provider='tmdb', medium='movie', scope='film') |
                       models.Q(provider='tmdb', medium='tv', scope='series_s01e01') |
                       models.Q(provider='hardcover', medium='book', scope='audiobook') |
                       models.Q(provider='album_request', medium='music', scope='album_flac', saved_item__isnull=True)),
            name='acquisition_exact_scope')),
        migrations.CreateModel(name='AlbumRequest', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
            ('profile', models.ForeignKey('shelf.profile', on_delete=models.PROTECT)),
            ('request_id', models.UUIDField(unique=True)),
            ('release_key', models.CharField(max_length=240)),
            ('identity', models.JSONField()),
            ('authorization', models.JSONField()),
            ('authorized_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'constraints': [models.UniqueConstraint(fields=['actor', 'profile', 'release_key'],
                                                            name='album_request_release')]}),
        migrations.CreateModel(name='PostprocessReceipt', fields=[
            ('transfer', models.OneToOneField('shelf.obtaintransfer', primary_key=True, serialize=False, on_delete=models.PROTECT)),
            ('receipt', models.JSONField()),
            ('state', models.CharField(max_length=24, default='postprocess_verified')),
            ('checked_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'constraints': [models.CheckConstraint(condition=models.Q(
            state__in=['postprocess_verified', 'unavailable']), name='postprocess_receipt_state')]}),
        migrations.CreateModel(name='PostprocessFile', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('receipt', models.ForeignKey('shelf.postprocessreceipt', on_delete=models.PROTECT, related_name='files')),
            ('relative_path', models.CharField(max_length=4096)),
            ('sha256', models.CharField(max_length=64)),
            ('size', models.PositiveBigIntegerField()),
            ('verified_at', models.DateTimeField()),
        ], options={'ordering': ['relative_path'], 'constraints': [models.UniqueConstraint(
            fields=['receipt', 'relative_path'], name='postprocess_file_path')]}),
    ]
