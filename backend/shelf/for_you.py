"""Grounded presentation snapshots; never invent candidate qualities or interpret notes."""
from django.contrib.auth import get_user_model
from django.db import connection

from shelf import synthesis
from shelf.acquisition_contract import Blocked
from shelf.calendar_handoff import digest
from shelf.discovery_state import Conflict
from shelf.feedback_scale import ALL_LABELS
from shelf.models import SynthesisRun
from shelf.pick_enhancement import positive_keys, schema_ready

LABELS = dict(ALL_LABELS)


def context(actor, profile_id):
    """Resolve one current owned input and, only when recorded, its real graph."""
    if not schema_ready():
        return {'current': None, 'run': None, 'positive': set(), 'signals': {}}
    try:
        current = synthesis.snapshot(actor, profile_id)
    except (Blocked, Conflict):
        return {'current': None, 'run': None, 'positive': set(), 'signals': {}}
    run = SynthesisRun.objects.filter(actor=actor, profile_id=profile_id,
        input_digest=current['input_digest'], state='completed').first()
    record = run.evidence.get('record', {}) if run else {}
    if (not run or not run.evidence.get('record_digest')
            or record.get('model') != synthesis.MODEL or record.get('reasoning') != synthesis.REASONING
            or record.get('input_digest') != current['input_digest'] or record.get('outcome') != 'completed'):
        run = None
    signals = {}
    for signal in current['signals']:
        signals.setdefault(signal['key'], []).append(signal)
    return {'current': current, 'run': run, 'positive': positive_keys(current['signals']), 'signals': signals}


def snapshot(card, resolved):
    key, source = card['key'], card.get('source') or {'tmdb': 'TMDB', 'hardcover': 'Hardcover'}.get(card.get('provider'), 'the source')
    roots = card.get('roots') or ([card['root']] if card.get('root') else [])
    anchors = sorted(set(roots) & resolved['positive'])
    explicit, inferred = [], []
    text = 'An exploratory pick. I do not yet have a clear personal connection for it; your response can help shape the next round.'
    if anchors:
        anchor = anchors[0]
        evidence = resolved['signals'][anchor]
        response = next((s for s in evidence if s['basis'] in ('anticipated_reaction', 'legacy_choice')), None)
        title = evidence[0]['title']
        label = LABELS.get(response['value'], 'a positive response') if response and type(response['value']) is str else 'a positive response'
        text = f'This may be worth a look: the source connects it with {title}, which you marked “{label}”.'
        explicit.append({'key': anchor, 'kind': 'owner_response', 'source': evidence[0]['source']})
        run = resolved['run']
        if run:
            for cluster in run.output.get('clusters', []):
                others = sorted((set(cluster['members']) & resolved['positive']) - {anchor})
                if anchor in cluster['members'] and others:
                    other = others[0]
                    if set(cluster['members']) <= resolved['positive'] and len(cluster['explanation']) <= 360:
                        text += ' Your current inferred taste snapshot suggests: ' + cluster['explanation']
                    else:
                        text += f' Your latest inferred taste cluster also connects that choice with {resolved["signals"][other][0]["title"]}.'
                    inferred.append({'keys': [anchor, other], 'kind': 'inferred_cluster_membership',
                        'label': cluster['label'], 'explanation': cluster['explanation'],
                        'run_id': str(run.pk), 'record_digest': run.evidence['record_digest']})
                    break
    current = resolved['current']
    bound = {'input_digest': current['input_digest'] if current else None,
             'source_key': key, 'source_roots': list(roots), 'source_url': card.get('url'),
             'source_fetched_at': card.get('fetched_at'), 'explicit': explicit, 'inferred': inferred}
    return {'text': text, 'version': digest(bound), 'source_keys': [key] + anchors,
            'explicit_edges': explicit, 'inferred_edges': inferred,
            'input_digest': bound['input_digest'], 'basis': 'grounded_snapshot' if anchors else 'tentative_exploration'}


def catalogue(profile, state, cards):
    actor = get_user_model().objects.get(pk=state['actor_id'])
    resolved = context(actor, profile.pk)
    for card in cards:
        card['for_you'] = snapshot(card, resolved)
    return cards


def discovery(request, profile_id, rows, *, mode='household'):
    if mode == 'sample':
        for row in rows:
            row['for_you'] = {'text': 'A sample pick for this rehearsal; no household taste is used.',
                'version': digest({'sample': row['card'].key}), 'source_keys': [row['card'].key]}
        return rows
    resolved = context(request.user, profile_id)
    for row in rows:
        card = row['card']
        row['for_you'] = snapshot({'key': card.key, 'title': card.title, 'source': card.source,
            'roots': list(card.roots), 'url': card.url, 'fetched_at': card.retrieved_at}, resolved)
    return rows
