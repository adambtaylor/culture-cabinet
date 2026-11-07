"""Concrete same-host witness tests; no production store/promotion or host loss."""
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch
from django.db import connection
from django.db.backends.sqlite3._functions import register
from django.http import Http404
from shelf.feedback import (mutate, feedback_context, EraseCommand, erase_feedback,
    key_token, FeedbackError, Conflict, Busy)
from shelf.models import FeedbackEvent, FeedbackProjection, RecoveryRestriction, MutationReceipt
from shelf.portability import human_export, import_legacy_fixture
from shelf.recovery import (CurrentRecoveryCoordinator, RecoveryBlocked, using_recovery,
    restrict_authority, trusted_operator, ExclusiveScratchFile, ReadOnlyBackup,
    consistent_backup, isolated_restore, semantic_check)
from shelf.tests.synthetic_feedback import FeedbackCase, legacy_fixture, binding_for_fixture


class RecoveryTests(FeedbackCase):
    def restriction(self,code='deny_export',**kwargs):
        value=dict(kind='revoke',profile_id=self.profile.pk,target_id=None,signal=None,scope=None,
            subject_id=str(self.actor.pk),restriction_code=code)
        value.update(kwargs)
        return value

    def erase(self,signal='enjoyment'):
        p=FeedbackProjection.objects.get(profile=self.profile,signal=signal)
        event=FeedbackEvent.objects.get(profile=self.profile,signal=signal,revision=p.revision)
        token=key_token(str(p.target_id),signal,p.scope)
        return EraseCommand(str(uuid.uuid4()),str(p.target_id),p.scope,(signal,),{token:p.revision},{token:str(event.pk)},'ERASE_FICTIONAL_FEEDBACK')

    def backup(self,name='backup.sqlite3'):
        folder=self.case/'backups'; folder.mkdir(mode=0o700,exist_ok=True)
        dbdir=self.case/'db'; dbdir.mkdir(mode=0o700,exist_ok=True)
        live=dbdir/'source.sqlite3'
        disk=sqlite3.connect(live); connection.connection.backup(disk); disk.close(); live.chmod(0o600)
        source=sqlite3.connect(f'file:{live}?mode=ro',uri=True); source.execute('PRAGMA query_only=ON')
        try: manifest=consistent_backup(source=source,destination=ExclusiveScratchFile(folder/name),recovery=self.recovery)
        finally: source.close()
        return ReadOnlyBackup(folder/name,manifest)

    def restore(self,backup,name='restored'):
        path=self.case/name; path.mkdir(mode=0o700)
        return isolated_restore(backup=backup,sandbox=path,recovery=self.recovery)

    def test_F07_pending_prepare_blocks_reads_exports_imports_retries_and_resume(self):
        command=self.command(); mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
        erase=self.erase(); original=self.recovery.apply_pending
        with patch.object(self.recovery,'apply_pending',side_effect=RuntimeError('prepare interruption')):
            with self.assertRaises(RuntimeError): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertIsNotNone(self.recovery.pending)
        self.assertEqual(FeedbackEvent.objects.count(),1)
        with self.assertRaises(Http404): feedback_context(actor=self.actor,profile_id=self.profile.pk,target_id=command.target_id)
        with self.assertRaises(Http404): human_export(actor=self.actor,profile_id=self.profile.pk,format='json')
        with self.assertRaises(Http404): mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
        fixture=legacy_fixture()
        with self.assertRaises(Http404): import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),fixture_bytes=__import__('shelf.feedback',fromlist=['canonical']).canonical(fixture),binding=binding_for_fixture(self.profile,fixture))
        self.recovery.resume_pending(connection.connection)
        self.assertIsNone(self.recovery.pending)
        self.assertTrue(FeedbackProjection.objects.get(signal='enjoyment').erased)
        self.assertEqual(FeedbackEvent.objects.count(),2)
        self.recovery.resume_pending(connection.connection)
        self.assertEqual(FeedbackEvent.objects.count(),2)

    def test_F07_local_commit_no_head_is_pending_then_idempotent_resume(self):
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        with patch.object(self.recovery,'finalize',side_effect=RuntimeError('head interruption')):
            with self.assertRaises(RuntimeError): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=self.erase(),recovery=self.recovery)
        self.assertEqual(RecoveryRestriction.objects.count(),1)
        self.assertEqual(FeedbackEvent.objects.count(),2)
        with self.assertRaises(Http404): human_export(actor=self.actor,profile_id=self.profile.pk,format='json')
        self.recovery.resume_pending(connection.connection)
        self.assertEqual(RecoveryRestriction.objects.count(),1)
        self.assertEqual(FeedbackEvent.objects.count(),2)

    def test_F07_uncertain_prepare_fsync_latches_all_access_blocked(self):
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        with patch('shelf.recovery.os.fsync',side_effect=OSError('synthetic fsync loss')):
            with self.assertRaises(RecoveryBlocked): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=self.erase(),recovery=self.recovery)
        self.assertTrue(self.recovery.blocked)
        with self.assertRaises(RecoveryBlocked): human_export(actor=self.actor,profile_id=self.profile.pk,format='json')
        self.assertEqual(FeedbackEvent.objects.count(),1)
        self.assertEqual(RecoveryRestriction.objects.count(),0)

    def test_F12_old_backup_erase_revoke_source_restrictions_before_isolated_access(self):
        fixture=legacy_fixture()
        from shelf.feedback import canonical
        import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),fixture_bytes=canonical(fixture),binding=binding_for_fixture(self.profile,fixture))
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('constraint',{'text':'independent human statement'}))
        backup=self.backup()
        erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=self.erase(),recovery=self.recovery)
        self.assertFalse(MutationReceipt.objects.exclude(state='retired',payload_sha256=None).exists())
        cap=trusted_operator(self.recovery,self.actor)
        command=dict(mutation_id=str(uuid.uuid4()),base_sequence=self.recovery.sequence,restrictions=[self.restriction(),
            self.restriction('deny_source_content',kind='source_restrict',profile_id=None,subject_id='legacy-fixture-v1')])
        restrict_authority(actor=cap,command=command,recovery=self.recovery)
        report=self.restore(backup)
        self.assertEqual(report.state,'ISOLATED_READY',report)
        self.assertEqual(report.applied_restriction_ids,(1,2,3))
        raw=sqlite3.connect(report.isolated_database)
        try:
            self.assertEqual(raw.execute("SELECT count(*) FROM shelf_feedbackevent WHERE signal='enjoyment' AND body IS NOT NULL").fetchone()[0],0)
            self.assertEqual(raw.execute("SELECT count(*) FROM shelf_feedbackevent WHERE original_origin!='direct' AND (body IS NOT NULL OR source_record_id IS NOT NULL)").fetchone()[0],0)
            self.assertIn('independent human statement',raw.execute("SELECT body FROM shelf_feedbackevent WHERE signal='constraint'").fetchone()[0])
            self.assertEqual(raw.execute('SELECT count(*) FROM django_session').fetchone()[0],0)
            self.assertEqual(raw.execute('SELECT count(*) FROM shelf_grant WHERE can_view OR can_mutate OR can_export OR can_select').fetchone()[0],0)
            self.assertEqual(raw.execute('SELECT count(*) FROM auth_user WHERE is_active').fetchone()[0],0)
        finally: raw.close()

    def test_F13_stale_missing_replaced_truncated_journal_and_lost_witness(self):
        # Each adversary gets its own provisioned empty DB/live witness. A
        # latched failure cannot be repaired by putting old bytes back.
        template=sqlite3.connect(':memory:'); connection.connection.backup(template)
        from shelf.tests.synthetic_feedback import fresh_destination
        try:
            for n,attack in enumerate(('missing','replace','truncate','same_inode_old','wrong_bytes','lost'),400):
                with fresh_destination(self,template,n) as (profile,coordinator):
                    cap=trusted_operator(coordinator,self.actor)
                    r=self.restriction(profile_id=profile.pk)
                    restrict_authority(actor=cap,command=dict(mutation_id=str(uuid.uuid4()),base_sequence=0,restrictions=[r]),recovery=coordinator)
                    journal=coordinator.directory/'journal'
                    if attack=='missing': journal.unlink()
                    elif attack=='replace':
                        data=journal.read_bytes(); journal.unlink(); journal.write_bytes(data); journal.chmod(0o600)
                    elif attack=='truncate':
                        with journal.open('r+b') as f: f.truncate(3)
                    elif attack=='same_inode_old': journal.write_bytes(b'')
                    elif attack=='wrong_bytes':
                        data=bytearray(journal.read_bytes()); data[-1]^=1; journal.write_bytes(data)
                    else: coordinator.close()
                    with self.assertRaises(RecoveryBlocked):
                        with coordinator.barrier(coordinator.store_id): pass
                    with self.assertRaises(RecoveryBlocked):
                        CurrentRecoveryCoordinator(store_id=coordinator.store_id,journal_directory=coordinator.directory)
                    with self.assertRaises(FeedbackError):
                        CurrentRecoveryCoordinator.provision_empty(store_id=coordinator.store_id,journal_directory=coordinator.directory,trusted_empty_db=connection.connection)
        finally: template.close()

    def test_F14_paths_corrupt_projection_unknown_schema_and_unsafe_copy(self):
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        backup=self.backup()
        self.assertEqual(self.restore(ReadOnlyBackup(backup.path,None),'unsafe').block_reasons,('UNSAFE_MAIN_FILE_COPY',))
        nonempty=self.case/'nonempty'; nonempty.mkdir(mode=0o700); (nonempty/'sentinel').write_text('fiction')
        self.assertEqual(isolated_restore(backup=backup,sandbox=nonempty,recovery=self.recovery).state,'BLOCKED')
        link=self.case/'link'; link.symlink_to(self.case/'backups',target_is_directory=True)
        self.assertEqual(isolated_restore(backup=backup,sandbox=link,recovery=self.recovery).state,'BLOCKED')
        altered=self.case/'backups/altered.sqlite3'; shutil.copyfile(backup.path,altered); altered.chmod(0o600)
        raw=sqlite3.connect(altered); raw.execute('UPDATE shelf_feedbackprojection SET revision=99'); raw.commit()
        with self.assertRaises(RecoveryBlocked): semantic_check(raw)
        raw.close()
        self.assertEqual(self.restore(ReadOnlyBackup(altered,backup.manifest),'altered-restore').block_reasons,('BACKUP_HASH_OR_LENGTH',))
        hard=self.case/'backups/hard.sqlite3'; os.link(backup.path,hard)
        self.assertEqual(self.restore(backup,'hardlink-restore').state,'BLOCKED')

    @contextmanager
    def disk_case(self):
        folder=self.case/'diskcase'; folder.mkdir(mode=0o700)
        dbdir=folder/'db'; dbdir.mkdir(mode=0o700)
        backdir=folder/'backups'; backdir.mkdir(mode=0o700)
        path=dbdir/'live.sqlite3'
        raw=sqlite3.connect(path,isolation_level=None,check_same_thread=False)
        register(raw); connection.connection.backup(raw); path.chmod(0o600)
        with patch.object(connection,'connection',raw):
            coordinator=CurrentRecoveryCoordinator.provision_empty(store_id=self.recovery.store_id,
                journal_directory=folder/'journal',trusted_empty_db=raw)
            try:
                with using_recovery(coordinator): yield raw,path,backdir,coordinator
            finally: coordinator.close(); raw.close()

    def test_F11_pinned_online_backup_overlapping_committed_writer_and_incomplete_WAL_copy(self):
        # Build/fix is checked BEFORE switching this scratch-only DB to WAL.
        self.assertGreaterEqual(sqlite3.sqlite_version_info,(3,51,3))
        root=Path(tempfile.gettempdir())
        with self.disk_case() as (writer,path,backdir,coordinator):
            # Only the measured Linux/local-btrfs context is supported here.
            # Unknown mounts must not inherit a previous session's attestation.
            if sys.platform!='linux': self.skipTest('F11 unsupported platform: '+sys.platform)
            try: mountinfo=Path('/proc/self/mountinfo').read_text()
            except OSError as error: self.skipTest('F11 cannot measure scratch mount: '+str(error))
            mounts=[]
            for line in mountinfo.splitlines():
                fields=line.split(); separator=fields.index('-')
                mountpoint=fields[4]
                for escaped,value in ((r'\040',' '),(r'\011','\t'),(r'\012','\n'),(r'\134','\\')):
                    mountpoint=mountpoint.replace(escaped,value)
                if backdir.resolve().is_relative_to(Path(mountpoint)):
                    mounts.append((len(Path(mountpoint).parts),line,fields[separator+1],fields[5]))
            if not mounts: self.skipTest('F11 unsupported scratch mount: no matching mountinfo entry')
            _,mount,filesystem,options=max(enumerate(mounts),key=lambda item:(item[1][0],item[0]))[1]
            if filesystem!='btrfs': self.skipTest('F11 unsupported scratch filesystem: '+filesystem)
            self.assertIn('rw',options.split(','))
            evidence={'python':sys.version,'platform':sys.platform,'linked_sqlite':sqlite3.sqlite_version,
                'source_id':writer.execute('select sqlite_source_id()').fetchone()[0],
                'compile_options':[row[0] for row in writer.execute('PRAGMA compile_options')],
                'fix_gate':sqlite3.sqlite_version_info>=(3,51,3),'fix_gate_minimum':'3.51.3',
                'exact_upstream_hash_claim':False,'scratch':str(backdir.resolve()),
                'filesystem':filesystem,'mount':mount,'evidence_directory':str(root)}
            # Real local fsync and conflicting flock probe, before any WAL use.
            fd=os.open(backdir/'lock-probe',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            other=os.open(backdir/'lock-probe',os.O_RDWR)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError): fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
                os.write(fd,b'fictional locking/fsync probe'); os.fsync(fd)
            finally: os.close(other); os.close(fd)
            self.assertEqual(writer.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            for i in range(48): mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('constraint',{'text':str(i)+'x'*480}))
            writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            # This complete transaction is known to remain only in the WAL.
            wal_statement=mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved'))
            unsafe=backdir/'main-only.sqlite3'; shutil.copyfile(path,unsafe); unsafe.chmod(0o600)
            main=sqlite3.connect(unsafe)
            try:
                self.assertEqual(main.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(main.execute('SELECT count(*) FROM shelf_feedbackevent WHERE id=?',(uuid.UUID(wal_statement.event_id).hex,)).fetchone()[0],0)
            finally: main.close()
            source=sqlite3.connect(f'file:{path}?mode=ro',uri=True); source.execute('PRAGMA query_only=ON')
            committed=[]; callbacks=[]
            def progress(status,remaining,total):
                callbacks.append((status,remaining,total))
                if remaining>0 and not committed:
                    committed.append(mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('opened')))
            try:
                manifest=consistent_backup(source=source,destination=ExclusiveScratchFile(backdir/'online.sqlite3'),recovery=coordinator,progress=progress)
                self.assertEqual(len(committed),1)
                snapshot=sqlite3.connect(backdir/'online.sqlite3')
                try:
                    self.assertEqual(snapshot.execute("SELECT count(*) FROM shelf_feedbackevent WHERE signal='opened'").fetchone()[0],0)
                    self.assertEqual(snapshot.execute('SELECT count(*) FROM shelf_feedbackevent WHERE id=?',(uuid.UUID(wal_statement.event_id).hex,)).fetchone()[0],1)
                    self.assertEqual(snapshot.execute('SELECT count(*) FROM shelf_mutationreceipt').fetchone()[0],49)
                    self.assertEqual(snapshot.execute('SELECT count(*) FROM shelf_feedbackprojection').fetchone()[0],2)
                finally: snapshot.close()
                later=consistent_backup(source=source,destination=ExclusiveScratchFile(backdir/'later.sqlite3'),recovery=coordinator)
                self.assertEqual(later.counts['feedbackevent'],50)
                empty=self.case/'unsafe-wal'; empty.mkdir(mode=0o700)
                report=isolated_restore(backup=ReadOnlyBackup(unsafe,None),sandbox=empty,recovery=coordinator)
                self.assertEqual(report.block_reasons,('UNSAFE_MAIN_FILE_COPY',))
                report=isolated_restore(backup=ReadOnlyBackup(unsafe,manifest),sandbox=empty,recovery=coordinator)
                self.assertEqual(report.block_reasons,('BACKUP_HASH_OR_LENGTH',))
                with self.assertRaises(RuntimeError): consistent_backup(source=source,destination=ExclusiveScratchFile(backdir/'interrupted.sqlite3'),recovery=coordinator,progress=lambda *_:(_ for _ in ()).throw(RuntimeError('interrupt')))
                self.assertFalse((backdir/'interrupted.sqlite3').exists())
                (root/'wal-execution.json').write_text(json.dumps({**evidence,'status':'PASS','linked_sqlite':sqlite3.sqlite_version,
                    'source_id':writer.execute('select sqlite_source_id()').fetchone()[0],
                    'scratch_local_lock_and_fsync':'PASS','rollback_default':'delete','scratch_wal':'wal',
                    'writer_committed_during_progress':True,'snapshot_events':49,'later_events':50,
                    'main_only_integrity_ok_but_missing_transaction':True,'progress_callback_count':len(callbacks),'first_progress':callbacks[0],'last_progress':callbacks[-1],
                    'production_journal_changed':False},indent=2)+'\n')
            finally: source.close()

    def test_F05_concurrent_connections_same_signal_vs_independent_and_bounded_begin_busy(self):
        # Two actual SQLite writers contend in rollback mode. All service writers
        # share one coordinator; the second raw lock is an adversarial outsider.
        with self.disk_case() as (writer,path,_,coordinator):
            blocker=sqlite3.connect(path,isolation_level=None)
            blocker.execute('BEGIN IMMEDIATE')
            command=self.command()
            timeout=writer.execute('PRAGMA busy_timeout').fetchone()[0]
            try:
                with self.assertRaises(Busy): mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
                self.assertFalse(writer.in_transaction)
                self.assertEqual(writer.execute('PRAGMA busy_timeout').fetchone()[0],timeout)
                self.assertEqual(MutationReceipt.objects.count(),0)
            finally: blocker.execute('ROLLBACK'); blocker.close()
            mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
            with self.assertRaises(Conflict): mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(command,mutation_id=str(uuid.uuid4())))
            mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved'))
            self.assertEqual(FeedbackProjection.objects.count(),2)

    def test_F05_two_participating_connections_race_same_and_independent_signals(self):
        with self.disk_case() as (writer,path,_,coordinator):
            def race(commands):
                start=threading.Barrier(3); outcomes=[]; result_lock=threading.Lock()
                def worker(command):
                    raw=sqlite3.connect(path,isolation_level=None,check_same_thread=False)
                    register(raw)
                    local=__import__('django.db',fromlist=['connections']).connections['default']
                    try:
                        with patch.object(local,'connection',raw), using_recovery(coordinator):
                            local.set_autocommit(True)
                            start.wait(timeout=5)
                            try: result=mutate(actor=self.actor,profile_id=self.profile.pk,command=command); outcome=result.status
                            except FeedbackError as error: outcome=error.status
                            with result_lock: outcomes.append(outcome)
                    except BaseException as error:
                        with result_lock: outcomes.append(type(error).__name__)
                    finally: raw.close()
                threads=[threading.Thread(target=worker,args=(command,)) for command in commands]
                for thread in threads: thread.start()
                start.wait(timeout=5)
                for thread in threads: thread.join(timeout=10); self.assertFalse(thread.is_alive())
                return outcomes
            same=self.command()
            outcomes=race([same,replace(same,mutation_id=str(uuid.uuid4()))])
            self.assertCountEqual(outcomes,['accepted',409])
            outcomes=race([self.command('saved'),self.command('constraint',{'text':'independent'})])
            self.assertCountEqual(outcomes,['accepted','accepted'])
            self.assertEqual(FeedbackEvent.objects.count(),3)
            self.assertEqual(FeedbackProjection.objects.count(),3)
            self.assertEqual(MutationReceipt.objects.count(),3)

    def test_F05_commit_busy_rollback_and_successful_identical_retry(self):
        with self.disk_case() as (writer,path,_,coordinator):
            reader=sqlite3.connect(path,isolation_level=None)
            reader.execute('BEGIN'); reader.execute('SELECT count(*) FROM shelf_feedbackevent').fetchone()
            command=self.command()
            timeout=writer.execute('PRAGMA busy_timeout').fetchone()[0]
            try:
                with self.assertRaises(Busy): mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
                self.assertEqual(FeedbackEvent.objects.count(),0)
                self.assertEqual(MutationReceipt.objects.count(),0)
                self.assertFalse(writer.in_transaction)
                self.assertFalse(connection.in_atomic_block)
                self.assertEqual(writer.execute('PRAGMA busy_timeout').fetchone()[0],timeout)
            finally: reader.execute('ROLLBACK'); reader.close()
            result=mutate(actor=self.actor,profile_id=self.profile.pk,command=command)
            self.assertEqual(result.status,'accepted')
            self.assertEqual(MutationReceipt.objects.count(),1)

    def test_F13_wrong_store_local_mirror_mismatch_and_lost_lock(self):
        with self.assertRaises(RecoveryBlocked):
            with self.recovery.barrier(str(uuid.uuid4())): pass
        self.assertFalse(self.recovery.blocked)
        # A valid-looking disk mirror cannot choose a different live history.
        from shelf.models import RecoveryAnchor
        anchor=RecoveryAnchor.objects.get(pk=1); original=anchor.store_id
        RecoveryAnchor.objects.filter(pk=1).update(store_id=uuid.uuid4())
        with self.assertRaises(RecoveryBlocked): feedback_context(actor=self.actor,profile_id=self.profile.pk,target_id=self.command().target_id)
        RecoveryAnchor.objects.filter(pk=1).update(store_id=original)
        os.close(self.recovery.fds['lock'])
        with self.assertRaises(RecoveryBlocked):
            with self.recovery.barrier(self.recovery.store_id): pass

    def test_F07_revoke_precedes_erase_and_authority_idempotency(self):
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        erase=self.erase(); cap=trusted_operator(self.recovery,self.actor)
        command=dict(mutation_id=str(uuid.uuid4()),base_sequence=0,restrictions=[self.restriction('deny_mutate')])
        result=restrict_authority(actor=cap,command=command,recovery=self.recovery)
        self.assertEqual(result,restrict_authority(actor=cap,command=command,recovery=self.recovery))
        before=(self.recovery.directory/'journal').read_bytes()
        with self.assertRaises(Http404): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertEqual((self.recovery.directory/'journal').read_bytes(),before)
        with self.assertRaises(Conflict): restrict_authority(actor=cap,command={**command,'restrictions':[self.restriction('deny_export')]},recovery=self.recovery)
        self.assertEqual(FeedbackEvent.objects.count(),1)
        self.assertEqual(RecoveryRestriction.objects.count(),1)

    def test_F07_head_fsync_uncertain_and_before_prepare_failure(self):
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        erase=self.erase()
        before=(self.recovery.directory/'journal').read_bytes()
        with patch.object(self.recovery,'prepare',side_effect=RuntimeError('before prepare')):
            with self.assertRaises(RuntimeError): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertEqual(before,(self.recovery.directory/'journal').read_bytes())
        self.assertEqual(FeedbackEvent.objects.count(),1)
        original=os.fsync; calls=[]
        def fsync(fd):
            calls.append(fd)
            if len(calls)==2: raise OSError('uncertain head fsync')
            return original(fd)
        with patch('shelf.recovery.os.fsync',side_effect=fsync):
            with self.assertRaises(RecoveryBlocked): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertTrue(self.recovery.blocked)
        self.assertTrue(FeedbackProjection.objects.get(signal='enjoyment').erased)
        with self.assertRaises(RecoveryBlocked): feedback_context(actor=self.actor,profile_id=self.profile.pk,target_id=erase.target_id,include_hidden=True)

    def test_F13_explicitly_unlocked_retained_fd_cannot_reacquire_continuity(self):
        fcntl.flock(self.recovery.fds['lock'],fcntl.LOCK_UN)
        with self.assertRaises(RecoveryBlocked):
            with self.recovery.barrier(self.recovery.store_id): pass
        self.assertTrue(self.recovery.blocked)

    def test_F04_invalid_authority_restriction_never_prepares(self):
        cap=trusted_operator(self.recovery,self.actor)
        for restriction in (self.restriction(subject_id='not-an-actor'),
            self.restriction('deny_source_content',kind='source_restrict',profile_id=None,subject_id='unknown-fiction-source'),
            self.restriction('disable_actor',profile_id=None,subject_id=str(self.actor.pk))):
            with self.assertRaises(FeedbackError): restrict_authority(actor=cap,command=dict(mutation_id=str(uuid.uuid4()),base_sequence=0,restrictions=[restriction]),recovery=self.recovery)
            self.assertEqual((self.recovery.directory/'journal').read_bytes(),b'')
        self.assertEqual(RecoveryRestriction.objects.count(),0)

    def test_F14_schema_reference_and_corrupt_database_rejected(self):
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        raw=sqlite3.connect(':memory:'); connection.connection.backup(raw)
        try:
            raw.execute('ALTER TABLE shelf_feedbackprojection ADD COLUMN unexpected_authority TEXT')
            with self.assertRaises(RecoveryBlocked): semantic_check(raw)
        finally: raw.close()
        raw=sqlite3.connect(':memory:'); connection.connection.backup(raw)
        try:
            raw.execute("UPDATE shelf_feedbackevent SET scope='session'"); raw.commit()
            with self.assertRaises(RecoveryBlocked): semantic_check(raw)
        finally: raw.close()
        backup=self.backup()
        truncated=self.case/'backups/truncated.sqlite3'; truncated.write_bytes(backup.path.read_bytes()[:100]); truncated.chmod(0o600)
        self.assertEqual(self.restore(ReadOnlyBackup(truncated,backup.manifest),'corrupt-restore').state,'BLOCKED')
        self.assertFalse(hasattr(self.recovery,'promote'))

    def test_F07_local_commit_error_rolls_back_with_pending_denial_then_resume(self):
        mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command())
        erase=self.erase(); original=self.recovery.apply_pending
        def fail_commit(raw,**kwargs):
            raw.set_authorizer(lambda action,first,second,database,trigger:
                sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_TRANSACTION and first=='COMMIT' else sqlite3.SQLITE_OK)
            try: return original(raw,**kwargs)
            finally: raw.set_authorizer(None)
        with patch.object(self.recovery,'apply_pending',side_effect=fail_commit):
            with self.assertRaises(sqlite3.DatabaseError): erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=erase,recovery=self.recovery)
        self.assertFalse(connection.connection.in_transaction)
        self.assertEqual(FeedbackEvent.objects.count(),1)
        self.assertEqual(RecoveryRestriction.objects.count(),0)
        self.assertIsNotNone(self.recovery.pending)
        with self.assertRaises(Http404): human_export(actor=self.actor,profile_id=self.profile.pk,format='json')
        self.recovery.resume_pending(connection.connection)
        self.assertEqual(FeedbackEvent.objects.count(),2)
        self.assertEqual(RecoveryRestriction.objects.count(),1)
        self.assertTrue(FeedbackProjection.objects.get(signal='enjoyment').erased)

    def test_repair_current_source_policy_correction_reverse_and_erase_in_isolated_restore(self):
        from shelf.feedback import canonical
        fixture=legacy_fixture(1)
        import_legacy_fixture(actor=self.actor,profile_id=self.profile.pk,mutation_id=str(uuid.uuid4()),
            fixture_bytes=canonical(fixture),binding=binding_for_fixture(self.profile,fixture))
        saved=FeedbackEvent.objects.get(signal='saved')
        pristine_history=self.backup('before-human.sqlite3')
        human=mutate(actor=self.actor,profile_id=self.profile.pk,command=self.command('saved',{'value':False},
            target_id=str(saved.target_id),corrects_id=str(saved.pk)))
        before_ban=self.backup('before-ban.sqlite3')
        cap=trusted_operator(self.recovery,self.actor)
        restrict_authority(actor=cap,recovery=self.recovery,command=dict(mutation_id=str(uuid.uuid4()),
            base_sequence=0,restrictions=[self.restriction('deny_source_content',kind='source_restrict',
                profile_id=None,subject_id='legacy-fixture-v1')]))
        for index,backup in enumerate((pristine_history,before_ban)):
            report=self.restore(backup,'repair-ban-'+str(index)); self.assertEqual(report.state,'ISOLATED_READY',report)
            raw=sqlite3.connect(report.isolated_database)
            try:
                counts=semantic_check(raw)
                self.assertEqual(counts['feedbackevent'],3+index)
                self.assertEqual(raw.execute("SELECT count(*) FROM shelf_feedbackevent WHERE original_origin!='direct' AND (body IS NOT NULL OR attribution IS NOT NULL OR source_time IS NOT NULL OR original_recorded_at IS NOT NULL OR source_metadata IS NOT NULL OR source_record_id IS NOT NULL)").fetchone()[0],0)
                if index:
                    self.assertEqual(json.loads(raw.execute('SELECT body FROM shelf_feedbackevent WHERE id=?',(uuid.UUID(human.event_id).hex,)).fetchone()[0]),{'value':False})
                    self.assertEqual(json.loads(raw.execute("SELECT value FROM shelf_feedbackprojection WHERE signal='saved'").fetchone()[0]),{'value':False})
                else: self.assertIsNone(raw.execute("SELECT value FROM shelf_feedbackprojection WHERE signal='saved'").fetchone()[0])
                self.assertEqual(raw.execute('SELECT count(*) FROM django_session').fetchone()[0],0)
                self.assertEqual(raw.execute('SELECT count(*) FROM auth_user WHERE is_active').fetchone()[0],0)
            finally: raw.close()
        reverse=mutate(actor=self.actor,profile_id=self.profile.pk,command=replace(self.command('saved',
            target_id=str(saved.target_id),operation='reverse',reverses_id=human.event_id),value=None))
        reversed_backup=self.backup('after-reverse.sqlite3')
        report=self.restore(reversed_backup,'repair-reverse'); self.assertEqual(report.state,'ISOLATED_READY',report)
        raw=sqlite3.connect(report.isolated_database)
        try:
            semantic_check(raw)
            p=raw.execute("SELECT effective_event_id,value FROM shelf_feedbackprojection WHERE signal='saved'").fetchone()
            self.assertEqual(p,(saved.pk.hex,None))
            self.assertEqual(raw.execute('SELECT reverses_id FROM shelf_feedbackevent WHERE id=?',(uuid.UUID(reverse.event_id).hex,)).fetchone()[0],uuid.UUID(human.event_id).hex)
        finally: raw.close()
        erase_feedback(actor=self.actor,profile_id=self.profile.pk,command=self.erase('saved'),recovery=self.recovery)
        restrict_authority(actor=cap,recovery=self.recovery,command=dict(mutation_id=str(uuid.uuid4()),
            base_sequence=self.recovery.sequence,restrictions=[self.restriction('deny_export')]))
        report=self.restore(reversed_backup,'repair-erase'); self.assertEqual(report.state,'ISOLATED_READY',report)
        raw=sqlite3.connect(report.isolated_database)
        try:
            semantic_check(raw)
            self.assertEqual(raw.execute("SELECT revision,value,erased FROM shelf_feedbackprojection WHERE signal='saved'").fetchone(),(4,None,1))
            self.assertEqual(raw.execute("SELECT count(*) FROM shelf_feedbackevent WHERE signal='saved' AND (body IS NOT NULL OR attribution IS NOT NULL OR source_record_id IS NOT NULL OR original_recorded_at IS NOT NULL OR source_time IS NOT NULL OR source_metadata IS NOT NULL)").fetchone()[0],0)
            self.assertEqual(raw.execute("SELECT count(*) FROM shelf_mutationreceipt WHERE state!='retired' OR payload_sha256 IS NOT NULL").fetchone()[0],0)
            self.assertEqual(raw.execute('SELECT count(*) FROM shelf_grant WHERE can_view OR can_select OR can_mutate OR can_export').fetchone()[0],0)
            self.assertEqual(raw.execute('SELECT count(*) FROM shelf_recoveryrestriction').fetchone()[0],3)
            self.assertFalse(hasattr(self.recovery,'promote'))
        finally: raw.close()


from .test_grant_authority import RecoveryCase as PortableRecoveryCase


class TrustedPortableRestartTests(PortableRecoveryCase):
    def disk_fixture(self):
        from shelf.models import ProfileCabinetState
        self.profile_ref = self.bind()
        self.state = ProfileCabinetState.objects.get()
        path = self.boundary.root / 'portable.sqlite3'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600); os.close(fd)
        raw = sqlite3.connect(path)
        try:
            connection.connection.backup(raw)
        finally:
            raw.close()
        return path

    def test_named_trusted_restart_requires_confirmation_and_retains_unknown_no_retry_identity(self):
        from shelf.recovery import trusted_restart_fixture
        from shelf.enhance_contract import RegistryView
        from shelf.portable_cabinet_store import CabinetStore
        from .test_portable_cabinet_store import IsolatedStateProvider, StoreCase
        from shelf.models import ProfileCabinetState
        self.profile_ref = self.bind()
        self.state = ProfileCabinetState.objects.get()
        self.consent(self.profile_ref); self.consent(self.profile_ref, purpose='catalogue_lookup')
        self.registry = RegistryView('1', ('books', 'films', 'tv', 'music', 'events', 'outings'))
        self.store = CabinetStore.isolated_test(self.authority, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), state_provider=IsolatedStateProvider(self))
        intent = StoreCase.intent(self)
        row = StoreCase.sealed(self, intent)
        row = self.store.reserve(str(row.pk), body_digest=row.body_digest, expected_revision=row.revision)
        path = self.boundary.root / 'portable.sqlite3'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600); os.close(fd)
        raw = sqlite3.connect(path)
        try: connection.connection.backup(raw)
        finally: raw.close()
        with self.assertRaises(RecoveryBlocked):
            with trusted_restart_fixture(self.boundary, confirmed=False, fresh=True): self.fail('unconfirmed trust served')
        with trusted_restart_fixture(self.boundary, confirmed=True, fresh=True) as first:
            values = first.bound_db.execute('SELECT state,attempts,transport_request_id,content_redacted_at FROM shelf_synthesisrun').fetchone()
            self.assertEqual(values[:3], ('unknown', 1, row.transport_request_id.hex))
            self.assertIsNotNone(values[3])  # Synthetic old expiry is removed before reopen.
            self.assertEqual(first.bound_db.execute('SELECT count(*) FROM shelf_portablevalidation').fetchone()[0], 0)
        self.assertTrue(first.closed)
        with trusted_restart_fixture(self.boundary, confirmed=True) as second:
            self.assertEqual(second.bound_db.execute('SELECT state,attempts,transport_request_id FROM shelf_synthesisrun').fetchone(), values[:3])
            with self.assertRaises(sqlite3.IntegrityError):
                second.bound_db.execute('UPDATE shelf_synthesisrun SET attempts=0,revision=revision+1')
        with self.assertRaises(RecoveryBlocked): CurrentRecoveryCoordinator(path)
        # No test claims that a complete-root replay is distinguishable here.

    def test_trusted_pending_revoke_settles_original_mirror_and_corruption_refuses(self):
        from shelf.recovery import trusted_restart_fixture
        path = self.disk_fixture()
        with trusted_restart_fixture(self.boundary, confirmed=True, fresh=True) as first:
            first.prepare(str(uuid.uuid4()), [dict(kind='revoke', profile_id=self.state.profile_id,
                target_id=None, signal=None, scope=None, subject_id=str(self.state.actor_id),
                restriction_code='deny_export')])
        with trusted_restart_fixture(self.boundary, confirmed=True) as second:
            self.assertIsNone(second.pending)
            self.assertEqual(len(second.committed_restrictions()), 1)
            self.assertEqual(second.bound_db.execute('SELECT can_export FROM shelf_grant WHERE profile_id=?',
                                                    (self.state.profile_id,)).fetchone()[0], 0)
        # Deliberate disposable mirror corruption, never tombstone cleanup.
        raw = sqlite3.connect(path)
        try:
            raw.execute('UPDATE shelf_recoveryrestriction SET external_head_id=?', (uuid.uuid4().hex,)); raw.commit()
        finally: raw.close()
        with self.assertRaises(RecoveryBlocked):
            with trusted_restart_fixture(self.boundary, confirmed=True): self.fail('mismatched mirror served')
