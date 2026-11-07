"""Explicit rehearsal providers. No browser, payment, contacts or calendar I/O."""
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
import re

from shelf.acquisition_contract import Blocked, ContractError
from shelf.calendar_handoff import CalendarAdapter, CalendarTarget, Event, Recipient, digest, instant
from shelf.ticket_handoff import TicketOffer

TARGET = CalendarTarget('fixture-owner-calendar', 'SYNTHETIC owner calendar')
CONTACTS = (
    Recipient('fixture-alex-personal', 'Alex Example · personal (fictional)', 'alex.personal@example.invalid'),
    Recipient('fixture-alex-work', 'Alex Example · work (fictional)', 'alex.work@example.invalid'),
    Recipient('fixture-sam', 'Sam Example (fictional)', 'sam@example.invalid'),
)
SCENARIOS = ('confirmed', 'failed', 'unknown', 'unknown_then_confirmed')


def sessions():
    return [Event('fixture-concert', f'fixture-session-{day}', 'SYNTHETIC concert — rehearsal only',
        f'2027-01-{day}T20:00:00+11:00', f'2027-01-{day}T22:00:00+11:00',
        'Australia/Melbourne', 'SYNTHETIC venue', 'https://events.example.invalid/concert',
        'Fictional official-source fixture', status='scheduled', source_verified=True)
        for day in (15, 16)]


def session(key):
    for event in sessions():
        if event.session_id == key:
            return event
    raise Blocked('Choose an exact available rehearsal session.')


def resolve_contacts(keys):
    if not isinstance(keys, (list, tuple)) or len(keys) > 10 or len(set(keys)) != len(keys):
        raise Blocked('Choose each resolved mailbox at most once.')
    by_id = {r.recipient_id: r for r in CONTACTS}
    if any(key not in by_id for key in keys):
        raise Blocked('Recipient unresolved. A name is not a mailbox selection.')
    return tuple(by_id[key] for key in keys)


@dataclass(frozen=True)
class Quote:
    offer: TicketOffer
    unit_price: str
    fees: str
    refund_terms: str
    reference: str
    quoted_at: str
    expires_at: str

    def __post_init__(self):
        for value in (self.unit_price, self.fees):
            if type(value) is not str or not re.fullmatch(r'(0|[1-9][0-9]{0,8})(\.[0-9]{1,4})?', value):
                raise ContractError('Invalid quote amount.')
        if Decimal(self.offer.total) != Decimal(self.unit_price) * self.offer.quantity + Decimal(self.fees):
            raise ContractError('Quote total must include all displayed fees.')
        if (type(self.refund_terms) is not str or not self.refund_terms or len(self.refund_terms) > 1000
                or type(self.reference) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', self.reference)):
            raise ContractError('Refund terms and quote reference required.')
        if instant(self.expires_at) <= instant(self.quoted_at):
            raise ContractError('Invalid quote validity window.')

    def current(self, now):
        return instant(self.quoted_at) <= now < instant(self.expires_at)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        offer = dict(data['offer']); offer['event'] = Event(**offer['event'])
        return cls(**dict(data, offer=TicketOffer(**offer)))


class UnconnectedProvider:
    def quote(self, *args, **kwargs):
        raise Blocked('Live session, seller quote and booking-evidence adapters are not connected.')


class RehearsalProvider(CalendarAdapter):
    def packet_email_evidence(self, receipt, scenario, *, reconcile=False):
        from shelf.outing_plans import OWNER_MAILBOX
        if (receipt.get('kind')!='packet_email' or receipt['payload'].get('fixture') is not True
                or receipt['payload']['to'] not in {OWNER_MAILBOX, *[r.address for r in CONTACTS]}
                or scenario not in SCENARIOS):
            raise Blocked('Rehearsal email cannot address a real mailbox.')
        status=('confirmed' if reconcile else 'unknown') if scenario=='unknown_then_confirmed' else scenario
        return {'fixture':True,'key':receipt['key'],'payload_digest':receipt['payload']['digest'],
            'state':status,'reference':'fixture-email-'+digest(receipt['key'])[:24]}

    def quote(self, event, quantity, now):
        if event not in sessions() or type(quantity) is not int or not 1 <= quantity <= 10:
            raise Blocked('Exact rehearsal session and quantity required.')
        now = now.replace(microsecond=0)
        total = f'{Decimal("40.00") * quantity + Decimal("4.50"):.2f}'
        offer = TicketOffer(event, quantity, 'AUD', total, 'https://seller.example.invalid/checkout',
                            'SYNTHETIC seller', seller_verified=True, shop_eligibility='eligible')
        return Quote(offer, '40.00', '4.50',
            'SYNTHETIC terms: non-refundable except organizer cancellation; refund is not automatic.',
            'fixture-quote-' + digest([event.session_id, quantity, now.isoformat()])[:24],
            now.isoformat(), (now + timedelta(minutes=10)).isoformat())

    def purchase_evidence(self, ticket, scenario, attempt):
        if scenario not in SCENARIOS:
            raise Blocked('Unknown rehearsal response scenario.')
        status = scenario
        if scenario == 'unknown_then_confirmed':
            status = 'unknown' if attempt == 0 else 'confirmed'
        return {'status': status, 'reference': 'fixture-order-' + ticket.request_id,
                'offer_digest': ticket.approval_digest, 'fixture': True}

    def check(self, receipt):
        if receipt.target != TARGET or (receipt.recipient and receipt.recipient not in CONTACTS):
            raise Blocked('Rehearsal cannot address a real calendar or recipient.')

    def calendar_evidence(self, calendar, receipt, scenario, *, reconcile=False):
        self.check(receipt)
        if scenario not in SCENARIOS:
            raise Blocked('Unknown rehearsal calendar scenario.')
        status = ('confirmed' if reconcile else 'unknown') if scenario == 'unknown_then_confirmed' else scenario
        return {'status': status, 'event_id': 'fixture-event-' + digest(calendar.uid)[:24],
                'reference': 'fixture-calendar-' + digest([calendar.uid, receipt.key])[:24],
                'fixture': True, 'rsvp': 'unknown'}


def provider(mode):
    # Account tools in the agent session do not confer backend credentials/grants.
    return RehearsalProvider() if mode == 'sample' else UnconnectedProvider()
