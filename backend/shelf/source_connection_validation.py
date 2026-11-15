"""One explicit nonpersonal source check, separate from inference readiness."""
from dataclasses import asdict
from datetime import timedelta
import hashlib

from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from . import grant_authority as ga
from .card_actions import history_digest
from .models import PortableConnection, PortableJourneyCommand, PortableSourceCheckAttempt, SavedCredentialBinding
from .source_check_contract import SourceCheckOperation, parse_source_check
from .discovery_transport import media_authorized, MediaHostLimiter

PROVIDERS = {'tmdb': ('tmdb_movie', 'tmdb_tv'), 'hardcover': ('hardcover',)}


class SourceConnectionValidation:
    def __init__(self, authority, *, transport=media_authorized):
        ga._require(type(authority) is ga.PortableAuthority)
        self.authority, self.transport = authority, transport
        self.limiters = {provider: MediaHostLimiter(provider) for provider in PROVIDERS}
        self._proofs = {}

    def _rows(self, cabinet, provider):
        ga._require(type(provider) is str and provider in PROVIDERS, 'CONTRACT_INVALID', 422)
        rows = list(PortableConnection.objects.filter(installation_id=cabinet.installation_id,
            adapter__in=PROVIDERS[provider], removed=False).order_by('adapter', 'pk'))
        ga._require(rows and len({row.adapter for row in rows}) == len(rows), 'CREDENTIAL_REQUIRED', 503)
        return rows

    def _binding(self, session_ref, cabinet, provider, operation, rows):
        from .source_authority import _connection_metadata
        session = self.authority.authorize(session_ref, touch=False)
        bindings, ends = [], [ga._end(self.authority, session), self.authority.now()+timedelta(minutes=15)]
        for row in rows:
            row, _, status = _connection_metadata(self.authority, session_ref, str(row.pk))
            saved = SavedCredentialBinding.objects.filter(pk=row.pk, state='ACTIVE', removed=False,
                                                          remove_requested=False).first()
            ga._require(saved is not None and status.get('credential_available') is True,
                        'CREDENTIAL_REQUIRED', 503)
            if status.get('usable_until') is not None:
                ends.append(status['usable_until'])
            bindings.append(dict(connection_ref=str(row.pk),config_revision=row.config_revision,
                credential_generation=row.credential_generation,saved_generation=str(saved.generation),
                store_revision=saved.store_revision))
        return dict(schema='native-source-check-binding/v1',provider=provider,
            installation_id=str(cabinet.installation_id),actor_id=cabinet.actor_id,
            owner_revision=cabinet.owner_revision,profile_ref=str(cabinet.profile_ref),
            process_generation=str(self.authority.boundary.process_generation),
            session_digest=hashlib.sha256(session_ref.encode()).hexdigest(),
            operation_digest=operation.operation_digest,connections=bindings),min(ends)

    def _public(self, attempt, *, current=False):
        return dict(attempt_ref=str(attempt.pk),state=attempt.state,code=attempt.outcome.get('code','UNKNOWN'),
            test_scope=attempt.outcome.get('test_scope',''),test_current=current,
            observed_at=attempt.created_at.isoformat(),expires_at=attempt.expires_at.isoformat())

    def current(self, session_ref, *, profile_ref, provider):
        ga._require(type(provider) is str and provider in PROVIDERS, 'CONTRACT_INVALID',422)
        with ga.recovery_guard(self.authority,session_ref,profile_ref=profile_ref),self.authority._lock:
            cabinet,_=ga._bound(self.authority,session_ref,profile_ref)
            operation=SourceCheckOperation(provider)
            rows=self._rows(cabinet,provider)
            binding,_=self._binding(session_ref,cabinet,provider,operation,rows)
            attempts=PortableSourceCheckAttempt.objects.filter(cabinet=cabinet,
                operation_digest=operation.operation_digest).order_by('-created_at','-id')
            for attempt in attempts:
                live=(attempt.state=='passed' and attempt.binding==binding
                    and self.authority.now()<attempt.expires_at
                    and self.authority.monotonic()<self._proofs.get(str(attempt.pk),float('-inf')))
                if live:return self._public(attempt,current=True)
            attempt=attempts.first()
            if attempt:return {**self._public(attempt),'state':'stale' if attempt.state=='passed' else attempt.state}
            return dict(attempt_ref=None,state='not_tested',code='',test_scope=operation.test_scope,
                        test_current=False,observed_at=None,expires_at=None)

    @sensitive_variables()
    def check(self, session_ref, *, profile_ref, provider, command_id, confirmed):
        ga._require(type(provider) is str and provider in PROVIDERS, 'CONTRACT_INVALID',422)
        ga._require(confirmed is True, 'CONTRACT_INVALID',422);ga._uuid(command_id)
        operation=SourceCheckOperation(provider)
        descriptor=dict(profile_ref=profile_ref,provider=provider,operation_digest=operation.operation_digest)
        secret=None
        with ga.current_writer(self.authority,session_ref,profile_ref) as cabinet:
            prior=PortableJourneyCommand.objects.filter(cabinet=cabinet,command_id=command_id).first()
            if prior:
                ga._require(prior.kind=='source.check' and prior.descriptor==descriptor
                    and prior.descriptor_digest==history_digest(descriptor),'IDEMPOTENCY_CONFLICT',409)
                attempt=PortableSourceCheckAttempt.objects.filter(cabinet=cabinet,
                    pk=prior.result.get('attempt_ref')).first() if prior.result else None
                if attempt is None:attempt=PortableSourceCheckAttempt.objects.get(command=prior)
                return self._public(attempt)  # Historical receipt, never another send.
            ga._require(not PortableSourceCheckAttempt.objects.filter(cabinet=cabinet,
                operation_digest=operation.operation_digest,state__in=['sending','unknown']).exists(),
                'UNKNOWN_RECONCILIATION_REQUIRED',409)
            reusable=self.current(session_ref,profile_ref=profile_ref,provider=provider)
            if reusable['test_current']:
                with transaction.atomic(),ga._write_window():
                    PortableJourneyCommand.objects.create(cabinet=cabinet,command_id=command_id,
                        kind='source.check',descriptor=descriptor,descriptor_digest=history_digest(descriptor),
                        state='committed',result=reusable,created_at=self.authority.now())
                return reusable
            rows=self._rows(cabinet,provider)
            # Hydration is explicit work for this check, never a GET side effect.
            vault=self.authority._source_vault
            secret=bytearray(vault.resolve(session_ref,rows[0].pk,rows[0].config_revision))
            try:
                binding,end=self._binding(session_ref,cabinet,provider,operation,rows)
                # TMDB movie/TV may share a proof only after the SAME acknowledged
                # guided input produced both exact current durable generations.
                if provider=='tmdb' and len(rows)>1:
                    acknowledged=False
                    for save in PortableJourneyCommand.objects.filter(cabinet=cabinet,
                            kind='journey.provider.save',state='committed'):
                        if (save.descriptor.get('provider')=='tmdb'
                                and save.result.get('bindings')==binding['connections']):
                            acknowledged=True;break
                    ga._require(acknowledged,'CREDENTIAL_BINDING_REQUIRED',409)
                with transaction.atomic(),ga._write_window():
                    command=PortableJourneyCommand.objects.create(cabinet=cabinet,command_id=command_id,
                        kind='source.check',descriptor=descriptor,descriptor_digest=history_digest(descriptor),
                        state='reserved',created_at=self.authority.now())
                    attempt=PortableSourceCheckAttempt.objects.create(cabinet=cabinet,connection=rows[0],
                        command=command,binding=binding,operation_digest=operation.operation_digest,
                        state='sending',outcome={},created_at=self.authority.now(),expires_at=end)
                proof_end=self.authority.monotonic()+min(900,max(0,(end-self.authority.now()).total_seconds()))
                def current():
                    ga._bound(self.authority,session_ref,profile_ref,sensitive=True)
                    now_binding,now_end=self._binding(session_ref,cabinet,provider,operation,rows)
                    ga._require(now_binding==binding and self.authority.now()<min(end,now_end)
                        and self.authority.monotonic()<proof_end,'PREVIEW_STALE',409)
                    return True
                try:
                    current()
                    status,body,_=self.transport(operation,secret=secret,revalidate=current,
                        limiter=self.limiters[provider],response_bytes=262144,request_seconds=5,parent_seconds=6)
                    current()
                    parsed=parse_source_check(operation,status,body)
                    outcome=asdict(parsed);state=parsed.state
                except Exception:
                    # Once an intent is reserved an uncertain transport is not a
                    # license to resend. No provider text or bearer is retained.
                    state='unknown';outcome=dict(code='UNKNOWN',test_scope=operation.test_scope)
                with transaction.atomic(),ga._write_window():
                    attempt.state=state;attempt.outcome=outcome
                    attempt.save(update_fields=['state','outcome'])
                    result=self._public(attempt,current=state=='passed')
                    PortableJourneyCommand.objects.filter(pk=command.pk,state='reserved').update(
                        state='committed' if state!='unknown' else 'unknown',result=result)
                if state=='passed':self._proofs[str(attempt.pk)]=proof_end
                return result
            finally:
                if secret is not None:secret[:]=b'\0'*len(secret)
