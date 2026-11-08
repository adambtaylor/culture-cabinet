/* The sole backend producer supplies status. This client only reads a bound local
 * job; native explicit forms own every submission, advance and reconciliation. */
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
  const fields = new Set(['id', 'state', 'label', 'detail_href', 'verification_until', 'stage', 'reason', 'revision', 'next_action', 'observed_at']);
  // This registry only coalesces duplicate views of one owned job in one document.
  // It neither persists status nor changes any application/authority timer.
  const documents = new WeakMap();
  const safeID = value => Number.isSafeInteger(value) && value > 0;
  const stamp = value => typeof value === 'string' && /^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(value) ? Date.parse(value) : NaN;

  function localHref(value, origin) {
    if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) throw new Error('Local Obtain link required');
    const url = new URL(value, origin);
    if (url.origin !== origin || !url.pathname.startsWith('/cabinet/obtain/')) throw new Error('Obtain link outside native flow');
    return url.pathname + url.search + url.hash;
  }
  function statusHref(value, origin, profile) {
    const href = localHref(value, origin), url = new URL(href, origin);
    const match = url.pathname.match(/^\/cabinet\/obtain\/jobs\/([1-9][0-9]*)\/status\/$/);
    if (!match || !safeID(Number(match[1])) || url.hash) throw new Error('Exact local job status required');
    for (const [key, value] of url.searchParams) {
      if (key !== 'profile_ref' || url.searchParams.getAll(key).length !== 1 || !profile || value !== profile) throw new Error('Different status profile or unexpected query');
    }
    return href;
  }
  function projection(value, origin, now = Date.now()) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || !states.has(value.state) || typeof value.label !== 'string' || !value.label.trim()) throw new Error('Flat Obtain projection required');
    const href = localHref(value.detail_href, origin);
    if (value.next_action !== undefined && !actions.has(value.next_action)) throw new Error('Closed next action required');
    if (value.revision !== undefined && (!Number.isSafeInteger(value.revision) || value.revision < 0)) throw new Error('Current revision required');
    // Queue labels and HTTP200 cannot create green. Finite done comes only from
    // the backend LibraryVerification; the five-minute ceiling is its contract.
    const until = stamp(value.verification_until);
    const verified = value.state === 'done' && Number.isFinite(until) && until > now && until <= now + 300000;
    const expired = value.state === 'done' && !verified, state = expired ? 'idle' : value.state;
    return {state, label: expired ? 'Verification needed' : value.label, detail_href: href,
      verification_until: verified ? value.verification_until : null, verified, icon: icons[state],
      recorded_state: value.state, next_action: value.next_action || 'none', revision: value.revision};
  }
  function statusRecord(text, context, lastRevision) {
    // One small, flat local record; this is a client payload guard, not a new
    // provider/resource authorization or an alternate verification service.
    if (typeof text !== 'string' || text.length > 65536) throw new Error('Bounded status record required');
    const value = JSON.parse(text);
    if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length !== fields.size || Object.keys(value).some(key => !fields.has(key))) throw new Error('Exact flat status record required');
    if (Object.values(value).some(item => item !== null && !['string', 'number'].includes(typeof item))) throw new Error('Flat status scalars required');
    if (value.id !== context.id || !Number.isSafeInteger(value.revision) || value.revision < Math.max(0, lastRevision)) throw new Error('Different job or older revision');
    if (typeof value.stage !== 'string' || !(value.reason === null || typeof value.reason === 'string') || !(value.observed_at === null || Number.isFinite(stamp(value.observed_at)))) throw new Error('Invalid status detail');
    projection(value, context.origin, context.clock());
    const detail = new URL(value.detail_href, context.origin);
    if (detail.pathname !== '/cabinet/obtain/jobs/' + context.id + '/') throw new Error('Different job detail');
    return value;
  }

  function coordinator(map, key, context, env, doc) {
    const members = new Set();
    let timer, windowTimer, busy = false, failed = false, stopped = false, abort, epoch = 0;
    let until = context.until, lastSent = -Infinity, revision = context.revision, state = context.state;
    const visible = () => !doc || doc.visibilityState !== 'hidden';
    const live = () => !stopped && env.clock() < until && [...members].some(member => member.live());
    function pause() { env.cancel(timer); epoch++; if (abort) abort.abort(); }
    function end() { stopped = true; pause(); env.cancel(windowTimer); members.forEach(member => member.ended()); }
    function next() {
      env.cancel(timer);
      if (!live() || !visible() || failed || busy || state !== 'working' || !Number.isFinite(until)) return;
      const delay = Math.max(5000, lastSent + 5000 - env.clock());
      if (env.clock() + delay < until) timer = env.schedule(() => read(true), delay);
    }
    async function read(automatic = false) {
      if (!live()) { if (env.clock() >= until) end(); return; }
      if (!visible() || busy || automatic && failed) return;
      if (automatic && env.clock() - lastSent < 5000) { next(); return; }
      busy = true; const token = ++epoch;
      abort = typeof AbortController === 'function' ? new AbortController() : null;
      lastSent = env.clock(); members.forEach(member => member.busy(true));
      try {
        const response = await env.fetch(context.url, {method: 'GET', mode: 'same-origin', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {Accept: 'application/json'}, ...(abort ? {signal: abort.signal} : {})});
        if (token !== epoch || !live() || !visible()) return;
        if (!response.ok || !/^application\/json(?:\s*;|$)/i.test(response.headers.get('Content-Type') || '')) throw new Error('Current JSON status unavailable');
        const text = await response.text();
        // Neither a late response nor changed markup may renew this original
        // server-owned window. Check again before interpreting/painting it.
        if (token !== epoch || !live() || !visible()) return;
        const value = statusRecord(text, context, revision);
        revision = value.revision; state = value.state; failed = false;
        members.forEach(member => { if (member.live()) member.apply(value); });
      } catch (_) {
        if (token === epoch && live() && visible()) { failed = true; members.forEach(member => { if (member.live()) member.failed(); }); }
      } finally {
        busy = false; abort = null; members.forEach(member => member.busy(false));
        if (env.clock() >= until) end(); else next();
      }
    }
    function armWindow() {
      env.cancel(windowTimer);
      if (Number.isFinite(until)) windowTimer = env.schedule(end, Math.max(0, until - env.clock()));
    }
    const group = {
      add(member, other) {
        members.add(member); until = Math.min(until, other.until); revision = Math.max(revision, other.revision);
        armWindow(); next();
      },
      remove(member) { members.delete(member); if (!members.size) { stopped = true; pause(); env.cancel(windowTimer); map.delete(key); } },
      read
    };
    if (doc) doc.addEventListener('visibilitychange', () => {
      pause();
      if (env.clock() >= until) { end(); return; }
      if (visible() && live()) { members.forEach(member => member.repaint()); next(); }
    });
    return group;
  }

  function mount(root, options = {}) {
    const origin = options.origin || window.location.origin, clock = options.clock || Date.now;
    const env = {clock, fetch: options.fetch || window.fetch.bind(window), schedule: options.setTimeout || window.setTimeout.bind(window), cancel: options.clearTimeout || window.clearTimeout.bind(window)};
    const doc = root.ownerDocument, profile = root.dataset.profileRef, binding = {};
    // Display fields may update, but an in-flight read never adopts a changed
    // job/profile/URL binding or a later deadline from reused markup.
    const initial = {job: root.dataset.obtainJobId, statusProfile: root.dataset.obtainStatusProfile, url: root.dataset.obtainStatusUrl};
    root._cabinetObtainBinding = binding;
    const label = root.querySelector('[data-obtain-label]'), icon = root.querySelector('[data-obtain-icon]');
    const detail = root.querySelector('[data-obtain-detail]'), notice = root.querySelector('[data-obtain-notice]');
    const refreshButton = root.querySelector('[data-obtain-refresh]');
    if (refreshButton) refreshButton.hidden = true;
    const rawUntil = root.dataset.obtainPollUntil || '', until = rawUntil ? stamp(rawUntil) : Infinity;
    let stopped = false, unavailable = false, ended = false, expiryTimer, current, group;
    const say = text => { if (notice && notice.textContent !== text) notice.textContent = text; };
    function connected() {
      if (stopped || root._cabinetObtainBinding !== binding) return false;
      if (root.isConnected === false) { root.dataset.obtainVerified = 'false'; return false; }
      if (root.dataset.profileRef !== profile || root.dataset.obtainJobId !== initial.job || root.dataset.obtainStatusProfile !== initial.statusProfile || root.dataset.obtainStatusUrl !== initial.url) {
        root.dataset.obtainVerified = 'false'; root.hidden = true; return false;
      }
      return true;
    }
    function render(value, fresh = false) {
      if (!connected()) return;
      if (fresh) unavailable = false;
      const view = projection(value, origin, clock()); current = value;
      if (clock() >= until || ended) { ended = true; view.verified = false; view.label = 'Open this request for current status'; view.icon = '○'; }
      else if (unavailable) { view.verified = false; view.label = 'Current status unavailable'; view.icon = '?'; }
      root.dataset.obtainState = view.state; root.dataset.obtainVerified = String(view.verified); root.dataset.obtainNextAction = view.next_action;
      if (label && label.textContent !== view.label) label.textContent = view.label;
      if (icon) icon.textContent = view.icon;
      if (detail) detail.setAttribute('href', view.detail_href);
      const time = root.querySelector('[data-obtain-verification-time]');
      if (time) { time.hidden = !view.verified; if (view.verified) { time.dateTime = view.verification_until; time.textContent = new Date(view.verification_until).toLocaleString(); } }
      const active = doc && doc.activeElement; let lostFocus = false;
      root.querySelectorAll('[data-obtain-next]').forEach(box => {
        const show = !unavailable && !ended && box.dataset.obtainNext.split(' ').includes(view.next_action);
        if (!show && active && box.contains(active)) lostFocus = true; box.hidden = !show;
      });
      if (view.revision !== undefined) root.querySelectorAll('[data-obtain-revision]').forEach(input => { input.value = String(view.revision); });
      if (lostFocus) root.querySelector('[data-obtain-status-heading]')?.focus();
      env.cancel(expiryTimer);
      if (view.verified) expiryTimer = env.schedule(() => render(current), Math.max(1, Math.min(Date.parse(view.verification_until), until) - clock()));
    }
    const member = {
      live: () => connected() && clock() < until,
      repaint() { if (current) render(current); },
      apply(value) { render(value, true); say('Recorded status updated.'); },
      failed() { unavailable = true; if (current) render(current); root.dataset.obtainVerified = 'false'; say('Current status is unavailable. Open the recorded request or review your owner session. Nothing was retried.'); },
      ended() { ended = true; if (current) render(current); if (refreshButton) refreshButton.disabled = true; say('The status read window ended. Open the recorded request to review current access.'); },
      busy(value) { if (refreshButton && connected()) refreshButton.disabled = value || ended; }
    };
    function stop() { stopped = true; env.cancel(expiryTimer); if (root._cabinetObtainBinding === binding) root.dataset.obtainVerified = 'false'; if (group) group.remove(member); }
    try {
      render({state: root.dataset.obtainState, label: label ? label.textContent : '', detail_href: detail ? detail.getAttribute('href') : '', verification_until: root.dataset.obtainVerificationUntil || null, next_action: root.dataset.obtainNextAction, revision: root.dataset.obtainRevision === undefined ? undefined : Number(root.dataset.obtainRevision)}, true);
      if (root.dataset.obtainStatusUrl) {
        const id = Number(root.dataset.obtainJobId), revision = root.dataset.obtainRevision === undefined ? -1 : Number(root.dataset.obtainRevision);
        if (!safeID(id) || String(id) !== root.dataset.obtainJobId || !profile || root.dataset.obtainStatusProfile !== profile || !Number.isFinite(until) && until !== Infinity || !Number.isSafeInteger(revision) || revision < -1) throw new Error('Current bound job context required');
        const url = statusHref(root.dataset.obtainStatusUrl, origin, profile);
        if (Number(new URL(url, origin).pathname.match(/\/jobs\/([0-9]+)\/status\/$/)[1]) !== id) throw new Error('Different status job');
        const context = {origin, id, profile, url, until, revision, state: root.dataset.obtainState, clock};
        const documentKey = doc || root;
        let map = documents.get(documentKey); if (!map) { map = new Map(); documents.set(documentKey, map); }
        const key = origin + '|' + profile + '|' + id;
        group = map.get(key); if (!group) { group = coordinator(map, key, context, env, doc); map.set(key, group); }
        group.add(member, context);
        if (refreshButton) { refreshButton.hidden = false; refreshButton.addEventListener('click', event => { event.preventDefault(); group.read(false); }); }
      }
    } catch (_) { member.failed(); say('Open the recorded request for its current status.'); }
    return {refresh: () => group ? group.read(false) : Promise.resolve(), stop};
  }
  return {projection, localHref, statusHref, mount};
});
