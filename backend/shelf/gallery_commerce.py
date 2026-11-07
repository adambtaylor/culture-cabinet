"""Pure offer screening. A passing cost check never authorizes a purchase."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

COMPONENTS = ('artwork', 'shipping', 'framing', 'fees', 'tax', 'installation', 'fx')
OFFER_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class PriceGate:
    qualified: bool
    total_aud: str | None
    reasons: tuple[str, ...]
    purchase_authorized: bool = False


def _cents(value):
    """Reject sub-cent amounts rather than round them across the strict ceiling."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    if len(str(value)) > 100:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0 or abs(amount.as_tuple().exponent) > 100:
        return None
    numerator, denominator = amount.as_integer_ratio()
    cents, remainder = divmod(numerator * 100, denominator)
    return cents if not remainder else None


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def evaluate_offer(offer, *, now):
    """Require an exact current available AUD offer and every cost's source basis.

    `included` means already counted in the artwork price; `not_required` must
    explain why the cost is unnecessary for this exact object and destination.
    Callers retain the source record; this function neither fetches nor purchases.
    """
    if not isinstance(offer, dict):
        return PriceGate(False, None, ('Exact offer unknown.',))
    reasons = []
    if offer.get('currency') != 'AUD':
        reasons.append('Final offer currency must be AUD; conversion cannot be assumed.')
    if offer.get('availability') != 'available':
        reasons.append('Exact offer availability is not confirmed available.')
    if not all(_text(offer.get(key)) for key in ('offer_id', 'offer_url')):
        reasons.append('Exact offer identity and source URL are required.')
    try:
        checked = datetime.fromisoformat(offer.get('checked_at', '').replace('Z', '+00:00'))
        current = (checked.tzinfo is not None and isinstance(now, datetime)
                   and now.tzinfo is not None and timedelta(0) <= now - checked <= OFFER_TTL)
    except (ValueError, TypeError, AttributeError):
        current = False
    if not current:
        reasons.append('Exact offer check is missing, future dated or stale; recheck the seller.')
    components = offer.get('components')
    if not isinstance(components, dict):
        components = {}
    total = 0
    costs_complete = True
    for name in COMPONENTS:
        component = components.get(name)
        valid = isinstance(component, dict)
        if valid:
            status = component.get('status')
            valid = (_text(component.get('source_basis'))
                     and component.get('currency', 'AUD') == 'AUD')
            if status == 'known':
                amount = _cents(component.get('amount'))
                valid = valid and amount is not None
            elif status in ('included', 'not_required') and name != 'artwork':
                amount = 0
                valid = valid and ('amount' not in component or _cents(component['amount']) == 0)
            else:
                valid = False
        if not valid:
            costs_complete = False
            reasons.append(f'{name.capitalize()} cost or explicit inclusion/non-requirement basis unknown or invalid.')
        else:
            total += amount
    # Unrecognised costs must never disappear silently from the delivered total.
    if set(components) - set(COMPONENTS):
        costs_complete = False
        reasons.append('Unrecognised cost components require reconciliation into the total.')
    total_aud = f'{total // 100}.{total % 100:02d}' if costs_complete else None
    if costs_complete and total >= 250000:
        reasons.append('All-in total must be strictly below AUD 2500; exactly AUD 2500 fails.')
    qualified = not reasons
    if qualified:
        reasons.append('Current stated all-in costs are below AUD 2500; exact purchase authorization is still required.')
    return PriceGate(qualified, total_aud, tuple(reasons))
