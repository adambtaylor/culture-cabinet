"""Trusted backend only; no view imports, source reads, ORM or checkpoint store.

Grants come from a trusted owner authorization boundary, never request JSON.
This service releases credentials only inside the exact authorized operation.
"""
import json
import re
from uuid import UUID
from contextlib import contextmanager

from shelf import credential_store as store
from shelf.acquisition_contract import (
    Blocked, Client, ConfirmedSelection, ContractError, Permissions, PublicRequest, MAX_NZB_BYTES,
)
from shelf.acquisition_pipeline import AdapterUnavailable, ProviderJob, QueueRejected, Snapshot
from shelf.acquisition_transport import LoopbackTransport


class PrivateSearch(store._Private):
    """Opaque backend bytes, pending a separately reviewed search-result parser."""
    __slots__ = ('_payload',)

    def __init__(self, payload):
        self._payload = bytearray(payload)

    def close(self):
        self._payload[:] = b'\0' * len(self._payload)


def _queue_id(payload):
    try:
        result = json.loads(payload, object_pairs_hook=store._pairs)
    except (ValueError, UnicodeError, store.Refused, RecursionError):
        raise AdapterUnavailable('Enqueue outcome unknown') from None
    if type(result) is dict:
        if (set(result) == {'status', 'error'} and result['status'] is False
                and type(result['error']) is str):
            raise QueueRejected('Enqueue rejected')
        ids = result.get('nzo_ids')
        if (set(result) == {'status', 'nzo_ids'} and result['status'] is True
                and type(ids) is list and len(ids) == 1 and type(ids[0]) is str
                and (re.fullmatch(r'SABnzbd_nzo_[A-Za-z0-9_-]{1,100}', ids[0])
                     or _canonical_uuid(ids[0]))):
            return ids[0]
    raise AdapterUnavailable('Enqueue outcome unknown')


def _canonical_uuid(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


class Backend:
    def __init__(self, *, hydra_grant=None, sab_grant=None,
                 _open_store=store.open_store, _transport=None, job_options=None):
        self._grants = {'hydra': hydra_grant, 'sab': sab_grant}
        self._open_store = _open_store
        self.job_options = job_options
        self._client = Client(Permissions(search=True, enqueue=True),
                              LoopbackTransport() if _transport is None else _transport)

    @contextmanager
    def _credential(self, provider, actor_id, profile_id):
        handle = record = None
        try:
            grant = self._grants[provider]
            store.validate_metadata(grant, now=store.utc_now(), provider=provider)
            if (type(actor_id) is not int or type(profile_id) is not int
                    or (str(actor_id), str(profile_id)) !=
                    (grant.binding.actor_id, grant.binding.profile_id)):
                raise store.Refused('invalid_input')
            handle = self._open_store(provider=provider)
            if handle.context != store._provider_context(provider):
                raise store.Refused('invalid_input')
            store.lock_store(handle)
            record = store.read_record(handle)
            store.validate_metadata(record.metadata, now=store.utc_now(), provider=provider)
            if record.metadata.binding != grant.binding:
                raise store.Refused('invalid_input')
            key = store._secret_text(record.secret, provider=provider)
        except (store.Refused, OSError):
            if record is not None:
                record.close()
            if handle is not None:
                handle.close()
            raise Blocked('Acquisition authorization unavailable') from None
        try:
            yield key
        finally:
            key = None
            record.close()
            handle.close()

    def caps(self, *, actor_id, profile_id):
        with self._credential('hydra', actor_id, profile_id) as key:
            return self._client.caps(credential=key)

    def search(self, caps, query, media, *, actor_id, profile_id):
        # Validate the contract before releasing credentials.
        from shelf.acquisition_contract import search_request
        search_request(caps, query, media)
        with self._credential('hydra', actor_id, profile_id) as key:
            return PrivateSearch(self._client.search(caps, query, media, credential=key))

    def enqueue(self, snapshot, request, nzb, *, actor_id, profile_id):
        if (type(snapshot) is not Snapshot or type(request) is not PublicRequest
                or request.operation != 'addfile'
                or request.request_id != snapshot.request_id
                or (actor_id, profile_id) != (snapshot.actor_id, snapshot.profile_id)):
            raise Blocked('Matching confirmed request required')
        snapshot.check_selection(request.selection)
        ConfirmedSelection(**vars(request.selection))
        if type(nzb) is not bytes or not 0 < len(nzb) <= MAX_NZB_BYTES:
            raise ContractError('Invalid or oversized NZB payload')
        try:
            with self._credential('sab', actor_id, profile_id) as key:
                payload = self._client.enqueue(request.selection, request.request_id,
                                               nzb, credential=key, job_options=self.job_options)
            return ProviderJob(snapshot, _queue_id(payload))
        except ContractError:
            raise AdapterUnavailable('Enqueue unavailable; outcome unknown') from None

    def retrieve(self, guid, *, actor_id, profile_id):
        from shelf.nzb_parser import validate_nzb
        PublicRequest('get', (('id', guid),))
        with self._credential('hydra', actor_id, profile_id) as key:
            payload = self._client.retrieve(guid, credential=key)
        validate_nzb(payload)
        return payload

    def provider(self, *, actor_id, profile_id, snapshot, selection, nzb):
        """Bind exact selected bytes/evidence to Pipeline's existing Provider port."""
        if type(snapshot) is not Snapshot:
            raise Blocked('Matching confirmed request required')
        snapshot.check_selection(selection)
        ConfirmedSelection(**vars(selection))
        return _SelectedProvider(self, actor_id, profile_id, snapshot, selection, nzb)


class _SelectedProvider(store._Private):
    def __init__(self, backend, actor_id, profile_id, snapshot, selection, nzb):
        self._backend = backend
        self._actor_id, self._profile_id = actor_id, profile_id
        self._snapshot, self._selection, self._nzb = snapshot, selection, nzb

    def enqueue(self, snapshot, request):
        if snapshot != self._snapshot or request.selection != self._selection:
            raise Blocked('Selected result identity mismatch')
        return self._backend.enqueue(snapshot, request, self._nzb,
                                     actor_id=self._actor_id, profile_id=self._profile_id)

    def lookup(self, snapshot):
        raise AdapterUnavailable('Manual reconciliation required')

    def poll(self, job):
        raise AdapterUnavailable('Monitoring unsupported')

    def cancel(self, job):
        raise AdapterUnavailable('Cancellation unsupported')
