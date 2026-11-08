"""Native job fences use the genuine non-1 owner fixture and selected scope."""
from unittest import TestCase
from shelf.acquisition_contract import Blocked
from shelf.obtain_native_context import NativeJobContext, native_job_context
from shelf.obtain_tv_flow import card_projection
from shelf.portable_authority import AuthorityFault
from shelf.portable_saved_authority import select_portable_profile
from shelf import grant_authority as ga, saved
from .test_portable_obtain import PortableObtainTests


class ContentIdentityContractTests(TestCase):
    def test_canonical_content_identity_refuses_ambiguous_episode_inputs(self):
        from shelf.obtain_tv_flow import tv_content_identity_digest
        from shelf.acquisition_contract import ContractError
        self.assertEqual(tv_content_identity_digest(tmdb_show_id=1399, season=1, episode=2),
                         '39bf6a3fcd9d753be2800f6ff78b6d993b74afbb5594719f72a74311d9ced0bc')
        for changes in ({'tmdb_show_id': True}, {'tmdb_show_id': '1399'},
                        {'tmdb_show_id': 0}, {'season': -1}, {'episode': 0},
                        {'season': 1000}, {'episode': 1000}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                tv_content_identity_digest(**({'tmdb_show_id': 1399, 'season': 1, 'episode': 2} | changes))


class DeliveryBindingTests(TestCase):
    def binding(self):
        from shelf.obtain_tv_flow import DeliveryBinding, DestinationBinding, ServiceGeneration
        from shelf.jellyfin_native import TV_ROOTS
        return DeliveryBinding(DestinationBinding('synthetic-destination', 'synthetic-mac', TV_ROOTS[0],
            ServiceGeneration('destination_file', 'd' * 64, 'synthetic-config1', 'synthetic-key1')),
            'Fiction Series 2024', False)

    def test_canonical_delivery_wire_golden_and_closed_mapping_inputs(self):
        from dataclasses import replace
        from shelf.obtain_tv_flow import delivery_binding_material, read_delivery_binding
        from shelf.acquisition_contract import ContractError
        value = self.binding(); wire = delivery_binding_material(value)
        self.assertEqual(wire['digest'], 'cae8ccb07f2c89e443ac6c5d279fed3db68769dad6cc33c24eaf861a15998870')
        self.assertEqual(set(wire), {'schema', 'destination', 'relative_parent', 'create_parent', 'digest'})
        self.assertEqual(read_delivery_binding(wire), value)
        for changes in ({'relative_parent': '/absolute'}, {'relative_parent': '../outside'},
                        {'relative_parent': 'Folder//Other'}, {'relative_parent': '' , 'create_parent': True},
                        {'relative_parent': 'Folder/Subfolder', 'create_parent': True}, {'create_parent': 1}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                replace(value, **changes)
        for changes in ({'unexpected': True}, {'digest': '0' * 64}, {'relative_parent': 'Other'},
                        {'destination': {**wire['destination'], 'untrusted_path': '/elsewhere'}}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                read_delivery_binding({**wire, **changes})

    def test_same_root_different_parent_or_create_flag_never_passes_copy_adapter(self):
        from dataclasses import replace
        from shelf.obtain_tv_flow import require_confirmed_delivery_plan
        from shelf.jellyfin_tv_import import CopyPlan, destination_binding_digest
        from shelf.acquisition_contract import ContractError
        from datetime import datetime, timezone
        value = self.binding(); path = value.relative_parent + '/Fiction.Series.2024.S02E01.mkv'
        plan = CopyPlan('00000000-0000-4000-8000-000000000001', 2, 'SABnzbd_nzo_synthetic',
            'a' * 64, 'b' * 64, 'c' * 64, destination_binding_digest(value.destination, path, False),
            '00000000-0000-4000-8000-000000000002', value.destination.host, value.destination.root,
            path, 10, 'e' * 64, 'episode_s02e01', datetime(2030, 1, 1, tzinfo=timezone.utc).isoformat(), True)
        self.assertIs(require_confirmed_delivery_plan(value, plan), plan)
        for changes in ({'relative_path': 'Other/' + path.split('/')[-1]}, {'create_parent': True},
                        {'relative_path': '../outside.mkv'}, {'host_id': 'other-host'},
                        {'relative_path': path.split('/')[-1]}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                require_confirmed_delivery_plan(value, replace(plan, **changes))


class NativeJobContextTests(PortableObtainTests):
    def test_cancelled_unsubmitted_job_invalidates_effect_context_but_remains_readable(self):
        with self.scope() as access:
            job = self.choice()
            context = native_job_context(access, job_id=job.pk, operation='submit')
            with ga.current_writer(self.authority, self.ref, self.profile_ref):
                saved.cancel(actor=self.actor, profile_id=self.state.profile_id, job_id=job.pk)
            with self.assertRaises(Blocked):
                context.revalidate()
            with self.assertRaises(Blocked):
                native_job_context(access, job_id=job.pk, operation='observe')
            native_job_context(access, job_id=job.pk, operation='view').revalidate()

    def test_native_context_rechecks_selected_non_one_owner_and_closed_scope(self):
        with self.scope() as access:
            job = self.choice()
            context = native_job_context(access, job_id=job.pk, operation='observe')
            self.assertIs(type(context), NativeJobContext)
            self.assertEqual((context.actor_id, context.profile_id), (self.actor.pk, self.state.profile_id))
            self.assertNotEqual(context.actor_id, 1)
            self.assertNotEqual(context.profile_id, 1)
            context.revalidate()
        with self.assertRaises((AuthorityFault, Blocked)):
            context.revalidate()

    def test_native_context_rotation_does_not_fall_back_to_legacy_authority(self):
        with self.scope() as access:
            job = self.choice()
            context = native_job_context(access, job_id=job.pk, operation='view')
            select_portable_profile(self.authority, self.ref, profile_ref=self.profile_ref)
            with self.assertRaises((AuthorityFault, Blocked)):
                context.revalidate()

    def test_native_view_context_cannot_effect_and_projection_is_closed(self):
        with self.scope() as access:
            job = self.choice()
            context = native_job_context(access, job_id=job.pk, operation='view')
            with self.assertRaises(Blocked), context.effect_window():
                self.fail('Read-only context entered effect window')
            with self.assertRaises(Blocked):
                NativeJobContext(access, job.pk, 'observe', None)
            self.assertEqual(card_projection(state='working', label='Checking library', detail_href='/cabinet/obtain/jobs/1/'),
                             dict(state='working', label='Checking library', detail_href='/cabinet/obtain/jobs/1/', verification_until=None))
            with self.assertRaises(ValueError):
                card_projection(state='available', label='False green', detail_href='/')
