"""Bounded owner-reported qualitative evidence; no provider identity or ratings.

Imports share the authored discovery state and its nonce ledger. The complete
observation, including provenance, participates in synthesis invalidation.
"""
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import re
import secrets
from urllib.parse import urlsplit

from django.db import transaction

from shelf import authored_discovery
from shelf.discovery_state import Conflict, public_authorized
from shelf.models import AuthoredDiscoveryState

MAX_PACKET_BYTES = 128 * 1024
MAX_PACKET_OBSERVATIONS = 100
MAX_OBSERVATIONS = 200
MAX_SIGNAL_VALUE = 1000
ENUMS = {
    'medium': ('book', 'movie', 'tv', 'animation', 'graphic_novel', 'creator', 'topic'),
    'reaction': ('like', 'dislike', 'mixed', 'relative_preference', 'selected_next',
                 'interested', 'planned', 'removed', 'assistant_suggestion'),
    'experience': ('reported_experience', 'completed', 'unknown', 'not_established'),
    'identity_status': ('confirmed', 'provisional', 'contextual', 'unresolved'),
}
FIELDS = {'id', 'title', 'raw_title', 'medium', 'reaction', 'experience',
          'identity_status', 'reasons', 'source_text', 'provenance', 'uncertainty'}


def _text(value, maximum, label, *, empty=False, multiline=False):
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value.strip())
            or any((ord(c) < 32 and not (multiline and c in '\n\r\t'))
                   or ord(c) == 127 for c in value)):
        raise ValueError('Invalid observation ' + label + '.')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate packet field.')
        result[key] = value
    return result


def validate_packet(packet, actor_id):
    """Validate an exact envelope and return detached, actor-bound observations.

    Wording is retained verbatim. A supplied title is never a canonical provider
    match; unresolved identities are projected using their raw title.
    """
    if type(actor_id) is not int or actor_id < 1:
        raise ValueError('Invalid capture actor.')
    if isinstance(packet, str):
        if len(packet.encode('utf-8')) > MAX_PACKET_BYTES:
            raise ValueError('Observation packet is too large.')
        try:
            packet = json.loads(packet, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('Invalid observation packet JSON.') from exc
    try:
        size = len(json.dumps(packet, ensure_ascii=False).encode('utf-8'))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid observation packet.') from exc
    if size > MAX_PACKET_BYTES:
        raise ValueError('Observation packet is too large.')
    if not isinstance(packet, dict) or set(packet) != {'observations'}:
        raise ValueError('Invalid observation packet fields.')
    rows = packet['observations']
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PACKET_OBSERVATIONS:
        raise ValueError('An observation packet needs 1–100 observations.')
    result, identities = [], set()
    for supplied in rows:
        if not isinstance(supplied, dict) or set(supplied) != FIELDS:
            raise ValueError('Invalid observation fields.')
        row = {key: _text(supplied[key], limit, key, empty=key == 'uncertainty',
                          multiline=key == 'source_text')
               for key, limit in [('id', 64), ('title', 240), ('raw_title', 240),
                                  ('source_text', 4000), ('uncertainty', 200)]}
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}', row['id']):
            raise ValueError('Invalid observation identity.')
        if row['id'] in identities:
            raise ValueError('Duplicate observation identity.')
        identities.add(row['id'])
        for key, choices in ENUMS.items():
            if not isinstance(supplied[key], str) or supplied[key] not in choices:
                raise ValueError('Invalid observation ' + key + '.')
            row[key] = supplied[key]
        if row['identity_status'] != 'confirmed' and not row['uncertainty'].strip():
            raise ValueError('Unconfirmed identity requires explicit uncertainty.')
        reasons = supplied['reasons']
        if not isinstance(reasons, list) or len(reasons) > 4:
            raise ValueError('Invalid observation reasons.')
        row['reasons'] = [_text(reason, 120, 'reason') for reason in reasons]
        provenance = supplied['provenance']
        required = {'source_id', 'observed_date', 'actor_origin', 'dossier_sha256'}
        if (not isinstance(provenance, dict) or not required <= set(provenance)
                or set(provenance) - required - {'source_url'}):
            raise ValueError('Invalid observation provenance fields.')
        source = {'source_id': _text(provenance['source_id'], 240, 'source id'),
                  'dossier_sha256': _text(provenance['dossier_sha256'], 64, 'dossier digest')}
        if not re.fullmatch(r'[a-f0-9]{64}', source['dossier_sha256']):
            raise ValueError('Invalid observation dossier digest.')
        origin = provenance['actor_origin']
        if origin not in ('owner', 'assistant', 'unknown'):
            raise ValueError('Invalid observation actor origin.')
        source['actor_origin'] = origin
        observed = provenance['observed_date']
        if observed is not None:
            if not isinstance(observed, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', observed):
                raise ValueError('Invalid observation date.')
            try:
                date.fromisoformat(observed)
            except ValueError as exc:
                raise ValueError('Invalid observation date.') from exc
        source['observed_date'] = observed
        if 'source_url' in provenance:
            url = _text(provenance['source_url'], 2048, 'source URL')
            try:
                parsed = urlsplit(url)
                valid = (parsed.scheme == 'https' and parsed.hostname == 'chatgpt.com'
                         and parsed.port in (None, 443) and not parsed.username and not parsed.password)
            except ValueError:
                valid = False
            if not valid or any(c.isspace() for c in url):
                raise ValueError('Observation source URL must be HTTPS ChatGPT.')
            source['source_url'] = url
        row.update(provenance=source, captured_by='user:' + str(actor_id))
        result.append(row)
    return result


def project(data):
    """Detached evidence with explicit origin exclusions for presentation."""
    result = []
    for row in deepcopy(data.get('observations', [])):
        reason = ('Assistant suggestion is not owner taste.' if row['reaction'] == 'assistant_suggestion'
                  else 'Owner origin is not established.' if row['provenance']['actor_origin'] != 'owner' else '')
        row.update(eligible=not reason, exclusion_reason=reason)
        result.append(row)
    return result


def signals(data):
    result = []
    for row in project(data):
        if not row['eligible']:
            continue
        summary = '; '.join((row['reaction'], 'experience: ' + row['experience'],
            'identity: ' + row['identity_status'], 'reasons: ' + ' | '.join(row['reasons']),
            'uncertainty: ' + (row['uncertainty'] or 'none reported')))
        if len(summary) > MAX_SIGNAL_VALUE:
            raise ValueError('Observation summary exceeds its bound.')
        result.append(dict(key='authored_observation:' + row['id'],
            title=row['title'] if row['identity_status'] == 'confirmed' else row['raw_title'],
            medium=row['medium'], source='Owner-authored qualitative observation',
            basis='authored_observation', value=summary))
    return result


def revision(data):
    return sha256(json.dumps(data.get('observations', []), sort_keys=True,
                            separators=(',', ':')).encode()).hexdigest()


def initial_nonce(actor_id, profile_id, mode='household'):
    return authored_discovery.initial_nonce(actor_id, profile_id, mode)


def apply(request, profile_id, fields):
    """Atomic bounded import with shared authored-state replay protection."""
    if (set(fields) != {'operation', 'mode', 'packet', 'revision', 'nonce'}
            or fields.get('operation') != 'observations_import' or fields.get('mode') != 'household'
            or not isinstance(fields.get('packet'), str) or type(fields.get('revision')) is not int
            or not isinstance(fields.get('nonce'), str) or not 1 <= len(fields['nonce']) <= 200):
        raise ValueError('Invalid observation import form.')
    public_authorized(request, profile_id)
    incoming = validate_packet(fields['packet'], request.user.pk)
    digest = authored_discovery.revision(fields)
    nonce = fields['nonce']
    with transaction.atomic():
        state = authored_discovery.owned(request, profile_id, 'household')
        if state is None:
            if fields['revision'] != 0 or nonce != initial_nonce(request.user.pk, profile_id):
                raise Conflict('Stale initial observation form. Reload.')
            state = AuthoredDiscoveryState.objects.create(actor=request.user, profile_id=profile_id,
                mode='household', nonce=nonce, data=authored_discovery.empty_data())
        if nonce in state.receipts:
            if state.receipts[nonce] != digest:
                raise Conflict('This form was already used for another action.')
            return state
        if fields['revision'] != state.revision or nonce != state.nonce:
            raise Conflict('Stale observation form. Reload.')
        if len(state.receipts) >= authored_discovery.MAX_RECEIPTS:
            raise Conflict('Local authored discovery request limit reached.')
        data = deepcopy(state.data)
        rows = data.setdefault('observations', [])
        existing = {row['id']: row for row in rows}
        for row in incoming:
            if row['id'] in existing:
                if existing[row['id']] != row:
                    raise Conflict('This observation identity has different authored content.')
                continue
            if len(rows) >= MAX_OBSERVATIONS:
                raise Conflict('The 200-observation limit is reached.')
            rows.append(row)
        public_authorized(request, profile_id)
        receipts = dict(state.receipts, **{nonce: digest})
        updated = AuthoredDiscoveryState.objects.filter(pk=state.pk, actor=request.user,
            profile_id=profile_id, mode='household', revision=state.revision, nonce=nonce).update(
                data=data, receipts=receipts, revision=state.revision + 1,
                nonce=secrets.token_urlsafe(24))
        if updated != 1:
            raise Conflict('A newer authored discovery action won. Reload.')
        state.refresh_from_db()
        return state
