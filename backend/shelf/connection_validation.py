"""Owner-bound neutral validation and current connection readiness component.

No personal Run, grants, source collection or recommendation publication is
implemented here. An isolated fake can exercise the lifecycle but never makes a
normal connection ready. Restored/process-old receipts have no live deadline.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import uuid

from django.db import IntegrityError, transaction
from django.views.decorators.debug import sensitive_variables

from .disclosure import canonical_bytes
from .inference_adapters import BUILD, CAPABILITY_CONTRACT, MODEL, OUTPUT_CONTRACT, REASONING, HostedResponsesAdapter, probe_body
from .inference_transport import ORIGIN, PATH, POLICY_REVISION, CheckedTransport, ProbePermit, TransportFault
from .models import PortableConnection, PortableProbeAttempt, PortableValidation
from .portable_authority import AuthorityFault


def _digest(value):
    return hashlib.sha256(value).hexdigest()


@dataclass
class _Preview:
    ref: uuid.UUID
    connection_id: uuid.UUID
    session_ref: str
    config_revision: int
    config_digest: str
    body_digest: str
    deadline: float


class ConnectionValidation:
    def __init__(self, authority, vault):
        if vault.authority is not authority:
            raise AuthorityFault('AUTHORITY_DENIED')
        self.authority, self.vault = authority, vault
        self.adapter = HostedResponsesAdapter()
        self._lock = authority._lock
        self._previews = {}
        self._deadlines = {}

    def _proof_kind(self):
        """Classify the trusted backend implementation, never request-supplied proof.

        Exact types keep isolated adapters/subclasses from claiming normal readiness.
        This is not independent model attestation or protection from arbitrary code
        monkeypatching inside this trusted process.
        """
        return ('real_transport_contract' if type(self) is ConnectionValidation
                and type(self.adapter) is HostedResponsesAdapter
                and type(self.adapter.transport) is CheckedTransport else 'isolated_test_fake')

    def _current(self, ref, connection_id, revision):
        """Recheck current authority without renewing owner idle activity.

        Explicit preview/confirmation count as owner actions; polling and dispatch
        do not. Config, credential, policy, owner, process and session changes stale
        a binding; validation_generation separately identifies the latest attempt.
        """
        session = self.authority.authorize(ref, touch=False)
        row = self.vault._connection(connection_id, revision)
        description = self.vault.describe(ref, connection_id, revision)
        config = {'adapter': row.adapter, 'protocol': 'responses', 'model': row.model,
            'reasoning': row.reasoning, 'origin': ORIGIN, 'path': PATH, 'config_revision': row.config_revision,
            'credential_generation': row.credential_generation, 'adapter_build': BUILD,
            'capability_contract': CAPABILITY_CONTRACT, 'output_contract': OUTPUT_CONTRACT,
            'transport_policy_revision': POLICY_REVISION, 'max_output_tokens': 2048}
        if (row.adapter != 'openai_responses' or row.model != MODEL or row.reasoning != REASONING):
            raise AuthorityFault('CAPABILITY_UNAVAILABLE', 503)
        binding = dict(config, schema='ConnectionValidation/v1',
            installation_id=str(self.authority.boundary.installation_id), owner_id=session.owner_id,
            owner_revision=session.owner_revision, session_id=ref,
            process_generation=str(self.authority.boundary.process_generation),
            connection_id=str(row.pk), validation_generation=row.validation_generation,
            config_digest=_digest(canonical_bytes(config)))
        return row, binding, description

    def _blocked(self):
        """Uncertainty blocks the installation, even after key/config replacement.

        A crash may leave a durable prepared/sending record without a known remote
        outcome. It is not evidence that nothing ran. Reconciliation is a separate,
        unimplemented operation; restart or a new connection must not clear this.
        """
        return PortableProbeAttempt.objects.filter(connection__installation_id=self.authority.boundary.installation_id,
            state__in=['prepared', 'sending', 'unknown']).exists()

    def preview(self, ref, connection_id, revision):
        with self._lock:
            self.authority.authorize(ref, sensitive=True)
            row, binding, description = self._current(ref, connection_id, revision)
            if not description['configured']:
                raise AuthorityFault('CREDENTIAL_REQUIRED', 503)
            if self._blocked():
                raise AuthorityFault('UNKNOWN_RECONCILIATION_REQUIRED', 409)
            body = probe_body()
            preview = _Preview(uuid.uuid4(), row.pk, ref, revision, binding['config_digest'],
                               _digest(body), self.authority.monotonic() + 300)
            self._previews = {key: value for key, value in self._previews.items()
                              if value.deadline > self.authority.monotonic()}
            if len(self._previews) >= 32:
                raise AuthorityFault('LIMIT_EXCEEDED', 422)
            self._previews[preview.ref] = preview
            return {'preview_ref': str(preview.ref), 'body': body.decode('utf-8'),
                    'body_digest': preview.body_digest, 'destination': ORIGIN + PATH,
                    'max_output_tokens': 2048, 'maximum_sends': 1, 'retry_sends': 0,
                    'requires_billable_confirmation': True}

    def _attempt_status(self, attempt):
        return {'attempt_ref': str(attempt.pk), 'state': attempt.state, 'outcome': attempt.outcome,
                'retry_allowed': False}

    @sensitive_variables()
    def confirm(self, ref, preview_ref, intent_ref, confirmed):
        """Commit one reservation before any possible send; replay is read-only.

        BoundRoot excludes a second live process and the authority's shared lock
        serializes service writers. Commit while locked, then release the lock for
        network work: a database rollback cannot undo a remote request. Replaying
        an intent returns history, not fresh authority or connection readiness.
        """
        if type(preview_ref) is not uuid.UUID or type(intent_ref) is not uuid.UUID or confirmed is not True:
            raise AuthorityFault('CONTRACT_INVALID', 422)
        with self._lock:
            session = self.authority.authorize(ref, sensitive=True)
            old = PortableProbeAttempt.objects.select_related('connection').filter(pk=intent_ref).first()
            if old is not None:
                if (old.owner_id != session.owner_id
                        or old.connection.installation_id != self.authority.boundary.installation_id):
                    raise AuthorityFault('AUTHORITY_DENIED')
                if old.preview_ref != preview_ref:
                    raise AuthorityFault('IDEMPOTENCY_CONFLICT', 409)
                return self._attempt_status(old)
            preview = self._previews.get(preview_ref)
            if (preview is None or preview.session_ref != ref
                    or self.authority.monotonic() >= preview.deadline):
                raise AuthorityFault('PREVIEW_STALE', 409)
            try:
                row, binding, description = self._current(ref, preview.connection_id, preview.config_revision)
            except AuthorityFault as fault:
                if fault.code == 'REVISION_CONFLICT':
                    raise AuthorityFault('PREVIEW_STALE', 409) from None
                raise
            body = probe_body()
            if (not description['configured'] or binding['config_digest'] != preview.config_digest
                    or _digest(body) != preview.body_digest):
                raise AuthorityFault('PREVIEW_STALE', 409)
            with transaction.atomic():
                if self._blocked():
                    raise AuthorityFault('UNKNOWN_RECONCILIATION_REQUIRED', 409)
                row.validation_generation += 1
                row.save(update_fields=['validation_generation'])
                binding['validation_generation'] = row.validation_generation
                try:
                    attempt = PortableProbeAttempt.objects.create(id=intent_ref, connection=row,
                        owner_id=session.owner_id, preview_ref=preview_ref, bindings=binding,
                        body_digest=preview.body_digest, state='sending', outcome='unknown', created_at=self.authority.now())
                except IntegrityError:
                    raise AuthorityFault('IDEMPOTENCY_CONFLICT', 409) from None
            self._previews.pop(preview_ref, None)

        @sensitive_variables()
        def credential():
            with self._lock:
                row, current, description = self._current(ref, preview.connection_id, preview.config_revision)
                if (current != binding or not description['configured']
                        or not PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').exists()):
                    raise AuthorityFault('VALIDATION_STALE', 409)
                return self.vault.resolve(ref, row.pk, row.config_revision)

        try:
            observation = self.adapter.generate(body, ProbePermit(preview.body_digest, credential))
        except TransportFault as fault:
            PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').update(
                state='unknown' if fault.unknown else 'failed', outcome=fault.outcome)
        except AuthorityFault:
            PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').update(state='failed', outcome='stale')
        except Exception:
            # The reservation remains unknown after any unexpected adapter failure;
            # no automatic resend is possible. Diagnostic frames are redacted.
            PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').update(state='unknown', outcome='unknown')
            raise
        else:
            try:
                with self._lock, transaction.atomic():
                    row, current, description = self._current(ref, preview.connection_id, preview.config_revision)
                    if current != binding or not description['configured']:
                        PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').update(state='failed', outcome='stale')
                    else:
                        now = self.authority.now()
                        # Never outlive the lease/session or the 15-minute proof cap.
                        # UTC expiry is durable/displayable; the process-local monotonic
                        # deadline prevents wall-clock rollback from extending authority.
                        expires = min(now + timedelta(minutes=15), description['usable_until'])
                        PortableValidation.objects.create(attempt=attempt, bindings=binding,
                            proof_kind=self._proof_kind(), observed_model=observation['observed_model'],
                            tested_at=now, expires_at=expires, observation_limits=observation['observation_limits'])
                        self._deadlines[attempt.pk] = self.authority.monotonic() + min(900, (expires - now).total_seconds())
                        PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').update(state='completed', outcome='passed')
            except AuthorityFault:
                PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').update(state='failed', outcome='stale')
                raise
            except Exception:
                PortableProbeAttempt.objects.filter(pk=attempt.pk, state='sending').update(state='unknown', outcome='unknown')
                raise
        attempt.refresh_from_db()
        return self._attempt_status(attempt)

    def status(self, ref, connection_id, revision):
        """Read the current generation; wall timestamps never choose authority."""
        with self._lock:
            row, binding, description = self._current(ref, connection_id, revision)
            # A later known failure must outrank any historical successful proof.
            # Generation is monotone even when the wall clock moves backwards.
            attempt = PortableProbeAttempt.objects.filter(connection=row,
                bindings__validation_generation=row.validation_generation).first()
            proof = PortableValidation.objects.filter(attempt=attempt).first() if attempt else None
            current = attempt is not None and attempt.bindings == binding
            blocked = self._blocked()
            # Restored/process-old receipts lack a live monotonic deadline. Keeping
            # both clocks is intentional: durable metadata alone cannot revive proof.
            valid = (current and attempt.state == 'completed' and proof is not None
                     and proof.bindings == binding and description['configured']
                     and self.authority.now() < proof.expires_at
                     and self.authority.monotonic() < self._deadlines.get(proof.pk, 0)
                     and proof.proof_kind == 'real_transport_contract' and self._proof_kind() == 'real_transport_contract'
                     and not blocked)
            if blocked:
                outcome = 'unknown'
            elif not description['configured']:
                outcome = 'missing_credential'
            elif not current:
                historical = attempt is not None or PortableProbeAttempt.objects.filter(connection=row).exists()
                outcome = 'stale' if historical else 'unconfigured'
            elif attempt.state == 'failed':
                outcome = attempt.outcome
            elif valid:
                outcome = 'passed'
            elif proof is not None and proof.proof_kind != 'real_transport_contract':
                outcome = 'isolated_test_only'
            else:
                outcome = 'stale'
            return {'connection_ref': str(row.pk), 'revision': row.config_revision,
                'config': {'adapter': row.adapter, 'model': row.model, 'reasoning': row.reasoning},
                'credential_configured': description['configured'], 'connection_ready': bool(valid),
                'outcome': outcome, 'proof_kind': proof.proof_kind if proof else None,
                'expires_at': proof.expires_at.isoformat() if proof else None,
                'run_ready': False}
