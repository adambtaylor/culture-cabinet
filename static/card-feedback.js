/* One receipt at a time: an uncertain request retains its exact payload and nonce. */
(() => {
  'use strict';
  let pending = null;
  let sending = false;
  let blocked = false;
  const status = document.getElementById('feedback-status');
  if (!status || !window.fetch || !window.FormData) return;
  const announce = (message) => { status.textContent = message; };
  function retryButton() {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Retry this saved response';
    button.addEventListener('click', send);
    status.append(' ', button);
  }
  function busy(value) {
    document.querySelectorAll('form[method="post"] button').forEach((button) => {
      if (value) {
        if (!button.disabled) { button.dataset.feedbackDisabled = 'true'; button.disabled = true; }
      } else if (button.dataset.feedbackDisabled) {
        button.disabled = false;
        delete button.dataset.feedbackDisabled;
      }
    });
  }
  function refreshAuthority(receipt) {
    ['revision', 'nonce', 'run_id', 'queue', 'mode'].forEach((name) => {
      if (receipt[name] === undefined || receipt[name] === null) return;
      const scopes = name === 'run_id' ? ['[data-feedback-form]', '[data-feedback-latest-undo]'] : ['[data-feedback-context]'];
      scopes.forEach(scope => {
        document.querySelectorAll(`${scope} input[type="hidden"][name="${name}"]`).forEach((input) => {
          input.value = String(receipt[name]);
        });
      });
    });
  }
  function replaceCard(receipt) {
    const old = pending.card;
    const template = document.createElement('template');
    template.innerHTML = receipt.replacement_html;
    let next = template.content.querySelector('[data-feedback-card]');
    if (!next) {
      next = document.createElement('article');
      next.className = 'paper-panel';
      next.tabIndex = -1;
      next.textContent = 'Response saved. No further picks in this local queue. Explore another category or explicitly refresh sources.';
    }
    const top = old.getBoundingClientRect().top;
    // A slow receipt must not interrupt editing in a sibling card.
    const focused = old.contains(document.activeElement) || status.contains(document.activeElement) ||
      (pending.retryHadFocus && document.activeElement === document.body);
    old.replaceWith(next);
    if (focused) next.focus({preventScroll: true});
    window.scrollBy({top: next.getBoundingClientRect().top - top, behavior: 'auto'});
  }
  function freezeFields(form) {
    return Array.from(form.querySelectorAll('textarea, input:not([type="hidden"]), select')).map((field) => {
      const property = field.tagName === 'SELECT' ? 'disabled' : 'readOnly';
      const previous = field[property];
      field[property] = true;
      return {field, property, previous};
    });
  }
  function unlockFields() {
    pending.fields.forEach(({field, property, previous}) => { field[property] = previous; });
  }
  function refreshExplanations(updates) {
    if (!updates || typeof updates !== 'object') return;
    document.querySelectorAll('[data-feedback-card]').forEach((card) => {
      const explanation = updates[card.dataset.cardKey];
      const region = card.querySelector('[data-for-you]');
      if (!region || !explanation || typeof explanation.text !== 'string') return;
      region.textContent = explanation.text;
      if (explanation.version !== undefined) region.dataset.version = String(explanation.version);
    });
  }
  async function send() {
    if (!pending || sending) return;
    sending = true;
    busy(true);
    pending.card.setAttribute('aria-busy', 'true');
    // Announcing the retry removes its button; retain focus only while it stays on the body.
    pending.retryHadFocus = status.contains(document.activeElement);
    announce('Saving your response…');
    let response;
    let receipt;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      response = await fetch(pending.url, {
        method: 'POST', body: pending.data, credentials: 'same-origin',
        headers: {'Accept': 'application/json'}, redirect: 'error', signal: controller.signal
      });
      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) receipt = await response.json();
      else receipt = {message: await response.text()};
      if (!response.ok) {
        pending.card.removeAttribute('aria-busy');
        if (response.status === 409) {
          blocked = true;
          announce('This page is out of date. Your draft is preserved. Copy any unsent notes, reload this page, then reconcile the saved response before submitting again.');
        } else if (response.status >= 500) {
          announce('The save outcome is uncertain. Your draft is preserved. Retry the identical saved response to confirm its receipt.');
          retryButton();
        } else {
          announce(receipt.message || `Response could not be saved (${response.status}). Your draft is preserved.`);
          unlockFields();
          pending = null;
          busy(false);
        }
        return;
      }
      if (!receipt.ok || receipt.submitted_key !== pending.key ||
          typeof receipt.replacement_html !== 'string' || receipt.revision === undefined || !receipt.nonce) {
        throw new Error('Unconfirmed receipt');
      }
      refreshAuthority(receipt);
      if (receipt.counts) {
        const total = Object.values(receipt.counts).reduce((sum, count) => sum + Number(count), 0);
        document.querySelectorAll('[data-feedback-response-count]').forEach(node => { node.textContent = String(total); });
      }
      refreshExplanations(receipt.explanation_updates);
      unlockFields();
      if (receipt.operation !== 'correct') replaceCard(receipt);
      else {
        pending.card.removeAttribute('aria-busy');
        pending.fields.forEach(({field}) => {
          if (field.tagName === 'SELECT') Array.from(field.options).forEach(option => { option.defaultSelected = option.selected; });
          else field.defaultValue = field.value;
        });
      }
      pending = null;
      busy(false);
      if (receipt.counts && receipt.run_id) {
        const hasResponses = Object.values(receipt.counts).some(count => Number(count) > 0);
        document.querySelectorAll('[data-feedback-latest-undo] button').forEach(button => { button.disabled = !hasResponses; });
      }
      window.CabinetEnhancement?.refreshContext(receipt.enhance_context);
      announce(receipt.status_message || 'Response saved.');
    } catch (_) {
      pending.card.removeAttribute('aria-busy');
      announce('The save outcome is uncertain. Your draft is preserved. Retry the identical saved response to confirm its receipt.');
      retryButton();
    } finally { window.clearTimeout(timeout); sending = false; }
  }
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (pending || blocked || window.CabinetEnhancement?.isApplying()) {
      event.preventDefault();
      if (blocked) announce('Copy unsent notes and reload to reconcile this out-of-date page.');
      return;
    }
    if (!form.matches('[data-feedback-form]')) return;
    if (form.hasAttribute('data-feedback-correction') && event.submitter?.value !== (form.dataset.feedbackCorrection || 'correct')) return;
    event.preventDefault();
    const card = form.closest('[data-feedback-card]');
    const data = new FormData(form);
    if (event.submitter && event.submitter.name) data.set(event.submitter.name, event.submitter.value);
    pending = {card, data, key: card.dataset.cardKey, url: form.getAttribute('action'),
      fields: freezeFields(form)};
    send();
  });
  window.CabinetFeedback = {isBusy: () => Boolean(pending || blocked), refreshAuthority};
  window.addEventListener('beforeunload', (event) => {
    if (pending) { event.preventDefault(); event.returnValue = ''; }
  });
})();
