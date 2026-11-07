"""Official Halftix navigation only; publisher permission is not established.

The listing is public, but the publisher restricts electronic retrieval-system
storage. Do not turn visibility, a robots response or a manual visit into consent
to ingest its listings. No transport or event-normalization path is enabled.
"""
from shelf.discovery_sources import Outcome

TERMS_URL = 'https://halftixmelbourne.com/website-terms-and-conditions/'
DISCLAIMER_URL = 'https://halftixmelbourne.com/website-disclaimer/'
FAQ_URL = 'https://halftixmelbourne.com/faqs/'


def fetch_listing(now):
    """Keep the shared Outcome interface without pretending to read inventory."""
    return Outcome('permission-required', message=(
        'Halftix is an official-source link only. Automatic listing reads and storage '
        'need publisher permission. Check its site for the exact session, offered '
        'price, full-price comparison, fees, availability and booking conditions. '
        'Discounts vary; not every offer is half price.'))
