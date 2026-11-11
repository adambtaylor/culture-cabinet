"""Composed producer + authored UI normal forms, with fictional source replies.

These are real guarded Django handlers/CSRF/session/history, not browser or live
source proof. A positive bookmark policy is supplied only in the explicit fixture
case; the ordinary production-null policy is tested separately.
"""
from contextlib import ExitStack
from html.parser import HTMLParser
from unittest.mock import patch

from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.conf import settings

from shelf import discovery_transport as transport, discovery_views as views
from shelf.discovery_reducer import replay
from shelf.models import DiscoveryState
from shelf.tests.test_discovery_public_ui import PublicDiscoveryUITests
from shelf.tests.test_event_saved_references import policies
from shelf.tests.test_pbs_events import listing


class _ArticleForms(HTMLParser):
    """Read native fields/buttons from the actual rendered history article."""
    def __init__(self, key):
        super().__init__(convert_charrefs=True)
        self.target='catalogue-card-'+key
        self.active=False
        self.current=None
        self.forms=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='article':
            self.active=attrs.get('id')==self.target
        if not self.active:
            return
        if tag=='form':
            self.current={'attrs':attrs,'fields':{},'buttons':[]}
        elif self.current is not None and tag=='input' and attrs.get('name'):
            self.current['fields'][attrs['name']]=attrs.get('value','')
        elif self.current is not None and tag=='textarea' and attrs.get('name'):
            self.current['fields'][attrs['name']]=''
        elif self.current is not None and tag=='button' and attrs.get('name'):
            self.current['buttons'].append((attrs['name'],attrs.get('value','')))

    def handle_endtag(self, tag):
        if tag=='form' and self.current is not None:
            self.forms.append(self.current)
            self.current=None
        elif tag=='article':
            self.active=False


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class EventReferenceIntegrationTests(TestCase):
    setUp=PublicDiscoveryUITests.setUp
    form=PublicDiscoveryUITests.form

    def _forms(self, page, key):
        parser=_ArticleForms(key);parser.feed(page.content.decode())
        self.assertEqual(len(parser.forms),2)
        return parser.forms

    def _flow(self, *, permitted_fixture):
        self.client=Client(enforce_csrf_checks=True);self.client.force_login(self.actor)
        session=self.client.session;session['selected_profile_id']=1;session.save()
        with ExitStack() as stack:
            if permitted_fixture:
                descriptors=policies()
                stack.enter_context(patch('shelf.event_source_registry.SOURCES',descriptors))
                stack.enter_context(patch.object(views,'EVENT_SOURCES',descriptors))
            read=stack.enter_context(patch.object(transport,'pbs',return_value=(200,listing())))
            source_form=self.form('event_source',source_id='pbs')
            self.assertEqual(self.client.post(self.action,source_form,
                HTTP_X_CSRFTOKEN=self.client.cookies[settings.CSRF_COOKIE_NAME].value).status_code,302)
            page=self.client.get(self.url)
            key=page.context['public_results'][0]['outcome'].candidates[0].key
            forms=self._forms(page,key)
            save=next(form for form in forms if ('operation','save') in form['buttons'])
            self.assertEqual(self.client.post(save['attrs']['action'],
                save['fields']|{'operation':'save'}).status_code,302)
            saved=DiscoveryState.objects.get()
            self.assertEqual(len(saved.history),1)
            self.assertEqual('saved_reference' in saved.history[0],permitted_fixture)
            self.assertNotIn('card',saved.history[0])
            self.client.get(self.url)  # Retain this same session's current deck.
            views.PUBLIC_CACHE.clear()
            page=self.client.get(self.url)
            stale=next(row for row in page.context['responses'] if row['card'].key==key)
            self.assertTrue(stale['reference_only'] and stale['saved'])
            self.assertIsNone(stale['card'].url)
            self.assertEqual(stale['saved_reference'] is not None,permitted_fixture)
            self.assertNotIn(key,[row['card'].key for row in
                page.context['events']+page.context['more_source_events']])
            self.assertContains(page,'source details expired')
            if permitted_fixture:
                self.assertContains(page,'Original source page; current details unverified')
            else:
                self.assertContains(page,'No checked source link is available.')
            response=next(form for form in self._forms(page,key)
                          if ('choice','intrigued') in form['buttons'])
            self.assertEqual(response['attrs']['method'],'post')
            self.assertNotIn('data-feedback-form',response['attrs'])
            self.assertNotIn('data-feedback-correction',response['attrs'])
            payload=response['fields']|{'choice':'intrigued'}
            self.assertEqual(payload['operation'],'respond')
            for field in ('saved_reference','official_url','occurrence_ref','retention_policy_ref'):
                self.assertNotIn(field,payload)
            # The original partial/deck guard stays closed for this history row.
            # Only the corrected authored template selects a full native POST.
            self.assertEqual(self.client.post(response['attrs']['action'],payload,
                HTTP_ACCEPT='application/json').status_code,400)
            self.assertEqual(len(DiscoveryState.objects.get().history),1)
            post=self.client.post(response['attrs']['action'],payload)
            self.assertEqual(post.status_code,302)
            self.assertEqual(self.client.post(response['attrs']['action'],payload).status_code,302)
            state=DiscoveryState.objects.get()
            self.assertEqual(len(state.history),2)
            self.assertNotIn('saved_reference',state.history[-1])
            self.assertEqual(replay(state.history)['choices'][key],'intrigued')
            self.assertIn(key,replay(state.history)['saved'])
            fresh=Client(enforce_csrf_checks=True);fresh.force_login(self.actor)
            session=fresh.session;session['selected_profile_id']=1;session.save()
            page=fresh.get(self.url)
            row=next(row for row in page.context['responses'] if row['card'].key==key)
            self.assertTrue(row['reference_only'] and row['saved'])
            self.assertEqual(row['choice'],'intrigued')
            self.assertEqual(row['saved_reference'] is not None,permitted_fixture)
            unsave=next(form for form in self._forms(page,key) if ('operation','unsave') in form['buttons'])
            self.assertEqual(fresh.post(unsave['attrs']['action'],
                unsave['fields']|{'operation':'unsave'}).status_code,302)
            self.assertNotIn(key,replay(DiscoveryState.objects.get().history)['saved'])
            page=fresh.get(self.url)
            undo=dict(mode='household',queue='all',revision=page.context['revision'],
                nonce=page.context['nonce'],operation='undo',key='',choice='')
            self.assertEqual(fresh.post(self.action,undo,
                HTTP_X_CSRFTOKEN=fresh.cookies[settings.CSRF_COOKIE_NAME].value).status_code,302)
            self.assertIn(key,replay(DiscoveryState.objects.get().history)['saved'])
            read.assert_called_once_with()

    def test_checked_reference_native_form_response_after_expiry_and_fresh_session(self):
        self._flow(permitted_fixture=True)

    def test_production_opaque_native_form_response_after_expiry_and_fresh_session(self):
        self._flow(permitted_fixture=False)


@override_settings(ROOT_URLCONF='cabinet.provider_urls',ALLOWED_HOSTS=['testserver'])
class EventReferenceRestrictionIntegrationTests(TransactionTestCase):
    """Actual handler-created history crosses the real restriction/restore path."""
    form=PublicDiscoveryUITests.form

    def setUp(self):
        import uuid
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from django.contrib.auth import get_user_model
        from django.db import connections
        from django.db.backends.sqlite3.base import DatabaseWrapper
        from shelf.models import Profile,Grant,RecoveryAnchor
        from shelf.recovery import CurrentRecoveryCoordinator,using_recovery
        super().setUp()
        original=connections['default'];original.ensure_connection()
        # Same accepted isolated migrated-memory pattern as the authored tail;
        # its protected tombstones never enter Django's unrelated flush store.
        wrapper=DatabaseWrapper({**original.settings_dict,'NAME':
            'file:memorydb_event_source_closure_'+uuid.uuid4().hex+'?mode=memory&cache=shared'},alias='default')
        wrapper.ensure_connection();original.connection.backup(wrapper.connection)
        connections['default']=wrapper
        def close_wrapper():
            try:
                if wrapper.connection is not None:wrapper.connection.close();wrapper.connection=None
            finally:connections['default']=original
        self.addCleanup(close_wrapper)
        self.tmp=TemporaryDirectory(prefix='event-source-closure-');self.addCleanup(self.tmp.cleanup)
        self.case=Path(self.tmp.name);self.case.chmod(0o700)
        self.actor=get_user_model().objects.create_user(pk=1,username='fictional-event-owner')
        self.profile=Profile.objects.create(pk=1,kind='owner',label='Fictional event profile',active=True)
        self.grant=Grant.objects.create(user=self.actor,profile=self.profile,can_view=True,
            can_select=True,can_mutate=True,can_export=True)
        anchor,_=RecoveryAnchor.objects.get_or_create(id=1,defaults={'store_id':uuid.uuid4()})
        self.recovery=CurrentRecoveryCoordinator.provision_empty(store_id=str(anchor.store_id),
            journal_directory=self.case/'journal',trusted_empty_db=wrapper.connection)
        self.addCleanup(self.recovery.close)
        context=using_recovery(self.recovery);context.__enter__()
        self.addCleanup(context.__exit__,None,None,None)
        from shelf.tests.test_discovery_transport import NOW
        clock=patch.object(views.timezone,'now',return_value=NOW)
        clock.start();self.addCleanup(clock.stop)
        network=patch.object(transport,'_send',side_effect=AssertionError('Unmocked source transport forbidden'))
        network.start();self.addCleanup(network.stop)
        self.client=Client();self.client.force_login(self.actor)
        session=self.client.session;session['selected_profile_id']=1;session.save()
        self.url='/profiles/1/discovery/';self.action=self.url+'action/'
        views.PUBLIC_CACHE.clear();self.addCleanup(views.PUBLIC_CACHE.clear)

    def test_handler_saved_reference_is_purged_and_old_backup_cannot_restore_it(self):
        import json,sqlite3,uuid
        from contextlib import closing
        from shelf.tests import test_recovery as fixture
        from shelf.recovery import restrict_authority,trusted_operator
        from shelf.models import FeedbackTarget
        descriptors=policies()
        with patch('shelf.event_source_registry.SOURCES',descriptors), \
             patch.object(views,'EVENT_SOURCES',descriptors), \
             patch.object(transport,'pbs',return_value=(200,listing())) as read:
            self.assertEqual(self.client.post(self.action,self.form('event_source',source_id='pbs')).status_code,302)
            key=self.client.get(self.url).context['public_results'][0]['outcome'].candidates[0].key
            self.assertEqual(self.client.post(self.action,self.form('save',key=key)).status_code,302)
            before=DiscoveryState.objects.get();receipts=dict(before.receipts);nonce=before.nonce
            self.assertIn('saved_reference',before.history[0])
            backup=fixture.RecoveryTests.backup(self,'handler-before-denial.sqlite3')
            restriction=dict(kind='source_restrict',profile_id=None,target_id=None,
                signal=None,scope=None,subject_id='event-source:pbs',restriction_code='deny_source_content')
            self.assertFalse(FeedbackTarget.objects.filter(source_namespace='event-source:pbs').exists())
            restrict_authority(actor=trusted_operator(self.recovery,self.actor),recovery=self.recovery,
                command=dict(mutation_id=str(uuid.uuid4()),base_sequence=self.recovery.sequence,
                             restrictions=[restriction]))
            after=DiscoveryState.objects.get()
            self.assertNotIn('saved_reference',after.history[0])
            self.assertEqual((after.receipts,after.nonce),(receipts,nonce))
            self.assertEqual(after.revision,before.revision+1)
            self.assertIn(key,replay(after.history)['saved'])
            views.PUBLIC_CACHE.clear()
            page=self.client.get(self.url)
            row=next(row for row in page.context['responses'] if row['card'].key==key)
            self.assertTrue(row['reference_only'] and row['saved'])
            self.assertIsNone(row['saved_reference']);self.assertIsNone(row['card'].url)
            self.assertNotContains(page,'Original source page; current details unverified')
            self.assertEqual(self.client.post(self.action,self.form('unsave',key=key)).status_code,302)
            self.assertEqual(self.client.post(self.action,self.form('undo')).status_code,302)
            self.assertIn(key,replay(DiscoveryState.objects.get().history)['saved'])
            self.assertFalse(any('saved_reference' in event for event in DiscoveryState.objects.get().history))
            report=fixture.RecoveryTests.restore(self,backup,'handler-old-backup-restored')
            self.assertEqual(report.state,'ISOLATED_READY',report)
            with closing(sqlite3.connect(report.isolated_database)) as raw:
                row=raw.execute('SELECT history,receipts,nonce FROM shelf_discoverystate').fetchone()
                self.assertFalse(any('saved_reference' in event for event in json.loads(row[0])))
                self.assertEqual((json.loads(row[1]),row[2]),(receipts,nonce))
            read.assert_called_once_with()
