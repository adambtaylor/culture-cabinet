"""Default composition must remain unavailable while real producers are absent."""
from shelf.portable_enhance_service import EnhanceService, get_service
from .test_portable_cabinet_store import StoreCase


class EnhanceIntegrationTests(StoreCase):
    def test_default_service_never_promotes_probe_or_missing_ports_to_working_enhance(self):
        service = get_service(self.authority)
        self.assertIs(get_service(self.authority), service)
        ready = service.readiness(self.ref, profile_ref=self.profile_ref, connection_ref=str(self.config.pk))
        self.assertFalse(ready['available'])
        self.assertFalse(ready['run_ready'])
        self.assertEqual(ready['actions'], [])
        import uuid
        self.fault('CAPABILITY_UNAVAILABLE', service.start, self.ref, profile_ref=self.profile_ref,
            connection_ref=str(self.config.pk), client_key=str(uuid.uuid4()), refresh_nonce=str(uuid.uuid4()))
        self.assertEqual(self.fake.calls, 1)  # Only the pre-existing isolated neutral probe.
        self.assertFalse(service.current(self.ref, profile_ref=self.profile_ref)['published'])
