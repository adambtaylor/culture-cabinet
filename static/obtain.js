/* Obtain status is a read-only projection. This script never submits, advances,
 * retries, imports or verifies a job; native explicit forms own those actions. */
(function (factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else {
    window.CabinetObtain = api;
    const start = () => document.querySelectorAll('[data-obtain-status]').forEach(root => api.mount(root));
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})(function () {
  'use strict';
  const states = new Set(['idle', 'working', 'error', 'done']);
  const actions = new Set(['none', 'choose', 'confirm', 'check', 'reconcile', 'reauthorize']);
  const icons = {idle: '○', working: '…', error: '!', done: '✓'};

  function localHref(value, origin) {
    if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) throw new Error('Local Obtain link required');
    const url = new URL(value, origin);
    if (url.origin !== origin || !url.pathname.startsWith('/cabinet/obtain/')) throw new Error('Obtain link outside native flow');
    return url.pathname + url.search + url.hash;
  }

  function statusHref(value, origin) {
    const href = localHref(value, origin), url = new URL(href, origin);
    if (!/^\/cabinet\/obtain\/jobs\/[1-9][0-9]*\/status\/$/.test(url.pathname) || url.hash) throw new Error('Exact local job status required');
    for (const [key] of url.searchParams) if (key !== 'profile_ref' || url.searchParams.getAll(key).length !== 1) throw new Error('Unexpected status query');
    return href;
  }

  function projection(value, origin, now = Date.now()) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || !states.has(value.state) || typeof value.label !== 'string' || !value.label.trim()) throw new Error('Flat Obtain projection required');
    const href = localHref(value.detail_href, origin);
    if (value.next_action !== undefined && !actions.has(value.next_action)) throw new Error('Closed next action required');
    if (value.revision !== undefined && (!Number.isSafeInteger(value.revision) || value.revision < 0)) throw new Error('Current revision required');
    // A stored success is not perpetual availability. Accept only explicit done
    // with a finite, current verification window; neither HTTP200 nor label text
    // can create green. The backend remains the sole verification authority.
    const until = typeof value.verification_until === 'string' && /^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(value.verification_until) ? Date.parse(value.verification_until) : NaN;
    const verified = value.state === 'done' && Number.isFinite(until) && until > now && until <= now + 300000;
    const expired = value.state === 'done' && !verified;
    const state = expired ? 'idle' : value.state;
    return {state, label: expired ? 'Verification needed' : value.label, detail_href: href,
      verification_until: verified ? value.verification_until : null, verified,
      icon: icons[state], recorded_state: value.state, next_action: value.next_action || 'none', revision: value.revision};
  }

  function mount(root, options = {}) {
    const origin = options.origin || window.location.origin;
    const clock = options.clock || Date.now;
    const request = options.fetch || window.fetch.bind(window);
    const schedule = options.setTimeout || window.setTimeout.bind(window);
    const cancel = options.clearTimeout || window.clearTimeout.bind(window);
    const initialProfile = root.dataset.profileRef;
    const binding = {};
    root._cabinetObtainBinding = binding;
    const doc = root.ownerDocument;
    const visible = () => !doc || doc.visibilityState !== 'hidden';
    const label = root.querySelector('[data-obtain-label]');
    const icon = root.querySelector('[data-obtain-icon]');
    const detail = root.querySelector('[data-obtain-detail]');
    const notice = root.querySelector('[data-obtain-notice]');
    const refreshButton = root.querySelector('[data-obtain-refresh]');
    let stopped = false, busy = false, readUnavailable = false, expiryTimer, pollTimer, current;
    const connected = () => {
      if (stopped || root.isConnected === false || root._cabinetObtainBinding !== binding) return false;
      if (root.dataset.profileRef !== initialProfile) {
        root.dataset.obtainVerified = 'false';
        root.hidden = true;
        return false;
      }
      return true;
    };
    const say = text => { if (notice && notice.textContent !== text) notice.textContent = text; };

    function render(value, fresh = false) {
      if (!connected()) return;
      if (fresh) readUnavailable = false;
      const view = projection(value, origin, clock());
      if (readUnavailable) { view.verified = false; view.label = 'Current status unavailable'; view.icon = '?'; }
      current = value;
      root.dataset.obtainState = view.state;
      root.dataset.obtainVerified = String(view.verified);
      root.dataset.obtainNextAction = view.next_action;
      if (label && label.textContent !== view.label) label.textContent = view.label;
      if (icon) icon.textContent = view.icon;
      if (detail) detail.setAttribute('href', view.detail_href);
      const time = root.querySelector('[data-obtain-verification-time]');
      if (time) {
        time.hidden = !view.verified;
        if (view.verified) { time.dateTime = view.verification_until; time.textContent = new Date(view.verification_until).toLocaleString(); }
      }
      const active = doc && doc.activeElement;
      let actionLostFocus = false;
      root.querySelectorAll('[data-obtain-next]').forEach(box => {
        const show = !readUnavailable && box.dataset.obtainNext.split(' ').includes(view.next_action);
        if (!show && active && box.contains(active)) actionLostFocus = true;
        box.hidden = !show;
      });
      if (view.revision !== undefined) root.querySelectorAll('[data-obtain-revision]').forEach(input => { input.value = String(view.revision); });
      if (actionLostFocus) root.querySelector('[data-obtain-status-heading]')?.focus();
      cancel(expiryTimer);
      if (view.verified) expiryTimer = schedule(() => render(current), Math.max(1, Date.parse(view.verification_until) - clock()));
    }

    function nextPoll() {
      // Optional polling needs an explicit finite host window. Missing/expired
      // bounds leave the native read-only refresh button available. Nothing here
      // renews a session, confirms a request or revives a stopped worker.
      const until = Date.parse(root.dataset.obtainPollUntil || '');
      if (connected() && visible() && !readUnavailable && Number.isFinite(until) && clock() + 5000 < until) pollTimer = schedule(refresh, 5000);
    }

    async function refresh() {
      if (!connected() || !visible() || busy) return;
      busy = true;
      if (refreshButton) refreshButton.disabled = true;
      try {
        const url = statusHref(root.dataset.obtainStatusUrl, origin);
        const response = await request(url, {method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {Accept: 'application/json'}});
        if (!connected()) return;
        if (!response.ok) throw new Error('Current status unavailable');
        const value = await response.json();
        if (!connected()) return;
        const expected = Number(new URL(url, origin).pathname.match(/\/jobs\/([1-9][0-9]*)\/status\/$/)[1]);
        if (!Number.isSafeInteger(expected) || value.id !== expected) throw new Error('Different recorded job');
        render(value, true);
        say('Recorded status updated.');
        nextPoll();
      } catch (_) {
        if (connected()) {
          // A read failure is not a failed download or an unknown submission.
          // Remove current-green styling and describe only the observation gap.
          readUnavailable = true;
          if (current) render(current);
          root.dataset.obtainVerified = 'false';
          say('Current status is unavailable. Open the recorded request or review your owner session. Nothing was retried.');
        }
      } finally {
        busy = false;
        if (refreshButton && connected()) refreshButton.disabled = false;
      }
    }

    function stop() { stopped = true; cancel(expiryTimer); cancel(pollTimer); }
    if (refreshButton) refreshButton.addEventListener('click', event => { event.preventDefault(); cancel(pollTimer); refresh(); });
    if (doc) doc.addEventListener('visibilitychange', () => {
      cancel(pollTimer);
      if (visible() && connected()) { if (current) render(current); nextPoll(); }
    });
    try {
      render({state: root.dataset.obtainState, label: label ? label.textContent : '',
        detail_href: detail ? detail.getAttribute('href') : '', verification_until: root.dataset.obtainVerificationUntil || null,
        next_action: root.dataset.obtainNextAction, revision: root.dataset.obtainRevision === undefined ? undefined : Number(root.dataset.obtainRevision)}, true);
      if (refreshButton && root.dataset.obtainStatusUrl) { statusHref(root.dataset.obtainStatusUrl, origin); refreshButton.hidden = false; }
      nextPoll();
    } catch (_) { root.dataset.obtainVerified = 'false'; say('Open the recorded request for its current status.'); }
    return {refresh, stop};
  }

  return {projection, localHref, statusHref, mount};
});
