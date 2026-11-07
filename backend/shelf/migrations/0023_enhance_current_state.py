"""Versioned current writers and one-way content removal, retaining no-retry IDs.

0021/0022 stay historical. This migration replaces only their two conflicting
immutable guards; every other owned definition is reinstalled exactly. SQLite
rebuilds require named guards to be removed BEFORE the table is remade.
"""
from importlib import import_module
from django.db import migrations, models

PRIOR = import_module('shelf.migrations.0021_portable_disclosure_authority').TRIGGERS
BROKER = import_module('shelf.migrations.0022_portable_enhance_state').TRIGGERS


def name(sql):
    return sql.split('CREATE TRIGGER ', 1)[1].split()[0]


FIRST_RUN_REDACTION = "(OLD.content_redacted_at IS NULL AND NEW.content_redacted_at IS NOT NULL AND json(NEW.output)='{}' AND json(NEW.evidence)='{}')"
run = next(sql for sql in BROKER if name(sql) == 'enhance_run_update')
for old, new in [
    ("json_extract(NEW.evidence,'$.expected_publication_revision') IS NOT json_extract(OLD.evidence,'$.expected_publication_revision')",
     "(NOT " + FIRST_RUN_REDACTION + " AND json_extract(NEW.evidence,'$.expected_publication_revision') IS NOT json_extract(OLD.evidence,'$.expected_publication_revision'))"),
    ("json_extract(NEW.output,'$.schema') IS NOT 'cabinet-inference-result/v1'",
     "(NEW.content_redacted_at IS NULL AND json_extract(NEW.output,'$.schema') IS NOT 'cabinet-inference-result/v1')"),
    ("(json(OLD.output)!='{}' AND NEW.output IS NOT OLD.output)",
     "(NOT " + FIRST_RUN_REDACTION + " AND json(OLD.output)!='{}' AND NEW.output IS NOT OLD.output)"),
]:
    assert run.count(old) == 1
    run = run.replace(old, new)

manifest = '''CREATE TRIGGER enhance_manifest_no_update BEFORE UPDATE ON shelf_portablecabinetmanifest
 BEGIN SELECT CASE WHEN cabinet_disclosure_write() IS NOT 1
  OR OLD.content_redacted_at IS NOT NULL OR NEW.content_redacted_at IS NULL
  OR NEW.content_digest IS NOT cabinet_content_digest(OLD.manifest)
  OR json(NEW.manifest)!='{}'
  OR NEW.id IS NOT OLD.id OR NEW.cabinet_id IS NOT OLD.cabinet_id OR NEW.run_id IS NOT OLD.run_id
  OR NEW.publication_revision IS NOT OLD.publication_revision OR NEW.bindings IS NOT OLD.bindings
  OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
 THEN RAISE(ABORT,'immutable manifest except one-way redaction') END; END'''

CONTENT_GUARDS = [
 '''CREATE TRIGGER enhance_run_content_insert BEFORE INSERT ON shelf_synthesisrun
 WHEN NEW.contract_version='enhance/v1' AND (NEW.content_redacted_at IS NOT NULL OR NEW.content_digest!='')
 BEGIN SELECT RAISE(ABORT,'redaction requires existing content'); END''',
 '''CREATE TRIGGER enhance_manifest_content_insert BEFORE INSERT ON shelf_portablecabinetmanifest
 WHEN NEW.content_redacted_at IS NOT NULL OR NEW.content_digest!=''
 OR EXISTS (SELECT 1 FROM shelf_synthesisrun r WHERE r.id=NEW.run_id AND r.content_redacted_at IS NOT NULL)
 BEGIN SELECT RAISE(ABORT,'redaction requires existing content'); END''',
 '''CREATE TRIGGER enhance_run_content_update BEFORE UPDATE ON shelf_synthesisrun
 WHEN OLD.contract_version='enhance/v1'
 BEGIN
  SELECT CASE WHEN OLD.content_redacted_at IS NULL AND NEW.content_redacted_at IS NOT NULL
   AND (cabinet_disclosure_write() IS NOT 1 OR NEW.content_digest IS NOT cabinet_content_digest(OLD.output,OLD.evidence)
    OR json(NEW.output)!='{}' OR json(NEW.evidence)!='{}'
    OR NEW.state IS NOT OLD.state OR NEW.attempts IS NOT OLD.attempts
    OR NEW.transport_request_id IS NOT OLD.transport_request_id OR NEW.portable_stage IS NOT OLD.portable_stage
    OR NEW.cancel_requested IS NOT OLD.cancel_requested)
   THEN RAISE(ABORT,'invalid first redaction') END;
  SELECT CASE WHEN OLD.content_redacted_at IS NULL AND NEW.content_redacted_at IS NULL AND NEW.content_digest!=''
   THEN RAISE(ABORT,'unbound content digest') END;
  SELECT CASE WHEN OLD.content_redacted_at IS NOT NULL AND
   (NEW.content_redacted_at IS NOT OLD.content_redacted_at OR NEW.content_digest IS NOT OLD.content_digest
    OR json(NEW.output)!='{}' OR json(NEW.evidence)!='{}'
    OR NEW.attempts IS NOT OLD.attempts OR NEW.transport_request_id IS NOT OLD.transport_request_id
    OR (NEW.attempts=0 AND NEW.state NOT IN ('queued','failed')))
   THEN RAISE(ABORT,'redacted payload cannot return') END;
 END''',
]

CONTENT_GUARDS.append('''CREATE TRIGGER enhance_publication_content BEFORE UPDATE ON shelf_profilecabinetstate
 WHEN (NEW.publication_revision!=OLD.publication_revision OR NEW.active_manifest_ref IS NOT OLD.active_manifest_ref)
 AND EXISTS (SELECT 1 FROM shelf_portablecabinetmanifest m WHERE m.id=NEW.active_manifest_ref AND m.content_redacted_at IS NOT NULL)
 BEGIN SELECT RAISE(ABORT,'redacted publication cannot return'); END''')

WRITER_GUARDS = []
# v1 remains the accepted legacy/fresh slice. Only an explicit owner binding
# enables v2 current-state writers; normal Enhance requires this guarded version.
for table in ('feedbackevent', 'feedbackprojection', 'feedbacksubject', 'mutationreceipt', 'discoverystate', 'saveditem'):
    actor = table in ('feedbackevent', 'mutationreceipt', 'discoverystate', 'saveditem')
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        ref = 'OLD' if operation == 'DELETE' else 'NEW'
        profiles = 'IN (OLD.profile_id,NEW.profile_id)' if operation == 'UPDATE' else '=' + ref + '.profile_id'
        scoped = f'EXISTS (SELECT 1 FROM shelf_profilecabinetstate s WHERE s.profile_id {profiles} AND s.data_schema_version=2)'
        extra = ''
        if actor and operation == 'INSERT':
            extra += f' OR NOT EXISTS (SELECT 1 FROM shelf_profilecabinetstate s WHERE s.profile_id=NEW.profile_id AND s.actor_id=NEW.actor_id)'
        if operation == 'UPDATE':
            extra += ' OR NEW.profile_id IS NOT OLD.profile_id'
            if actor:
                extra += ' OR NEW.actor_id IS NOT OLD.actor_id'
        if table == 'feedbackevent' and operation == 'INSERT':
            extra += " OR NEW.origin!='direct' OR NEW.original_origin!='direct'"
        if table == 'feedbackevent' and operation == 'DELETE':
            extra += ' OR 1=1'
        WRITER_GUARDS.append(f'''CREATE TRIGGER current_{table}_{operation.lower()} BEFORE {operation} ON shelf_{table}
         WHEN {scoped} BEGIN SELECT CASE WHEN cabinet_disclosure_write() IS NOT 1{extra}
         THEN RAISE(ABORT,'scoped current writer required') END; END''')
        WRITER_GUARDS.append(f'''CREATE TRIGGER current_{table}_{operation.lower()}_floor AFTER {operation} ON shelf_{table}
         WHEN {scoped} BEGIN UPDATE shelf_profilecabinetstate SET policy_epoch=policy_epoch+1,
          restriction_watermark=restriction_watermark+1 WHERE profile_id={ref}.profile_id; END''')
for table in ('intakerun', 'authoreddiscoverystate', 'socialdiscoverystate'):
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        ref = 'OLD' if operation == 'DELETE' else 'NEW'
        profiles = 'IN (OLD.profile_id,NEW.profile_id)' if operation == 'UPDATE' else '=' + ref + '.profile_id'
        WRITER_GUARDS.append(f'''CREATE TRIGGER current_disabled_{table}_{operation.lower()} BEFORE {operation} ON shelf_{table}
         WHEN EXISTS (SELECT 1 FROM shelf_profilecabinetstate s WHERE s.profile_id {profiles} AND s.data_schema_version=2)
         BEGIN SELECT RAISE(ABORT,'unsupported portable history writer'); END''')
WRITER_GUARDS.append('''CREATE TRIGGER current_legacy_synthesis BEFORE INSERT ON shelf_synthesisrun
 WHEN NEW.contract_version!='enhance/v1' AND EXISTS
 (SELECT 1 FROM shelf_profilecabinetstate s WHERE s.profile_id=NEW.profile_id AND s.data_schema_version=2)
 BEGIN SELECT RAISE(ABORT,'legacy synthesis unavailable for current profile'); END''')

WRITER_GUARDS.extend([
    """CREATE TRIGGER current_target_insert BEFORE INSERT ON shelf_feedbacktarget
     WHEN NEW.source_namespace='enhance-current' AND cabinet_disclosure_write() IS NOT 1
     BEGIN SELECT RAISE(ABORT,'trusted published target required'); END""",
    """CREATE TRIGGER current_target_update BEFORE UPDATE ON shelf_feedbacktarget
     WHEN OLD.source_namespace='enhance-current' OR NEW.source_namespace='enhance-current'
     BEGIN SELECT CASE WHEN cabinet_disclosure_write() IS NOT 1 OR NEW.id IS NOT OLD.id
      OR NEW.source_namespace IS NOT OLD.source_namespace OR NEW.source_key IS NOT OLD.source_key
      OR NEW.title IS NOT OLD.title OR NEW.category IS NOT OLD.category
      OR NEW.kind IS NOT OLD.kind OR NEW.parent_id IS NOT OLD.parent_id OR NEW.deleted<OLD.deleted
      THEN RAISE(ABORT,'immutable published target identity') END; END""",
    """CREATE TRIGGER current_target_no_delete BEFORE DELETE ON shelf_feedbacktarget
     WHEN OLD.source_namespace='enhance-current'
     BEGIN SELECT RAISE(ABORT,'authored target tombstone required'); END""",
])

# Explicit effective inventory, not discovery of arbitrary installed migrations.
TRIGGERS = list(PRIOR) + [run if name(sql) == 'enhance_run_update' else
    manifest if name(sql) == 'enhance_manifest_no_update' else sql for sql in BROKER] + CONTENT_GUARDS + WRITER_GUARDS
assert len({name(sql) for sql in TRIGGERS}) == len(TRIGGERS)


def drop_owned(apps, editor):
    if editor.connection.vendor != 'sqlite':
        raise RuntimeError('PORTABLE_SQLITE_REQUIRED')
    normalize = lambda sql: sql.strip().removesuffix(';').rstrip()
    with editor.connection.cursor() as cursor:
        actual = {normalize(row[0]) for row in cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
    if actual != {normalize(sql) for sql in list(PRIOR) + list(BROKER)}:
        raise RuntimeError('PORTABLE_PREDECESSOR_GUARDS_REQUIRED')
    for sql in TRIGGERS:
        editor.execute('DROP TRIGGER IF EXISTS ' + name(sql))


def install_owned(apps, editor):
    from shelf.models import _content_digest
    editor.connection.connection.create_function('cabinet_disclosure_write', 0, lambda: 0)
    editor.connection.connection.create_function('cabinet_content_digest', -1, _content_digest)
    for sql in TRIGGERS:
        try:
            editor.execute(sql)
        except Exception as error:
            # Retain the exact trusted guard name in a failed maintenance run.
            # Propagate through the atomic migration; never commit a partial set
            # or hide a restoration failure with best-effort cleanup in finally.
            raise RuntimeError('PORTABLE_GUARD_INSTALL_FAILED:' + name(sql)) from error


def reverse_install_owned(apps, editor):
    predecessor = import_module('shelf.migrations.0022_portable_enhance_state')
    predecessor.guard_inventory(editor, TRIGGERS)
    predecessor.legacy_reverse_eligible(apps, editor)
    predecessor.drop_inventory(editor, TRIGGERS)


def reverse_drop_owned(apps, editor):
    predecessor = import_module('shelf.migrations.0022_portable_enhance_state')
    predecessor.restore_inventory(editor, list(PRIOR) + list(BROKER))


class Migration(migrations.Migration):
    dependencies = [('shelf', '0022_portable_enhance_state')]
    operations = [migrations.RunPython(drop_owned, reverse_drop_owned)] + [
        migrations.AddField(model_name=model, name=field, field=definition)
        for model in ('synthesisrun', 'portablecabinetmanifest')
        for field, definition in [('content_redacted_at', models.DateTimeField(null=True)),
                                  ('content_digest', models.CharField(max_length=64, default=''))]
    ] + [migrations.RemoveConstraint(model_name='profilecabinetstate', name='cabinet_schema_v1'),
         migrations.AddConstraint(model_name='profilecabinetstate', constraint=models.CheckConstraint(
             condition=models.Q(data_schema_version__in=[1, 2]), name='cabinet_schema_v1')),
         migrations.RunPython(install_owned, reverse_install_owned)]
