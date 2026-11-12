/* Native forms remain the only write path. Holding reveals choices; neither
 * pointer movement nor a lifted card is a preference or an acquisition intent. */
(function () {
  'use strict';
  const DIRECTIONS = { right: 'like', left: 'dislike', up: 'favourite', down: 'obtain' };
  class HoldGesture {
    constructor(hooks = {}, clock = {}) {
      this.hooks = hooks;
      // Browser timers need their Window receiver; injected clocks keep theirs.
      // Calling a raw timer as a gesture method can throw before hold activation.
      this.schedule = clock.setTimeout ? clock.setTimeout.bind(clock) : setTimeout.bind(globalThis);
      this.clear = clock.clearTimeout ? clock.clearTimeout.bind(clock) : clearTimeout.bind(globalThis);
      this.phase = 'idle';
      this.pointer = null;
      this.timer = null;
    }
    down(event) {
      if (this.phase !== 'idle') { this.cancel('multiple-pointers'); return false; }
      if (event.excluded || event.isPrimary === false || event.button !== 0) return false;
      this.pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
      this.phase = 'pending';
      this.timer = this.schedule(() => {
        if (this.phase !== 'pending') return;
        this.timer = null;
        this.phase = 'active';
        this.hooks.lift?.(this.pointer);
      }, 380);
      return true;
    }
    move(event, target = null) {
      if (this.phase === 'idle') return;
      if (event.pointerId !== this.pointer.id) { this.cancel('multiple-pointers'); return; }
      if (this.phase === 'pending') {
        if (Math.hypot(event.clientX - this.pointer.x, event.clientY - this.pointer.y) > 10) this.cancel('scroll-intent');
        return;
      }
      this.hooks.move?.(event, target?.enabled ? target : null);
    }
    up(event, target = null) {
      if (this.phase === 'idle' || event.pointerId !== this.pointer.id) return false;
      const active = this.phase === 'active';
      const action = active && target?.enabled && Object.values(DIRECTIONS).includes(target.action) ? target.action : null;
      this.reset();
      this.hooks.end?.(active ? 'release' : 'short-click');
      if (action) this.hooks.commit?.(action);
      return !!action;
    }
    cancel(reason = 'cancel') {
      if (this.phase === 'idle') return;
      const active = this.phase === 'active';
      this.reset();
      this.hooks.end?.(reason, active);
    }
    reset() {
      if (this.timer !== null) this.clear(this.timer);
      this.timer = null;
      this.phase = 'idle';
      this.pointer = null;
    }
  }
  class SubmissionGate {
    constructor() { this.busy = false; }
    claim() { if (this.busy) return false; this.busy = true; return true; }
  }
  function targetAt(targets, x, y) {
    return targets.find(target => x >= target.left && x <= target.right && y >= target.top && y <= target.bottom) || null;
  }
  function confirmedSlotResult(submitted, receipt, cards, consumed = new Set()) {
    const fail = () => { throw Error('Current saved response could not be confirmed'); };
    const uuid = value => typeof value === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
    const revision = value => Number.isSafeInteger(value) && value >= 0;
    if (!submitted || !receipt || !Array.isArray(cards) || !['like', 'dislike', 'favourite'].includes(submitted.action) ||
        !['profile_ref', 'publication_ref', 'target_ref', 'command_id'].every(key => uuid(submitted[key])) ||
        !revision(submitted.publication_revision) || submitted.publication_revision < 1 ||
        receipt.profile_ref !== submitted.profile_ref || receipt.command_id !== submitted.command_id ||
        receipt.target_ref !== submitted.target_ref || receipt.requested_action !== submitted.action ||
        !['committed', 'unchanged'].includes(receipt.preference_outcome) ||
        receipt.selected_state !== submitted.action || !revision(receipt.interest_revision) || !revision(receipt.saved_revision)) fail();
    // Only fresh server-rendered cards from this same publication can replace
    // the slot. A saved receipt never authorizes a refill, rerank or next run.
    if (new Set(cards.map(card => card.target_ref)).size !== cards.length || cards.some(card => card.profile_ref !== submitted.profile_ref || card.publication_ref !== submitted.publication_ref ||
        card.publication_revision !== submitted.publication_revision || !uuid(card.target_ref) || !uuid(card.command_id) ||
        !['none', 'like', 'dislike', 'favourite'].includes(card.selected_state) || typeof card.enabled !== 'boolean')) fail();
    const saved = cards.filter(card => card.target_ref === submitted.target_ref);
    if (saved.length !== 1 || saved[0].selected_state !== receipt.selected_state ||
        saved[0].interest_revision !== receipt.interest_revision || saved[0].saved_revision !== receipt.saved_revision ||
        saved[0].command_id === submitted.command_id) fail();
    const index = cards.indexOf(saved[0]);
    const ordered = cards.slice(index + 1);
    const next = ordered.find(card => card.category === submitted.category && card.enabled && card.selected_state === 'none' && !consumed.has(card.target_ref));
    return { saved: saved[0], next: next || null };
  }
  const SLOT_UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
  const SLOT_LIMIT = 2 * 1024 * 1024; // Local read containment, not an owner/provider allowance.
  class SlotReadError extends Error {
    constructor(recorded = false, refused = false) { super('Current response could not be reconciled'); this.recorded = recorded; this.refused = refused; }
  }
  function slotRevision(value, positive = false) {
    const number = Number(value);
    if (!/^(?:0|[1-9][0-9]*)$/.test(String(value)) || !Number.isSafeInteger(number) || number < (positive ? 1 : 0)) throw new SlotReadError();
    return number;
  }
  function refreshMarker(data) {
    if (!data || data.schema !== 'card-action-refresh/v1' || !SLOT_UUID.test(data.profileRef || '') ||
        !['current', 'unavailable'].includes(data.readState) || !['true', 'false'].includes(data.receiptPresent)) throw new SlotReadError();
    const publication = data.publicationRef || null;
    const revision = data.publicationRevision ? slotRevision(data.publicationRevision, true) : null;
    if ((publication === null) !== (revision === null) || publication && !SLOT_UUID.test(publication) || data.readState === 'current' && !publication) throw new SlotReadError();
    let receipt = null;
    if (data.receiptPresent === 'true') {
      if (!SLOT_UUID.test(data.commandId || '') || !SLOT_UUID.test(data.targetRef || '') ||
          !['like', 'dislike', 'favourite', 'obtain', 'undo'].includes(data.requestedAction) ||
          !['committed', 'unchanged'].includes(data.preferenceOutcome) || !['none', 'like', 'dislike', 'favourite'].includes(data.selectedState) ||
          !['none', 'fresh', 'feedback_pending'].includes(data.rankingState)) throw new SlotReadError();
      receipt = { profile_ref: data.profileRef, command_id: data.commandId, target_ref: data.targetRef,
        requested_action: data.requestedAction, preference_outcome: data.preferenceOutcome, selected_state: data.selectedState,
        interest_revision: slotRevision(data.interestRevision), saved_revision: slotRevision(data.savedRevision), ranking_state: data.rankingState };
    }
    return { profile_ref: data.profileRef, publication_ref: publication, publication_revision: revision, read_state: data.readState, receipt };
  }
  function cabinetReadURL(page, submitted) {
    const url = new URL(page);
    if (url.pathname !== '/cabinet' || url.searchParams.get('profile_ref') !== submitted.profile_ref ||
        Array.from(url.searchParams.keys()).some(key => !['profile_ref', 'connection_ref', 'category', 'run_ref', 'action_receipt'].includes(key) || url.searchParams.getAll(key).length !== 1)) throw new SlotReadError();
    for (const key of ['profile_ref', 'connection_ref', 'run_ref', 'action_receipt']) if (url.searchParams.has(key) && !SLOT_UUID.test(url.searchParams.get(key))) throw new SlotReadError();
    if (url.searchParams.has('category') && !/^[a-z_]{1,64}$/.test(url.searchParams.get('category'))) throw new SlotReadError();
    url.hash = ''; url.searchParams.set('action_receipt', submitted.command_id);
    return { url: url.href, contextual: ['category', 'connection_ref', 'run_ref'].some(key => url.searchParams.has(key)) };
  }
  function saveRedirectURL(actual, origin, submitted) {
    const url = new URL(actual);
    if (url.origin !== origin || url.pathname !== '/cabinet' || url.hash || Array.from(url.searchParams.keys()).length !== 2 ||
        url.searchParams.getAll('profile_ref').length !== 1 || url.searchParams.get('profile_ref') !== submitted.profile_ref ||
        url.searchParams.getAll('action_receipt').length !== 1 || url.searchParams.get('action_receipt') !== submitted.command_id) throw new SlotReadError(false, true);
    return url;
  }
  function ownedSaveReceipt(marker, submitted) {
    const receipt = marker.receipt;
    if (marker.profile_ref !== submitted.profile_ref || !receipt || receipt.profile_ref !== submitted.profile_ref ||
        receipt.command_id !== submitted.command_id || receipt.target_ref !== submitted.target_ref ||
        receipt.requested_action !== submitted.action || receipt.selected_state !== submitted.action || !['committed', 'unchanged'].includes(receipt.preference_outcome)) throw new SlotReadError(false, marker.read_state === 'unavailable');
    return receipt;
  }
  async function boundedSlotHTML(response) {
    if ((response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() !== 'text/html' ||
        Number(response.headers.get('Content-Length') || 0) > SLOT_LIMIT || !response.body?.getReader) throw new SlotReadError();
    const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true });
    const chunks = []; let size = 0;
    try {
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        size += value.byteLength;
        if (size > SLOT_LIMIT) { await reader.cancel(); throw new SlotReadError(); }
        chunks.push(decoder.decode(value, { stream: true }));
      }
      chunks.push(decoder.decode()); return chunks.join('');
    } finally { reader.releaseLock(); }
  }
  async function requestCardSave({ submitted, pageURL, body, fetchImpl, parse }) {
    const context = cabinetReadURL(pageURL, submitted), origin = new URL(pageURL).origin;
    let recorded = false;
    try {
      // Never retry or fall back to native resubmission after this boundary:
      // a missing/failed redirected read can follow a committed response.
      const response = await fetchImpl(origin + '/cabinet/actions/', { method: 'POST', body, mode: 'same-origin', credentials: 'same-origin',
        cache: 'no-store', redirect: 'follow', headers: { Accept: 'text/html' } });
      if (response.status !== 200) throw new SlotReadError(false, [401, 403, 409, 410].includes(response.status));
      if (!response.redirected) throw new SlotReadError();
      saveRedirectURL(response.url, origin, submitted);
      let snapshot = parse(await boundedSlotHTML(response));
      ownedSaveReceipt(snapshot.marker, submitted); recorded = true;
      if (context.contextual) {
        const current = await fetchImpl(context.url, { method: 'GET', mode: 'same-origin', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'text/html' } });
        if (current.status !== 200 || current.redirected || new URL(current.url).href !== context.url) throw new SlotReadError(true, true);
        snapshot = parse(await boundedSlotHTML(current));
        ownedSaveReceipt(snapshot.marker, submitted);
      }
      return { snapshot, receipt: snapshot.marker.receipt };
    } catch (error) { throw new SlotReadError(recorded || error?.recorded === true, error?.refused === true); }
  }
  function one(root, selector) {
    const nodes = root.querySelectorAll(selector);
    if (nodes.length !== 1) throw new SlotReadError();
    return nodes[0];
  }
  function field(form, name) {
    const input = one(form, 'input[name="' + name + '"]');
    if (input.type !== 'hidden' || input.disabled) throw new SlotReadError();
    return input.value;
  }
  function cardModel(form) {
    const card = form.closest('[data-card-action-card]');
    const category = card?.closest('[data-card-category]')?.dataset.cardCategory;
    const state = card && one(card, '.card-actions[data-selected-state]').dataset.selectedState;
    const model = { category, selected_state: state, form, node: card };
    for (const key of ['profile_ref', 'publication_ref', 'target_ref', 'command_id']) {
      model[key] = field(form, key); if (!SLOT_UUID.test(model[key])) throw new SlotReadError();
    }
    for (const key of ['publication_revision', 'interest_revision', 'saved_revision']) model[key] = slotRevision(field(form, key), key === 'publication_revision');
    if (!/^[a-z_]{1,64}$/.test(category || '') || !['none', 'like', 'dislike', 'favourite'].includes(state)) throw new SlotReadError();
    model.enabled = ['like', 'dislike', 'favourite'].map(action => {
      const button = one(form, 'button[name="action"][value="' + action + '"]');
      return !button.disabled;
    }).some(Boolean);
    return model;
  }
  function parseSlotDocument(doc) {
    const markerNode = one(doc, '[data-card-action-refresh]');
    return { doc, markerNode, marker: refreshMarker(markerNode.dataset) };
  }
  function currentSlotView(snapshot, origin, consumed) {
    const { doc, marker } = snapshot;
    const status = one(doc, '[data-card-current-status]'), deck = one(doc, '[data-card-current-deck]');
    // Parse inert HTML, then admit only the two server-designated fragments.
    // Never transplant document policy/scripts or an arbitrary write caller.
    for (const root of [status, deck]) {
      if (root.querySelector('script,style,link,base,meta,iframe,object,embed')) throw new SlotReadError();
      for (const node of [root, ...root.querySelectorAll('*')]) {
        for (const attr of node.attributes) if (/^on/i.test(attr.name) || attr.name === 'srcdoc') throw new SlotReadError();
        for (const attr of ['href', 'src']) if (node.hasAttribute(attr)) {
          const url = new URL(node.getAttribute(attr), origin);
          if (!['http:', 'https:'].includes(url.protocol)) throw new SlotReadError();
        }
      }
      for (const form of root.querySelectorAll('form')) {
        const path = root === status ? (form.hasAttribute('data-native-enhance-start') ? '/enhance/start' : '/cabinet/saved/select/') :
          (form.hasAttribute('data-card-action-form') ? '/cabinet/actions/' : form.hasAttribute('data-card-action-undo') ? '/cabinet/actions/undo/' : null);
        const url = new URL(form.getAttribute('action'), origin);
        if (!path || url.origin !== origin || url.pathname !== path || url.search || url.hash || form.method.toLowerCase() !== 'post' ||
            field(form, 'profile_ref') !== marker.profile_ref || !/^[A-Za-z0-9]{32,128}$/.test(field(form, 'csrfmiddlewaretoken')) ||
            form.querySelector('[formaction],[formmethod],[formenctype],[formtarget]')) throw new SlotReadError();
      }
    }
    const categories = Array.from(deck.querySelectorAll('[data-card-category]'), node => node.dataset.cardCategory);
    if (new Set(categories).size !== categories.length) throw new SlotReadError();
    const cards = Array.from(deck.querySelectorAll('[data-card-action-form]'), cardModel);
    if (new Set(cards.map(card => card.target_ref)).size !== cards.length || new Set(cards.map(card => card.command_id)).size !== cards.length ||
        cards.some(card => card.profile_ref !== marker.profile_ref || card.publication_ref !== marker.publication_ref ||
          card.publication_revision !== marker.publication_revision || consumed.has(card.command_id))) throw new SlotReadError();
    if (marker.read_state !== 'current') throw new SlotReadError(!!marker.receipt, true);
    if (deck.querySelectorAll('[data-card-action-card]').length !== cards.length) throw new SlotReadError();
    return { status, deck, cards };
  }
  const boundNoiseInputs = new WeakSet();
  function bindNoiseControls(root) {
    for (const widget of root.querySelectorAll('[data-noise-control]')) {
      const input = widget.querySelector('input[type="range"][name="noise"]');
      const output = widget.querySelector('[data-noise-output]');
      if (!input || !output || !input.form || input.form !== widget.closest('form') ||
          !input.id || output.getAttribute('for') !== input.id || boundNoiseInputs.has(input) ||
          input.min !== '0' || input.max !== '100' || input.step !== '1') continue;
      // This is a form-local numeric readout only. Never touch a saved default,
      // frozen run summary, card gesture, storage or native submit path.
      const update = () => {
        if (!/^(?:100|[1-9]?\d)$/.test(input.value)) return;
        output.textContent = input.value;
        input.setAttribute('aria-valuetext', input.value + ' · next round, Precise to Experimental');
      };
      update();
      input.addEventListener('input', update);
      input.addEventListener('change', update);
      boundNoiseInputs.add(input);
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { HoldGesture, SubmissionGate, targetAt, bindNoiseControls, confirmedSlotResult, refreshMarker, cabinetReadURL, saveRedirectURL, boundedSlotHTML, requestCardSave, parseSlotDocument, currentSlotView };
  if (typeof document === 'undefined') return;
  // Guide/Prepare pages have no cards; the range binding must precede that exit.
  bindNoiseControls(document);

  const forms = Array.from(document.querySelectorAll('[data-card-action-form]'));
  if (!forms.length) return;
  let submissions = new SubmissionGate();
  const gestures = new Set(), bindings = new WeakMap(), undoBindings = new WeakSet(), livePointers = new Set();
  let current = null, suppress = null, returningFocus = false;
  const layer = document.createElement('div');
  layer.className = 'card-bin-layer'; layer.id = 'cabinet-action-bins';
  layer.hidden = true;
  layer.setAttribute('role', 'menu');
  layer.setAttribute('aria-label', 'Card actions');
  const bins = new Map();
  const labels = { like: 'Like', dislike: 'Don’t like', favourite: 'Favourite', obtain: 'Obtain' };
  const icons = { like: '✓', dislike: '×', favourite: '♥', obtain: '↓' };
  for (const [direction, action] of Object.entries(DIRECTIONS)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'card-bin card-bin-' + direction;
    button.dataset.action = action;
    button.setAttribute('role', 'menuitem');
    const icon = document.createElement('span');
    icon.className = 'card-bin-icon'; icon.setAttribute('aria-hidden', 'true'); icon.textContent = icons[action];
    const label = document.createElement('span'); label.textContent = labels[action];
    button.append(icon, label); layer.append(button); bins.set(action, button);
    button.addEventListener('click', () => {
      if (!current || button.disabled) return;
      const form = current.form;
      hide(true);
      commit(form, action);
    });
  }
  document.body.append(layer);
  const announcer = document.createElement('span');
  announcer.className = 'card-actions-sr'; announcer.setAttribute('role', 'status');
  announcer.setAttribute('aria-live', 'polite'); document.body.append(announcer);
  function nativeButton(form, action) { return form.querySelector('button[name="action"][value="' + action + '"]'); }
  function commit(form, action) {
    const button = nativeButton(form, action);
    if (submissions.busy || !button || button.disabled || !form.isConnected) return;
    // requestSubmit preserves the chosen submitter's name/value, CSRF and all
    // server-issued command/revision fields. No optimistic state or second POST.
    form.requestSubmit(button);
  }
  function hide(restoreFocus) {
    const was = current; current = null; layer.hidden = true;
    for (const bin of bins.values()) bin.classList.remove('is-targeted');
    if (!was) return;
    was.card.classList.remove('card-action-floating');
    was.card.style.removeProperty('--card-dx'); was.card.style.removeProperty('--card-dy');
    was.trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus && was.trigger.isConnected) {
      returningFocus = true; was.trigger.focus({ preventScroll: true }); returningFocus = false;
    }
  }
  function show(form, card, trigger, point, keyboard) {
    hide(false);
    if (submissions.busy) return;
    const width = window.innerWidth, height = window.innerHeight;
    const x = Math.max(142, Math.min(width - 142, point.x));
    const y = Math.max(132, Math.min(height - 132, point.y));
    layer.style.setProperty('--bin-x', x + 'px'); layer.style.setProperty('--bin-y', y + 'px');
    current = { form, card, trigger, keyboard };
    for (const [action, bin] of bins) {
      const original = nativeButton(form, action);
      bin.disabled = !original || original.disabled;
      bin.title = original?.getAttribute('title') || '';
    }
    layer.hidden = false; card.classList.add('card-action-floating'); trigger.setAttribute('aria-expanded', 'true');
    announcer.textContent = 'Choose Like on the right, Don’t like on the left, Favourite above, or Obtain below. Escape cancels.';
    if (keyboard) Array.from(bins.values()).find(button => !button.disabled)?.focus();
  }
  function hit(event) {
    if (!current) return null;
    return targetAt(Array.from(bins, ([action, button]) => {
      const rect = button.getBoundingClientRect();
      return { action, enabled: !button.disabled, left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
    }), event.clientX, event.clientY);
  }
  const consumedCommands = new Set();
  function slotEnabled(form) {
    if (typeof fetch !== 'function' || typeof DOMParser !== 'function' || typeof FormData !== 'function') return false;
    try {
      const snapshot = parseSlotDocument(document);
      currentSlotView(snapshot, location.origin, consumedCommands);
      const submitted = cardModel(form);
      cabinetReadURL(location.href, submitted);
      return snapshot.marker.profile_ref === submitted.profile_ref && form.getAttribute('action') === '/cabinet/actions/' &&
        !form.querySelector('[formaction],[formmethod],[formenctype],[formtarget]');
    } catch (_) { return false; } // Unenhanced pages retain the normal native form.
  }
  function prepareSlotSave(form, submitter) {
    const submitted = { ...cardModel(form), action: submitter.value };
    const body = new FormData(form);
    if (body.has('action')) throw new SlotReadError();
    body.append(submitter.name, submitter.value);
    const card = submitted.node;
    return { submitted, body, card, pageURL: location.href, top: card.getBoundingClientRect().top,
      height: card.getBoundingClientRect().height, focus: document.activeElement,
      oldView: currentSlotView(parseSlotDocument(document), location.origin, consumedCommands) };
  }
  function slotMessage(root, text, error = false) {
    root.querySelector('[data-card-slot-feedback]')?.remove();
    const box = document.createElement('p'); box.className = 'card-slot-feedback';
    box.dataset.cardSlotFeedback = ''; if (error) box.dataset.error = '';
    box.setAttribute('role', 'status'); box.tabIndex = -1; box.textContent = text;
    root.prepend(box); return box;
  }
  function showSlotFailure(card, recorded, refused) {
    hide(false);
    const deck = document.querySelector('[data-card-current-deck]'), status = document.querySelector('[data-card-current-status]');
    // Unknown writes stay locked until an explicit current read. An explicit
    // authority refusal additionally conceals the protected old publication.
    for (const root of [deck, status]) if (root) {
      for (const control of root.querySelectorAll('form button,form input:not([type="hidden"]),form select,form textarea')) control.disabled = true;
      for (const reason of root.querySelectorAll('[data-card-reasons]')) reason.hidden = true;
    }
    if (refused) {
      if (deck) deck.hidden = true;
      if (status) status.hidden = true;
    }
    const root = refused ? deck?.parentElement : card;
    if (!root) return;
    const message = slotMessage(root, recorded ? 'Response recorded, but current picks could not refresh. Check the recorded outcome before another action.' :
      'The response could not be confirmed. Check its recorded receipt before another action; nothing is retried automatically.', true);
    const receipt = card.querySelector('[data-card-action-pending] a');
    if (receipt) { const link = receipt.cloneNode(true); link.textContent = 'Check this action’s recorded receipt'; message.append(' ', link); }
    const read = document.createElement('a'); read.href = location.href; read.textContent = 'Read the current cabinet'; message.append(' ', read);
    if (refused) message.focus({ preventScroll: true });
    announcer.textContent = message.textContent;
  }
  async function saveInPlace(prepared) {
    const { submitted, card, oldView } = prepared;
    consumedCommands.add(submitted.command_id);
    let recorded = false;
    try {
      const { snapshot, receipt } = await requestCardSave({ ...prepared, fetchImpl: window.fetch.bind(window),
        parse: text => parseSlotDocument(new DOMParser().parseFromString(text, 'text/html')) });
      recorded = true;
      const view = currentSlotView(snapshot, location.origin, consumedCommands);
      let result = null;
      if (snapshot.marker.publication_ref === submitted.publication_ref && snapshot.marker.publication_revision === submitted.publication_revision) {
        try { result = confirmedSlotResult(submitted, receipt, view.cards); } catch (_) { /* Current server truth supersedes the historical response. */ }
      }
      const shouldFocus = document.activeElement === prepared.focus || document.activeElement === document.body || card.contains(document.activeElement);
      let anchor = null;
      if (result) {
        // Retain local slot order, but select the successor from SERVER order
        // above. Move each real node once; the saved card and its native Undo
        // remain in the deck, never a cloned card or a durable replacement queue.
        for (const category of view.deck.querySelectorAll('[data-card-category]')) {
          const models = view.cards.filter(item => item.category === category.dataset.cardCategory);
          const byTarget = new Map(models.map(item => [item.target_ref, item.node])), ordered = [];
          for (const old of oldView.cards.filter(item => item.category === category.dataset.cardCategory)) {
            const fresh = byTarget.get(old.target_ref); if (fresh) { ordered.push(fresh); byTarget.delete(old.target_ref); }
          }
          ordered.push(...byTarget.values());
          // Place into the existing action-card positions, retaining other
          // read-only content and category notices in their server positions.
          const positions = models.map(item => {
            const position = document.createComment('current card position'); item.node.replaceWith(position); return position;
          });
          positions.forEach((position, index) => position.replaceWith(ordered[index]));
        }
        anchor = result.saved.node;
        if (result.next) {
          const placeholder = document.createComment('saved response slot');
          result.saved.node.replaceWith(placeholder);
          result.next.node.replaceWith(result.saved.node);
          placeholder.replaceWith(result.next.node); anchor = result.next.node;
          anchor.classList.add('card-slot-enter');
        }
        slotMessage(anchor, result.next ? labels[submitted.action] + ' recorded. Next available pick in this category. Your previous response and Undo remain in the deck.' :
          labels[submitted.action] + ' recorded. No later unanswered pick is available in this category. You can review or Undo this response here.');
      } else slotMessage(view.status, 'Response recorded. Showing the current cabinet; these picks may have changed since your response.');
      // Prepare everything before replacing either region: forms, ranking and
      // For You copy must come from the same current read, including run barriers.
      const minimumHeight = oldView.deck.getBoundingClientRect().height;
      const targetTop = card.getBoundingClientRect().top;
      for (const form of oldView.deck.querySelectorAll('[data-card-action-form]')) bindings.get(form)?.();
      oldView.status.replaceWith(view.status); oldView.deck.replaceWith(view.deck);
      document.querySelector('[data-card-action-refresh]').replaceWith(snapshot.markerNode);
      if (result) view.deck.style.minHeight = minimumHeight + 'px';
      submissions = new SubmissionGate();
      for (const form of view.deck.querySelectorAll('[data-card-action-form]')) bindCard(form);
      for (const undo of view.deck.querySelectorAll('[data-card-action-undo]')) bindUndo(undo);
      bindNoiseControls(view.status);
      for (const status of view.deck.querySelectorAll('[data-obtain-status]')) {
        try { window.CabinetObtain?.mount(status); } catch (_) { /* Native Obtain details remain available. */ }
      }
      if (anchor) {
        anchor.style.minHeight = prepared.height + 'px';
        const heading = anchor.querySelector('h3');
        if (shouldFocus && heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
        window.scrollBy({ top: anchor.getBoundingClientRect().top - targetTop, behavior: 'instant' });
      } else if (shouldFocus) view.status.querySelector('[data-card-slot-feedback]')?.focus({ preventScroll: true });
      announcer.textContent = result?.next ? 'Response recorded. Next pick ready in the same place.' : 'Response recorded. Current cabinet refreshed.';
    } catch (error) { showSlotFailure(card, recorded || error?.recorded === true, error?.refused === true); }
  }
  document.addEventListener('submit', event => {
    if (submissions.busy && event.target.closest('[data-card-current-status],[data-card-current-deck]')) event.preventDefault();
  }, true);
  function bindCard(form) {
    if (bindings.has(form)) return;
    const disposers = [];
    const listen = (node, type, handler, options) => { node.addEventListener(type, handler, options); disposers.push(() => node.removeEventListener(type, handler, options)); };
    const card = form.closest('[data-card-action-card]');
    const trigger = card?.querySelector('[data-card-actions-open]');
    if (!card || !trigger) return;
    trigger.hidden = false; trigger.setAttribute('aria-controls', layer.id);
    listen(trigger, 'click', () => {
      if (returningFocus) return;
      if (current?.trigger === trigger) { gesture.cancel('toggle'); hide(true); return; }
      gesture.cancel('keyboard');
      const rect = card.getBoundingClientRect();
      show(form, card, trigger, { x: rect.left + rect.width / 2, y: Math.max(132, rect.top + 150) }, true);
    });
    // Disabling a submitter here would omit action=... from native form data.
    // Keep its values intact and block subsequent submit events instead.
    listen(form, 'submit', event => {
      if (event.defaultPrevented) return;
      if (!submissions.claim()) { event.preventDefault(); return; }
      const submitter = event.submitter;
      const inPlace = ['like', 'dislike', 'favourite'].includes(submitter?.value) && slotEnabled(form);
      let prepared;
      if (inPlace) {
        event.preventDefault();
        try { prepared = prepareSlotSave(form, submitter); }
        catch (_) { gesture.cancel('submitting'); showSlotFailure(card, false, false); return; }
      }
      gesture.cancel('submitting'); hide(false);
      form.setAttribute('aria-busy', 'true');
      const pending = card.querySelector('[data-card-action-pending]'); if (pending) pending.hidden = false;
      for (const button of form.querySelectorAll('button[name="action"]')) button.setAttribute('aria-disabled', 'true');
      announcer.textContent = 'Saving your action. Wait for the recorded result.';
      if (prepared) void saveInPlace(prepared);
    });
    let heldForm = null;
    const gesture = new HoldGesture({
      lift(point) {
        heldForm = form;
        show(form, card, trigger, point, false);
        try { card.setPointerCapture(point.id); } catch { gesture.cancel('capture-lost'); }
      },
      move(event, target) {
        if (!current) return;
        const rect = card.getBoundingClientRect();
        card.style.setProperty('--card-dx', Math.max(-18, Math.min(18, (event.clientX - rect.left - rect.width / 2) / 8)) + 'px');
        card.style.setProperty('--card-dy', Math.max(-18, Math.min(18, (event.clientY - rect.top - Math.min(rect.height / 2, 150)) / 8)) + 'px');
        for (const [action, button] of bins) button.classList.toggle('is-targeted', target?.action === action);
      },
      end(reason, active) {
        if (reason === 'release' || active) suppress = { card, until: Date.now() + 600 };
        hide(false);
      },
      commit(action) { if (heldForm) commit(heldForm, action); heldForm = null; }
    });
    gestures.add(gesture);
    listen(card, 'pointerdown', event => {
      suppress = null;
      if (current?.keyboard) hide(true);
      const excluded = !!event.target.closest('a,button,input,select,textarea,label,output,summary,details,[data-noise-control],[contenteditable],[role="button"]')
        || !!window.getSelection()?.toString() || livePointers.size > 1;
      gesture.down({ pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY,
        button: event.button, isPrimary: event.isPrimary, excluded });
    });
    listen(card, 'pointermove', event => {
      if (gesture.phase === 'active') event.preventDefault();
      gesture.move(event, hit(event));
    }, { passive: false });
    listen(card, 'pointerup', event => {
      const active = gesture.phase === 'active';
      gesture.up(event, hit(event));
      if (active) {
        event.preventDefault();
        try { if (card.hasPointerCapture(event.pointerId)) card.releasePointerCapture(event.pointerId); } catch { /* Already released. */ }
      }
    });
    for (const name of ['pointercancel', 'lostpointercapture']) listen(card, name, () => gesture.cancel(name));
    listen(card, 'contextmenu', event => { if (gesture.phase === 'active') event.preventDefault(); });
    listen(window, 'blur', () => { livePointers.clear(); gesture.cancel('blur'); hide(true); });
    listen(document, 'visibilitychange', () => { if (document.hidden) { livePointers.clear(); gesture.cancel('visibility'); hide(false); } });
    listen(window, 'scroll', () => { if (gesture.phase === 'pending') gesture.cancel('scroll-intent'); }, { passive: true });
    listen(document, 'keydown', event => {
      if (event.key === 'Escape') { gesture.cancel('escape'); hide(true); }
    });
    bindings.set(form, () => { gesture.cancel('replacement'); gestures.delete(gesture); for (const dispose of disposers) dispose(); bindings.delete(form); });
  }
  for (const form of forms) bindCard(form);
  document.addEventListener('pointerdown', event => {
    livePointers.add(event.pointerId);
    if (livePointers.size > 1) { for (const gesture of gestures) gesture.cancel('multiple-pointers'); hide(true); }
  }, true);
  for (const name of ['pointerup', 'pointercancel']) document.addEventListener(name, event => livePointers.delete(event.pointerId), true);
  document.addEventListener('keydown', event => {
    if (!current || layer.hidden) return;
    const action = { ArrowRight: 'like', ArrowLeft: 'dislike', ArrowUp: 'favourite', ArrowDown: 'obtain' }[event.key];
    if (action) { event.preventDefault(); const button = bins.get(action); if (!button.disabled) button.focus(); else announcer.textContent = button.title || 'That action is unavailable.'; }
    else if (event.key === 'Tab') hide(true);
  });
  document.addEventListener('click', event => {
    if (suppress && Date.now() <= suppress.until && suppress.card.contains(event.target) && event.detail !== 0) {
      event.preventDefault(); event.stopPropagation(); suppress = null;
    }
    if (current && !layer.contains(event.target) && !current.trigger.contains(event.target)) hide(true);
  }, true);
  function bindUndo(undo) {
    if (undoBindings.has(undo)) return; undoBindings.add(undo);
    undo.addEventListener('submit', event => {
      if (event.defaultPrevented) return;
      if (!submissions.claim()) { event.preventDefault(); return; }
      for (const gesture of gestures) gesture.cancel('submitting');
      hide(false); undo.setAttribute('aria-busy', 'true');
    });
  }
  for (const undo of document.querySelectorAll('[data-card-action-undo]')) bindUndo(undo);
  window.addEventListener('resize', () => { for (const gesture of gestures) gesture.cancel('resize'); hide(true); });
  window.addEventListener('pagehide', () => { for (const gesture of gestures) gesture.cancel('pagehide'); hide(false); livePointers.clear(); });
})();
