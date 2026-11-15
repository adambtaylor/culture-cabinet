"""Nonsecret coordination rows, guarded by the existing native writer window."""
from importlib import import_module
import uuid
from django.db import migrations, models
import django.db.models.deletion

PREVIOUS = import_module('shelf.migrations.0025_obtain_native_origin')
GUARDS = []
for table in ('portablejourneystate', 'portablejourneycommand', 'portablesourcecheckattempt'):
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        GUARDS.append(f'''CREATE TRIGGER journey_{table}_{operation.lower()}
 BEFORE {operation} ON shelf_{table}
 BEGIN SELECT CASE WHEN cabinet_disclosure_write() IS NOT 1
 THEN RAISE(ABORT,'checked native journey writer required') END; END''')
GUARDS.append('''CREATE TRIGGER journey_command_identity
 BEFORE UPDATE ON shelf_portablejourneycommand
 WHEN NEW.cabinet_id IS NOT OLD.cabinet_id OR NEW.command_id IS NOT OLD.command_id
 OR NEW.kind IS NOT OLD.kind OR NEW.descriptor IS NOT OLD.descriptor
 OR NEW.descriptor_digest IS NOT OLD.descriptor_digest OR NEW.proof IS NOT OLD.proof
 OR NEW.created_at IS NOT OLD.created_at
 BEGIN SELECT RAISE(ABORT,'immutable journey command proof'); END''')
TRIGGERS = list(PREVIOUS.TRIGGERS) + GUARDS


def install(apps, editor):
    for sql in GUARDS:
        editor.execute(sql)


def uninstall(apps, editor):
    for model in ('PortableJourneyCommand', 'PortableJourneyState', 'PortableSourceCheckAttempt'):
        if apps.get_model('shelf', model).objects.exists():
            raise RuntimeError('JOURNEY_HISTORY_REVERSAL_REFUSED')
    PREVIOUS.drop(editor, GUARDS)


def effective_inventory(raw):
    applied = raw.execute("SELECT 1 FROM django_migrations WHERE app='shelf' AND name='0028_guided_owner_journey'").fetchone() is not None
    previous = PREVIOUS.effective_inventory(raw)
    tables = ('shelf_portablejourneystate', 'shelf_portablejourneycommand', 'shelf_portablesourcecheckattempt')
    present = [raw.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None for name in tables]
    if (not all(present) if applied else any(present)):
        raise RuntimeError('JOURNEY_SCHEMA_MISMATCH')
    return list(previous) + GUARDS if applied else previous


class Migration(migrations.Migration):
    dependencies = [('shelf', '0027_saved_delete_no_effect')]
    operations = [
        migrations.CreateModel(name='PortableJourneyState', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)),
            ('cabinet', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='shelf.profilecabinetstate')),
            ('revision', models.PositiveBigIntegerField(default=1)), ('step', models.CharField(max_length=16, default='providers')),
            ('completed', models.JSONField(default=list)), ('skipped', models.JSONField(default=list)),
            ('slots', models.JSONField(default=dict)), ('connections', models.JSONField(default=dict)),
            ('run_ref', models.UUIDField(null=True)), ('created_at', models.DateTimeField()), ('updated_at', models.DateTimeField()),
        ]),
        migrations.CreateModel(name='PortableJourneyCommand', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)),
            ('cabinet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.profilecabinetstate')),
            ('command_id', models.UUIDField()), ('kind', models.CharField(max_length=40)),
            ('descriptor', models.JSONField()), ('descriptor_digest', models.CharField(max_length=64)),
            ('state', models.CharField(max_length=16)), ('result', models.JSONField(default=dict)),
            ('proof', models.JSONField(default=dict)), ('created_at', models.DateTimeField()),
        ], options={'constraints': [models.UniqueConstraint(fields=['cabinet', 'command_id'], name='journey_owner_command')]}),
        migrations.CreateModel(name='PortableSourceCheckAttempt', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)),
            ('cabinet', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.profilecabinetstate')),
            ('connection', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shelf.portableconnection')),
            ('command', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='shelf.portablejourneycommand')),
            ('binding', models.JSONField()), ('operation_digest', models.CharField(max_length=64)),
            ('state', models.CharField(max_length=16)), ('outcome', models.JSONField(default=dict)),
            ('created_at', models.DateTimeField()), ('expires_at', models.DateTimeField()),
        ]),
        migrations.RunPython(install, uninstall),
    ]
