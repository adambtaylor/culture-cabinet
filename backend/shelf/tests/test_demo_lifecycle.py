"""Real dedicated first-session lifecycle. Only the socket dispatcher is stubbed."""
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import io
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import connections, connection
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.test import TransactionTestCase,Client

from cabinet import boundary
from shelf.catalogue import CARDS,TARGET_IDS
from shelf.models import (Profile,Grant,RecoveryAnchor,FeedbackTarget,FeedbackEvent,
                          FeedbackProjection,MutationReceipt)
from shelf.management.commands.demo_runserver import Command
from shelf.recovery import (APPLICATION_ID,RecoveryBlocked,validated_fixture_store,
    validated_demo_store,synthetic_demo_lifecycle,installed_coordinator,
    trusted_operator,restrict_authority,consistent_backup,isolated_restore,
    ExclusiveScratchFile,ReadOnlyBackup)


class DemoLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(prefix='demo-lifecycle-'); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); self.base.chmod(0o700)
        self.actor=get_user_model().objects.create(username='fictional-lifecycle')
        self.profile=Profile.objects.create(kind='owner',label='Fictional lifecycle',active=True)
        self.grant=Grant.objects.create(user=self.actor,profile=self.profile,can_view=True,
            can_select=True,can_mutate=True,can_export=True)
        self.anchor,_=RecoveryAnchor.objects.get_or_create(id=1,defaults={'store_id':uuid.uuid4()})
        for identifier,card,key in zip(TARGET_IDS,CARDS,('paper-observatory','mosslight-station','clockwork-lagoon')):
            FeedbackTarget.objects.get_or_create(id=identifier,defaults=dict(kind='item',title=card.title,category=card.category,
                source_namespace='fiction-p1101',source_key=key))

    def fixture(self,name='fixture',*,issue=True):
        root=self.base/name; root.mkdir(mode=0o700); (root/'db').mkdir(mode=0o700)
        path=root/'db/synthetic.sqlite3'
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); os.close(fd)
        raw=sqlite3.connect(path)
        try: connection.connection.backup(raw)
        finally: raw.close()
        store=validated_fixture_store(root=root,expected_store_id=str(self.anchor.store_id)) if issue else None
        return root,path,store

    @contextmanager
    def wrapper(self,path):
        original=connections['default']
        wrapper=DatabaseWrapper({**original.settings_dict,'NAME':str(path)},alias='default')
        connections['default']=wrapper
        try: yield wrapper
        finally:
            wrapper.close()
            connections['default']=original
        self.assertIs(connections['default'],original)

    def run_command(self,store,callback):
        command=Command(validated_store=store,stdout=io.StringIO(),stderr=io.StringIO())
        options=vars(command.create_parser('manage.py','demo_runserver').parse_args(['--synthetic-demo']))
        with patch('django.core.management.commands.runserver.run',side_effect=callback) as dispatch:
            command.handle(**options)
        self.assertEqual(dispatch.call_count,1)

    def assert_closed(self,coordinator):
        self.assertIsNone(installed_coordinator()); self.assertTrue(coordinator.closed)
        with self.assertRaises(sqlite3.ProgrammingError): coordinator.bound_db.execute('SELECT 1')
        for fd in (*coordinator.fds.values(),coordinator.directory_fd,*coordinator.store_fds.values()):
            with self.assertRaises(OSError): os.fstat(fd)

    def test_real_command_requests_close_reopen_finalize_backup_and_marker(self):
        root,path,store=self.fixture(); observed=[]
        with self.wrapper(path) as wrapper:
            def callback(addr,port,handler,**kwargs):
                self.assertEqual((addr,port,kwargs['ipv6'],kwargs['threading']),('127.0.0.1',8000,False,False))
                self.assertTrue(callable(handler)); self.assertIsNone(wrapper.connection)
                current=installed_coordinator(); observed.append(current)
                self.assertIsNot(current.bound_db,wrapper.connection)
                self.assertEqual(current.bound_db.execute('PRAGMA application_id').fetchone()[0],APPLICATION_ID)
                client=Client(enforce_csrf_checks=True,HTTP_HOST='127.0.0.1')
                client.force_login(self.actor)
                detail=f'/profiles/{self.profile.pk}/feedback/{TARGET_IDS[0]}/'
                for route in ('/profiles/','/',detail,detail):
                    page=client.get(route,follow=True); self.assertEqual(page.status_code,200)
                self.assertTemplateUsed(page,'cabinet/feedback.html')
                def post(signal='saved',operation='set',revision=0,context='',corrects='',reverses='',**fields):
                    return client.post(detail+'mutate/',dict(csrfmiddlewaretoken=client.cookies['cc_synthetic_csrf'].value,
                        mutation_id=str(uuid.uuid4()),target_type='item',scope='item',signal=signal,
                        operation=operation,base_revision=str(revision),context_event_id=context,
                        corrects_id=corrects,reverses_id=reverses,**fields))
                self.assertEqual(post(checked='true').status_code,303)
                saved=FeedbackEvent.objects.get(signal='saved')
                self.assertEqual(post(revision=1,context=str(saved.pk),corrects=str(saved.pk),checked='false').status_code,303)
                correction=FeedbackEvent.objects.get(signal='saved',revision=2)
                old_request=wrapper.connection
                wrapper.close(); self.assertIsNone(wrapper.connection)
                self.assertEqual(client.get(detail).status_code,200)
                self.assertIsNot(wrapper.connection,old_request)
                self.assertEqual(current.bound_db.execute('SELECT 1').fetchone(),(1,))
                self.assertEqual(post(operation='reverse',revision=2,context=str(correction.pk),reverses=str(correction.pk)).status_code,303)
                self.assertEqual(FeedbackProjection.objects.get(signal='saved').value,{'value':True})
                self.assertEqual(post(signal='visibility',choice='hidden').status_code,303)
                visibility=FeedbackEvent.objects.get(signal='visibility')
                self.assertEqual(post(signal='visibility',revision=1,context=str(visibility.pk),choice='visible').status_code,303)
                for fmt in ('json','csv','md'):
                    response=client.post(f'/profiles/{self.profile.pk}/feedback-export/',
                        dict(csrfmiddlewaretoken=client.cookies['cc_synthetic_csrf'].value,format=fmt))
                    self.assertEqual(response.status_code,200)
                    self.assertIn('culture-cabinet-fiction-v1.'+fmt,response['Content-Disposition'])
                # Both action grants are reread on retries despite a live lifecycle.
                from shelf.feedback import mutate,MutationCommand
                command=MutationCommand(str(uuid.uuid4()),TARGET_IDS[0],'item','opened','item','set',0,None,{'value':True})
                mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
                Grant.objects.filter(pk=self.grant.pk).update(can_mutate=False)
                from django.http import Http404
                with self.assertRaises(Http404): mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
                Grant.objects.filter(pk=self.grant.pk).update(can_mutate=True)
                # Finalize uses the independent raw connection after request close.
                wrapper.close(); connection.ensure_connection()
                cap=trusted_operator(current,self.actor)
                restrict_authority(actor=cap,recovery=current,command=dict(mutation_id=str(uuid.uuid4()),base_sequence=0,
                    restrictions=[dict(kind='revoke',profile_id=self.profile.pk,target_id=None,signal=None,
                        scope=None,subject_id=str(self.actor.pk),restriction_code='deny_export')]))
                self.assertEqual((current.sequence,current.pending),(1,None))
                (root/'backups').mkdir(mode=0o700)
                source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True); source.execute('PRAGMA query_only=ON')
                try: manifest=consistent_backup(source=source,destination=ExclusiveScratchFile(root/'backups/copy.sqlite3'),recovery=current)
                finally: source.close()
                self.assertEqual(manifest.version,1)
                backup=sqlite3.connect(root/'backups/copy.sqlite3')
                try: self.assertEqual(backup.execute('PRAGMA application_id').fetchone()[0],APPLICATION_ID)
                finally: backup.close()
                sandbox=root/'isolated'; sandbox.mkdir(mode=0o700)
                report=isolated_restore(backup=ReadOnlyBackup(root/'backups/copy.sqlite3',manifest),sandbox=sandbox,recovery=current)
                self.assertEqual(report.state,'ISOLATED_READY',report)
                restored=sqlite3.connect(report.isolated_database)
                try:
                    self.assertEqual(restored.execute('PRAGMA application_id').fetchone()[0],APPLICATION_ID)
                    self.assertEqual(restored.execute('SELECT count(*) FROM auth_user WHERE is_active').fetchone()[0],0)
                finally: restored.close()
            self.run_command(store,callback)
        self.assert_closed(observed[0])
        restored_root,restored_path,_=self.fixture('restored-copy',issue=False)
        shutil.copyfile(root/'backups/copy.sqlite3',restored_path)
        with self.assertRaises(RecoveryBlocked):
            validated_fixture_store(root=restored_root,expected_store_id=str(self.anchor.store_id))
        self.assertFalse((restored_root/'journal').exists())
        self.assertTrue((root/'journal/journal').exists())
        with self.assertRaises(RecoveryBlocked):
            with synthetic_demo_lifecycle(validated_store=store): pass

    def test_command_exception_systemexit_keyboardinterrupt_cleanup_and_restart(self):
        for index,error in enumerate((RuntimeError('fictional failure'),SystemExit(7),KeyboardInterrupt())):
            root,path,store=self.fixture(str(index)); observed=[]
            def callback(*args,**kwargs):
                observed.append(installed_coordinator()); raise error
            with self.wrapper(path),self.assertRaises((RuntimeError,SystemExit)):
                self.run_command(store,callback)
            self.assert_closed(observed[0]); self.assertTrue((root/'journal').is_dir())
            shutil.rmtree(root/'journal')  # adversary removes evidence, never product cleanup
            with self.assertRaises(RecoveryBlocked): validated_fixture_store(root=root,expected_store_id=str(self.anchor.store_id))

    def test_lazy_discovery_handle_validation_and_production_factory_refusal(self):
        with patch('shelf.recovery.validated_demo_store',side_effect=AssertionError('eager factory')):
            command=Command(); command.create_parser('manage.py','demo_runserver')
            with self.assertRaises(CommandError): command.handle(synthetic_demo=False)
            for options in ({'use_reloader':True,'use_threading':False},
                            {'use_reloader':False,'use_threading':True},
                            {'use_reloader':False,'use_threading':False}):
                with self.assertRaises(CommandError): command.inner_run(**options)
        with self.assertRaises(RecoveryBlocked): validated_demo_store()
        self.assertNotEqual(boundary.ROOT,boundary.DEPLOYMENT_ROOT)
        with self.assertRaises(boundary.BoundaryError): boundary.validate_paths(demo=True)
        self.assertFalse((boundary.ROOT/'.demo-recovery').exists())

    def test_sealed_single_use_nested_and_empty_session_marker(self):
        root,path,store=self.fixture()
        with self.assertRaises(RecoveryBlocked):
            with synthetic_demo_lifecycle(validated_store=replace(store)): pass
        with synthetic_demo_lifecycle(validated_store=store) as current:
            second_root,second_path,second=self.fixture('nested')
            with self.assertRaises(RecoveryBlocked):
                with synthetic_demo_lifecycle(validated_store=second): pass
            self.assertEqual(current.bound_db.execute('SELECT count(*) FROM shelf_mutationreceipt').fetchone()[0],0)
        self.assert_closed(current)
        with self.assertRaises(RecoveryBlocked):
            with synthetic_demo_lifecycle(validated_store=store): pass
        shutil.rmtree(root/'journal')
        with self.assertRaises(RecoveryBlocked): validated_fixture_store(root=root,expected_store_id=str(self.anchor.store_id))
        with synthetic_demo_lifecycle(validated_store=second): pass  # nested refusal consumed no descriptor

    def test_startup_used_unknown_schema_target_marker_existing_journal_and_identity_negatives(self):
        for index,attack in enumerate(('marker','unknown_schema','target','subject','event','projection','receipt','restriction',
                                        'existing_journal','symlink','hardlink','mode','directory_mode','replaced_db','wrong_store')):
            root,path,store=self.fixture(str(index))
            raw=sqlite3.connect(path)
            try:
                if attack=='marker': raw.execute('PRAGMA application_id=77')
                elif attack=='unknown_schema': raw.execute('CREATE TABLE unapproved (payload TEXT)')
                elif attack=='target': raw.execute("UPDATE shelf_feedbacktarget SET title='Unapproved fiction metadata'")
                elif attack=='subject': raw.execute('INSERT INTO shelf_feedbacksubject (profile_id,subject_id) VALUES (?,?)',(self.profile.pk,uuid.uuid4().hex))
                elif attack in ('event','projection','receipt','restriction'):
                    target=uuid.UUID(TARGET_IDS[0]).hex
                    if attack=='event':
                        raw.execute('INSERT INTO shelf_feedbacksubject (profile_id,subject_id) VALUES (?,?)',
                            (self.profile.pk,uuid.uuid4().hex))
                        event=uuid.uuid4().hex
                        raw.execute("INSERT INTO shelf_feedbackevent (id,profile_id,target_id,signal,scope,revision,operation,actor_id,authority_grant_id,recorded_at,origin,original_origin,body,erased) VALUES (?,?,?,'saved','item',1,'set',?,?,'2026-01-01 00:00:00','direct','direct','{\"value\":true}',0)",
                            (event,self.profile.pk,target,self.actor.pk,self.grant.pk))
                        raw.execute("INSERT INTO shelf_feedbackprojection (profile_id,target_id,signal,scope,revision,effective_event_id,value,erased) VALUES (?,?,'saved','item',1,?,'{\"value\":true}',0)",
                            (self.profile.pk,target,event))
                    elif attack=='projection':
                        raw.execute("INSERT INTO shelf_feedbackprojection (profile_id,target_id,signal,scope,revision,value,erased) VALUES (?,?,'saved','item',0,NULL,0)",
                            (self.profile.pk,target))
                    elif attack=='receipt':
                        raw.execute("INSERT INTO shelf_mutationreceipt (actor_id,mutation_id,profile_id,operation_family,payload_sha256,state,created_at) VALUES (?,?,?,'import.legacy',?,'accepted','2026-01-01 00:00:00')",
                            (self.actor.pk,uuid.uuid4().hex,self.profile.pk,'0'*64))
                    else:
                        raw.execute("INSERT INTO shelf_recoveryrestriction (sequence,kind,profile_id,subject_id,restriction_code,operation_uuid,local_commit_uuid,external_head_id) VALUES (1,'revoke',?,?,'deny_export',?,?,?)",
                            (self.profile.pk,str(self.actor.pk),uuid.uuid4().hex,uuid.uuid4().hex,uuid.uuid4().hex))
                elif attack=='wrong_store': raw.execute('UPDATE shelf_recoveryanchor SET store_id=?',(uuid.uuid4().hex,))
                raw.commit()
            finally: raw.close()
            if attack=='existing_journal': (root/'journal').mkdir(mode=0o700)
            elif attack=='symlink':
                moved=path.with_name('moved.sqlite3'); path.rename(moved); path.symlink_to(moved)
            elif attack=='hardlink': os.link(path,path.with_name('alias.sqlite3'))
            elif attack=='mode': path.chmod(0o644)
            elif attack=='directory_mode': path.parent.chmod(0o755)
            elif attack=='replaced_db':
                path.rename(path.with_name('old.sqlite3')); shutil.copyfile(path.with_name('old.sqlite3'),path); path.chmod(0o600)
            with self.assertRaises(RecoveryBlocked):
                with synthetic_demo_lifecycle(validated_store=store): self.fail('startup admitted invalid store')
            self.assertIsNone(installed_coordinator())
            if attack!='existing_journal': self.assertFalse((root/'journal').exists())

    def test_partial_initialization_fsync_failures_close_all_handles_and_leave_evidence(self):
        real_fsync=os.fsync
        for fail_at in range(1,7):
            before=set(os.listdir('/proc/self/fd'))
            root,path,store=self.fixture(str(fail_at)); count=[]
            def fsync(fd):
                count.append(fd)
                if len(count)==fail_at: raise OSError('fictional durability failure')
                read=sqlite3.connect(path)
                try: self.assertEqual(read.execute('PRAGMA application_id').fetchone()[0],0 if len(count)<=4 else APPLICATION_ID)
                finally: read.close()
                return real_fsync(fd)
            with patch('shelf.recovery.os.fsync',side_effect=fsync),self.assertRaises(RecoveryBlocked):
                with synthetic_demo_lifecycle(validated_store=store): self.fail('uncertain startup served')
            self.assertEqual(set(os.listdir('/proc/self/fd')),before)
            self.assertIsNone(installed_coordinator()); self.assertTrue((root/'journal').exists())
            with self.assertRaises(RecoveryBlocked):
                with synthetic_demo_lifecycle(validated_store=store): pass

    def test_live_control_database_directory_journal_and_lock_loss_fail_closed(self):
        for index,attack in enumerate(('control','db','db_mode','directory','journal','replace_journal','truncate','lock','second_owner','journal_mode','journal_hardlink','journal_symlink','directory_mode','db_missing')):
            root,path,store=self.fixture(str(index))
            with synthetic_demo_lifecycle(validated_store=store) as current:
                if attack=='second_owner':
                    other=os.open(root/'journal/lock',os.O_RDWR)
                    try:
                        with self.assertRaises(BlockingIOError): fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    finally: os.close(other)
                    with self.assertRaises(RecoveryBlocked):
                        with synthetic_demo_lifecycle(validated_store=store): pass
                    continue
                if attack=='control': current.bound_db.close()
                elif attack=='db':
                    # Keep old inode linked until replacement so inode reuse cannot mask attack.
                    path.rename(path.with_name('old.sqlite3')); shutil.copyfile(path.with_name('old.sqlite3'),path); path.chmod(0o600)
                elif attack=='db_mode': path.chmod(0o644)
                elif attack=='directory': (root/'journal').rename(root/'old-journal'); (root/'journal').mkdir(mode=0o700)
                elif attack=='journal': (root/'journal/journal').unlink()
                elif attack=='replace_journal':
                    journal=root/'journal/journal'; journal.rename(root/'journal/old'); journal.write_bytes(b''); journal.chmod(0o600)
                elif attack=='truncate':
                    current._append('probe',str(uuid.uuid4()),{}); (root/'journal/journal').write_bytes(b'')
                elif attack=='lock': fcntl.flock(current.fds['lock'],fcntl.LOCK_UN)
                elif attack=='journal_mode': (root/'journal/journal').chmod(0o644)
                elif attack=='journal_hardlink': os.link(root/'journal/journal',root/'journal/alias')
                elif attack=='journal_symlink':
                    journal=root/'journal/journal'; journal.rename(root/'journal/old'); journal.symlink_to(root/'journal/old')
                elif attack=='directory_mode': (root/'journal').chmod(0o755)
                elif attack=='db_missing':
                    path.unlink()
                    with self.wrapper(path):
                        from shelf.recovery import access_barrier
                        with self.assertRaises(RecoveryBlocked):
                            with access_barrier(): self.fail('missing DB admitted')
                    self.assertFalse(path.exists())

                with self.assertRaises(RecoveryBlocked): current.verify_current()
                self.assertTrue(current.blocked)
            self.assert_closed(current)

    def test_factory_owner_private_root_and_nonmemory_seam_and_lock_unwind(self):
        root,path,_=self.fixture(issue=False)
        with patch('shelf.recovery.os.getuid',return_value=-1),self.assertRaises(RecoveryBlocked):
            validated_fixture_store(root=root,expected_store_id=str(self.anchor.store_id))
        root.chmod(0o755)
        with self.assertRaises(RecoveryBlocked): validated_fixture_store(root=root,expected_store_id=str(self.anchor.store_id))
        root.chmod(0o700)
        with self.wrapper(path),self.assertRaises(RecoveryBlocked):
            validated_fixture_store(root=root,expected_store_id=str(self.anchor.store_id))
        store=validated_fixture_store(root=root,expected_store_id=str(self.anchor.store_id))
        before={fd for fd in os.listdir('/proc/self/fd') if not self._fd_points_inside(fd,root)}
        with patch('shelf.recovery.fcntl.flock',side_effect=BlockingIOError('fictional contention')):
            with self.assertRaises(RecoveryBlocked):
                with synthetic_demo_lifecycle(validated_store=store): self.fail('lock failure served')
        self.assertIsNone(installed_coordinator()); self.assertTrue((root/'journal/lock').exists())
        self.assertFalse(any(self._fd_points_inside(fd,root) for fd in os.listdir('/proc/self/fd')))
        raw=sqlite3.connect(path)
        try: self.assertEqual(raw.execute('PRAGMA application_id').fetchone()[0],0)
        finally: raw.close()

    def _fd_points_inside(self,fd,root):
        try: return os.readlink('/proc/self/fd/'+fd).startswith(str(root))
        except OSError: return False

    def test_unused_factory_descriptor_releases_pinned_identities(self):
        import gc
        root,path,store=self.fixture()
        self.assertTrue(any(self._fd_points_inside(fd,root) for fd in os.listdir('/proc/self/fd')))
        del store; gc.collect()
        self.assertFalse(any(self._fd_points_inside(fd,root) for fd in os.listdir('/proc/self/fd')))


    def test_exact_authority_trigger_schema_remains_compatible(self):
        from importlib import import_module
        from shelf import recovery
        migration = import_module('shelf.migrations.0021_portable_disclosure_authority')
        enhance = import_module('shelf.migrations.0022_portable_enhance_state')
        authority_definitions = {statement.strip().removesuffix(';').rstrip() for statement in migration.TRIGGERS}
        broker_definitions = {statement.strip().removesuffix(';').rstrip() for statement in enhance.TRIGGERS}
        self.assertEqual(len(authority_definitions), 17)
        self.assertEqual(len(broker_definitions), 7)
        names = [statement.split('CREATE TRIGGER ', 1)[1].split()[0]
                 for statement in list(migration.TRIGGERS) + list(enhance.TRIGGERS)]
        self.assertEqual(len(set(names)), 24)
        # The named migration adds seven mandatory guards. Exact definitions,
        # including negative schema checks, remain the contract; 24 alone is not.
        expected = authority_definitions | broker_definitions
        self.assertEqual(len(expected), 24)
        current = import_module('shelf.migrations.0023_enhance_current_state')
        self.assertEqual(len(current.CONTENT_GUARDS), 4)
        self.assertEqual(len(current.WRITER_GUARDS), 49)
        expected = {statement.strip().removesuffix(';').rstrip() for statement in current.TRIGGERS}
        self.assertEqual(len(expected), 77)
        self.assertEqual(len({statement.split('CREATE TRIGGER ', 1)[1].split()[0] for statement in current.TRIGGERS}), 77)
        original = recovery._empty_synthetic_store
        with patch.object(recovery, '_empty_synthetic_store', wraps=original) as checks:
            root, path, store = self.fixture('exact-authority-schema')
            raw = sqlite3.connect(path)
            try:
                definitions = {row[0].strip().removesuffix(';').rstrip() for row in
                    raw.execute("SELECT sql FROM sqlite_master WHERE type='trigger'")}
                self.assertEqual(definitions, expected)
                self.assertFalse(raw.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone())
                for table in ('profilecabinetstate', 'portabledisclosuregrant', 'portabledisclosurepreview',
                              'synthesisrun', 'portablecabinetmanifest'):
                    self.assertEqual(raw.execute(f'SELECT count(*) FROM shelf_{table}').fetchone()[0], 0)
                self.assertEqual(raw.execute('SELECT count(*) FROM shelf_recoveryanchor').fetchone()[0], 1)
            finally:
                raw.close()
            with synthetic_demo_lifecycle(validated_store=store) as current:
                self.assertFalse(current.blocked)
                self.assertIs(installed_coordinator(), current)
            # Factory plus both lifecycle-entry checks must still run.
            self.assertEqual(checks.call_count, 3)
            self.assert_closed(current)

    def test_changed_or_populated_authority_schema_is_refused(self):
        from importlib import import_module
        from shelf import recovery
        import re
        migration = import_module('shelf.migrations.0021_portable_disclosure_authority')
        definitions = []
        for sql in migration.TRIGGERS:
            match = re.match(r'CREATE TRIGGER (\w+)\s+(?:BEFORE|AFTER) (?:INSERT|UPDATE|DELETE) ON (\w+)\b', sql)
            self.assertIsNotNone(match)
            definitions.append((match[1], match[2], sql))
        attacks = [('extra', None), ('unknown_table', None)]
        attacks += [(kind, item) for kind in ('missing', 'altered', 'wrong_table') for item in definitions]
        attacks += [('view', table) for table in ('profilecabinetstate', 'portabledisclosuregrant', 'portabledisclosurepreview')]
        attacks += [('populated', table) for table in ('profilecabinetstate', 'portabledisclosuregrant', 'portabledisclosurepreview')]

        def populate(raw, table):
            # Real valid parent rows keep every guard installed. Leaf-table cases
            # exercise their own empty predicate before the required cabinet row.
            raw.create_function('cabinet_disclosure_write', 0, lambda: 1)
            installation, config = uuid.uuid4().hex, uuid.uuid4().hex
            raw.execute('INSERT INTO shelf_portableinstallation (id,data_schema_version,root_identity,owner_id,owner_revision,process_generation,failed_auth) VALUES (?,1,?,?,1,?,0)',
                (installation, uuid.uuid4().hex * 2, self.actor.pk, uuid.uuid4().hex))
            raw.execute("INSERT INTO shelf_portableconnection (id,installation_id,adapter,model,reasoning,config_revision,credential_generation,validation_generation,removed) VALUES (?,?,'synthetic','synthetic','none',1,0,0,0)", (config, installation))
            raw.execute('INSERT INTO shelf_profilecabinetstate (profile_id,installation_id,actor_id,profile_ref,actor_ref,subject_ref,owner_revision,active_manifest_ref,publication_revision,policy_epoch,restriction_watermark,data_schema_version,blocked) VALUES (?,?,?,?,?,?,1,NULL,0,0,0,1,0)',
                (self.profile.pk, installation, self.actor.pk, uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex))
            if table == 'portabledisclosuregrant':
                raw.execute("INSERT INTO shelf_portabledisclosuregrant (id,cabinet_id,connection_id,purpose,config_revision,config_digest,allowed_data_classes,revision,issued_at,expires_at,state) VALUES (?,?,?,'inference',1,?,'[\"canonical_seed\"]',1,'2026-01-01 00:00:00','2026-01-01 00:10:00','active')",
                    (uuid.uuid4().hex, self.profile.pk, config, '0' * 64))
            elif table == 'portabledisclosurepreview':
                raw.execute("INSERT INTO shelf_portabledisclosurepreview (id,cabinet_id,connection_id,input_refs,bindings,serializer_version,body_digest,omitted_groups,created_at,expires_at,state) VALUES (?,?,?,'{}','{}','synthetic',?,'[]','2026-01-01 00:00:00','2026-01-01 00:10:00','preview')",
                    (uuid.uuid4().hex, self.profile.pk, config, '0' * 64))
            self.assertEqual(raw.execute(f'SELECT count(*) FROM shelf_{table}').fetchone()[0], 1)

        for index, (kind, item) in enumerate(attacks):
            with self.subTest(kind=kind, item=item):
                root, path, store = self.fixture('schema-attack-' + str(index))
                raw = sqlite3.connect(path)
                try:
                    if kind == 'extra':
                        raw.execute('CREATE TRIGGER unexpected_guard BEFORE INSERT ON shelf_profile BEGIN SELECT 1; END')
                    elif kind == 'unknown_table':
                        raw.execute('CREATE TABLE unexpected_authority (id INTEGER)')
                    elif kind == 'view':
                        raw.execute(f'CREATE VIEW unexpected_view AS SELECT * FROM shelf_{item}')
                    elif kind == 'populated':
                        populate(raw, item)
                    else:
                        name, table, trusted_sql = item
                        raw.execute('DROP TRIGGER ' + name)
                        if kind == 'altered':
                            # Adversarial disposable schema; restore from trusted
                            # source text only, never execute stored sqlite_master SQL.
                            changed = trusted_sql.replace('BEGIN ', 'BEGIN SELECT 1; ', 1)
                            self.assertNotEqual(changed, trusted_sql)
                            raw.execute(changed)
                        elif kind == 'wrong_table':
                            raw.execute(trusted_sql.replace(' ON ' + table, ' ON shelf_profile', 1))
                    raw.commit()
                finally:
                    raw.close()
                with self.assertRaises(RecoveryBlocked):
                    validated_fixture_store(root=root, expected_store_id=str(self.anchor.store_id))
                with self.assertRaises(RecoveryBlocked):
                    with synthetic_demo_lifecycle(validated_store=store):
                        self.fail('changed schema entered lifecycle')
                self.assertIsNone(installed_coordinator())

        # Mutate between the two entry checks, after the first succeeds. This
        # verifies the second check rather than merely counting its invocation.
        root, path, store = self.fixture('between-entry-checks')
        original = recovery._empty_synthetic_store
        calls = []
        def change_after_first(raw):
            original(raw)
            calls.append(True)
            if len(calls) == 1:
                raw.execute('CREATE VIEW intervening_view AS SELECT 1 AS value')
        with patch.object(recovery, '_empty_synthetic_store', side_effect=change_after_first):
            with self.assertRaises(RecoveryBlocked):
                with synthetic_demo_lifecycle(validated_store=store):
                    self.fail('second entry check missed schema change')
        self.assertEqual(len(calls), 1)
        self.assertIsNone(installed_coordinator())


    def test_broker_guards_are_exact_after_fresh_upgrade_and_schema_attacks_are_refused(self):
        from importlib import import_module
        import re
        from django.db.migrations.executor import MigrationExecutor
        authority = import_module('shelf.migrations.0021_portable_disclosure_authority')
        broker = import_module('shelf.migrations.0022_portable_enhance_state')
        normalize = lambda sql: sql.strip().removesuffix(';').rstrip()
        expected = {normalize(sql) for sql in list(authority.TRIGGERS) + list(broker.TRIGGERS)}
        self.assertEqual(len(expected), 24)
        # Build through the supported predecessor, then upgrade on a separate
        # disposable connection. This catches SQLite rebuilds dropping old guards.
        root, _, _ = self.fixture('fresh-upgrade-guards', issue=False)
        path = root / 'db' / 'fresh-upgrade.sqlite3'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.wrapper(path) as wrapper:
            MigrationExecutor(wrapper).migrate([('shelf', '0021_portable_disclosure_authority')])
            with wrapper.cursor() as cursor:
                definitions = {normalize(row[0]) for row in cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
            self.assertEqual(definitions, {normalize(sql) for sql in authority.TRIGGERS})
            MigrationExecutor(wrapper).migrate([('shelf', '0022_portable_enhance_state')])
            with wrapper.cursor() as cursor:
                definitions = {normalize(row[0]) for row in cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
            self.assertEqual(definitions, expected)

        records = []
        for sql in broker.TRIGGERS:
            match = re.match(r'CREATE TRIGGER (\w+)\s+(?:BEFORE|AFTER) (?:INSERT|UPDATE|DELETE) ON (\w+)\b', sql)
            self.assertIsNotNone(match)
            records.append((match[1], match[2], sql))
        attacks = [(kind, row) for kind in ('missing', 'altered', 'wrong_table') for row in records]
        attacks += [('view', None), ('unknown_table', None), ('unknown_guard', None)]
        for index, (kind, record) in enumerate(attacks):
            with self.subTest(kind=kind, guard=record[0] if record else None):
                root, path, store = self.fixture('broker-schema-' + str(index))
                raw = sqlite3.connect(path)
                try:
                    if kind == 'view':
                        raw.execute('CREATE VIEW unexpected_broker_view AS SELECT 1')
                    elif kind == 'unknown_table':
                        raw.execute('CREATE TABLE unexpected_broker_table (id INTEGER)')
                    elif kind == 'unknown_guard':
                        raw.execute('CREATE TRIGGER unexpected_broker_guard BEFORE INSERT ON shelf_profile BEGIN SELECT 1; END')
                    else:
                        name, table, sql = record
                        raw.execute('DROP TRIGGER ' + name)
                        if kind == 'altered':
                            changed = sql.replace('BEGIN', 'BEGIN SELECT 1;', 1)
                            self.assertNotEqual(changed, sql)
                            raw.execute(changed)
                        elif kind == 'wrong_table':
                            raw.execute(sql.replace(' ON ' + table, ' ON shelf_profile', 1))
                    raw.commit()
                finally:
                    raw.close()
                with self.assertRaises(RecoveryBlocked):
                    validated_fixture_store(root=root, expected_store_id=str(self.anchor.store_id))
                with self.assertRaises(RecoveryBlocked):
                    with synthetic_demo_lifecycle(validated_store=store):
                        self.fail('untrusted broker schema entered lifecycle')
                self.assertIsNone(installed_coordinator())

    def test_populated_broker_and_manifest_refuse_empty_factory_and_lifecycle(self):
        from datetime import datetime, timedelta, timezone
        from django.db import transaction
        from shelf import grant_authority as ga
        from shelf.disclosure import canonical_bytes
        from shelf.enhance_contract import EnhanceBindings, digest
        from shelf.models import (PortableInstallation, PortableConnection, ProfileCabinetState,
                                  SynthesisRun, PortableCabinetManifest)
        for include_manifest in (False, True):
            with self.subTest(manifest=include_manifest):
                root, path, store = self.fixture('populated-broker-' + str(include_manifest))
                # Valid parent rows and all 24 real guards stay installed. Never
                # clear a tombstone or forge an empty-table query to isolate a leaf.
                with self.wrapper(path), transaction.atomic():
                    installation = PortableInstallation.objects.create(id=uuid.uuid4(),
                        root_identity=uuid.uuid4().hex * 2, owner=self.actor, owner_revision=1,
                        process_generation=uuid.uuid4())
                    config = PortableConnection.objects.create(id=uuid.uuid4(), installation=installation,
                        adapter='synthetic', model='synthetic', reasoning='none')
                    with ga._write_window():
                        cabinet = ProfileCabinetState.objects.create(profile_id=self.profile.pk,
                            installation=installation, actor=self.actor, owner_revision=1)
                        binding = EnhanceBindings(str(installation.pk), str(cabinet.actor_ref),
                            str(cabinet.profile_ref), 'a' * 64, 0, '0', '1', 1, 'b' * 64, 'c' * 64)
                        refresh = uuid.uuid4()
                        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
                        run = SynthesisRun.objects.create(actor=self.actor, profile_id=self.profile.pk,
                            contract_version='enhance/v1', mode='portable', installation=installation,
                            portable_connection=config, client_key=uuid.uuid4(), refresh_nonce=refresh,
                            input_snapshot={}, input_digest=digest(canonical_bytes({'schema': 'enhance/v1',
                                'bindings': vars(binding), 'refresh_nonce': str(refresh)})),
                            portable_bindings={'bindings': vars(binding), 'categories': ['books'],
                                               'source_revision': 'isolated-empty-store-probe'},
                            portable_stage='authorized', portable_expires_at=now + timedelta(minutes=5),
                            evidence={'code': 'OK', 'outcomes': [], 'phase': 'catalogue',
                                      'expected_publication_revision': 0})
                        if include_manifest:
                            # Use the guarded queued->reserved->completed transitions;
                            # no model or source is called, and no human taste is added.
                            SynthesisRun.objects.filter(pk=run.pk).update(revision=1, state='running',
                                attempts=1, transport_request_id=uuid.uuid4(), portable_stage='reserved',
                                collection_digest='d' * 64, body_digest='e' * 64, sealed_run_digest='f' * 64)
                            SynthesisRun.objects.filter(pk=run.pk).update(revision=2, state='completed',
                                portable_stage='validated', output={'schema': 'cabinet-inference-result/v1',
                                    'associations': None, 'candidate_assessments': []})
                            PortableCabinetManifest.objects.create(cabinet=cabinet, run=run,
                                publication_revision=1, bindings=run.portable_bindings,
                                manifest={'schema': 'cabinet-manifest/v1', 'categories': [], 'for_you': []},
                                created_at=now, expires_at=now + timedelta(minutes=5))
                        self.assertEqual(SynthesisRun.objects.count(), 1)
                        self.assertEqual(PortableCabinetManifest.objects.count(), int(include_manifest))
                with self.assertRaises(RecoveryBlocked):
                    validated_fixture_store(root=root, expected_store_id=str(self.anchor.store_id))
                with self.assertRaises(RecoveryBlocked):
                    with synthetic_demo_lifecycle(validated_store=store):
                        self.fail('populated broker store entered empty lifecycle')
                self.assertIsNone(installed_coordinator())


    def test_0022_ordering_failure_rolls_back_predecessor_data_schema_and_guards(self):
        from importlib import import_module
        from django.db.migrations.executor import MigrationExecutor
        from django.db.migrations.recorder import MigrationRecorder
        migration = import_module('shelf.migrations.0022_portable_enhance_state')
        prior = import_module('shelf.migrations.0021_portable_disclosure_authority')
        root, _, _ = self.fixture('0022-rollback', issue=False)
        path = root / 'db' / 'upgrade.sqlite3'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600); os.close(fd)
        normalize = lambda sql: sql.strip().removesuffix(';').rstrip()
        with self.wrapper(path) as wrapper:
            executor = MigrationExecutor(wrapper)
            executor.migrate([('shelf', '0021_portable_disclosure_authority')])
            historical = executor.loader.project_state([('shelf', '0021_portable_disclosure_authority')]).apps
            Profile21 = historical.get_model('shelf', 'Profile')
            original = Profile21.objects.create(kind='owner', label='Synthetic preserved predecessor', active=True)
            with wrapper.cursor() as cursor:
                columns = cursor.execute('PRAGMA table_info(shelf_profilecabinetstate)').fetchall()
            install = next(op for op in migration.Migration.operations
                           if getattr(getattr(op, 'code', None), '__name__', '') == 'install_guards')
            reached = []
            def fail_install(apps, editor):
                with editor.connection.cursor() as cursor:
                    names = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
                self.assertNotIn('disclosure_grant_binding', names)
                reached.append(True)
                raise RuntimeError('injected after removal before install')
            with patch.object(install, 'code', side_effect=fail_install), self.assertRaisesRegex(RuntimeError, '^injected after removal'):
                MigrationExecutor(wrapper).migrate([('shelf', '0022_portable_enhance_state')])
            self.assertEqual(reached, [True])
            self.assertEqual(Profile21.objects.get(pk=original.pk).label, 'Synthetic preserved predecessor')
            self.assertNotIn(('shelf', '0022_portable_enhance_state'), MigrationRecorder(wrapper).applied_migrations())
            with wrapper.cursor() as cursor:
                self.assertEqual(cursor.execute('PRAGMA table_info(shelf_profilecabinetstate)').fetchall(), columns)
                definitions = {normalize(row[0]) for row in cursor.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
            self.assertEqual(definitions, {normalize(sql) for sql in prior.TRIGGERS})
            MigrationExecutor(wrapper).migrate([('shelf', '0022_portable_enhance_state')])
            with wrapper.cursor() as cursor:
                exact = {normalize(row[0]) for row in cursor.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
            self.assertEqual(exact, {normalize(sql) for sql in list(prior.TRIGGERS) + list(migration.TRIGGERS)})
            MigrationExecutor(wrapper).migrate([('shelf', '0022_portable_enhance_state')])
            with wrapper.cursor() as cursor:
                self.assertEqual({normalize(row[0]) for row in cursor.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}, exact)


    def test_0023_guard_install_failure_rolls_back_all_predecessor_data_schema_and_guards(self):
        from importlib import import_module
        from django.db.migrations.executor import MigrationExecutor
        from django.db.migrations.recorder import MigrationRecorder
        current = import_module('shelf.migrations.0023_enhance_current_state')
        prior = import_module('shelf.migrations.0021_portable_disclosure_authority')
        broker = import_module('shelf.migrations.0022_portable_enhance_state')
        root, _, _ = self.fixture('0023-atomic-rollback', issue=False)
        path = root / 'db' / 'upgrade-0023.sqlite3'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600); os.close(fd)
        normalize = lambda sql: sql.strip().removesuffix(';').rstrip()
        with self.wrapper(path) as wrapper:
            executor = MigrationExecutor(wrapper)
            executor.migrate([('shelf', '0022_portable_enhance_state')])
            historical = executor.loader.project_state([('shelf', '0022_portable_enhance_state')]).apps
            Profile22 = historical.get_model('shelf', 'Profile')
            original = Profile22.objects.create(kind='owner', label='Synthetic0023 predecessor preserved', active=True)
            tables = ('shelf_profilecabinetstate', 'shelf_synthesisrun', 'shelf_portablecabinetmanifest')
            with wrapper.cursor() as cursor:
                columns = {table: cursor.execute('PRAGMA table_info(' + table + ')').fetchall() for table in tables}
            install = next(op for op in current.Migration.operations
                           if getattr(getattr(op, 'code', None), '__name__', '') == 'install_owned')
            reached = []
            def interrupted_install(apps, editor):
                with editor.connection.cursor() as cursor:
                    self.assertEqual(cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 0)
                    self.assertIn('content_redacted_at', {row[1] for row in cursor.execute('PRAGMA table_info(shelf_synthesisrun)').fetchall()})
                # A genuinely partial install must roll back with the schema,
                # data and migration record; no restoration finally may commit it.
                editor.execute(current.TRIGGERS[0])
                reached.append(True)
                raise RuntimeError('injected partial0023 guard installation')
            with patch.object(install, 'code', side_effect=interrupted_install), self.assertRaisesRegex(RuntimeError, '^injected partial0023'):
                MigrationExecutor(wrapper).migrate([('shelf', '0023_enhance_current_state')])
            self.assertEqual(reached, [True])
            self.assertEqual(Profile22.objects.get(pk=original.pk).label, 'Synthetic0023 predecessor preserved')
            self.assertNotIn(('shelf', '0023_enhance_current_state'), MigrationRecorder(wrapper).applied_migrations())
            with wrapper.cursor() as cursor:
                self.assertEqual({table: cursor.execute('PRAGMA table_info(' + table + ')').fetchall() for table in tables}, columns)
                definitions = {normalize(row[0]) for row in cursor.execute("SELECT sql FROM sqlite_master WHERE type='trigger'").fetchall()}
            self.assertEqual(definitions, {normalize(sql) for sql in list(prior.TRIGGERS) + list(broker.TRIGGERS)})
            self.assertIn(('shelf', '0022_portable_enhance_state'), MigrationRecorder(wrapper).applied_migrations())
