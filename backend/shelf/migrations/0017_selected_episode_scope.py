"""Permit explicit single-episode identities without SQLite custom functions.

Canonical season/episode validation belongs to the shared Python scope parser.
The database keeps provider/media families and every pre-existing safety check.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('shelf', '0016_taste_synthesis')]
    operations = [
        migrations.RemoveConstraint(model_name='acquisitionjob', name='acquisition_exact_scope'),
        migrations.AlterField(model_name='acquisitionjob', name='scope',
                              field=models.CharField(max_length=16)),
        migrations.AlterField(model_name='obtainpreview', name='scope',
                              field=models.CharField(max_length=16)),
        migrations.AddConstraint(model_name='acquisitionjob', constraint=models.CheckConstraint(
            condition=(models.Q(provider='tmdb', medium='movie', scope='film')
                | (models.Q(provider='tmdb', medium='tv')
                   & (models.Q(scope='series_s01e01') | models.Q(scope__startswith='episode_s')))
                | models.Q(provider='hardcover', medium='book', scope='audiobook')
                | models.Q(provider='album_request', medium='music', scope='album_flac',
                           saved_item__isnull=True)), name='acquisition_exact_scope')),
    ]
