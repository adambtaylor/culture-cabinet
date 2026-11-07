"""Existing synthesis broker + immutable portable publication, SQLite guarded."""
from importlib import import_module
import uuid

from django.db import migrations, models
import django.db.models.deletion


TRIGGERS = [
    '''CREATE TRIGGER enhance_run_insert BEFORE INSERT ON shelf_synthesisrun
       WHEN NEW.contract_version='enhance/v1'
       BEGIN
         SELECT CASE WHEN cabinet_disclosure_write() IS NOT 1 OR NEW.attempts!=0
           OR NEW.state!='queued' OR NEW.transport_request_id IS NOT NULL
           OR NEW.mode!='portable' OR json(NEW.input_snapshot)!='{}'
           OR NOT EXISTS (SELECT 1 FROM shelf_profilecabinetstate s, shelf_portableconnection c
             WHERE s.profile_id=NEW.profile_id AND s.installation_id=NEW.installation_id
               AND s.actor_id=NEW.actor_id AND s.blocked=0 AND c.id=NEW.portable_connection_id
               AND c.installation_id=NEW.installation_id AND c.removed=0)
         THEN RAISE(ABORT,'enhance authority required') END;
       END''',
    '''CREATE TRIGGER enhance_run_update BEFORE UPDATE ON shelf_synthesisrun
       WHEN OLD.contract_version='enhance/v1' OR NEW.contract_version='enhance/v1'
       BEGIN
         SELECT CASE WHEN cabinet_disclosure_write() IS NOT 1
           OR NEW.id IS NOT OLD.id OR NEW.contract_version IS NOT OLD.contract_version
           OR NEW.actor_id IS NOT OLD.actor_id OR NEW.profile_id IS NOT OLD.profile_id
           OR NEW.installation_id IS NOT OLD.installation_id
           OR NEW.portable_connection_id IS NOT OLD.portable_connection_id
           OR NEW.client_key IS NOT OLD.client_key OR NEW.refresh_nonce IS NOT OLD.refresh_nonce
           OR NEW.request_identity IS NOT OLD.request_identity OR NEW.input_digest IS NOT OLD.input_digest
           OR NEW.input_snapshot IS NOT OLD.input_snapshot OR NEW.portable_bindings IS NOT OLD.portable_bindings
           OR json_extract(NEW.evidence,'$.expected_publication_revision') IS NOT json_extract(OLD.evidence,'$.expected_publication_revision')
           OR NEW.mode IS NOT OLD.mode OR NEW.created_at IS NOT OLD.created_at
           OR NEW.revision!=OLD.revision+1 OR NEW.attempts<OLD.attempts OR NEW.attempts>1
           OR (OLD.transport_request_id IS NOT NULL AND NEW.transport_request_id IS NOT OLD.transport_request_id)
           OR (OLD.collection_digest!='' AND NEW.collection_digest IS NOT OLD.collection_digest)
           OR (OLD.body_digest!='' AND NEW.body_digest IS NOT OLD.body_digest)
           OR (OLD.sealed_run_digest!='' AND NEW.sealed_run_digest IS NOT OLD.sealed_run_digest)
           OR NEW.cancel_requested<OLD.cancel_requested
           OR (OLD.portable_expires_at IS NOT NULL AND (NEW.portable_expires_at IS NULL OR NEW.portable_expires_at>OLD.portable_expires_at))
           OR (NEW.transport_request_id IS NOT NULL AND NEW.attempts!=1)
           OR (NEW.attempts>OLD.attempts AND (OLD.state!='queued' OR NEW.state!='running'
                OR NEW.transport_request_id IS NULL OR NEW.body_digest='' OR NEW.collection_digest=''))
           OR (OLD.attempts=1 AND NEW.state='queued')
           OR (OLD.state IN ('completed','failed') AND NEW.state!=OLD.state)
           OR (NEW.state='completed' AND (NEW.attempts!=1
                OR json_extract(NEW.output,'$.schema') IS NOT 'cabinet-inference-result/v1'))
           OR (json(OLD.output)!='{}' AND NEW.output IS NOT OLD.output)
         THEN RAISE(ABORT,'immutable enhance intent or attempt') END;
       END''',
    '''CREATE TRIGGER enhance_run_no_delete BEFORE DELETE ON shelf_synthesisrun
       WHEN OLD.contract_version='enhance/v1'
       BEGIN SELECT RAISE(ABORT,'enhance attempt tombstone required'); END''',
    '''CREATE TRIGGER enhance_manifest_insert BEFORE INSERT ON shelf_portablecabinetmanifest
       WHEN cabinet_disclosure_write() IS NOT 1 OR NOT EXISTS
         (SELECT 1 FROM shelf_synthesisrun r, shelf_profilecabinetstate s
          WHERE r.id=NEW.run_id AND r.profile_id=NEW.cabinet_id AND s.profile_id=NEW.cabinet_id
            AND r.installation_id=s.installation_id AND r.actor_id=s.actor_id
            AND r.contract_version='enhance/v1' AND r.state='completed' AND r.attempts=1
            AND s.blocked=0 AND NEW.publication_revision=s.publication_revision+1)
       BEGIN SELECT RAISE(ABORT,'enhance publication authority required'); END''',
    '''CREATE TRIGGER enhance_manifest_no_update BEFORE UPDATE ON shelf_portablecabinetmanifest
       BEGIN SELECT RAISE(ABORT,'immutable cabinet manifest'); END''',
    '''CREATE TRIGGER enhance_manifest_no_delete BEFORE DELETE ON shelf_portablecabinetmanifest
       BEGIN SELECT RAISE(ABORT,'cabinet publication tombstone required'); END''',
    '''CREATE TRIGGER enhance_publication_floor BEFORE UPDATE ON shelf_profilecabinetstate
       WHEN NEW.publication_revision!=OLD.publication_revision OR NEW.active_manifest_ref IS NOT OLD.active_manifest_ref
       BEGIN
         SELECT CASE WHEN cabinet_disclosure_write() IS NOT 1
           OR NEW.publication_revision!=OLD.publication_revision+1
           OR NOT EXISTS (SELECT 1 FROM shelf_portablecabinetmanifest m
             WHERE m.id=NEW.active_manifest_ref AND m.cabinet_id=NEW.profile_id
               AND m.publication_revision=NEW.publication_revision)
         THEN RAISE(ABORT,'cabinet publication CAS required') END;
       END''',
]


def install_guards(apps, schema_editor):
    if schema_editor.connection.vendor != 'sqlite':
        raise RuntimeError('PORTABLE_SQLITE_REQUIRED')
    schema_editor.connection.connection.create_function('cabinet_disclosure_write', 0, lambda: 0)
    # SQLite constraint changes rebuild tables and remove attached triggers.
    # Restore ONLY trusted migration-owned definitions, never stored SQL.
    prior = import_module('shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    for statement in list(prior) + TRIGGERS:
        name = statement.split('CREATE TRIGGER ', 1)[1].split()[0]
        schema_editor.execute('DROP TRIGGER IF EXISTS ' + name)
        schema_editor.execute(statement)


def prepare_cabinet_remake(apps, schema_editor):
    # Five guards on OTHER tables reference the cabinet table SQLite remakes.
    # Verify the exact predecessor before removing only those dependencies. The
    # atomic migration restores all owned guards before commit; any error rolls
    # back DDL/data/guard removal together. Stored SQL is never executed.
    prior = import_module('shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    normalize = lambda sql: sql.strip().removesuffix(';').rstrip()
    expected = {normalize(sql) for sql in prior}
    with schema_editor.connection.cursor() as cursor:
        actual = {normalize(row[0]) for row in cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
    if schema_editor.connection.vendor != 'sqlite' or len(expected) != 17 or actual != expected:
        raise RuntimeError('PORTABLE_PREDECESSOR_GUARDS_REQUIRED')
    for name in ('disclosure_grant_binding', 'disclosure_grant_update',
                 'disclosure_grant_insert_floor', 'disclosure_grant_update_floor',
                 'disclosure_preview_binding'):
        schema_editor.execute('DROP TRIGGER ' + name)


class Migration(migrations.Migration):
    dependencies = [('shelf', '0021_portable_disclosure_authority')]
    operations = [
        migrations.AddField(model_name='synthesisrun', name=name, field=field)
        for name, field in [
            ('contract_version', models.CharField(max_length=20, default='legacy/v1')),
            ('installation', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='shelf.portableinstallation')),
            ('portable_connection', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='shelf.portableconnection')),
            ('client_key', models.UUIDField(null=True)), ('refresh_nonce', models.UUIDField(null=True)),
            ('portable_stage', models.CharField(max_length=24, default='')),
            ('portable_bindings', models.JSONField(default=dict)),
            ('collection_digest', models.CharField(max_length=64, default='')),
            ('body_digest', models.CharField(max_length=64, default='')),
            ('sealed_run_digest', models.CharField(max_length=64, default='')),
            ('transport_request_id', models.UUIDField(null=True, unique=True)),
            ('portable_expires_at', models.DateTimeField(null=True)),
            ('cancel_requested', models.BooleanField(default=False)),
        ]
    ] + [
        migrations.RemoveConstraint(model_name='synthesisrun', name='synthesis_actor_profile_input'),
        migrations.RemoveConstraint(model_name='synthesisrun', name='synthesis_household_only'),
        migrations.AddConstraint(model_name='synthesisrun', constraint=models.UniqueConstraint(fields=['actor', 'profile', 'input_digest'], condition=models.Q(contract_version='legacy/v1'), name='synthesis_actor_profile_input')),
        migrations.AddConstraint(model_name='synthesisrun', constraint=models.CheckConstraint(condition=(models.Q(contract_version='legacy/v1', mode='household', installation__isnull=True, client_key__isnull=True) | models.Q(contract_version='enhance/v1', mode='portable', installation__isnull=False, portable_connection__isnull=False, client_key__isnull=False, refresh_nonce__isnull=False)), name='synthesis_household_only')),
        migrations.AddConstraint(model_name='synthesisrun', constraint=models.UniqueConstraint(fields=['installation', 'actor', 'profile', 'client_key'], condition=models.Q(contract_version='enhance/v1'), name='enhance_client_identity')),
        migrations.AddConstraint(model_name='synthesisrun', constraint=models.UniqueConstraint(fields=['installation', 'actor', 'profile', 'input_digest'], condition=models.Q(contract_version='enhance/v1'), name='enhance_input_identity')),
        migrations.AddConstraint(model_name='synthesisrun', constraint=models.UniqueConstraint(fields=['profile'], condition=models.Q(contract_version='enhance/v1', attempts=1, state__in=['running', 'unknown']), name='enhance_unresolved_profile')),
        migrations.RunPython(prepare_cabinet_remake),
        migrations.RemoveConstraint(model_name='profilecabinetstate', name='cabinet_publication_dormant'),
        migrations.AddConstraint(model_name='profilecabinetstate', constraint=models.CheckConstraint(condition=(models.Q(active_manifest_ref__isnull=True, publication_revision=0) | models.Q(active_manifest_ref__isnull=False, publication_revision__gte=1)), name='cabinet_publication_dormant')),
        migrations.CreateModel(name='PortableCabinetManifest', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)),
            ('publication_revision', models.PositiveBigIntegerField()), ('bindings', models.JSONField()),
            ('manifest', models.JSONField()), ('created_at', models.DateTimeField()), ('expires_at', models.DateTimeField()),
            ('cabinet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.profilecabinetstate')),
            ('run', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='shelf.synthesisrun')),
        ], options={'constraints': [
            models.UniqueConstraint(fields=['cabinet', 'publication_revision'], name='cabinet_manifest_revision'),
            models.CheckConstraint(condition=models.Q(publication_revision__gte=1), name='cabinet_manifest_positive'),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('created_at')), name='cabinet_manifest_expiry'),
        ]}),
        migrations.RunPython(install_guards),
    ]
