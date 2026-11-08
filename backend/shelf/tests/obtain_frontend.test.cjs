// Actual static module with injected DOM/clock/fetch. No backend authority,
// provider, file import, runtime or browser is created by these checks.
const assert = require('node:assert/strict');
const path = require('node:path');
const api = require(process.argv[2] || path.resolve(__dirname, '../../../static/obtain.js'));
const origin = 'http://127.0.0.1:50000';
const now = Date.parse('2026-11-08T10:00:00Z');
const base = () => ({id: 42, state: 'working', label: 'Waiting for library indexing', detail_href: '/cabinet/obtain/jobs/42/', verification_until: null, next_action: 'check', revision: 2, stage: 'indexing', reason: 'pending', observed_at: new Date(now).toISOString()});
const done = () => ({...base(), state: 'done', label: 'Verified in Jellyfin', verification_until: new Date(now + 60000).toISOString(), next_action: 'none'});
const results = [];
async function test(name, fn) {
  try { await fn(); results.push({name, pass: true}); }
  catch (error) { results.push({name, pass: false, reason: error.message}); }
}
function setup(value = base(), fetcher = async () => ({ok: true, json: async () => value}), sharedDocument, context = {}) {
  const events = {}, timers = [], calls = [], label = {textContent: value.label}, icon = {textContent: ''}, notice = {textContent: ''};
  const detail = {href: value.detail_href, getAttribute() { return this.href; }, setAttribute(_, v) { this.href = v; }};
  const refresh = {disabled: false, hidden: true, addEventListener(_, fn) { this.click = fn; }};
  const revision = {value: String(value.revision)};
  const button = {}, action = {dataset: {obtainNext: 'check reconcile'}, hidden: false, contains(el) { return el === button; }};
  let focus = 0, time = now;
  const doc = sharedDocument || {visibilityState: 'visible', activeElement: button, addEventListener(k, fn) { events[k] = fn; }};
  const elements = {'[data-obtain-label]': label, '[data-obtain-icon]': icon, '[data-obtain-detail]': detail, '[data-obtain-notice]': notice, '[data-obtain-refresh]': refresh, '[data-obtain-status-heading]': {focus() { focus++; }}};
  const root = {dataset: {profileRef: 'profile-a', obtainStatusProfile: 'profile-a', obtainJobId: '42', obtainState: value.state, obtainVerificationUntil: value.verification_until || '', obtainNextAction: value.next_action, obtainRevision: String(value.revision), obtainStatusUrl: '/cabinet/obtain/jobs/42/status/'}, ownerDocument: doc, isConnected: true,
    querySelector(s) { return elements[s] || null; }, querySelectorAll(s) { return s === '[data-obtain-next]' ? [action] : s === '[data-obtain-revision]' ? [revision] : []; }};
  Object.assign(root.dataset, context);
  function mount() { return api.mount(root, {origin, clock: () => time, fetch: async (url, opts) => {
    calls.push({url, opts}); const response = await fetcher();
    response.headers ||= {get: () => 'application/json'};
    response.text ||= async () => JSON.stringify(await response.json());
    return response;
  }, setTimeout(fn, ms) { timers.push({fn, ms, canceled: false}); return timers.length; }, clearTimeout(id) { if (timers[id - 1]) timers[id - 1].canceled = true; }}); }
  const mounted = mount();
  return {root, mounted, mount, calls, timers, events, doc, label, icon, notice, refresh, action, revision, focus: () => focus, setTime(v) { time = v; }};
}
(async () => {
  await test('working_or_successful_HTTP_is_not_verified_library_arrival', async () => {
    const s = setup(); await s.mounted.refresh();
    assert.equal(s.root.dataset.obtainState, 'working'); assert.equal(s.root.dataset.obtainVerified, 'false');
    assert.equal(s.calls[0].opts.method, 'GET'); assert.equal(s.calls[0].opts.credentials, 'same-origin'); assert.equal(s.calls[0].opts.cache, 'no-store');
    assert.equal(s.calls[0].opts.mode, 'same-origin'); assert.equal(s.calls[0].opts.redirect, 'error');
    assert.equal(s.calls[0].url, '/cabinet/obtain/jobs/42/status/'); assert.equal('body' in s.calls[0].opts, false);
    assert.equal(s.refresh.hidden, false); assert.equal(s.timers.length, 0);
  });
  await test('only_current_explicit_done_is_green_and_expiry_clears_it', () => {
    const s = setup(done()); assert.equal(s.root.dataset.obtainVerified, 'true');
    s.setTime(now + 60001); s.timers[0].fn();
    assert.equal(s.root.dataset.obtainVerified, 'false'); assert.equal(s.label.textContent, 'Verification needed');
    const detached = setup(done()); detached.root.isConnected = false; detached.setTime(now + 60001); detached.timers[0].fn();
    detached.root.isConnected = true; assert.equal(detached.root.dataset.obtainVerified, 'false');
  });
  await test('queued_words_unknown_states_missing_or_stale_proof_never_make_green', () => {
    assert.equal(api.projection({state: 'working', label: 'Downloading', detail_href: '/cabinet/obtain/jobs/42/', verification_until: null}, origin, now).verified, false);
    assert.equal(api.projection({...base(), label: 'Available in Jellyfin'}, origin, now).verified, false);
    assert.throws(() => api.projection({...base(), state: 'queued'}, origin, now));
    for (const until of [null, 'not-a-time', new Date(now - 1).toISOString(), new Date(now + 300001).toISOString()]) assert.equal(api.projection({...done(), verification_until: until}, origin, now).verified, false);
  });
  await test('native_known_error_and_unknown_reconciliation_keep_exact_next_action', () => {
    const v = api.projection({...base(), state: 'error', label: 'Submission outcome unknown', next_action: 'reconcile'}, origin, now);
    assert.equal(v.state, 'error'); assert.equal(v.next_action, 'reconcile'); assert.equal(v.verified, false);
    assert.throws(() => api.projection({...base(), next_action: 'retry_download'}, origin, now));
  });
  await test('failed_or_denied_read_demotes_current_verification_without_claiming_job_failure', async () => {
    for (const response of [{ok: false, status: 403}, {ok: true, headers: {get: () => 'text/html'}, json: async () => done()}, {ok: true, text: async () => JSON.stringify({...done(), label: 'x'.repeat(70000)})}]) {
      const s = setup(done(), async () => response); await s.mounted.refresh();
      assert.equal(s.root.dataset.obtainVerified, 'false'); assert.equal(s.root.dataset.obtainState, 'done');
      assert.equal(s.label.textContent, 'Current status unavailable'); assert.equal(s.icon.textContent, '?'); assert.equal(s.action.hidden, true);
      s.events.visibilitychange(); assert.equal(s.root.dataset.obtainVerified, 'false');
      assert(s.notice.textContent.includes('Nothing was retried.')); assert.equal(s.calls.length, 1);
    }
  });
  await test('foreign_or_effect_URL_is_rejected_before_fetch', async () => {
    for (const url of ['https://other.example/status/', '/cabinet/obtain/jobs/42/advance/', '/cabinet/obtain/jobs/42/status/?actor=1', '/cabinet/obtain/jobs/42/status/?profile_ref=profile-b']) {
      const s = setup(); s.mounted.stop(); s.root.dataset.obtainStatusUrl = url; const mounted = s.mount(); await mounted.refresh(); assert.equal(s.calls.length, 0); assert.equal(s.refresh.hidden, true);
    }
    assert.throws(() => api.localHref('//other.example/', origin));
    assert.equal(api.statusHref('/cabinet/obtain/jobs/42/status/?profile_ref=profile-a', origin, 'profile-a'), '/cabinet/obtain/jobs/42/status/?profile_ref=profile-a');
  });
  await test('late_response_cannot_expose_a_previous_profile_job', async () => {
    let complete; const s = setup(base(), () => new Promise(resolve => { complete = resolve; }));
    const pending = s.mounted.refresh(); s.root.dataset.profileRef = 'profile-b'; complete({ok: true, json: async () => done()}); await pending;
    assert.equal(s.root.hidden, true); assert.equal(s.root.dataset.obtainVerified, 'false'); assert.notEqual(s.label.textContent, 'Verified in Jellyfin');
  });
  await test('wrong_job_or_older_revision_is_not_applied', async () => {
    for (const change of [{id: 43}, {revision: 1}, {revision: undefined}]) {
      const s = setup(base(), async () => ({ok: true, json: async () => ({...done(), ...change})}));
      await s.mounted.refresh(); assert.equal(s.root.dataset.obtainVerified, 'false'); assert.notEqual(s.label.textContent, 'Verified in Jellyfin');
      assert.equal(s.revision.value, '2');
    }
  });
  await test('read_only_update_preserves_focus_until_current_action_disappears', async () => {
    const s = setup(base(), async () => ({ok: true, json: async () => ({...done(), revision: 3})}));
    assert.equal(s.focus(), 0); await s.mounted.refresh(); assert.equal(s.focus(), 1); assert.equal(s.action.hidden, true); assert.equal(s.revision.value, '3');
  });
  await test('polling_requires_finite_host_window_stops_hidden_and_never_goes_below_five_seconds', async () => {
    const s = setup(); assert.equal(s.timers.length, 0);
    s.mounted.stop(); s.root.dataset.obtainPollUntil = new Date(now + 30000).toISOString(); const mounted = s.mount();
    assert.equal(s.timers.at(-1).ms, 5000); s.doc.visibilityState = 'hidden'; await mounted.refresh(); assert.equal(s.calls.length, 0);
    s.doc.visibilityState = 'visible'; s.setTime(now + 30001); s.events.visibilitychange();
    await mounted.refresh(); assert.equal(s.calls.length, 0);
    const verified = setup(done(), undefined, undefined, {obtainPollUntil: new Date(now + 30000).toISOString()});
    assert.equal(verified.root.dataset.obtainVerified, 'true');
    verified.doc.visibilityState = 'hidden'; verified.events.visibilitychange(); verified.setTime(now + 30001);
    verified.doc.visibilityState = 'visible'; verified.events.visibilitychange();
    assert.equal(verified.root.dataset.obtainVerified, 'false');
  });
  await test('double_refresh_has_one_inflight_read_and_stop_prevents_more', async () => {
    let complete; const s = setup(base(), () => new Promise(resolve => { complete = resolve; }));
    const pending = s.mounted.refresh(); await s.mounted.refresh(); assert.equal(s.calls.length, 1);
    complete({ok: true, json: async () => base()}); await pending; s.mounted.stop(); await s.mounted.refresh(); assert.equal(s.calls.length, 1);
  });
  await test('duplicate_views_share_one_read_and_do_not_create_another_poller', async () => {
    const context = {obtainPollUntil: new Date(now + 30000).toISOString()};
    let complete; const first = setup(base(), () => new Promise(resolve => { complete = resolve; }), undefined, context);
    const second = setup(base(), async () => { throw Error('Duplicate fetch must not run'); }, first.doc, context);
    const polls = first.timers.filter(t => !t.canceled && t.ms === 5000);
    assert.equal(polls.length, 1); assert.equal(second.timers.filter(t => !t.canceled && t.ms === 5000).length, 0);
    first.setTime(now + 5000); second.setTime(now + 5000);
    const pending = polls[0].fn(); await second.mounted.refresh();
    assert.equal(first.calls.length, 1); assert.equal(second.calls.length, 0);
    complete({ok: true, json: async () => done()}); await pending;
    assert.equal(first.root.dataset.obtainVerified, 'true'); assert.equal(second.root.dataset.obtainVerified, 'true');
  });
  await test('late_read_is_discarded_after_original_window_and_hidden_read_is_aborted', async () => {
    const context = {obtainPollUntil: new Date(now + 30000).toISOString()};
    const queued = setup(base(), undefined, undefined, context);
    const callback = queued.timers.find(t => t.ms === 5000 && !t.canceled).fn;
    queued.setTime(now + 30001); queued.root.dataset.obtainPollUntil = new Date(now + 3600000).toISOString();
    await callback(); assert.equal(queued.calls.length, 0);
    let delayed;
    const late = setup(base(), () => new Promise(resolve => { delayed = resolve; }), undefined, context);
    const latePending = late.mounted.refresh(); late.setTime(now + 30001);
    late.root.dataset.obtainPollUntil = new Date(now + 3600000).toISOString(); delayed({ok: true, json: async () => done()}); await latePending;
    assert.equal(late.root.dataset.obtainVerified, 'false'); assert.notEqual(late.label.textContent, 'Verified in Jellyfin');
    let complete; const s = setup(base(), () => new Promise(resolve => { complete = resolve; }));
    s.mounted.stop(); s.root.dataset.obtainPollUntil = new Date(now + 30000).toISOString(); const mounted = s.mount();
    const pending = mounted.refresh(); s.doc.visibilityState = 'hidden'; s.events.visibilitychange();
    assert.equal(s.calls[0].opts.signal.aborted, true);
    s.root.dataset.obtainPollUntil = new Date(now + 3600000).toISOString(); s.setTime(now + 30001); s.doc.visibilityState = 'visible';
    complete({ok: true, json: async () => done()}); await pending;
    assert.equal(s.root.dataset.obtainVerified, 'false'); assert.notEqual(s.label.textContent, 'Verified in Jellyfin');
    await mounted.refresh(); assert.equal(s.calls.length, 1);
  });
  await test('same_profile_job_or_URL_change_after_body_await_is_discarded', async () => {
    for (const key of ['obtainJobId', 'obtainStatusUrl']) {
      let body, started;
      const entered = new Promise(resolve => { started = resolve; });
      const s = setup(base(), async () => ({ok: true, text: () => { started(); return new Promise(resolve => { body = resolve; }); }}));
      const pending = s.mounted.refresh(); await entered;
      s.root.dataset[key] = key === 'obtainJobId' ? '43' : '/cabinet/obtain/jobs/43/status/';
      body(JSON.stringify({...done(), revision: 3})); await pending;
      assert.equal(s.root.hidden, true); assert.equal(s.root.dataset.obtainVerified, 'false'); assert.equal(s.revision.value, '2');
    }
  });
  console.log(JSON.stringify({checks: results.length, passed: results.filter(r => r.pass).length, results}, null, 2));
  if (results.some(r => !r.pass)) process.exitCode = 1;
})();
