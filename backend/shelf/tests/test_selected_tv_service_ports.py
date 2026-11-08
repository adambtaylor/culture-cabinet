"""Genuine non-1 native context + synthetic service/file ports, never live proof.

The controlled acknowledged receipt is installed under the real native writer.
No legacy operator, forged login, private credential or provider call is used.
"""
from contextvars import Context
from dataclasses import asdict, replace
from datetime import timedelta
import io
import json
from pathlib import Path
import socket
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.db import transaction
from django.utils import timezone

from shelf import grant_authority as ga, obtain_preview as preview, jellyfin_native as jf
from shelf import jellyfin_tv_import as imp, sab_selected as sab
from shelf import obtain_library_verification as library
from shelf.acquisition_backend import PrivateSearch
from shelf.models import SavedItem, ObtainTransfer, ObtainSelection
from shelf.obtain_native_context import native_job_context
from shelf.obtain_tv_flow import DestinationBinding, ServiceGeneration
from shelf.portable_authority import AuthorityFault
from shelf.portable_saved_authority import select_portable_profile
from .test_portable_obtain import PortableObtainTests
from .test_obtain_preview import item, payload
from . import test_sab_selected as saved_sab
from .test_jellyfin_native import Clock, Connection, Response


class NativeTVServicePortsTests(PortableObtainTests):
    def tv_choice(self):
        with ga.current_writer(self.authority, self.ref, self.profile_ref), \
             transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
            row = SavedItem.objects.create(actor=self.actor, profile_id=self.state.profile_id,
                provider='tmdb', medium='tv', source_id=1399, list_kind='watch',
                title='Fictional Fixture Show', url='https://www.themoviedb.org/tv/1399',
                fetched_at=timezone.now())
        backend = Mock()
        backend.search.return_value = PrivateSearch(payload(item(
            title='Fictional Fixture Show S01E02 1080p', guid='123', category='5000')))
        receipt = preview.search(actor=self.actor, profile_id=self.state.profile_id,
            item_id=row.pk, season=1, episode=2, factory=lambda **_: backend)
        result = receipt.results.get()
        job, _ = preview.confirm(actor=self.actor, profile_id=self.state.profile_id,
            preview_id=receipt.pk, result_id=result.pk, data=dict(
                review_token=preview.review_token(receipt, result), scope=receipt.scope,
                edition=result.metadata['title'], authorized_content='on', storage='local_library',
                storage_check='on'))
        return job

    def controlled_ack(self, access):
        fixture = saved_sab.SelectedSabEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.now = self.authority.now()
        job = self.tv_choice()
        selected = ObtainSelection.objects.get(job=job).result
        # This is a controlled synthetic operational receipt, not a claim that
        # an external submission happened. All service ports below are local.
        with ga.current_writer(self.authority, self.ref, self.profile_ref), \
             transaction.atomic(), ga.writer_window(self.actor, self.state.profile_id):
            ObtainTransfer.objects.create(job=job, binding={'test_only': True,
                'synthetic_ack': fixture.job.nzo_id, 'request_id': str(job.request_id)},
                result_guid='123', nzb_sha256=fixture.job.nzb_sha256,
                state='queued', provider_job=fixture.job.nzo_id)
        context = native_job_context(access, job_id=job.pk, operation='observe')
        old = fixture.root
        fixture.request = str(job.request_id)
        fixture.name = 'CC-' + fixture.request
        fixture.root = fixture.complete_root / fixture.name
        old.rename(fixture.root)
        fixture.job = replace(fixture.job, request_id=fixture.request, job_id=job.pk,
            actor_id=self.actor.pk, profile_id=self.state.profile_id,
            binding_digest=context.binding_digest, result_id=str(selected.pk),
            result_digest=selected.digest)
        fixture.entry.update(request_id=fixture.request, result_id=str(selected.pk),
            result_digest=selected.digest, root=str(fixture.root))
        fixture.hook = fixture.make_hook(fixture.files)
        fixture.history.update(name=fixture.name, storage=str(fixture.root),
            completed=int((fixture.now - timedelta(seconds=1)).timestamp()))
        return job, context, fixture

    def sab_ports(self, fixture):
        generation = fixture.job.service_generations[0]
        return sab.SelectedSabPorts(generation, lambda: generation,
            Mock(return_value=fixture.history), Mock(return_value=fixture.entry),
            Mock(return_value=fixture.hook), lambda: fixture.now)

    def test_selected_observer_rechecks_genuine_non_one_context_and_generation(self):
        with self.scope() as access:
            job, context, fixture = self.controlled_ack(access)
            ports = self.sab_ports(fixture)
            result = sab.observe_selected(context, fixture.job, ports=ports)
            self.assertEqual(result.state, 'completed')
            self.assertNotEqual(result.job.actor_id, 1)
            self.assertNotEqual(result.job.profile_id, 1)
            ports.read_history.assert_called_once_with(fixture.job.nzo_id)
            ports.read_registration.assert_called_once_with(fixture.job.nzo_id)
            ports.read_hook.assert_called_once_with(fixture.job.request_id)
            wrong = replace(fixture.job, profile_id=1)
            with self.assertRaises(ValueError):
                sab.observe_selected(context, wrong, ports=ports)
        with self.assertRaises((ValueError, AuthorityFault)):
            sab.observe_selected(context, fixture.job, ports=ports)

    def test_current_service_or_profile_revocation_stops_before_further_reads(self):
        with self.scope() as access:
            _, context, fixture = self.controlled_ack(access)
            ports = self.sab_ports(fixture)
            stale = replace(ports, revalidate=lambda: replace(ports.generation, credential_generation='0' * 32))
            with self.assertRaises(ValueError):
                sab.observe_selected(context, fixture.job, ports=stale)
            ports.read_registration.assert_not_called()
            def registration(_):
                Context().run(select_portable_profile, self.authority, self.ref, profile_ref=self.profile_ref)
                return fixture.entry
            revoked = replace(ports, read_registration=registration)
            with self.assertRaises((ValueError, AuthorityFault)):
                sab.observe_selected(context, fixture.job, ports=revoked)
            ports.read_history.assert_not_called()
            ports.read_hook.assert_not_called()

    def test_unacknowledged_native_job_never_opens_service_ports(self):
        with self.scope() as access:
            job = self.tv_choice()
            context = native_job_context(access, job_id=job.pk, operation='observe')
            fixture = saved_sab.SelectedSabEvidenceTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.job = replace(fixture.job, request_id=str(job.request_id), job_id=job.pk,
                actor_id=self.actor.pk, profile_id=self.state.profile_id,
                binding_digest=context.binding_digest)
            ports = self.sab_ports(fixture)
            with self.assertRaises(ValueError):
                sab.observe_selected(context, fixture.job, ports=ports)
            ports.read_registration.assert_not_called()
            ports.read_history.assert_not_called()
            ports.read_hook.assert_not_called()

    def test_native_import_resolver_and_verification_chain_is_synthetic_and_no_recopy(self):
        with self.scope() as access, TemporaryDirectory() as root:
            job, _, fixture = self.controlled_ack(access)
            generation = ServiceGeneration('destination_file', 'd' * 64, 'e' * 32, 'f' * 32)
            clock = Clock()
            clock.started = fixture.now
            binding = jf.ServiceBinding(jf.ORIGIN, '1' * 32, 'fixture-version', '2' * 32,
                '3' * 32, self.state.installation_id.hex, self.actor.pk, self.state.profile_id,
                '5' * 32, '6' * 32, root, 'selected_tv_read', fixture.now + timedelta(seconds=600))
            library_generation = ServiceGeneration('jellyfin', jf._digest(binding.material()) if root in jf.TV_ROOTS
                else '', binding.configuration_generation, binding.credential_generation)
            with patch.object(jf, 'TV_ROOTS', (root,)):
                from shelf.obtain_tv_flow import tv_content_identity_digest
                identity = tv_content_identity_digest(tmdb_show_id=1399, season=1, episode=2)
                library_generation = replace(library_generation, binding_digest=jf._digest(binding.material()))
                fixture.job = replace(fixture.job, service_generations=fixture.job.service_generations +
                                      (generation, library_generation), content_identity_digest=identity)
                completion = sab.observe_selected(native_job_context(access, job_id=job.pk, operation='observe'),
                    fixture.job, ports=self.sab_ports(fixture))
                destination = DestinationBinding(fixture.job.destination_ref, socket.gethostname(), root, generation)
                intent = '7c035cd0-51eb-4e66-9096-68e1fe57a303'
                def plan_for(completed, dest, selected_intent):
                    return imp.CopyPlan(completed.job.request_id, completed.job.job_id, completed.job.nzo_id,
                        completed.job.binding_digest, completed.job.content_identity_digest,
                        completed.source_manifest_digest, imp.destination_binding_digest(dest,
                            'FixtureShow/S01E02.mkv', True), selected_intent, dest.host, dest.root,
                        'FixtureShow/S01E02.mkv', completed.files[0].size, completed.files[0].sha256,
                        completed.job.episode_scope, (fixture.now + timedelta(seconds=60)).isoformat(), True, True)
                def inspect(plan):
                    return imp.receive_selected(plan, io.BytesIO(), operation='inspect',
                        allowed_roots=(root,), clock=lambda: fixture.now)
                def receive(plan, source):
                    return imp.receive_selected(plan, source or io.BytesIO(), operation='receive',
                        allowed_roots=(root,), clock=lambda: fixture.now)
                ports = imp.DestinationImportPorts(generation, lambda: generation, plan_for,
                    inspect, receive, lambda: fixture.now,
                    authority_deadline=lambda: fixture.now + timedelta(seconds=60))
                importing = native_job_context(access, job_id=job.pk, operation='import')
                absent_deadline = imp.import_selected(importing, completion, destination,
                    intent=intent, ports=replace(ports, authority_deadline=None))
                self.assertEqual(absent_deadline.status, 'unknown')
                overrun = imp.import_selected(importing, completion, destination, intent=intent,
                    ports=replace(ports, authority_deadline=lambda: fixture.now + timedelta(seconds=30)))
                self.assertEqual(overrun.status, 'unknown')
                self.assertEqual(list(Path(root).iterdir()), [])
                copied = imp.import_selected(importing, completion, destination, intent=intent, ports=ports)
                self.assertEqual(copied.status, 'verified')
                self.assertTrue(copied.receipt.test_only)
                with patch.object(imp, '_selected_source', side_effect=AssertionError('Unexpected second copy')):
                    self.assertEqual(imp.import_selected(importing, completion, destination,
                        intent=intent, ports=ports).status, 'verified')
                target = jf.EpisodeResolutionTarget(fixture.job.request_id, fixture.job.binding_digest,
                    identity, copied.receipt.destination_binding_digest, 1399, fixture.job.episode_scope,
                    'Fictional Fixture Show', None, fixture.now + timedelta(seconds=60))
                series = {'Id': '7' * 32, 'ServerId': binding.server_id, 'Type': 'Series',
                    'Name': target.show_name, 'ProviderIds': {'Tmdb': '1399'}, 'Path': root + '/FixtureShow'}
                episode = {'Id': '8' * 32, 'ServerId': binding.server_id, 'Type': 'Episode',
                    'SeriesId': series['Id'], 'ParentId': '9' * 32, 'IndexNumber': 2, 'ParentIndexNumber': 1,
                    'Path': root + '/FixtureShow/S01E02.mkv', 'MediaSources': [{'Id': 'a' * 32,
                        'Path': root + '/FixtureShow/S01E02.mkv', 'Size': copied.receipt.size, 'Protocol': 'File'}]}
                verifying = native_job_context(access, job_id=job.pk, operation='verify_library')
                from contextlib import contextmanager
                @contextmanager
                def credential(bound):
                    verifying.revalidate()
                    yield jf.ScopedCredential(jf._digest(bound.material()), 'SYNTHETIC_NOT_A_KEY')
                def current(bound, selected):
                    verifying.revalidate()
                    return bound, selected
                connections = [Connection(Response(json.dumps({'Items': [value]}).encode()))
                               for value in (series, episode, episode)]
                with patch.object(jf, '_Connection', side_effect=connections):
                    resolution = jf.resolve_episode(binding, target, credential_scope=credential,
                                                     revalidate=current, clock=clock)
                self.assertEqual(resolution.status, 'resolved')
                self.assertEqual(library.verify_library_match(verifying, completion=completion,
                    destination=copied.receipt, resolution=resolution,
                    observation=resolution.observation, clock=lambda: fixture.now).status, 'unavailable')
                outcome = library.verify_library_match(verifying, completion=completion,
                    destination=copied.receipt, resolution=resolution, observation=resolution.observation,
                    revalidate_services=lambda: ((library_generation, generation),
                        fixture.now + timedelta(seconds=20)), clock=lambda: fixture.now)
                self.assertEqual(outcome.status, 'verified')
                self.assertEqual(outcome.verification.valid_until, fixture.now + timedelta(seconds=20))
                self.assertEqual((Path(root) / copied.receipt.relative_path).read_bytes(), fixture.payload)


def load_tests(loader, _discovered, _pattern):
    # Reuse the real owner fixture without duplicating every inherited portable
    # acceptance test when the whole test directory is discovered later.
    methods = sorted(name for name, value in NativeTVServicePortsTests.__dict__.items()
                     if name.startswith('test_') and callable(value))
    return unittest.TestSuite(loader.loadTestsFromName(
        __name__ + '.NativeTVServicePortsTests.' + name) for name in methods)
