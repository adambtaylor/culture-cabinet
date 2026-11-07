"""Restaurant adapter seam and deterministic fiction. No network or personal data.

Availability is an expiring observation, not a reservation. Live adapters must
return exact branch/party/terms and reconcile an existing idempotency key.
"""
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from shelf.acquisition_contract import Blocked, ContractError
from shelf.calendar_handoff import digest, instant
from shelf.event_plan_provider import SCENARIOS, sessions

BRANCH = 'fixture-garden-carlton'


@dataclass(frozen=True)
class TableOffer:
    key: str
    branch_id: str
    branch: str
    address: str
    party_size: int
    start: str
    end: str
    timezone: str
    session_id: str
    observed_at: str
    expires_at: str
    availability: str
    vegetarian: bool
    menu_source: str
    booking_source: str
    currency: str
    deposit: str
    cancellation_terms: str
    basis: str = 'fictional_rehearsal'

    def __post_init__(self):
        # This seam is intentionally fail-closed: there is no live provider yet.
        if (self.basis != 'fictional_rehearsal' or self.branch_id != BRANCH
                or self.branch != 'SYNTHETIC Garden · Carlton branch'
                or self.address != 'Fictional Carlton address'
                or type(self.party_size) is not int or not 1 <= self.party_size <= 10
                or self.timezone != 'Australia/Melbourne' or type(self.vegetarian) is not bool
                or self.availability not in ('available', 'booked_out', 'unknown')
                or self.menu_source != 'https://restaurant.example.invalid/carlton/menu'
                or self.booking_source != 'https://restaurant.example.invalid/carlton/book'
                or self.currency != 'AUD' or self.deposit != f'{20 * self.party_size:.2f}'
                or self.cancellation_terms != 'SYNTHETIC terms: deposit credited to meal; AUD 40 per person for cancellation within 24 hours or no-show.'):
            raise ContractError('Exact fictional branch, party, menu and reservation terms required.')
        if (instant(self.end) <= instant(self.start)
                or not instant(self.observed_at) < instant(self.expires_at) <= instant(self.observed_at) + timedelta(minutes=5)
                or instant(self.expires_at) >= instant(self.start)
                or self.session_id not in {e.session_id for e in sessions()}):
            raise ContractError('Invalid table availability window.')
        values = asdict(self); values.pop('key')
        if self.key != digest(values):
            raise ContractError('Table offer identity changed.')

    def current(self, now):
        return instant(self.observed_at) <= now < instant(self.expires_at)

    def compatible(self, event, constraints):
        return (event.status == 'scheduled' and event.start is not None
            and event.session_id == self.session_id
            and self.party_size == constraints['party_size']
            and (not constraints['vegetarian'] or self.vegetarian)
            and instant(self.end) + timedelta(minutes=constraints['travel_minutes'] + constraints['buffer_minutes'])
                <= instant(event.start))


class RestaurantProvider:
    def search(self, constraints, now):
        raise Blocked('Live restaurant availability is not connected.')

    def reserve(self, offer, key, scenario):
        raise Blocked('Live restaurant reservations are not connected.')

    def reconcile(self, offer, key, scenario):
        raise Blocked('Live reservation evidence is not connected.')


class RehearsalRestaurantProvider(RestaurantProvider):
    def search(self, constraints, now):
        now = now.replace(microsecond=0)
        observations = []
        for event in sessions():
            date = event.start[:10]
            if not constraints['date_from'] <= date <= constraints['date_to']:
                continue
            # First-choice 18:30 on Friday is booked out; Saturday is available.
            for clock in ('17:30', '18:00', '18:30'):
                if not constraints['earliest'] <= clock <= constraints['latest']:
                    continue
                start = instant(f'{date}T{clock}:00+11:00')
                values = dict(branch_id=BRANCH, branch='SYNTHETIC Garden · Carlton branch',
                    address='Fictional Carlton address', party_size=constraints['party_size'],
                    start=start.isoformat(), end=(start + timedelta(minutes=constraints['meal_minutes'])).isoformat(),
                    timezone='Australia/Melbourne', session_id=event.session_id,
                    observed_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat(),
                    availability='booked_out' if date.endswith('15') and clock == '18:30' else 'available',
                    vegetarian=True, menu_source='https://restaurant.example.invalid/carlton/menu',
                    booking_source='https://restaurant.example.invalid/carlton/book', currency='AUD',
                    deposit=f'{Decimal(20) * constraints["party_size"]:.2f}',
                    cancellation_terms='SYNTHETIC terms: deposit credited to meal; AUD 40 per person for cancellation within 24 hours or no-show.',
                    basis='fictional_rehearsal')
                offer = TableOffer(key=digest(values), **values)
                observations.append(asdict(offer))
        return observations

    def _evidence(self, offer, key, scenario, *, checking):
        if scenario not in SCENARIOS:
            raise Blocked('Unknown reservation rehearsal scenario.')
        state = scenario
        if scenario == 'unknown_then_confirmed':
            state = 'confirmed' if checking else 'unknown'
        return {'state': state, 'key': key, 'offer_key': offer.key,
                'reference': 'fixture-reservation-' + key[:24], 'fixture': True}

    def reserve(self, offer, key, scenario):
        return self._evidence(offer, key, scenario, checking=False)

    def reconcile(self, offer, key, scenario):
        return self._evidence(offer, key, scenario, checking=True)


def provider(mode):
    return RehearsalRestaurantProvider() if mode == 'sample' else RestaurantProvider()
