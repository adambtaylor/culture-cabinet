"""Exact ticket approval and conservative receipts, with no payment transport.

The caller owns authentication and atomic durable storage. Persist a reservation
before exposing the human final-click handoff; an unknown outcome never retries.
No card, payment credential, ticket barcode or pass bytes enter this contract.
"""
from dataclasses import asdict, dataclass, field, fields, replace
import re
from urllib.parse import urlsplit

from shelf.acquisition_contract import Blocked, ContractError, _text
from shelf.calendar_handoff import Event, digest, https_url, identifier


@dataclass(frozen=True)
class TicketOffer:
    event: Event
    quantity: int
    currency: str
    total: str
    official_seller_url: str
    seller_label: str
    seller_verified: bool = False
    shop_eligibility: str = 'unknown'

    def __post_init__(self):
        if type(self.event) is not Event or type(self.quantity) is not int or not 1 <= self.quantity <= 100:
            raise ContractError('Exact event and positive ticket quantity required')
        if type(self.currency) is not str or not re.fullmatch(r'[A-Z]{3}', self.currency):
            raise ContractError('Explicit three-letter currency required')
        # Decimal text only: no binary floats, exponent notation, rounding or conversion.
        if type(self.total) is not str or not re.fullmatch(r'(0|[1-9][0-9]{0,8})(\.[0-9]{1,4})?', self.total):
            raise ContractError('Exact nonnegative total including all fees required')
        https_url(self.official_seller_url)
        _text(self.seller_label)
        if type(self.seller_verified) is not bool or self.shop_eligibility not in ('unknown', 'unsupported', 'eligible'):
            raise ContractError('Invalid seller verification or shop eligibility')

    def blockers(self):
        reasons = self.event.blockers()
        if self.event.status == 'tentative' or not self.event.source_verified:
            reasons.append('A planning hold is not a verified ticket session')
        if not self.seller_verified:
            reasons.append('Official seller unverified')
        if self.shop_eligibility != 'eligible':
            reasons.append('Shop eligibility ' + self.shop_eligibility + '; no fallback bypass')
        return reasons


@dataclass(frozen=True)
class WalletArtifact:
    """Trusted issuer-supplied URL only; availability is not user installation.

    The root must verify the issuer origin and provenance independently of the URL.
    Private delivery URLs are omitted from previews and object reprs.
    """
    kind: str
    url: str = field(repr=False)
    issuer_origin: str
    issuer_label: str
    issuer_verified: bool = False

    def __post_init__(self):
        if self.kind not in ('issuer_pkpass', 'apple_wallet_url') or self.issuer_verified is not True:
            raise Blocked('Trusted issuer Wallet artifact required')
        https_url(self.url, private=True)
        https_url(self.issuer_origin)
        origin = urlsplit(self.issuer_origin)
        link = urlsplit(self.url)
        if (origin.path not in ('', '/') or (origin.hostname, origin.port or 443) != (link.hostname, link.port or 443)):
            raise Blocked('Wallet link must match the independently trusted issuer origin')
        _text(self.issuer_label)

    @property
    def fingerprint(self):
        return digest(asdict(self))


class TicketAdapter:
    def prepare(self, offer):
        raise Blocked('Ticket adapter unconfigured; no purchase capability')


class ManualOfficialTicketAdapter(TicketAdapter):
    """Instructions only. This adapter cannot open a browser or click Pay."""
    def prepare(self, offer):
        if offer.blockers():
            raise Blocked('; '.join(offer.blockers()))
        return {'official_seller_url': offer.official_seller_url,
                'action': 'Human final click in the official seller remote desktop',
                'payment_actor': 'human', 'autonomous_payment': False,
                'instruction': 'Check this exact event, session, quantity, currency and full total. Stop if any detail changes.'}


@dataclass(frozen=True)
class TicketHandoff:
    actor_id: str
    request_id: str
    offer: TicketOffer
    revision: int = 0
    purchase_state: str = 'requested'
    approval_digest: str | None = None
    submission_outcome: str | None = None
    order_reference: str | None = field(default=None, repr=False)
    ticket_reference: str | None = field(default=None, repr=False)
    wallet_artifact: WalletArtifact | None = field(default=None, repr=False)
    wallet_add_confirmed: bool = False
    failure_reference: str | None = field(default=None, repr=False)

    def __post_init__(self):
        identifier(self.actor_id)
        identifier(self.request_id)
        if type(self.offer) is not TicketOffer or type(self.revision) is not int or self.revision < 0:
            raise ContractError('Invalid ticket intent')
        if self.purchase_state not in ('requested', 'approved', 'submitted', 'confirmed', 'failed'):
            raise ContractError('Invalid purchase state')
        if self.purchase_state == 'requested':
            if self.approval_digest is not None:
                raise ContractError('Requested purchase cannot retain approval')
        elif self.approval_digest != self.preview_digest:
            raise ContractError('Approval does not match exact ticket intent')
        expected = {'requested': None, 'approved': None, 'submitted': 'unknown', 'confirmed': 'confirmed', 'failed': 'failed'}
        if self.submission_outcome != expected[self.purchase_state]:
            raise ContractError('Invalid purchase outcome')
        if (self.order_reference is not None) != (self.purchase_state == 'confirmed'):
            raise ContractError('Confirmed purchase requires issuer order reference')
        for value in (self.order_reference, self.ticket_reference):
            if value is not None:
                identifier(value)
        if (self.failure_reference is not None) != (self.purchase_state == 'failed'):
            raise ContractError('Failed purchase requires verified failure evidence')
        if self.failure_reference is not None:
            identifier(self.failure_reference)
        if self.ticket_reference is not None and self.purchase_state != 'confirmed':
            raise ContractError('Ticket issuance requires confirmed purchase')
        if self.wallet_artifact is not None and (type(self.wallet_artifact) is not WalletArtifact or self.ticket_reference is None):
            raise ContractError('Wallet availability requires an issued ticket')
        if type(self.wallet_add_confirmed) is not bool or (self.wallet_add_confirmed and self.wallet_artifact is None):
            raise ContractError('Wallet addition requires an issuer artifact')

    def _actor(self, actor_id):
        if actor_id != self.actor_id:
            raise Blocked('Ticket actor mismatch')

    @property
    def active_key(self):
        """Caller storage must enforce one unresolved purchase per actor/session."""
        return digest(['ticket-v1', self.actor_id, self.offer.event.event_id,
                       self.offer.event.session_id])

    @property
    def preview_digest(self):
        return digest({'actor_id': self.actor_id, 'request_id': self.request_id,
                       'revision': self.revision, 'offer': asdict(self.offer)})

    def preview(self):
        return {'actor_id': self.actor_id, 'request_id': self.request_id,
                'revision': self.revision, 'digest': self.preview_digest, 'active_key': self.active_key,
                'event': self.offer.event.preview(), 'quantity': self.offer.quantity,
                'currency': self.offer.currency, 'total': self.offer.total,
                'total_label': f'{self.offer.currency} {self.offer.total} total including fees',
                'official_seller_url': self.offer.official_seller_url,
                'seller_label': self.offer.seller_label, 'source_label': self.offer.event.source_label,
                'shop_eligibility': self.offer.shop_eligibility, 'blockers': self.offer.blockers(),
                'purchase_state': self.purchase_state, 'submission_outcome': self.submission_outcome,
                'ticket_issued': self.ticket_reference is not None,
                'issuer_wallet_pass_available': self.wallet_artifact is not None,
                'user_wallet_add_confirmed': self.wallet_add_confirmed,
                'privacy_label': 'Private purchase intent; no payment credentials or barcodes stored',
                'payment_actor': 'human', 'autonomous_payment': False,
                'message': ('Submission reserved; outcome unknown. Reconcile the existing seller order; do not purchase again.'
                            if self.purchase_state == 'submitted' else 'No payment is performed by this app.')}

    def revise(self, offer, *, actor_id, expected_revision):
        self._actor(actor_id)
        if expected_revision != self.revision:
            raise Blocked('Stale ticket revision')
        if offer == self.offer:
            return self
        if self.purchase_state in ('submitted', 'confirmed', 'failed'):
            raise Blocked('Preserve existing purchase receipt; reconcile before a new intent')
        return replace(self, offer=offer, revision=self.revision + 1,
                       purchase_state='requested', approval_digest=None)

    def approve(self, *, actor_id, expected_revision, preview_digest, user_confirmed=False):
        self._actor(actor_id)
        if (user_confirmed is not True or expected_revision != self.revision
                or preview_digest != self.preview_digest):
            raise Blocked('Explicit approval of exact event/session/quantity/currency/total/seller required')
        if self.offer.blockers():
            raise Blocked('; '.join(self.offer.blockers()))
        if self.purchase_state != 'requested':
            return self
        return replace(self, purchase_state='approved', approval_digest=preview_digest)

    def reserve_submission(self, *, actor_id, current_offer, expected_revision,
                           preview_digest, human_final_click_authorized=False, adapter=None):
        """Return (receipt, instructions-or-None); save receipt BEFORE showing instructions.

        'submitted/unknown' is a conservative write-ahead reservation, not evidence
        of payment. Replays return no further final-click instructions. The root
        must serialize calls/CAS revisions and reuse the stored request ID.
        """
        self._actor(actor_id)
        if (current_offer != self.offer or expected_revision != self.revision
                or preview_digest != self.preview_digest):
            raise Blocked('Seller details changed; approval invalid')
        if self.purchase_state in ('submitted', 'confirmed', 'failed'):
            return self, None
        if human_final_click_authorized is not True or self.purchase_state != 'approved':
            raise Blocked('Separate human final-click authorization required')
        if current_offer.blockers():
            raise Blocked('; '.join(current_offer.blockers()))
        instructions = (adapter or TicketAdapter()).prepare(current_offer)
        return replace(self, purchase_state='submitted', submission_outcome='unknown'), instructions

    def confirm_purchase(self, *, actor_id, offer_digest, order_reference, issuer_verified=False):
        self._actor(actor_id)
        identifier(order_reference)
        if (issuer_verified is not True or offer_digest != self.approval_digest
                or self.purchase_state not in ('submitted', 'confirmed')):
            raise Blocked('Verified issuer confirmation for this exact purchase required')
        if self.order_reference is not None and self.order_reference != order_reference:
            raise Blocked('Conflicting issuer order reference')
        return replace(self, purchase_state='confirmed', submission_outcome='confirmed', order_reference=order_reference)

    def confirm_failure(self, *, actor_id, offer_digest, failure_reference, issuer_verified=False):
        """A definite issuer failure is evidence, not permission to purchase again."""
        self._actor(actor_id)
        identifier(failure_reference)
        if (issuer_verified is not True or offer_digest != self.approval_digest
                or self.purchase_state not in ('submitted', 'failed')):
            raise Blocked('Verified issuer failure for this exact purchase required')
        if self.failure_reference not in (None, failure_reference):
            raise Blocked('Conflicting failure evidence')
        return replace(self, purchase_state='failed', submission_outcome='failed',
                       failure_reference=failure_reference)

    def record_ticket_issued(self, *, actor_id, order_reference, ticket_reference, issuer_verified=False):
        self._actor(actor_id)
        identifier(ticket_reference)
        if (issuer_verified is not True or self.purchase_state != 'confirmed'
                or order_reference != self.order_reference):
            raise Blocked('Issuer ticket must belong to the confirmed order')
        if self.ticket_reference is not None and self.ticket_reference != ticket_reference:
            raise Blocked('Conflicting ticket issuance reference')
        return replace(self, ticket_reference=ticket_reference)

    def record_wallet_available(self, artifact, *, actor_id, ticket_reference):
        self._actor(actor_id)
        if type(artifact) is not WalletArtifact or self.ticket_reference is None or ticket_reference != self.ticket_reference:
            raise Blocked('Trusted Wallet artifact must belong to this issued ticket')
        if artifact == self.wallet_artifact:
            return self
        return replace(self, wallet_artifact=artifact, wallet_add_confirmed=False)

    def confirm_wallet_add(self, *, actor_id, artifact_digest, user_confirmed=False):
        self._actor(actor_id)
        if (user_confirmed is not True or self.wallet_artifact is None
                or artifact_digest != self.wallet_artifact.fingerprint):
            raise Blocked('Explicit user confirmation for this issuer Wallet artifact required')
        return replace(self, wallet_add_confirmed=True)

    def to_dict(self):
        """Private server storage; includes issuer references/Wallet delivery URL."""
        return {'schema': 2, **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        try:
            data = dict(value)
            # Only complete legacy schema-1 records may omit the newly added
            # failure evidence. A damaged current record must not gain defaults.
            if type(data.get('schema')) is int and data['schema'] == 1:
                if 'failure_reference' in data or data.get('purchase_state') == 'failed':
                    raise ValueError
                data['failure_reference'] = None
            if set(data) != {'schema', *(f.name for f in fields(cls))}:
                raise ValueError
            if type(data['schema']) is not int or data.pop('schema') not in (1, 2):
                raise ValueError
            offer = dict(data['offer'])
            offer['event'] = Event(**offer['event'])
            data['offer'] = TicketOffer(**offer)
            if data['wallet_artifact'] is not None:
                data['wallet_artifact'] = WalletArtifact(**data['wallet_artifact'])
            return cls(**data)
        except (KeyError, TypeError, ValueError):
            raise ContractError('Invalid stored ticket handoff') from None
