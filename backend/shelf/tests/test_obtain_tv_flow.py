"""Native job fences use the genuine non-1 owner fixture and selected scope."""
from shelf.acquisition_contract import Blocked
from shelf.obtain_native_context import NativeJobContext, native_job_context
from shelf.obtain_tv_flow import card_projection
from shelf.portable_authority import AuthorityFault
from shelf.portable_saved_authority import select_portable_profile
from .test_portable_obtain import PortableObtainTests


class NativeJobContextTests(PortableObtainTests):
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
