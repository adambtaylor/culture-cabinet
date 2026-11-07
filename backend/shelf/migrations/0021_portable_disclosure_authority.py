"""Dormant portable authority, including raw/bulk-write and no-delete guards.

SQLite triggers require a connection-local write window from the owning service.
There is no ORM-signal bypass: unknown connections fail closed. The capability is
not an adversarial SQL sandbox; arbitrary trusted Python can replace a UDF. No
rollback/restore safety is claimed for replacing the whole database (D5/E3).
"""
import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def guard_sql():
    statements = []
    for table in ('profilecabinetstate', 'portabledisclosuregrant', 'portabledisclosurepreview'):
        full = 'shelf_' + table
        for operation in ('INSERT', 'UPDATE'):
            statements.append(f'''CREATE TRIGGER {table}_{operation.lower()}_authority
                BEFORE {operation} ON {full}
                WHEN cabinet_disclosure_write() IS NOT 1
                BEGIN SELECT RAISE(ABORT, 'disclosure authority required'); END''')
        statements.append(f'''CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {full}
            BEGIN SELECT RAISE(ABORT, 'disclosure tombstone required'); END''')
    statements.extend([
        '''CREATE TRIGGER cabinet_binding_insert BEFORE INSERT ON shelf_profilecabinetstate
           WHEN NOT EXISTS (SELECT 1 FROM shelf_profile p, shelf_portableinstallation i
             WHERE p.id=NEW.profile_id AND p.kind='owner' AND p.active=1
             AND i.id=NEW.installation_id AND i.owner_id=NEW.actor_id
             AND i.owner_revision=NEW.owner_revision)
           BEGIN SELECT RAISE(ABORT, 'foreign cabinet binding'); END''',
        '''CREATE TRIGGER cabinet_binding_update BEFORE UPDATE ON shelf_profilecabinetstate
           WHEN NEW.profile_id IS NOT OLD.profile_id OR NEW.installation_id IS NOT OLD.installation_id
             OR NEW.actor_id IS NOT OLD.actor_id OR NEW.profile_ref IS NOT OLD.profile_ref
             OR NEW.actor_ref IS NOT OLD.actor_ref OR NEW.subject_ref IS NOT OLD.subject_ref
             OR NEW.owner_revision IS NOT OLD.owner_revision
             OR NEW.policy_epoch < OLD.policy_epoch
             OR NEW.restriction_watermark < OLD.restriction_watermark
             OR NEW.data_schema_version < OLD.data_schema_version OR NEW.blocked < OLD.blocked
           BEGIN SELECT RAISE(ABORT, 'cabinet binding or floor regression'); END''',
        '''CREATE TRIGGER disclosure_grant_binding BEFORE INSERT ON shelf_portabledisclosuregrant
           WHEN NOT EXISTS (SELECT 1 FROM shelf_profilecabinetstate s, shelf_portableconnection c
             WHERE s.profile_id=NEW.cabinet_id AND c.id=NEW.connection_id
             AND c.installation_id=s.installation_id AND c.removed=0
             AND c.config_revision=NEW.config_revision)
             OR NEW.revision != 1 OR length(NEW.config_digest) != 64
           BEGIN SELECT RAISE(ABORT, 'foreign grant binding'); END''',
        '''CREATE TRIGGER disclosure_grant_update BEFORE UPDATE ON shelf_portabledisclosuregrant
           WHEN NEW.id IS NOT OLD.id OR NEW.cabinet_id IS NOT OLD.cabinet_id
             OR NEW.connection_id IS NOT OLD.connection_id OR NEW.purpose IS NOT OLD.purpose
             OR NEW.revision != OLD.revision+1 OR NEW.state = OLD.state
             OR NEW.issued_at < OLD.issued_at OR length(NEW.config_digest) != 64
             OR NOT EXISTS (SELECT 1 FROM shelf_profilecabinetstate s, shelf_portableconnection c
                WHERE s.profile_id=NEW.cabinet_id AND c.id=NEW.connection_id
                AND c.installation_id=s.installation_id)
           BEGIN SELECT RAISE(ABORT, 'grant revision or scope regression'); END''',
        # The service and these triggers share one DB transaction. A consent
        # change cannot commit without moving the profile-wide invalidation floor.
        '''CREATE TRIGGER disclosure_grant_insert_floor AFTER INSERT ON shelf_portabledisclosuregrant
           BEGIN UPDATE shelf_profilecabinetstate SET policy_epoch=policy_epoch+1,
             restriction_watermark=restriction_watermark+1 WHERE profile_id=NEW.cabinet_id; END''',
        '''CREATE TRIGGER disclosure_grant_update_floor AFTER UPDATE ON shelf_portabledisclosuregrant
           BEGIN UPDATE shelf_profilecabinetstate SET policy_epoch=policy_epoch+1,
             restriction_watermark=restriction_watermark+1 WHERE profile_id=NEW.cabinet_id; END''',
        '''CREATE TRIGGER disclosure_preview_binding BEFORE INSERT ON shelf_portabledisclosurepreview
           WHEN length(NEW.body_digest)!=64 OR NOT EXISTS
             (SELECT 1 FROM shelf_profilecabinetstate s, shelf_portableconnection c
              WHERE s.profile_id=NEW.cabinet_id AND c.id=NEW.connection_id
              AND s.installation_id=c.installation_id AND s.blocked=0)
           BEGIN SELECT RAISE(ABORT, 'foreign preview binding'); END''',
        '''CREATE TRIGGER disclosure_preview_update BEFORE UPDATE ON shelf_portabledisclosurepreview
           WHEN NEW.id IS NOT OLD.id OR NEW.cabinet_id IS NOT OLD.cabinet_id
             OR NEW.connection_id IS NOT OLD.connection_id OR NEW.input_refs IS NOT OLD.input_refs
             OR NEW.bindings IS NOT OLD.bindings OR NEW.serializer_version IS NOT OLD.serializer_version
             OR NEW.body_digest IS NOT OLD.body_digest OR NEW.omitted_groups IS NOT OLD.omitted_groups
             OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
             OR OLD.state!='preview' OR NEW.state!='stale'
           BEGIN SELECT RAISE(ABORT, 'immutable disclosure preview'); END''',
    ])
    return statements


TRIGGERS = guard_sql()


class Migration(migrations.Migration):
    dependencies = [('shelf', '0020_portable_validation'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='ProfileCabinetState', fields=[
            ('profile', models.OneToOneField(primary_key=True, serialize=False, on_delete=django.db.models.deletion.PROTECT, to='shelf.profile')),
            ('profile_ref', models.UUIDField(default=uuid.uuid4, unique=True)),
            ('actor_ref', models.UUIDField(default=uuid.uuid4, unique=True)),
            ('subject_ref', models.UUIDField(default=uuid.uuid4, unique=True)),
            ('owner_revision', models.PositiveBigIntegerField()),
            ('active_manifest_ref', models.UUIDField(null=True)),
            ('publication_revision', models.PositiveBigIntegerField(default=0)),
            ('policy_epoch', models.PositiveBigIntegerField(default=0)),
            ('restriction_watermark', models.PositiveBigIntegerField(default=0)),
            ('data_schema_version', models.PositiveIntegerField(default=1)),
            ('blocked', models.BooleanField(default=False)),
            ('installation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.portableinstallation')),
            ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
        ], options={'constraints': [
            models.CheckConstraint(condition=models.Q(owner_revision__gte=1), name='cabinet_owner_revision'),
            models.CheckConstraint(condition=models.Q(active_manifest_ref__isnull=True, publication_revision=0), name='cabinet_publication_dormant'),
            models.CheckConstraint(condition=models.Q(data_schema_version=1), name='cabinet_schema_v1'),
        ]}),
        migrations.CreateModel(name='PortableDisclosureGrant', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)),
            ('purpose', models.CharField(max_length=16)),
            ('config_revision', models.PositiveBigIntegerField()), ('config_digest', models.CharField(max_length=64)),
            ('allowed_data_classes', models.JSONField(default=list)),
            ('revision', models.PositiveBigIntegerField(default=1)),
            ('issued_at', models.DateTimeField()), ('expires_at', models.DateTimeField()),
            ('state', models.CharField(max_length=7, default='active')),
            ('cabinet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.profilecabinetstate')),
            ('connection', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.portableconnection')),
        ], options={'constraints': [
            models.UniqueConstraint(fields=['cabinet', 'connection', 'purpose'], name='disclosure_grant_scope'),
            models.CheckConstraint(condition=models.Q(purpose__in=['inference', 'catalogue_lookup']), name='disclosure_grant_purpose'),
            models.CheckConstraint(condition=models.Q(state__in=['active', 'revoked']), name='disclosure_grant_state'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='disclosure_grant_revision'),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('issued_at')), name='disclosure_grant_expiry'),
        ]}),
        migrations.CreateModel(name='PortableDisclosurePreview', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)),
            ('input_refs', models.JSONField()), ('bindings', models.JSONField()),
            ('serializer_version', models.CharField(max_length=40)), ('body_digest', models.CharField(max_length=64)),
            ('omitted_groups', models.JSONField(default=list)),
            ('created_at', models.DateTimeField()), ('expires_at', models.DateTimeField()),
            ('state', models.CharField(max_length=7, default='preview')),
            ('cabinet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.profilecabinetstate')),
            ('connection', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.portableconnection')),
        ], options={'constraints': [
            models.CheckConstraint(condition=models.Q(state__in=['preview', 'stale']), name='disclosure_preview_state'),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('created_at')), name='disclosure_preview_expiry'),
        ]}),
        migrations.RunSQL(sql=TRIGGERS, reverse_sql=[
            'DROP TRIGGER ' + statement.split('CREATE TRIGGER ', 1)[1].split()[0]
            for statement in reversed(TRIGGERS)]),
    ]
