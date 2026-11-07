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


def guard_inventory(editor, definitions):
    """Compare trusted names/tables/full definitions; stored SQL is never run."""
    import re
    expected = set()
    for sql in definitions:
        match = re.match(r'CREATE TRIGGER (\w+)\s+(?:BEFORE|AFTER) (?:INSERT|UPDATE|DELETE) ON (\w+)\b', sql)
        if match is None:
            raise RuntimeError('PORTABLE_GUARD_SOURCE_INVALID')
        expected.add((match[1], match[2], sql.strip().removesuffix(';').rstrip()))
    with editor.connection.cursor() as cursor:
        actual = {(name, table, sql.strip().removesuffix(';').rstrip()) for name, table, sql in
                  cursor.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'").fetchall()}
    if editor.connection.vendor != 'sqlite' or actual != expected or len(expected) != len(definitions):
        raise RuntimeError('PORTABLE_PREDECESSOR_GUARDS_REQUIRED')


def trusted_table_inventory(apps, editor):
    """Render trusted historical models without executing inspected schema SQL.

    Column types alone cannot detect a removed CHECK or foreign key. Compare
    the full trusted table definition too, tolerating only SQLite's identifier
    quoting and insignificant whitespace, while preserving string literals.
    Rendering uses a separate collector so deferred DDL cannot leak into the
    migration editor that will actually perform the reverse.
    """
    import re
    collector = type(editor)(editor.connection, collect_sql=True)
    collector.deferred_sql = []
    def tokens(sql):
        parts = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\w+|[^\s]", sql.strip().removesuffix(';'))
        return tuple(part if part.startswith("'") else part.strip('"').lower() for part in parts)
    with editor.connection.cursor() as cursor:
        actual = dict(cursor.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall())
        index_sql = []
        for model in apps.get_models(include_auto_created=True):
            sql, params = collector.table_sql(model)
            if params or tokens(actual[model._meta.db_table]) != tokens(sql):
                raise RuntimeError('PORTABLE_REVERSE_SCHEMA_REFUSED')
            index_sql.extend(collector._model_indexes_sql(model))
        index_sql.extend(collector.deferred_sql)
        actual_indexes = {tokens(row[0]) for row in cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall()}
        if actual_indexes != {tokens(str(sql)) for sql in index_sql if sql is not None}:
            raise RuntimeError('PORTABLE_REVERSE_SCHEMA_REFUSED')


def legacy_reverse_eligible(apps, editor):
    """A downgrade is only a legacy traversal, never portable-state disposal.

    This runs before the first reverse DDL in the migration's atomic transaction.
    Unknown schema, protected identity, private content or nondefault additions
    refuse unchanged; rows are never erased/neutralized to make them eligible.
    """
    if not editor.connection.in_atomic_block:
        raise RuntimeError('PORTABLE_ATOMIC_REVERSE_REQUIRED')
    with editor.connection.cursor() as cursor:
        models_by_table = {m._meta.db_table: m for m in apps.get_models(include_auto_created=True)}
        actual = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if actual != set(models_by_table) | {'django_migrations', 'sqlite_sequence'}:
            raise RuntimeError('PORTABLE_REVERSE_SCHEMA_REFUSED')
        if cursor.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone():
            raise RuntimeError('PORTABLE_REVERSE_SCHEMA_REFUSED')
        for table, model in models_by_table.items():
            columns = cursor.execute('PRAGMA table_info(' + editor.quote_name(table) + ')').fetchall()
            expected = {field.column: field.db_type(editor.connection).lower() for field in model._meta.local_fields}
            if {row[1]: row[2].lower() for row in columns} != expected:
                raise RuntimeError('PORTABLE_REVERSE_SCHEMA_REFUSED')
        for table in ('portableinstallation', 'portableconnection', 'portableprobeattempt', 'portablevalidation',
                      'profilecabinetstate', 'portabledisclosuregrant', 'portabledisclosurepreview', 'portablecabinetmanifest'):
            full = 'shelf_' + table
            if full in actual and cursor.execute('SELECT 1 FROM ' + full + ' LIMIT 1').fetchone():
                raise RuntimeError('PORTABLE_STATE_DOWNGRADE_REFUSED')
        if cursor.execute("SELECT 1 FROM shelf_feedbacktarget WHERE source_namespace='enhance-current' LIMIT 1").fetchone():
            raise RuntimeError('PORTABLE_STATE_DOWNGRADE_REFUSED')
        defaults = {'contract_version': 'legacy/v1', 'mode': 'household', 'installation_id': None,
            'portable_connection_id': None, 'client_key': None, 'refresh_nonce': None, 'portable_stage': '',
            'portable_bindings': {}, 'collection_digest': '', 'body_digest': '', 'sealed_run_digest': '',
            'transport_request_id': None, 'portable_expires_at': None, 'cancel_requested': 0}
        columns = {row[1] for row in cursor.execute('PRAGMA table_info(shelf_synthesisrun)').fetchall()}
        if 'content_redacted_at' in columns:
            defaults.update(content_redacted_at=None, content_digest='')
        import json
        names = list(defaults)
        for row in cursor.execute('SELECT ' + ','.join(names) + ' FROM shelf_synthesisrun').fetchall():
            values = dict(zip(names, row))
            try:
                values['portable_bindings'] = json.loads(values['portable_bindings'])
            except (ValueError, TypeError):
                raise RuntimeError('PORTABLE_STATE_DOWNGRADE_REFUSED') from None
            if values != defaults:
                raise RuntimeError('PORTABLE_STATE_DOWNGRADE_REFUSED')
        if cursor.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('PORTABLE_REVERSE_SCHEMA_REFUSED')
    trusted_table_inventory(apps, editor)


def drop_inventory(editor, definitions):
    for sql in definitions:
        editor.execute('DROP TRIGGER ' + sql.split('CREATE TRIGGER ', 1)[1].split()[0])


def restore_inventory(editor, definitions):
    guard_inventory(editor, [])
    for sql in definitions:
        editor.execute(sql)
    guard_inventory(editor, definitions)


def prepare_cabinet_remake(apps, schema_editor):
    # Remove every named predecessor dependency BEFORE any table remake. Atomic
    # DDL keeps the temporary absence private; restoration failure rolls it back.
    prior = import_module('shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    guard_inventory(schema_editor, prior)
    drop_inventory(schema_editor, prior)


def reverse_install_guards(apps, schema_editor):
    prior = import_module('shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    guard_inventory(schema_editor, list(prior) + TRIGGERS)
    legacy_reverse_eligible(apps, schema_editor)
    drop_inventory(schema_editor, list(prior) + TRIGGERS)


def reverse_prepare(apps, schema_editor):
    prior = import_module('shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
    restore_inventory(schema_editor, prior)


class Migration(migrations.Migration):
    dependencies = [('shelf', '0021_portable_disclosure_authority')]
    operations = [migrations.RunPython(prepare_cabinet_remake, reverse_prepare)] + [
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
        migrations.RunPython(install_guards, reverse_install_guards),
    ]
