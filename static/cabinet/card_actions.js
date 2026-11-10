/* Native forms remain the only write path. Holding reveals choices; neither
 * pointer movement nor a lifted card is a preference or an acquisition intent. */
(function () {
  'use strict';
  const DIRECTIONS = { right: 'like', left: 'dislike', up: 'favourite', down: 'obtain' };
  class HoldGesture {
    constructor(hooks = {}, clock = {}) {
      this.hooks = hooks;
      this.schedule = clock.setTimeout || setTimeout;
      this.clear = clock.clearTimeout || clearTimeout;
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
  if (typeof module !== 'undefined' && module.exports) module.exports = { HoldGesture, SubmissionGate, targetAt, bindNoiseControls };
  if (typeof document === 'undefined') return;
  // Guide/Prepare pages have no cards; the range binding must precede that exit.
  bindNoiseControls(document);

  const forms = Array.from(document.querySelectorAll('[data-card-action-form]'));
  if (!forms.length) return;
  const submissions = new SubmissionGate(), gestures = [], livePointers = new Set();
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
  for (const form of forms) {
    const card = form.closest('[data-card-action-card]');
    const trigger = card?.querySelector('[data-card-actions-open]');
    if (!card || !trigger) continue;
    trigger.hidden = false; trigger.setAttribute('aria-controls', layer.id);
    trigger.addEventListener('click', () => {
      if (returningFocus) return;
      if (current?.trigger === trigger) { gesture.cancel('toggle'); hide(true); return; }
      gesture.cancel('keyboard');
      const rect = card.getBoundingClientRect();
      show(form, card, trigger, { x: rect.left + rect.width / 2, y: Math.max(132, rect.top + 150) }, true);
    });
    // Disabling a submitter here would omit action=... from native form data.
    // Keep its values intact and block subsequent submit events instead.
    form.addEventListener('submit', event => {
      if (!submissions.claim()) { event.preventDefault(); return; }
      gesture.cancel('submitting'); hide(false);
      form.setAttribute('aria-busy', 'true');
      const pending = card.querySelector('[data-card-action-pending]'); if (pending) pending.hidden = false;
      for (const button of form.querySelectorAll('button[name="action"]')) button.setAttribute('aria-disabled', 'true');
      announcer.textContent = 'Saving your action. Wait for the recorded result.';
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
    gestures.push(gesture);
    card.addEventListener('pointerdown', event => {
      suppress = null;
      if (current?.keyboard) hide(true);
      const excluded = !!event.target.closest('a,button,input,select,textarea,label,output,summary,details,[data-noise-control],[contenteditable],[role="button"]')
        || !!window.getSelection()?.toString() || livePointers.size > 1;
      gesture.down({ pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY,
        button: event.button, isPrimary: event.isPrimary, excluded });
    });
    card.addEventListener('pointermove', event => {
      if (gesture.phase === 'active') event.preventDefault();
      gesture.move(event, hit(event));
    }, { passive: false });
    card.addEventListener('pointerup', event => {
      const active = gesture.phase === 'active';
      gesture.up(event, hit(event));
      if (active) {
        event.preventDefault();
        try { if (card.hasPointerCapture(event.pointerId)) card.releasePointerCapture(event.pointerId); } catch { /* Already released. */ }
      }
    });
    for (const name of ['pointercancel', 'lostpointercapture']) card.addEventListener(name, () => gesture.cancel(name));
    card.addEventListener('contextmenu', event => { if (gesture.phase === 'active') event.preventDefault(); });
    window.addEventListener('blur', () => { livePointers.clear(); gesture.cancel('blur'); hide(true); });
    document.addEventListener('visibilitychange', () => { if (document.hidden) { livePointers.clear(); gesture.cancel('visibility'); hide(false); } });
    window.addEventListener('scroll', () => { if (gesture.phase === 'pending') gesture.cancel('scroll-intent'); }, { passive: true });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') { gesture.cancel('escape'); hide(true); }
    });
  }
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
  for (const undo of document.querySelectorAll('[data-card-action-undo]')) undo.addEventListener('submit', event => {
    if (!submissions.claim()) { event.preventDefault(); return; }
    for (const gesture of gestures) gesture.cancel('submitting');
    hide(false); undo.setAttribute('aria-busy', 'true');
  });
  window.addEventListener('resize', () => { for (const gesture of gestures) gesture.cancel('resize'); hide(true); });
  window.addEventListener('pagehide', () => { for (const gesture of gestures) gesture.cancel('pagehide'); hide(false); livePointers.clear(); });
})();
