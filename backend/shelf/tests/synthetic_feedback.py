"""Only fictional fixtures. Counts in tests are literal independent expectations."""
from dataclasses import replace
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from shelf.catalogue import CARDS, TARGET_IDS
from shelf.feedback import MutationCommand, canonical, NATIVE_LABELS
from shelf.models import (Profile, Grant, FeedbackTarget, FeedbackEvent, RecoveryAnchor)
from shelf.portability import ValidatedImportBinding, NAMESPACE
from shelf.recovery import CurrentRecoveryCoordinator, using_recovery


def legacy_fixture(n=12,person='person-a'):
    subject=str(uuid.uuid5(NAMESPACE,canonical(['legacy-fixture-v1',person]).decode()))
    data=dict(schema_version='cc.legacy-fiction.v1',fictional=True,source_namespace='legacy-fixture-v1',
              person=person,subject_id=subject,labels=NATIVE_LABELS,interactions=[],seeds=[])
    for i in range(n):
        r=i%7
        data['interactions'].append(dict(person=person,media='film' if i%2==0 else 'book',id=i//2+1,
            state='rated' if r<5 else 'unseen' if r==5 else 'skipped',rating=r+1 if r<5 else None,saved=i%2==0))
    for j in range(n//6):
        row=data['interactions'][6*j]
        data['seeds'].append(dict(media=row['media'],id=row['id'],text='A fictional attributed statement.',attribution='Fictional seed author'))
    return data


def binding_for_fixture(profile,fixture):
    mapping=tuple(dict(source_namespace='legacy-fixture-v1',media=r['media'],id=r['id'],
        target_uuid=str(uuid.uuid5(NAMESPACE,canonical(['legacy-fixture-v1',r['media'],r['id']]).decode()))) for r in fixture['interactions'])
    return ValidatedImportBinding(profile.pk,'legacy-fixture-v1',fixture['person'],fixture['subject_id'],
        fixture['subject_id'],mapping,hashlib.sha256(canonical(fixture)).hexdigest())


def binding_for_package(profile,package):
    mapping=[]
    for t in package['targets']:
        if t['source_namespace']=='legacy-fixture-v1':
            media,identifier=t['source_key'].split(':'); identifier=int(identifier)
        else: media,identifier=t['kind'],t['source_key']
        mapping.append(dict(source_namespace=t['source_namespace'],media=media,id=identifier,target_uuid=t['id']))
    return ValidatedImportBinding(profile.pk,'portable-fiction-v1',None,package['subject_id'],package['subject_id'],
        tuple(mapping),hashlib.sha256(canonical(package['targets'])).hexdigest())


class FeedbackCase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.tmp=TemporaryDirectory(prefix='feedback-case-'); self.addCleanup(self.tmp.cleanup)
        self.case=Path(self.tmp.name); self.case.chmod(0o700)
        self.actor=get_user_model().objects.create(username='fictional-actor')
        self.profile=Profile.objects.create(kind='owner',label='Fictional profile',active=True)
        self.grant=Grant.objects.create(user=self.actor,profile=self.profile,can_view=True,can_select=True,can_mutate=True,can_export=True)
        anchor,_=RecoveryAnchor.objects.get_or_create(id=1,defaults={'store_id':uuid.uuid4()})
        for target,card,key in zip(TARGET_IDS,CARDS,('paper-observatory','mosslight-station','clockwork-lagoon')):
            FeedbackTarget.objects.get_or_create(id=target,defaults=dict(kind='item',title=card.title,category=card.category,source_namespace='fiction-p1101',source_key=key))
        connection.ensure_connection()
        self.recovery=CurrentRecoveryCoordinator.provision_empty(store_id=str(anchor.store_id),
            journal_directory=self.case/'journal',trusted_empty_db=connection.connection)
        self.addCleanup(self.recovery.close)
        self.context=using_recovery(self.recovery); self.context.__enter__()
        self.addCleanup(self.context.__exit__,None,None,None)

    def command(self,signal='enjoyment',value=None,**kwargs):
        target=kwargs.get('target_id',TARGET_IDS[0]); scope=kwargs.get('scope','item')
        last=FeedbackEvent.objects.filter(profile=self.profile,target_id=target,signal=signal,scope=scope).order_by('-revision').first()
        fields=dict(mutation_id=str(uuid.uuid4()),target_id=target,target_type=scope,signal=signal,scope=scope,operation='set',
            base_revision=last.revision if last else 0,context_event_id=str(last.pk) if last else None,
            value=value if value is not None else dict(value=3,basis='experienced',scale='integer-1-5') if signal=='enjoyment' else {'value':True})
        fields.update(kwargs)
        return MutationCommand(**fields)


# Independent ephemeral stores for repeated portable hops with different PKs.
# This is only a trusted test harness connection, never a runtime loader/path.
from contextlib import contextmanager
from unittest.mock import patch
import sqlite3
from django.db.backends.sqlite3._functions import register


@contextmanager
def fresh_destination(case,template,number):
    raw=sqlite3.connect(':memory:',isolation_level=None,detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)
    register(raw); template.backup(raw)
    raw.execute('PRAGMA foreign_keys=OFF')
    raw.execute('UPDATE shelf_profile SET id=? WHERE id=?',(number,case.profile.pk))
    raw.execute('UPDATE shelf_grant SET profile_id=?',(number,))
    raw.execute('UPDATE shelf_recoveryanchor SET store_id=?',(uuid.uuid4().hex,))
    raw.execute('PRAGMA foreign_keys=ON')
    with patch.object(connection,'connection',raw):
        profile=Profile.objects.get(pk=number)
        store=str(RecoveryAnchor.objects.get(pk=1).store_id)
        coordinator=CurrentRecoveryCoordinator.provision_empty(store_id=store,
            journal_directory=case.case/f'journal-{number}',trusted_empty_db=raw)
        try:
            with using_recovery(coordinator): yield profile,coordinator
        finally: coordinator.close(); raw.close()
