"""Private bookmark projections, never event content or current source evidence.

Callers supply reviewed descriptors and current restrictions under their existing
access barrier. This module performs no I/O and never mutates supplied history.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import re

from shelf.disclosure import canonical_bytes
from shelf.discovery_sources import Candidate
from shelf.event_sources import EventCandidate, OCCURRENCE_BASES

SCHEMA = 'event-saved-reference/v1'
FIELDS = frozenset(('schema', 'source_id', 'official_url', 'occurrence_ref',
                    'occurrence_basis', 'retention_policy_ref'))
PROVENANCE = frozenset(('live-automated', 'browser-observed', 'supplied-snapshot'))


@dataclass(frozen=True)
class ReferenceCandidate(Candidate):
    """An opaque human-history identity with separately checked presentation."""
    saved_reference: dict | None = None

    def freshness(self, now):
        return 'Stale — source details expired; current details unverified'


def _policy(source_id, descriptors, denied_namespaces):
    descriptor = descriptors.get(source_id) if type(source_id) is str else None
    if (descriptor is None or not descriptor.implemented
            or not descriptor.reference_policy_ref or not descriptor.restriction_namespace
            or descriptor.restriction_namespace in denied_namespaces
            or descriptor.reference_url_validator is None):
        return None
    return descriptor


def validate_reference(reference, *, key, mode, provenance, descriptors, denied_namespaces=()):
    """Return a detached valid link, or None without affecting human state."""
    try:
        if (type(reference) is not dict or set(reference) != FIELDS
                or len(canonical_bytes(reference)) > 3072 or mode != 'household'
                or provenance not in PROVENANCE or reference['schema'] != SCHEMA):
            return None
        source_id = reference['source_id']
        if not isinstance(source_id, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', source_id):
            return None
        descriptor = _policy(source_id, descriptors, denied_namespaces)
        if descriptor is None or reference['retention_policy_ref'] != descriptor.reference_policy_ref:
            return None
        url = reference['official_url']
        if (type(url) is not str or len(url.encode('utf-8')) > 2048
                or descriptor.reference_url_validator(url) != url):
            return None
        ref, basis = reference['occurrence_ref'], reference['occurrence_basis']
        if ref is not None:
            if (type(ref) is not str or not re.fullmatch(r'v1:[0-9a-f]{64}', ref)
                    or basis not in OCCURRENCE_BASES):
                return None
            expected = 'official:event:' + sha256(f'occurrence-v1\0{source_id}\0{ref}'.encode()).hexdigest()
            if key != expected:
                return None
        elif basis is not None:
            return None
        elif source_id == 'pbs':
            if key != 'official:event:' + sha256(('pbs\0url\0' + url).encode()).hexdigest():
                return None
        elif source_id == 'rrr':
            # The original numeric source ID is absent from a bookmark. Do not
            # invent it from a URL; the server-owned writer checked it at save.
            if not re.fullmatch(r'official:event:[0-9a-f]{64}', key):
                return None
        elif source_id == 'moshtix':
            if key != 'moshtix:event:' + url.rsplit('/', 1)[-1]:
                return None
        else:
            return None  # These adapters already require occurrence identity.
        return deepcopy(reference)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return None


def make_reference(card, *, mode, descriptors, denied_namespaces=()):
    """Derive only from a current server-owned event, never a stale placeholder."""
    if (not isinstance(card, EventCandidate) or not card.evidence
            or card.provenance not in PROVENANCE):
        return None
    source_id = card.source_id
    descriptor = _policy(source_id, descriptors, denied_namespaces)
    if descriptor is None or card.url != card.official_url:
        return None
    if any(e.source_id != source_id or e.provenance not in PROVENANCE
           or e.official_url != card.official_url
           or e.occurrence_ref != card.occurrence_ref
           or e.occurrence_basis != card.occurrence_basis for e in card.evidence):
        return None
    if card.occurrence_ref is None:
        if source_id == 'rrr' and card.key != 'official:event:' + sha256(
                f'rrr\0id\0{card.source_event_id}'.encode()).hexdigest():
            return None
    return validate_reference(dict(schema=SCHEMA, source_id=source_id,
        official_url=card.official_url, occurrence_ref=card.occurrence_ref,
        occurrence_basis=card.occurrence_basis, retention_policy_ref=descriptor.reference_policy_ref),
        key=card.key, mode=mode, provenance=card.provenance,
        descriptors=descriptors, denied_namespaces=denied_namespaces)


def project_references(history, *, mode, descriptors, denied_namespaces=()):
    """Latest matching retained reference per key; conflicts suppress the link.

    Never backfill from card content or another event's URL. A malformed entry
    cannot remove the user's choice/save state; only its optional link is hidden.
    """
    output, identities, conflicts = {}, {}, set()
    for event in history:
        reference = event.get('saved_reference')
        if reference is None:
            continue
        key = event.get('key')
        checked = validate_reference(reference, key=key, mode=event.get('mode'),
            provenance=event.get('source', {}).get('provenance'),
            descriptors=descriptors, denied_namespaces=denied_namespaces)
        if (checked is None or event.get('mode') != mode
                or event.get('origin') != 'authenticated-human-post'
                or event.get('operation') not in ('respond', 'save')):
            conflicts.add(key)
            continue
        identity = (checked['source_id'], checked['occurrence_ref'], checked['occurrence_basis'], mode)
        if key in identities and identities[key] != identity:
            conflicts.add(key)
        identities[key] = identity
        output[key] = checked
    return {key: value for key, value in output.items() if key not in conflicts}


def redact_history(history, *, source_namespaces, descriptors):
    """Remove ONLY new links from every affected action, including undone ones.

    Raw recovery callers can use this before replay/restore without importing
    Django. Recognition uses fixed descriptor namespaces, never labels or URLs.
    """
    affected = {source.source_id for source in descriptors.values()
                if source.restriction_namespace in source_namespaces}
    output = deepcopy(history)
    for event in output:
        reference = event.get('saved_reference')
        if type(reference) is dict and reference.get('source_id') in affected:
            event.pop('saved_reference')
    return output


def referenced_namespaces(history, *, descriptors):
    """Recognize fixed namespaces present even when no FeedbackTarget exists."""
    return frozenset(descriptors[ref['source_id']].restriction_namespace
        for event in history if type(ref := event.get('saved_reference')) is dict
        and type(ref.get('source_id')) is str and ref['source_id'] in descriptors
        and descriptors[ref['source_id']].restriction_namespace)
