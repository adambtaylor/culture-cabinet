"""Digest-only source origins and scoped operational writers; no legacy conversion."""
from importlib import import_module

from django.db import migrations, models

PREVIOUS = import_module('shelf.migrations.0024_portable_saved_lineage')

ORIGIN_GUARDS = []
for table in ('acquisitionjob', 'obtainpreview'):
    for operation in ('INSERT', 'UPDATE'):
        ORIGIN_GUARDS.append(f'''CREATE TRIGGER obtain_origin_{table}_{operation.lower()}
         BEFORE {operation} ON shelf_{table} WHEN NEW.publication_origin IS NOT NULL
         BEGIN SELECT CASE WHEN cabinet_obtain_origin_valid(NEW.publication_origin) IS NOT 1
          OR cabinet_obtain_scope_valid(NEW.scope) IS NOT 1 OR NOT EXISTS
          (SELECT 1 FROM shelf_profilecabinetstate s WHERE s.profile_id=NEW.profile_id
           AND s.actor_id=NEW.actor_id AND s.data_schema_version=2
           AND s.profile_ref=replace(json_extract(NEW.publication_origin,'$.profile_ref'),'-','')
           AND s.installation_id=replace(json_extract(NEW.publication_origin,'$.installation_ref'),'-',''))
          THEN RAISE(ABORT,'current bounded native origin required') END; END''')
    ORIGIN_GUARDS.append(f'''CREATE TRIGGER obtain_origin_{table}_immutable
     BEFORE UPDATE ON shelf_{table} WHEN NEW.publication_origin IS NOT OLD.publication_origin
      OR (OLD.publication_origin IS NOT NULL AND NEW.scope IS NOT OLD.scope)
     BEGIN SELECT RAISE(ABORT,'immutable obtain origin and episode'); END''')

# Operational rows do not change preference floors. Their existing parent keys
# identify the real actor/profile; none can borrow a writer opened for another
# profile or move an existing receipt onto a different request.
OWNERS = {
    'acquisitionjob': ('SELECT {r}.actor_id actor_id,{r}.profile_id profile_id', 'id'),
    'obtainpreview': ('SELECT {r}.actor_id actor_id,{r}.profile_id profile_id', 'id'),
    'obtainresult': ('SELECT p.actor_id,p.profile_id FROM shelf_obtainpreview p WHERE p.id={r}.preview_id', 'preview_id'),
    'obtainselection': ('SELECT j.actor_id,j.profile_id FROM shelf_acquisitionjob j WHERE j.id={r}.job_id', 'job_id'),
    'obtaintransfer': ('SELECT j.actor_id,j.profile_id FROM shelf_acquisitionjob j WHERE j.id={r}.job_id', 'job_id'),
    'obtainlocalfile': ('SELECT j.actor_id,j.profile_id FROM shelf_acquisitionjob j WHERE j.id={r}.transfer_id', 'transfer_id'),
    'postprocessreceipt': ('SELECT j.actor_id,j.profile_id FROM shelf_acquisitionjob j WHERE j.id={r}.transfer_id', 'transfer_id'),
    'postprocessfile': ('SELECT j.actor_id,j.profile_id FROM shelf_acquisitionjob j WHERE j.id={r}.receipt_id', 'receipt_id'),
    'obtaincompletionstatus': ('SELECT j.actor_id,j.profile_id FROM shelf_acquisitionjob j WHERE j.id={r}.transfer_id', 'transfer_id'),
    'obtainreviewednzb': ('SELECT p.actor_id,p.profile_id FROM shelf_obtainpreview p WHERE p.id={r}.request_id', 'request_id'),
}
WRITER_GUARDS = []
for table, (owner, parent) in OWNERS.items():
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        refs = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
        query = ' UNION ALL '.join(owner.format(r=ref) for ref in refs)
        scoped = f'''SELECT 1 FROM ({query}) owned JOIN shelf_profilecabinetstate s
          ON s.profile_id=owned.profile_id WHERE s.data_schema_version=2'''
        refusal = f'''EXISTS (SELECT 1 FROM ({query}) owned JOIN shelf_profilecabinetstate s
          ON s.profile_id=owned.profile_id WHERE s.data_schema_version=2
          AND (s.actor_id IS NOT owned.actor_id
           OR cabinet_obtain_write(owned.actor_id,owned.profile_id) IS NOT 1))'''
        if operation == 'UPDATE':
            refusal += f' OR NEW.{parent} IS NOT OLD.{parent}'
            if table in ('acquisitionjob', 'obtainpreview'):
                refusal += ' OR NEW.actor_id IS NOT OLD.actor_id OR NEW.profile_id IS NOT OLD.profile_id'
            if table == 'acquisitionjob':
                refusal += ' OR NEW.request_id IS NOT OLD.request_id'
            if table == 'obtaintransfer':
                refusal += ''' OR NEW.binding IS NOT OLD.binding OR NEW.result_guid IS NOT OLD.result_guid
                 OR NEW.nzb_sha256 IS NOT OLD.nzb_sha256
                 OR ((NEW.state IS NOT OLD.state OR NEW.reason IS NOT OLD.reason
                  OR NEW.provider_job IS NOT OLD.provider_job
                  OR NEW.local_cancelled_at IS NOT OLD.local_cancelled_at
                  OR NEW.native_progress IS NOT OLD.native_progress
                  OR NEW.library_verification IS NOT OLD.library_verification)
                  AND NEW.revision IS NOT OLD.revision+1)'''
        WRITER_GUARDS.append(f'''CREATE TRIGGER obtain_native_{table}_{operation.lower()}
         BEFORE {operation} ON shelf_{table} WHEN EXISTS ({scoped})
         BEGIN SELECT CASE WHEN {refusal}
          THEN RAISE(ABORT,'scoped native obtain writer required') END; END''')

SELECTION_GUARDS = [f'''CREATE TRIGGER obtain_native_selection_identity_{operation.lower()}
 BEFORE {operation} ON shelf_obtainselection
 WHEN EXISTS (SELECT 1 FROM shelf_acquisitionjob j JOIN shelf_profilecabinetstate s
  ON s.profile_id=j.profile_id WHERE j.id=NEW.job_id AND s.data_schema_version=2)
 BEGIN SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM shelf_acquisitionjob j
  JOIN shelf_obtainresult r ON r.id=NEW.result_id JOIN shelf_obtainpreview p ON p.id=r.preview_id
  WHERE j.id=NEW.job_id AND j.actor_id=p.actor_id AND j.profile_id=p.profile_id
   AND j.request_id=p.id AND j.scope=p.scope AND j.publication_origin IS p.publication_origin)
 THEN RAISE(ABORT,'exact owned native preview selection required') END; END'''
 for operation in ('INSERT', 'UPDATE')]

TRIGGERS = list(PREVIOUS.TRIGGERS) + ORIGIN_GUARDS + WRITER_GUARDS + SELECTION_GUARDS


def forward_drop(apps, editor):
    PREVIOUS.drop(editor, PREVIOUS.TRIGGERS)


def forward_install(apps, editor):
    PREVIOUS.install(editor, TRIGGERS)


def reverse_drop(apps, editor):
    # Reversal may not discard surviving commands or verified operational proof.
    for name in ('AcquisitionJob', 'ObtainPreview'):
        if apps.get_model('shelf', name).objects.filter(publication_origin__isnull=False).exists():
            raise RuntimeError('OBTAIN_ORIGIN_HISTORY_REVERSAL_REFUSED')
    transfer = apps.get_model('shelf', 'ObtainTransfer')
    if transfer.objects.exclude(native_progress={}).exists() or transfer.objects.filter(library_verification__isnull=False).exists():
        raise RuntimeError('OBTAIN_OPERATIONAL_HISTORY_REVERSAL_REFUSED')
    PREVIOUS.drop(editor, TRIGGERS)


def reverse_install(apps, editor):
    PREVIOUS.install(editor, PREVIOUS.TRIGGERS)


def effective_inventory(raw):
    tables = {'acquisitionjob': {'publication_origin'}, 'obtainpreview': {'publication_origin'},
              'obtaintransfer': {'native_progress', 'library_verification'}}
    applied = raw.execute("SELECT 1 FROM django_migrations WHERE app='shelf' AND name='0025_obtain_native_origin'").fetchone() is not None
    for table, fields in tables.items():
        columns = {row[1] for row in raw.execute('PRAGMA table_info(shelf_' + table + ')')}
        present = fields & columns
        if (present != fields if applied else bool(present)):
            raise RuntimeError('OBTAIN_ORIGIN_SCHEMA_MISMATCH')
    return TRIGGERS if applied else PREVIOUS.effective_inventory(raw)


PUBLICATION_SCOPE = (
    models.Q(publication_origin__isnull=False, scope='series_s01e01')
    | models.Q(publication_origin__isnull=False, scope__startswith='episode_s')
    | models.Q(provider='tmdb', medium='movie', scope='film')
    | (models.Q(provider='tmdb', medium='tv') &
       (models.Q(scope='series_s01e01') | models.Q(scope__startswith='episode_s')))
    | models.Q(provider='hardcover', medium='book', scope='audiobook')
    | models.Q(provider='album_request', medium='music', scope='album_flac', saved_item__isnull=True))


class Migration(migrations.Migration):
    atomic = True
    dependencies = [('shelf', '0024_portable_saved_lineage')]
    operations = [
        migrations.RunPython(forward_drop, reverse_install),
        migrations.AddField('acquisitionjob', 'publication_origin', models.JSONField(null=True, default=None)),
        migrations.AddField('obtainpreview', 'publication_origin', models.JSONField(null=True, default=None)),
        migrations.AddField('obtaintransfer', 'native_progress', models.JSONField(default=dict)),
        migrations.AddField('obtaintransfer', 'library_verification', models.JSONField(null=True, default=None)),
        *[migrations.AlterField(name, field, spec) for name in ('acquisitionjob', 'obtainpreview')
          for field, spec in (('provider', models.CharField(max_length=16, null=True)),
                              ('medium', models.CharField(max_length=5, null=True)),
                              ('source_id', models.PositiveIntegerField(null=True)),
                              ('title', models.CharField(max_length=300, null=True)))],
        migrations.AlterField('obtainpreview', 'saved_identity', models.PositiveBigIntegerField(null=True)),
        migrations.RemoveConstraint('acquisitionjob', 'acquisition_exact_scope'),
        migrations.AddConstraint('acquisitionjob', models.CheckConstraint(condition=PUBLICATION_SCOPE, name='acquisition_exact_scope')),
        migrations.AddConstraint('acquisitionjob', models.CheckConstraint(condition=(
            models.Q(publication_origin__isnull=True, provider__isnull=False,
                     medium__isnull=False, source_id__isnull=False, title__isnull=False)
            | models.Q(publication_origin__isnull=False, saved_item__isnull=True,
                       provider__isnull=True, medium__isnull=True, source_id__isnull=True, title__isnull=True)), name='acquisition_origin_variant')),
        migrations.AddConstraint('obtainpreview', models.CheckConstraint(condition=(
            models.Q(publication_origin__isnull=True, saved_identity__isnull=False,
                     provider__isnull=False, medium__isnull=False, source_id__isnull=False, title__isnull=False)
            | models.Q(publication_origin__isnull=False, saved_identity__isnull=True,
                       provider__isnull=True, medium__isnull=True, source_id__isnull=True, title__isnull=True)), name='obtain_preview_origin_variant')),
        migrations.RunPython(forward_install, reverse_drop),
    ]
