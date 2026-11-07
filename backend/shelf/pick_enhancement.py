"""Human-approved consumers of one supervised association run; never executes inference."""
from copy import deepcopy
from uuid import uuid4

from django.core import signing
from django.db import transaction, connection
from django.urls import reverse
from django.utils import timezone

from shelf import synthesis
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest
from shelf.discovery_state import Conflict, public_authorized
from shelf.models import SynthesisRun, DiscoveryState, AuthoredDiscoveryState, SocialDiscoveryState, IntakeRun, SavedItem

CATEGORIES = ('all', 'book', 'movie', 'tv', 'artist')
SALT = 'shelf.pick-enhancement.v1'


def schema_ready():
    required = {model._meta.db_table for model in (SynthesisRun, DiscoveryState,
        AuthoredDiscoveryState, SocialDiscoveryState, IntakeRun, SavedItem)}
    return required <= set(connection.introspection.table_names())


def authorize(request, profile_id, category):
    if request.user.pk != 1 or profile_id != 1 or category not in CATEGORIES:
        raise Blocked('Current owner household and supported category required.')
    public_authorized(request, profile_id)


def catalogue_state(request, profile_id, category):
    """Read cached metadata and current choices without upgrading or saving a session.

    Expired metadata is an empty pool, even when durable judged identities remain.
    Validation must not import legacy choices or restore a historical deck as a side effect.
    """
    from shelf import durable_intake, intake, provider_views
    cached = deepcopy(request.session.get(intake.SESSION_KEY))
    state = intake.load({intake.SESSION_KEY: cached}, request.user.pk, profile_id)
    if state is None:
        state = intake.new_state(request.user.pk, profile_id)
    elif state.get('run_id') and durable_intake.available():
        before = deepcopy(state)
        state = durable_intake.hydrate(durable_intake.owned(request.user, profile_id, state['run_id']), state)
        removed = [key for key in before['current'] if key in state['choices']]
        provider_views._pin_round(before, state, removed[0] if len(removed) == 1 else '')
    return provider_views._select_queue(state, category)


def pool(request, profile_id, category):
    """Resolve registered cached identities only, without fetching provider content."""
    from shelf import discovery_views
    from shelf.discovery_state import owned
    from shelf.discovery_reducer import replay
    from shelf import durable_intake, intake
    state = catalogue_state(request, profile_id, category) if category != 'artist' else {'current': []}
    cards = {}
    if category != 'artist':
        for key, card in state['candidates'].items():
            if (key not in state.get('history_only', []) and key not in state['choices']
                    and card.get('medium') in ('book', 'movie', 'tv')
                    and category in ('all', card['medium']) and card.get('url')
                    and key == f"{card.get('provider')}:{card.get('medium')}:{card.get('source_id')}"):
                try:
                    durable_intake.snapshot(card)
                except intake.IntakeError:
                    continue
                cards[key] = {'key': key, 'roots': [card['root']] if card.get('root') else [],
                              'url': card['url'], 'medium': card['medium']}
    if category == 'artist':
        row = owned(request, profile_id, 'household')
        judged = replay(row.history if row else [])['choices']
        for card in discovery_views.candidates(request, profile_id, 'household', row.history if row else []):
            if (card.kind == 'artist' and card.key not in judged and card.provenance != 'fixture' and card.url
                    and card.key.startswith('musicbrainz:artist:')):
                cards[card.key] = {'key': card.key, 'roots': list(card.roots),
                                   'url': card.url, 'medium': 'artist'}
    before = [key for key in state['current'] if key in cards]
    if category == 'artist':
        deck = discovery_views._deck(request, profile_id, 'household', 'artist')
        before = [key for key in deck.get('keys', []) if key in cards] if deck else []
    return cards, before


def preview(request, profile_id, category):
    authorize(request, profile_id, category)
    current = synthesis.snapshot(request.user, profile_id)
    cards, before = pool(request, profile_id, category)
    return {'actor_id': request.user.pk, 'profile_id': profile_id, 'category': category,
            'input_digest': current['input_digest'], 'before_ids': before, 'pool_digest': digest(cards)}


def context(request, profile_id, category='all'):
    if not schema_ready():
        raise Blocked('Enhancement requires the prepared local taste schema.')
    binding = preview(request, profile_id, category)
    run = SynthesisRun.objects.filter(actor=request.user, profile_id=profile_id,
                                     input_digest=binding['input_digest']).first()
    consumer = (run.evidence.get('enhancements', {}).get(category) if run else None)
    kwargs = {'profile_id': profile_id, 'category': category}
    request_id = consumer['request_id'] if consumer else ''
    display = consumer_progress(run, consumer, binding) if consumer else {'state': 'not_requested'}
    state = display['state']
    descriptions = {
        'not_requested': ('Enhance my picks', 'Request supervised Astra taste associations for verified cached picks.'),
        'awaiting_authorized_worker': ('Enhancement queued', 'Awaiting the authorized supervised Astra worker; no result recorded yet.'),
        'running': ('Enhancement running', 'One supervised Astra attempt is reserved; completion is not recorded yet.'),
        'completed': ('Enhancement completed', 'Actual supervised Astra associations are recorded; cached picks may be applied.'),
        'failed': ('Enhancement failed', 'The supervised attempt failed. It will not be retried automatically.'),
        'unknown': ('Enhancement outcome unknown', 'Reconcile the existing attempt before any further model work.'),
        'stale': ('Enhancement preview changed', 'Review the current taste and cached picks before requesting again.'),
        'cancelled': ('Enhancement application cancelled', 'Picks will not be applied by this request. External work may continue.'),
    }
    label, explanation = descriptions[state]
    if consumer and consumer.get('state') == 'applied' and state == 'completed':
        explanation = ('Verified cached picks use recorded supervised associations.'
            if consumer.get('outcome') == 'supported' else
            'The recorded associations did not support new cached picks; the selection is unchanged.'
            if consumer.get('outcome') == 'unchanged' else 'No verified cached candidates are available.')
    urls = {name + '_url': reverse('enhancement_' + name,
        kwargs={**kwargs, 'request_id': request_id}) if request_id else ''
        for name in ('status', 'apply', 'cancel')}
    return {'enhance_digest': binding['input_digest'], 'enhance_token': signing.dumps(binding, salt=SALT),
            'enhance_url': reverse('enhancement_request', kwargs=kwargs), 'active_queue': category,
            'enhancement': {'label': label, 'explanation': explanation, 'state': state,
                            'receipt_state': run.state if run else None,
                            'request_id': request_id, 'attempts': run.attempts if run else 0,
                            'application_state': consumer['state'] if consumer else 'not_requested',
                            'outcome': consumer.get('outcome', '') if consumer else '', **urls}}



def enroll(request, profile_id, category, token, expected_digest):
    try:
        approved = signing.loads(token, salt=SALT, max_age=900)
    except signing.BadSignature:
        raise Conflict('Enhancement preview expired. Reload before requesting.') from None
    with transaction.atomic():
        current = preview(request, profile_id, category)
        if approved != current or expected_digest != current['input_digest']:
            raise Conflict('Taste or cached picks changed. Review a fresh preview.')
        run = synthesis.request(request, profile_id, expected_digest, purpose='enhancement')
        evidence = deepcopy(run.evidence)
        consumers = evidence.setdefault('enhancements', {})
        previous = consumers.get(category)
        replace = previous and (any(previous.get(k) != value for k, value in current.items())
                                or previous.get('state') == 'cancelled')
        if replace:
            archive = evidence.setdefault('enhancement_archive', [])
            if len(archive) >= 20:
                raise Blocked('Enhancement preview history is full for this taste snapshot.')
            archive.append({**previous, 'state': 'superseded'})
        if previous is None or replace:
            if previous is None and len(consumers) >= 5:
                raise Blocked('At most five category consumers per synthesis.')
            consumers[category] = {**current, 'request_id': str(uuid4()), 'human_approved': True,
                'approved_at': timezone.now().isoformat(), 'state': 'pending'}
            public_authorized(request, profile_id)
            synthesis.update(run, evidence=evidence)
        return run, consumers[category]


def lookup(request, profile_id, category, request_id, *, lock=False):
    authorize(request, profile_id, category)
    runs = SynthesisRun.objects.filter(actor=request.user, profile_id=profile_id).order_by('-created_at')
    if lock:
        runs = runs.select_for_update()
    for run in runs:
        consumer = run.evidence.get('enhancements', {}).get(category)
        if consumer and consumer['request_id'] == str(request_id):
            return run, consumer
        for historical in run.evidence.get('enhancement_archive', []):
            if historical.get('category') == category and historical['request_id'] == str(request_id):
                return run, historical
    raise Blocked('Enhancement request not found.')


def progress(run, consumer, current_digest):
    if consumer['state'] == 'cancelled':
        state = 'cancelled'
    elif consumer['state'] == 'superseded' or run.input_digest != current_digest:
        state = 'stale'
    else:
        state = {'queued': 'awaiting_authorized_worker', 'running': 'running'}.get(run.state, run.state)
    return {'state': state, 'request_id': consumer['request_id'], 'run_id': str(run.pk),
            'application_state': consumer['state'], 'attempts': run.attempts, 'receipt_state': run.state,
            'external_work_cancelled': False}


def recorded_result(run):
    """Require the completed output to retain its authoritative reservation/receipt binding."""
    record = run.evidence.get('record')
    if (run.state != 'completed' or run.attempts != 1 or not run.evidence.get('reservation')
            or not isinstance(record, dict) or record.get('outcome') != 'completed'
            or record.get('output') != run.output or digest(record) != run.evidence.get('record_digest')):
        return False
    return all(record.get(key) == value for key, value in synthesis.binding(run).items())


def consumer_current(run, consumer, binding):
    if not binding or consumer['state'] == 'superseded':
        return False
    if any(consumer.get(key) != value for key, value in binding.items() if key != 'before_ids'):
        return False
    # A committed receipt may precede a lost session save. Recover only its exact
    # approved pre-order or already-materialized post-order, never a newer deck.
    orders = [consumer['before_ids']]
    if consumer['state'] == 'applied':
        orders.append(consumer.get('after_ids', []))
        if (consumer.get('applied_input_digest') != binding['input_digest']
                or consumer.get('result_record_digest') != run.evidence.get('record_digest')):
            return False
    return (run.input_digest == binding['input_digest'] and binding['before_ids'] in orders
            and (run.state != 'completed' or recorded_result(run)))


def consumer_progress(run, consumer, binding):
    result = progress(run, consumer, binding['input_digest'] if binding else None)
    if result['state'] != 'cancelled' and not consumer_current(run, consumer, binding):
        result['state'] = 'stale'
    return result


def validate_application(request, profile_id, category, request_id):
    """Called inside the materializer transaction before any session publication."""
    run, consumer = lookup(request, profile_id, category, request_id, lock=True)
    binding = preview(request, profile_id, category)
    if (consumer['state'] != 'applied' or not consumer_current(run, consumer, binding)
            or not recorded_result(run)):
        raise Conflict('Enhancement input, cached pool or recorded result changed.')
    return run, consumer


def positive_keys(signals):
    """Conflicting or negative owner evidence vetoes an anchor; notes are not scored."""
    from shelf.feedback_scale import is_positive, is_negative, ALL_CHOICES
    positive, negative = set(), set()
    for signal in signals:
        basis, value, key = signal['basis'], signal['value'], signal['key']
        values = value if type(value) is list else [value]
        if basis in ('legacy_choice', 'anticipated_reaction'):
            if len(values) > 1 or any(type(v) is not str or v not in ALL_CHOICES for v in values):
                negative.add(key)
                continue
            if any(is_negative(v) for v in values): negative.add(key)
            if any(is_positive(v) for v in values): positive.add(key)
        elif basis == 'favourite' and value is True:
            positive.add(key)
        elif basis in ('interest', 'enjoyment') and type(value) is int:
            (positive if value >= 4 else negative if value <= 2 else set()).add(key)
    return positive - negative


def consume(run, cards, before):
    """Only a source-supplied root cited in an actual cluster can support a pick."""
    positives = positive_keys(run.input_snapshot['signals'])
    why = {}
    for key, card in cards.items():
        for index, cluster in enumerate(run.output.get('clusters', [])):
            anchors = sorted(set(card['roots']) & positives & set(cluster['members']))
            if anchors:
                why[key] = {'anchor_keys': anchors, 'cluster_index': index,
                            'basis': 'supervised_association_and_source_root'}
                break
    ordered = [key for key in cards if key in why]
    ordered.extend(key for key in before if key in cards and key not in ordered)
    # No association support means keep the current visible selection unchanged.
    after = ordered[:max(len(before), 6)] if why else list(before)
    return after, why


def apply(request, profile_id, category, request_id, cancel=False):
    with transaction.atomic():
        run, consumer = lookup(request, profile_id, category, request_id, lock=True)
        if consumer['state'] == 'superseded':
            raise Conflict('This preview was replaced by a newer explicit request.')
        if consumer['state'] == 'cancelled':
            return run, consumer
        if consumer['state'] == 'applied':
            if not cancel:
                validate_application(request, profile_id, category, request_id)
            return run, consumer
        evidence = deepcopy(run.evidence)
        target = evidence['enhancements'][category]
        if cancel:
            target.update(state='cancelled', cancelled_at=timezone.now().isoformat())
        else:
            current = preview(request, profile_id, category)
            if not consumer_current(run, consumer, current):
                raise Conflict('Enhancement input or cached pool changed.')
            if not recorded_result(run):
                raise Conflict('Actual supervised completion required before application.')
            cards, before = pool(request, profile_id, category)
            if digest(cards) != current['pool_digest'] or before != current['before_ids']:
                raise Conflict('Enhancement cached pool changed during application.')
            after, why = consume(run, cards, before)
            target.update(state='applied', after_ids=after, why=why,
                          result_record_digest=run.evidence['record_digest'], result_revision=run.revision,
                          applied_input_digest=current['input_digest'],
                          outcome='no_candidates' if not cards else 'supported' if why else 'unchanged',
                          applied_at=timezone.now().isoformat())
        public_authorized(request, profile_id)
        synthesis.update(run, evidence=evidence)
        return run, target


def ready(*, actor, profile_id, session_guard):
    session_guard()
    from shelf.provider_views import _profiles
    if actor.pk != 1 or profile_id != 1 or not _profiles(actor, 'select').filter(pk=1).exists():
        raise Blocked('Current owner grant required.')
    current = synthesis.snapshot(actor, profile_id)['input_digest']
    jobs = []
    for run in SynthesisRun.objects.filter(actor=actor, profile_id=profile_id,
            input_digest=current, state='queued', attempts=0).order_by('created_at')[:5]:
        consumers = run.evidence.get('enhancements', {}).values()
        ids = [c['request_id'] for c in consumers if c.get('human_approved') is True and c.get('state') == 'pending']
        if ids:
            jobs.append({'run_id': str(run.pk), 'input_digest': run.input_digest,
                         'model': synthesis.MODEL, 'reasoning': synthesis.REASONING, 'request_ids': ids})
    session_guard()
    return {'state': 'ready', 'jobs': jobs}


def current_application(request, profile_id, category='all'):
    """Return an applied ordering only while all original authority bindings hold."""
    current = preview(request, profile_id, category)
    run = SynthesisRun.objects.filter(actor=request.user, profile_id=profile_id,
        input_digest=current['input_digest'], state='completed').first()
    consumer = run.evidence.get('enhancements', {}).get(category) if run else None
    if (not consumer or consumer.get('state') != 'applied'
            or not consumer_current(run, consumer, current)):
        return [], {}
    cards, _ = pool(request, profile_id, category)
    ids = consumer.get('after_ids', [])
    if any(key not in cards for key in ids):
        return [], {}
    return list(ids), deepcopy(consumer.get('why', {}))


def applied_result(actor, profile_id, category, current_digest):
    """Persisted receipt for callers that independently revalidate the current pool."""
    from shelf.provider_views import _profiles
    if (actor.pk != 1 or profile_id != 1 or category not in CATEGORIES
            or not _profiles(actor, 'select').filter(pk=profile_id).exists()):
        return None
    run = SynthesisRun.objects.filter(actor=actor, profile_id=profile_id,
        input_digest=current_digest, state='completed').first()
    consumer = run.evidence.get('enhancements', {}).get(category) if run else None
    if (not consumer or consumer.get('state') != 'applied'
            or consumer.get('applied_input_digest') != current_digest
            or consumer.get('result_record_digest') != run.evidence.get('record_digest')
            or not recorded_result(run)):
        return None
    return deepcopy(consumer)
