/* Poll only an existing request. Model work belongs to the authorized worker. */
(() => {
  'use strict';
  const root = document.querySelector('[data-enhancement]');
  if (!root || !window.fetch || !window.FormData) return;
  const find = name => root.querySelector(`[data-enhance-${name}]`);
  const form = find('form');
  const message = find('message');
  const check = find('check');
  const applyButton = find('apply');
  const cancelButton = find('cancel');
  let current = {state: root.dataset.state, status_url: root.dataset.statusUrl};
  const portable = receipt => receipt?.schema === 'enhance/v1';
  const initial = root.querySelector('#enhance-stage-receipt');
  if (root.dataset.schema === 'enhance/v1') {
    // Invalid/missing projection stays read-only; never fall back to a legacy send.
    current = {schema:'enhance/v1', stage:'blocked', state:'blocked', code:'CONTRACT_INVALID'};
    try {
      const receipt = JSON.parse(initial?.textContent || 'null');
      if (portable(receipt)) current = receipt;
    } catch (_) { /* The server-rendered explanation remains available without JS. */ }
  }
  let working = false;
  let applying = false;
  let timer;
  let deadline = Date.now() + 600000;
  let generation = 0;
  let progressSnapshot = null;
  const terminal = new Set(['failed', 'unknown', 'cancelled', 'stale', 'no_candidates', 'unchanged']);
  const drafts = () => Array.from(document.querySelectorAll('textarea[name="note"]')).some(note => note.value !== note.defaultValue);
  const say = text => { message.textContent = text; };
  const feedbackBusy = () => Boolean(window.CabinetFeedback?.isBusy());
  function canStart() {
    if (portable(current)) return false;
    if (current.request_id === 'unconfirmed' || ['failed', 'unknown'].includes(current.state)) return false;
    if (!current.request_id && !current.status_url) return true;
    return current.state === 'stale' ||
      (current.state === 'cancelled' && Number(current.attempts) === 0) ||
      current.application_state === 'applied' ||
      ['unchanged', 'no_candidates'].includes(current.state) ||
      ['unchanged', 'no_candidates'].includes(current.application_state) ||
      ['unchanged', 'no_candidates'].includes(current.outcome);
  }
  function controls() {
    const readOnly = portable(current);
    if (form) {
      form.hidden = readOnly;
      form.querySelector('button').disabled = readOnly || working || !canStart();
    }
    root.setAttribute('aria-busy', working ? 'true' : 'false');
    if (readOnly) {
      // StageEvent.public has no action capability. Even extra URL-shaped fields
      // cannot turn a progress receipt into poll/apply/cancel/send authority.
      [check, applyButton, cancelButton, find('cancel-note'), find('status-link')].forEach(node => { node.hidden = true; });
      [check, applyButton, cancelButton].forEach(node => { node.disabled = true; });
      return;
    }
    check.hidden = !current.status_url;
    check.disabled = working;
    applyButton.hidden = !(current.state === 'completed' && current.apply_url && current.application_state !== 'applied' && current.application_state !== 'cancelled');
    applyButton.disabled = working;
    cancelButton.hidden = !current.cancel_url || current.application_state === 'applied' || current.application_state === 'cancelled';
    cancelButton.disabled = working;
    find('cancel-note').hidden = cancelButton.hidden;
  }
  async function request(url, data) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(url, {method: data ? 'POST' : 'GET', body: data,
        credentials: 'same-origin', headers: {Accept:'application/json'}, redirect:'error', signal:controller.signal});
      if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('No confirmed JSON receipt. Check existing status; your notes are preserved.');
      const receipt = await response.json();
      if (!response.ok) throw new Error(receipt.message || receipt.explanation || `Request could not finish (${response.status}). Check existing status before trying further actions.`);
      if (typeof receipt.state !== 'string') throw new Error('Unconfirmed enhancement receipt. Check existing status.');
      return receipt;
    } finally { window.clearTimeout(timeout); }
  }
  function csrfData() {
    const data = new FormData();
    const token = root.querySelector('[name="csrfmiddlewaretoken"]');
    if (token) data.set('csrfmiddlewaretoken', token.value);
    return data;
  }
  const outcomeText = row => {
    switch (row.state) {
      case 'fresh': return Number.isInteger(row.candidate_count) && row.candidate_count > 0 && row.candidate_count <= 20
        ? `${row.candidate_count} sourced candidate${row.candidate_count === 1 ? '' : 's'} collected; new picks are not confirmed by collection alone.` : 'Outcome not confirmed.';
      case 'empty_bounded': return 'No candidates within this round’s collection limits.';
      case 'unavailable': return row.code === 'CAPABILITY_UNAVAILABLE'
        ? 'Unsupported by the current source connections.' : 'Source unavailable; no fresh result.';
      case 'failed': return 'Collection failed; this is not an empty result.';
      case 'cancelled': return 'Collection cancelled; no fresh result.';
      case 'retained_previous_stale': return 'Older picks retained; not refreshed by this round.';
      default: return 'Outcome not confirmed.';
    }
  };
  const reasonText = code => {
    switch (code) {
      case 'UNAUTHENTICATED': return 'Open owner setup to sign in or confirm your session, then review the existing round. Nothing is retried automatically.';
      case 'AUTHORITY_DENIED': return 'Review the selected profile’s permissions before continuing. No extra send is available here.';
      case 'CREDENTIAL_REQUIRED': return 'Review the connection that needs a credential before continuing. Nothing is retried automatically.';
      case 'CAPABILITY_UNAVAILABLE': return 'The current connections do not support this operation. Keep your existing picks.';
      case 'SOURCE_UNAVAILABLE': return 'A source is unavailable. Review the affected categories below; do not treat this as an empty search.';
      case 'RATE_LIMITED': return 'A source has asked Cabinet to wait. This round will not retry automatically.';
      case 'UNKNOWN_RECONCILIATION_REQUIRED': return 'Reconcile the existing attempt before any further inference. Do not resend this round.';
      case 'PREVIEW_STALE': case 'VALIDATION_STALE': case 'RUN_STALE': case 'REVISION_CONFLICT':
        return 'Review your current responses and permissions and obtain a fresh preview before continuing.';
      case 'LIMIT_EXCEEDED': return 'This round exceeds the permitted limits. No extra work was authorized; keep your current picks.';
      case 'NO_GROUNDED_CANDIDATES': return 'No grounded candidates are available for new picks. Review category outcomes below.';
      case 'IDEMPOTENCY_CONFLICT': return 'Check the existing round before continuing; do not start another attempt with changed request details.';
      default: return 'This round needs attention. No additional send or automatic retry is available here.';
    }
  };
  function renderProgress(receipt) {
    const stages = Array.from(root.querySelectorAll('[data-enhance-stage]'));
    const known = stages.some(node => node.dataset.enhanceStage === receipt.stage);
    const selected = known ? receipt.stage : 'blocked';
    stages.forEach(node => {
      const hidden = node.dataset.enhanceStage !== selected;
      if (node.hidden !== hidden) node.hidden = hidden;
    });
    const reason = find('reason');
    reason.hidden = known && (!receipt.code || receipt.code === 'OK');
    reason.textContent = reasonText(known ? receipt.code : 'CONTRACT_INVALID');
    const publication = receipt.stage === 'published' && receipt.published === true &&
      Number.isInteger(receipt.publication_revision) && receipt.publication_revision > 0;
    find('publication').hidden = !publication;
    find('revision').textContent = publication ? String(receipt.publication_revision) : '';
    const rows = Array.isArray(receipt.category_outcomes) ? receipt.category_outcomes.slice(0, 100) : [];
    const list = find('outcomes');
    list.replaceChildren();
    rows.forEach(row => {
      if (!row || typeof row.category !== 'string') return;
      const item = document.createElement('li');
      const label = document.createElement('strong');
      label.textContent = row.category;
      item.append(label, document.createTextNode(' — ' + outcomeText(row)));
      if (row.code && row.code !== 'OK') {
        const cause = document.createElement('span');
        cause.setAttribute('data-enhance-category-cause', '');
        cause.textContent = ' ' + reasonText(row.code);
        item.append(cause);
      }
      list.append(item);
    });
    find('coverage-pending').hidden = list.children.length > 0;
    const snapshot = {stage:selected, code:receipt.code, revision:publication ? receipt.publication_revision : null,
      outcomes:rows.map(row => [row?.category, row?.state, row?.candidate_count, row?.code])};
    const update = find('update');
    // The stage region already announces stage changes. Announce only changed
    // same-stage facts here, once per snapshot, without reading the entire list.
    if (progressSnapshot && progressSnapshot.stage === selected &&
        JSON.stringify(progressSnapshot) !== JSON.stringify(snapshot)) {
      const attention = rows.filter(row => row && ['unavailable','failed','cancelled'].includes(row.state)).length;
      const retained = rows.filter(row => row?.state === 'retained_previous_stale').length;
      update.textContent = 'This round’s status was updated. ' +
        (attention ? `${attention} categor${attention === 1 ? 'y needs' : 'ies need'} attention. ` : '') +
        (retained ? `${retained} categor${retained === 1 ? 'y retains' : 'ies retain'} older picks. ` : '') +
        (receipt.code && receipt.code !== 'OK' ? reasonText(receipt.code) : 'Review the latest category outcomes below.') +
        (publication ? ` Publication revision ${receipt.publication_revision}.` : '');
    } else if (progressSnapshot?.stage !== selected) update.textContent = '';
    progressSnapshot = snapshot;
    // Only update the nearby status. Never replace a deck or move keyboard focus
    // for a stage event; it is not a publication manifest or reaction receipt.
  }
  function render(receipt) {
    current = receipt;
    find('progress').hidden = !portable(receipt);
    find('explanation').hidden = portable(receipt);
    if (portable(receipt)) {
      if (find('label').textContent !== 'Enhance progress') find('label').textContent = 'Enhance progress';
      renderProgress(receipt);
      controls();
      return;
    }
    find('label').textContent = receipt.label || receipt.state;
    find('explanation').textContent = receipt.explanation || 'Check this request’s status before taking further action.';
    const link = find('status-link');
    link.hidden = !receipt.status_url;
    if (receipt.status_url) link.href = receipt.status_url;
    controls();
  }
  function schedule() {
    window.clearTimeout(timer);
    if (portable(current) || !current.status_url || terminal.has(current.state) || current.application_state === 'applied' || current.application_state === 'cancelled') return;
    if (current.state === 'completed') return;
    if (Date.now() >= deadline) { say('Automatic status checks paused after ten minutes. Check existing status to continue.'); return; }
    timer = window.setTimeout(poll, 2000);
  }
  async function apply() {
    if (portable(current)) return;
    if (working || feedbackBusy()) { say('Finish confirming the pending response before applying picks.'); return; }
    if (drafts()) { say('Picks are ready. Save your current reactions and notes first; your drafts have been preserved. Then apply ready picks.'); return; }
    if (!current.apply_url) return;
    working = true;
    applying = true;
    controls();
    const version = generation;
    try {
      const receipt = await request(current.apply_url, csrfData());
      if (version !== generation) return;
      window.CabinetFeedback?.refreshAuthority(receipt);
      if (receipt.application_state === 'applied' && typeof receipt.deck_html === 'string') {
        const deck = document.querySelector(`[data-feedback-deck="${receipt.deck_kind === 'artist' ? 'artist' : 'catalogue'}"]`);
        if (deck && !drafts()) {
          const top = deck.getBoundingClientRect().top;
          deck.innerHTML = receipt.deck_html;
          window.scrollBy({top:deck.getBoundingClientRect().top - top, behavior:'auto'});
        } else say('Result confirmed. Your drafts are preserved; return to your picks after saving them to review the applied result.');
      }
      if (receipt.enhance_context) window.CabinetEnhancement.refreshContext(receipt.enhance_context);
      render(receipt);
    } catch (error) { say(error.message || 'Application outcome is uncertain. Check existing status before another action.'); }
    finally { working = false; applying = false; controls(); }
  }
  async function poll() {
    if (portable(current) || working || !current.status_url) return;
    working = true;
    controls();
    const version = generation;
    try {
      const receipt = await request(current.status_url);
      if (version !== generation) return;
      render(receipt);
      say('');
    } catch (error) { say(error.message || 'Status is unknown. Check existing status; no new model request was made.'); return; }
    finally { working = false; controls(); }
    if (current.state === 'completed' && current.application_state !== 'applied' && current.application_state !== 'cancelled') await apply();
    schedule();
  }
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    if (working || feedbackBusy()) { say('Finish confirming the pending response first.'); return; }
    if (!canStart()) { say('Check the existing request before starting another enhancement.'); return; }
    if (drafts()) { say('Save your current reactions and notes before enhancing picks. Your drafts are preserved.'); return; }
    working = true;
    controls();
    const version = generation;
    try {
      const receipt = await request(form.getAttribute('action'), new FormData(form));
      if (version !== generation) return;
      render(receipt);
      deadline = Date.now() + 600000;
      say('');
    } catch (error) {
      // An uncertain reservation must never become a blind fresh model request.
      current.request_id = 'unconfirmed';
      say((error.message || 'Request outcome is uncertain.') + ' Reload to check existing status before requesting enhancement again.');
    } finally { working = false; controls(); }
    if (current.state === 'completed' && current.application_state !== 'applied' && current.application_state !== 'cancelled') await apply();
    schedule();
  });
  check.addEventListener('click', () => { deadline = Date.now() + 600000; poll(); });
  applyButton.addEventListener('click', apply);
  cancelButton.addEventListener('click', async () => {
    if (portable(current) || working || feedbackBusy() || !current.cancel_url) return;
    working = true;
    controls();
    window.clearTimeout(timer);
    const version = generation;
    try {
      const receipt = await request(current.cancel_url, csrfData());
      if (version !== generation) return;
      render(receipt);
      say('This round will not be applied. External model work may continue.');
    }
    catch (error) { say(error.message || 'Cancellation outcome is uncertain. Check existing status.'); }
    finally { working = false; controls(); }
  });
  window.CabinetEnhancement = {
    isApplying: () => applying,
    refreshContext(context) {
      if (!context) return;
      generation++;
      window.clearTimeout(timer);
      if (portable(context.enhancement)) {
        render(context.enhancement);
        return;
      }
      if (portable(current)) {
        // A legacy active-tab refresh cannot replace a profile-wide portable run
        // or manufacture new preview/send authority after a reaction.
        say('Your responses changed. Review the existing round and current preview before continuing; no new request was sent.');
        return;
      }
      if (!form) return;
      [['expected_digest','enhance_digest'],['preview_token','enhance_token'],['category','active_queue']].forEach(([name,key]) => {
        if (context[key] !== undefined) form.elements.namedItem(name).value = context[key];
      });
      if (context.enhance_url) form.action = context.enhance_url;
      render(context.enhancement || {state:'stale',label:'Preferences updated',explanation:'Enhance again to use your current responses and notes.'});
    }
  };
  if (portable(current)) render(current);
  else controls();
  if (!portable(current) && current.status_url) poll();
})();
