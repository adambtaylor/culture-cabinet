'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const { HoldGesture, SubmissionGate, targetAt, bindNoiseControls } = require('../../../static/cabinet/card_actions.js');
function fixture() {
  const timers = new Map(), events = []; let next = 0;
  const gesture = new HoldGesture({ lift: p => events.push(['lift', p.id]),
    move: (e, t) => events.push(['target', t?.action || null]),
    end: reason => events.push(['end', reason]), commit: action => events.push(['commit', action]) },
  { setTimeout(fn) { timers.set(++next, fn); return next; }, clearTimeout(id) { timers.delete(id); } });
  return { gesture, events, tick() { const work = [...timers.values()]; timers.clear(); work.forEach(fn => fn()); } };
}
const pointer = changes => ({ pointerId: 7, clientX: 100, clientY: 100, button: 0, isPrimary: true, ...changes });
const commits = f => f.events.filter(e => e[0] === 'commit');
function noiseWidget(value, id = 'noise-next') {
  const events = new Map(), attrs = {}, form = { requestSubmit() { throw Error('Unexpected submit'); } };
  const input = { id, min: '0', max: '100', step: '1', value, form,
    setAttribute(name, text) { attrs[name] = text; },
    addEventListener(name, fn) { events.set(name, [...(events.get(name) || []), fn]); } };
  const output = { textContent: 'initial', getAttribute(name) { return name === 'for' ? id : null; } };
  const widget = { closest() { return form; }, querySelector(selector) { return selector.startsWith('input') ? input : output; } };
  return { input, output, widget, attrs, events,
    move(value, event) { input.value = value; for (const fn of events.get(event) || []) fn(); } };
}
test('Noise pointer/input and keyboard/change update only matching readout and native accessible value', () => {
  const one = noiseWidget('25'), two = noiseWidget('80', 'other-noise');
  const root = { querySelectorAll() { return [one.widget, two.widget]; } };
  bindNoiseControls(root);
  assert.equal(one.output.textContent, '25'); assert.equal(two.output.textContent, '80');
  one.move('100', 'input');
  assert.equal(one.output.textContent, '100'); assert.match(one.attrs['aria-valuetext'], /^100 · next round/);
  assert.equal(two.output.textContent, '80');
  one.move('0', 'change'); assert.equal(one.output.textContent, '0');
  assert.equal(one.input.value, '0');
  bindNoiseControls(root);
  assert.equal(one.events.get('input').length, 1); assert.equal(one.events.get('change').length, 1);
});
test('Noise rejects foreign form/output bindings and invalid values without changing another surface', () => {
  const foreign = noiseWidget('25'); foreign.input.form = {};
  const wrongOutput = noiseWidget('25'); wrongOutput.output.getAttribute = () => 'wrong-input';
  const valid = noiseWidget('25');
  bindNoiseControls({ querySelectorAll: () => [foreign.widget, wrongOutput.widget, valid.widget] });
  assert.equal(foreign.events.size, 0); assert.equal(wrongOutput.events.size, 0);
  for (const invalid of ['101', '-1', '25.5', ' 25', '1e2', '<markup>']) {
    valid.move(invalid, 'input'); assert.equal(valid.output.textContent, '25');
  }
});
test('Noise initializes on a guide without cards and cannot invoke effects', () => {
  const fs = require('node:fs'), vm = require('node:vm');
  const native = noiseWidget('25');
  const forbidden = () => { throw Error('Unexpected side effect'); };
  const document = { querySelectorAll(selector) {
    if (selector === '[data-noise-control]') return [native.widget];
    if (selector === '[data-card-action-form]') return [];
    throw Error('Unexpected document query');
  } };
  vm.runInNewContext(fs.readFileSync(require.resolve('../../../static/cabinet/card_actions.js'), 'utf8'), {
    document, fetch: forbidden, XMLHttpRequest: forbidden, localStorage: new Proxy({}, { get: forbidden }),
    sessionStorage: new Proxy({}, { get: forbidden }), setTimeout: forbidden,
    window: new Proxy({}, { get: forbidden })
  });
  native.move('60', 'input'); native.move('61', 'change');
  assert.equal(native.output.textContent, '61');
});
test('short click and a held card alone never commit or choose a default bin', () => {
  const short = fixture(); short.gesture.down(pointer()); short.gesture.up(pointer()); short.tick(); assert.deepEqual(commits(short), []);
  const held = fixture(); held.gesture.down(pointer()); held.tick(); assert.equal(held.gesture.phase, 'active'); assert.deepEqual(commits(held), []);
  held.gesture.up(pointer()); assert.deepEqual(commits(held), []);
});
test('each explicit enabled cardinal release commits exactly its native action once', () => {
  for (const action of ['like', 'dislike', 'favourite', 'obtain']) {
    const f = fixture(); f.gesture.down(pointer()); f.tick(); f.gesture.move(pointer({ clientX: 180 }), { action, enabled: true });
    f.gesture.up(pointer(), { action, enabled: true }); f.gesture.up(pointer(), { action, enabled: true }); f.tick();
    assert.deepEqual(commits(f), [['commit', action]]);
  }
});
test('pre-hold movement cancels so native scrolling and ordinary click behavior remain available', () => {
  const f = fixture(); f.gesture.down(pointer()); f.gesture.move(pointer({ clientY: 130 })); f.tick();
  f.gesture.up(pointer(), { action: 'obtain', enabled: true }); assert.equal(f.gesture.phase, 'idle'); assert.deepEqual(commits(f), []);
});
test('outside, disabled and unknown release targets produce no action', () => {
  for (const target of [null, { action: 'like', enabled: false }, { action: 'made-up', enabled: true }]) {
    const f = fixture(); f.gesture.down(pointer()); f.tick(); f.gesture.up(pointer(), target); assert.deepEqual(commits(f), []);
  }
});
test('every cancellation is terminal even if a late timer or pointer-up arrives', () => {
  for (const reason of ['escape', 'pointercancel', 'lostpointercapture', 'blur', 'visibility', 'scroll-intent', 'multiple-pointers']) {
    for (const lift of [false, true]) {
      const f = fixture(); f.gesture.down(pointer()); if (lift) f.tick(); f.gesture.cancel(reason); f.tick();
      f.gesture.up(pointer(), { action: 'favourite', enabled: true }); assert.deepEqual(commits(f), []); assert.equal(f.gesture.phase, 'idle');
    }
  }
});
test('a second pointer cancels the first rather than creating two submissions', () => {
  const f = fixture(); f.gesture.down(pointer()); f.tick(); f.gesture.down(pointer({ pointerId: 8 }));
  f.gesture.up(pointer(), { action: 'like', enabled: true }); f.gesture.up(pointer({ pointerId: 8 }), { action: 'obtain', enabled: true });
  assert.deepEqual(commits(f), []);
});
test('nested controls, secondary buttons and nonprimary touches do not start a gesture', () => {
  for (const changes of [{ excluded: true }, { button: 2 }, { isPrimary: false }]) {
    const f = fixture(); assert.equal(f.gesture.down(pointer(changes)), false); f.tick();
    f.gesture.up(pointer(), { action: 'obtain', enabled: true }); assert.deepEqual(f.events, []);
  }
});
test('target feedback is only feedback, and disabled targets never become selected', () => {
  const f = fixture(); f.gesture.down(pointer()); f.tick(); f.gesture.move(pointer(), { action: 'favourite', enabled: true });
  f.gesture.move(pointer(), { action: 'obtain', enabled: false }); assert.deepEqual(commits(f), []);
  assert.deepEqual(f.events.filter(e => e[0] === 'target'), [['target', 'favourite'], ['target', null]]);
});
test('one page submission gate blocks duplicate clicks and a second card while the native result is pending', () => {
  const gate = new SubmissionGate(); assert.equal(gate.claim(), true); assert.equal(gate.claim(), false); assert.equal(gate.claim(), false);
  // There is no reset/retry on a lost response: only a newly rendered native
  // page with a new server-issued command gets a new gate.
  assert.equal(new SubmissionGate().claim(), true);
});
test('hit testing uses the actual labelled target rectangle rather than inferred direction', () => {
  const bins = [{ action: 'like', enabled: true, left: 200, right: 280, top: 100, bottom: 170 }];
  assert.equal(targetAt(bins, 240, 130).action, 'like'); assert.equal(targetAt(bins, 190, 130), null);
  assert.equal(targetAt(bins, 240, 180), null);
});

test('default browser timers retain their global receiver through hold activation and pending cancellation', () => {
  const fs = require('node:fs'), vm = require('node:vm');
  const context = vm.createContext({ module: { exports: {} } });
  // Node timers accept the gesture as receiver. Model the browser's stricter
  // host-call boundary so this test catches a failure before a timer is queued.
  vm.runInContext(`const work = new Map(); let next = 0, lifts = 0;
    function setTimeout(callback, delay) {
      'use strict'; if (this !== globalThis) throw TypeError('Illegal invocation');
      if (delay !== 380) throw Error('Unexpected hold delay');
      work.set(++next, callback); return next;
    }
    function clearTimeout(id) {
      'use strict'; if (this !== globalThis) throw TypeError('Illegal invocation');
      work.delete(id);
    }`, context);
  vm.runInContext(fs.readFileSync(require.resolve('../../../static/cabinet/card_actions.js'), 'utf8'), context);
  vm.runInContext(`const gesture = new module.exports.HoldGesture({ lift() { lifts++; } });
    const press = { pointerId: 1, clientX: 10, clientY: 20, button: 0, isPrimary: true };
    gesture.down(press);`, context);
  assert.equal(vm.runInContext('work.size', context), 1);
  vm.runInContext('const callbacks = [...work.values()]; work.clear(); callbacks.forEach(fn => fn());', context);
  assert.equal(vm.runInContext('lifts', context), 1);
  assert.equal(vm.runInContext('gesture.phase', context), 'active');
  vm.runInContext(`gesture.cancel('escape'); gesture.down(press); gesture.cancel('scroll-intent');`, context);
  assert.equal(vm.runInContext('work.size', context), 0);
  assert.equal(vm.runInContext('gesture.phase', context), 'idle');
  assert.equal(vm.runInContext('lifts', context), 1);
});
test('injected clocks retain their own receiver when scheduling and cancelling a pending hold', () => {
  const clock = { callback: null, cleared: false,
    setTimeout(callback, delay) { assert.equal(this, clock); assert.equal(delay, 380); this.callback = callback; return 42; },
    clearTimeout(id) { assert.equal(this, clock); assert.equal(id, 42); this.callback = null; this.cleared = true; }
  };
  const gesture = new HoldGesture({}, clock);
  gesture.down(pointer()); assert.equal(typeof clock.callback, 'function');
  gesture.cancel('escape'); assert.equal(clock.callback, null); assert.equal(clock.cleared, true); assert.equal(gesture.phase, 'idle');
});

test('confirmed slot chooses only the next unanswered enabled card in the same published category', () => {
  const { confirmedSlotResult } = require('../../../static/cabinet/card_actions.js');
  const ref = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
  const submitted = { profile_ref: ref(1), publication_ref: ref(2), publication_revision: 1, target_ref: ref(3), command_id: ref(4), category: 'movie', action: 'like' };
  const receipt = { profile_ref: ref(1), target_ref: ref(3), command_id: ref(4), requested_action: 'like', preference_outcome: 'committed', selected_state: 'like', interest_revision: 1, saved_revision: 0 };
  const card = changes => ({ ...submitted, command_id: ref(5), selected_state: 'none', enabled: true, interest_revision: 0, saved_revision: 0, ...changes });
  const saved = card({ selected_state: 'like', interest_revision: 1 });
  const wrongCategory = card({ target_ref: ref(6), category: 'gallery' });
  const disabled = card({ target_ref: ref(7), enabled: false });
  const answered = card({ target_ref: ref(8), selected_state: 'favourite' });
  const next = card({ target_ref: ref(9) });
  assert.equal(confirmedSlotResult(submitted, receipt, [saved, wrongCategory, disabled, answered, next]).next, next);
  assert.equal(confirmedSlotResult(submitted, receipt, [saved, wrongCategory, disabled, answered, next], new Set([ref(9)])).next, null);
  assert.equal(confirmedSlotResult(submitted, receipt, [saved]).saved, saved);
});
test('Like, Dislike and Favourite each keep their acknowledged card and advance to the next same-category pick', () => {
  const { confirmedSlotResult } = require('../../../static/cabinet/card_actions.js');
  const ref = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
  for (const [offset, action] of ['like', 'dislike', 'favourite'].entries()) {
    const profile = ref(1), publication = ref(2), target = ref(10 + offset), command = ref(20 + offset);
    const submitted = { profile_ref: profile, publication_ref: publication, publication_revision: 3,
      target_ref: target, command_id: command, category: 'movie', action };
    const interestRevision = action === 'favourite' ? 0 : 4;
    const savedRevision = action === 'favourite' ? 6 : 5;
    const receipt = { profile_ref: profile, target_ref: target, command_id: command, requested_action: action,
      preference_outcome: 'committed', selected_state: action, interest_revision: interestRevision, saved_revision: savedRevision };
    const savedNode = { id: 'saved-' + action };
    const saved = { ...submitted, selected_state: action, enabled: true,
      interest_revision: interestRevision, saved_revision: savedRevision, command_id: ref(30 + offset), node: savedNode };
    const otherCategory = { ...saved, target_ref: ref(40 + offset), command_id: ref(50 + offset), category: 'book', selected_state: 'none' };
    const next = { ...saved, target_ref: ref(60 + offset), command_id: ref(70 + offset), selected_state: 'none', interest_revision: 0, saved_revision: 0 };
    const later = { ...next, target_ref: ref(80 + offset), command_id: ref(90 + offset) };
    const result = confirmedSlotResult(submitted, receipt, [saved, otherCategory, next, later]);
    assert.equal(result.saved, saved, action + ' retains the acknowledged response card');
    assert.equal(result.saved.node, savedNode, action + ' keeps the same real card node');
    assert.equal(result.saved.selected_state, action);
    assert.equal(result.next, next, action + ' advances only to the first later unanswered card in its category');
    assert.throws(() => confirmedSlotResult(submitted, { ...receipt, selected_state: action === 'like' ? 'dislike' : 'like' }, [saved, next]));
  }
});
test('an exhausted single-book category does not refill or substitute another category', () => {
  const { confirmedSlotResult } = require('../../../static/cabinet/card_actions.js');
  const ref = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
  const submitted = { profile_ref: ref(1), publication_ref: ref(2), publication_revision: 1,
    target_ref: ref(3), command_id: ref(4), category: 'book', action: 'favourite' };
  const receipt = { profile_ref: ref(1), target_ref: ref(3), command_id: ref(4), requested_action: 'favourite',
    preference_outcome: 'committed', selected_state: 'favourite', interest_revision: 0, saved_revision: 1 };
  const undoForm = { action: '/cabinet/actions/undo/', method: 'post', native: true };
  const bookNode = { id: 'the-only-book', undoForm };
  const savedBook = { ...submitted, command_id: ref(5), selected_state: 'favourite', enabled: true,
    interest_revision: 0, saved_revision: 1, node: bookNode };
  const nextCategory = { ...savedBook, target_ref: ref(8), command_id: ref(9), category: 'movie', selected_state: 'none', saved_revision: 0 };
  const result = confirmedSlotResult(submitted, receipt, [savedBook, nextCategory]);
  assert.equal(result.saved, savedBook);
  assert.equal(result.saved.node, bookNode, 'the server-rendered book and its native Undo form remain in place');
  assert.equal(result.saved.node.undoForm, undoForm);
  assert.equal(result.next, null, 'no other category fills the exhausted book slot');
});
test('receipt mismatch, stale revision or a consumed command never authorizes slot advancement', () => {
  const { confirmedSlotResult } = require('../../../static/cabinet/card_actions.js');
  const ref = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
  const submitted = { profile_ref: ref(1), publication_ref: ref(2), publication_revision: 1, target_ref: ref(3), command_id: ref(4), category: 'movie', action: 'favourite' };
  const receipt = { profile_ref: ref(1), target_ref: ref(3), command_id: ref(4), requested_action: 'favourite', preference_outcome: 'committed', selected_state: 'favourite', interest_revision: 1, saved_revision: 1 };
  const saved = { ...submitted, command_id: ref(5), selected_state: 'favourite', enabled: true, interest_revision: 1, saved_revision: 1 };
  for (const change of [{ profile_ref: ref(8) }, { target_ref: ref(8) }, { command_id: ref(8) }, { preference_outcome: 'pending' }, { selected_state: 'like' }]) assert.throws(() => confirmedSlotResult(submitted, { ...receipt, ...change }, [saved]));
  for (const change of [{ publication_ref: ref(8) }, { publication_revision: 2 }, { command_id: ref(4) }, { saved_revision: 0 }]) assert.throws(() => confirmedSlotResult(submitted, receipt, [{ ...saved, ...change }]));
  assert.throws(() => confirmedSlotResult(submitted, receipt, []));
});

// These responses are bounded local HTML streams, not a provider or browser.
// Keeping the real transport function under test catches accidental replay after
// a committed POST whose following current read cannot be confirmed.
const slots = require('../../../static/cabinet/card_actions.js');
const slotRef = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0');
function slotFixture() {
  const submitted = { profile_ref: slotRef(1), publication_ref: slotRef(2), publication_revision: 1, target_ref: slotRef(3), command_id: slotRef(4), category: 'movie', action: 'like' };
  const data = { schema: 'card-action-refresh/v1', profileRef: slotRef(1), publicationRef: slotRef(2), publicationRevision: '1', readState: 'current', receiptPresent: 'true',
    commandId: slotRef(4), targetRef: slotRef(3), requestedAction: 'like', preferenceOutcome: 'committed', selectedState: 'like', interestRevision: '1', savedRevision: '0', rankingState: 'feedback_pending' };
  const marker = slots.refreshMarker(data), pageURL = 'http://localhost:4321/cabinet?profile_ref=' + slotRef(1);
  const redirect = pageURL + '&action_receipt=' + slotRef(4);
  const response = (url = redirect, html = 'confirmed') => {
    const stream = new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    return { status: 200, redirected: true, url, headers: stream.headers, body: stream.body };
  };
  return { submitted, data, marker, pageURL, redirect, response };
}
test('typed primitive marker rejects invalid enum, nullable pair, unsafe revisions and unowned receipt', () => {
  const f = slotFixture();
  for (const change of [{ schema: 'v2' }, { publicationRef: '' }, { publicationRevision: '' }, { interestRevision: '01' }, { savedRevision: '9007199254740992' }, { requestedAction: 'send' }, { rankingState: 'pending' }, { readState: 'maybe' }, { receiptPresent: 'yes' }]) assert.throws(() => slots.refreshMarker({ ...f.data, ...change }));
  const empty = slots.refreshMarker({ ...f.data, publicationRef: '', publicationRevision: '', readState: 'unavailable', receiptPresent: 'false' });
  assert.equal(empty.receipt, null); assert.equal(empty.publication_ref, null);
});
test('next eligible means subsequent server order: no wrapping, duplicate targets or foreign publication', () => {
  const f = slotFixture(), receipt = f.marker.receipt;
  const saved = { ...f.submitted, command_id: slotRef(5), selected_state: 'like', interest_revision: 1, saved_revision: 0, enabled: true };
  const earlier = { ...saved, target_ref: slotRef(6), command_id: slotRef(7), selected_state: 'none', interest_revision: 0 };
  assert.equal(slots.confirmedSlotResult(f.submitted, receipt, [earlier, saved]).next, null);
  assert.throws(() => slots.confirmedSlotResult(f.submitted, receipt, [saved, saved]));
  assert.throws(() => slots.confirmedSlotResult(f.submitted, receipt, [saved, { ...earlier, publication_ref: slotRef(8) }]));
});
test('normal save sends original body once, follows native acknowledgement and needs no contextual read', async () => {
  const f = slotFixture(), calls = [], body = { originalFormAndChosenSubmitter: true };
  const result = await slots.requestCardSave({ ...f, body, fetchImpl: async (...args) => { calls.push(args); return f.response(); }, parse: text => { assert.equal(text, 'confirmed'); return { marker: f.marker }; } });
  assert.equal(calls.length, 1); assert.equal(calls[0][0], 'http://localhost:4321/cabinet/actions/');
  assert.equal(calls[0][1].method, 'POST'); assert.equal(calls[0][1].body, body);
  assert.equal(calls[0][1].redirect, 'follow'); assert.equal(calls[0][1].mode, 'same-origin');
  assert.equal(calls[0][1].credentials, 'same-origin'); assert.equal(calls[0][1].cache, 'no-store');
  assert.equal(result.receipt.command_id, f.submitted.command_id);
});
test('actual enhanced form serializer produces native URL-encoded fields and one chosen action', async () => {
  for (const action of ['like', 'dislike', 'favourite']) {
    const formData = new FormData();
    const fields = [['csrfmiddlewaretoken', 'FICTIONAL+csrf/token='], ['profile_ref', slotRef(1)],
      ['publication_ref', slotRef(2)], ['target_ref', slotRef(3)], ['command_id', slotRef(4)],
      ['publication_revision', '1'], ['interest_revision', '0'], ['saved_revision', '12'],
      ['repeated', 'first'], ['repeated', 'second'], ['text', 'A & B + café']];
    for (const entry of fields) formData.append(...entry);
    const body = slots.encodeCardAction(formData, { name: 'action', value: action });
    // A real Request exercises the browser-standard body encoder without any
    // transport. A mocked fetch with a placeholder body missed this boundary.
    const request = new Request('http://localhost:4321/cabinet/actions/', { method: 'POST', body });
    assert.match(request.headers.get('Content-Type'), /^application\/x-www-form-urlencoded(?:;|$)/i);
    const decoded = new URLSearchParams(await request.text());
    assert.deepEqual([...decoded], [...fields, ['action', action]]);
    assert.deepEqual(decoded.getAll('action'), [action]);
    assert.deepEqual(decoded.getAll('repeated'), ['first', 'second'], 'retain multiplicity for native rejection, never silently collapse');
    assert.equal(formData.has('action'), false, 'the original successful controls are not mutated');
  }
});
test('serializer rejects an existing action, non-text entry and wrong submitter before transport', () => {
  const submitter = { name: 'action', value: 'like' };
  assert.throws(() => slots.encodeCardAction([['action', 'like'], ['action', 'dislike']], submitter));
  assert.throws(() => slots.encodeCardAction([['file', { name: 'not-a-text-field' }]], submitter));
  for (const invalid of [{ name: 'other', value: 'like' }, { name: 'action', value: 'obtain' }, null]) {
    assert.throws(() => slots.encodeCardAction([], invalid));
  }
});
test('URL-encoded ordinary actions still send once and remain unknown after a lost acknowledgement', async () => {
  for (const action of ['like', 'dislike', 'favourite']) {
    const f = slotFixture(); f.submitted.action = action;
    const fields = Object.entries(f.submitted).filter(([key]) => key !== 'action');
    const body = slots.encodeCardAction(fields.map(([key, value]) => [key, String(value)]), { name: 'action', value: action });
    let posts = 0;
    await assert.rejects(slots.requestCardSave({ ...f, body, fetchImpl: async (url, options) => {
      posts++; assert.equal(options.method, 'POST');
      const request = new Request(url, options);
      assert.match(request.headers.get('Content-Type'), /^application\/x-www-form-urlencoded/);
      assert.deepEqual(new URLSearchParams(await request.text()).getAll('action'), [action]);
      throw Error('Lost after possible commit');
    }, parse: () => { throw Error('No receipt received'); } }), error => !error.recorded && !error.refused);
    assert.equal(posts, 1, 'no retry, native fallback or second write');
  }
});
test('pending and failed response show unknown status with a single native recovery instruction', () => {
  class Node {
    constructor(tag = 'p') { this.tagName = tag; this.children = []; this.dataset = {}; this.hidden = false; this.attrs = {}; }
    set textContent(text) { this.children = [text]; }
    get textContent() { return this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
    setAttribute(name, value) { this.attrs[name] = value; }
    append(...nodes) { this.children.push(...nodes); }
    cloneNode() { const clone = new Node(this.tagName); clone.href = this.href; clone.textContent = this.textContent; return clone; }
  }
  for (const recorded of [false, true]) {
    const selected = new Node(), pending = new Node(), receipt = new Node('a');
    selected.textContent = 'Selected: No response yet';
    receipt.href = '/cabinet/actions/' + slotRef(4) + '/';
    pending.append('Waiting for the native result. ', receipt, ' Nothing is retried automatically.');
    pending.querySelector = selector => selector === 'a' ? receipt : null;
    const root = { messages: [], querySelector() { return this.messages[0] || null; },
      prepend(message) { this.messages.unshift(message); message.remove = () => { this.messages.splice(this.messages.indexOf(message), 1); }; } };
    const card = { querySelector: selector => selector === '.card-selected-state' ? selected : selector === '[data-card-action-pending]' ? pending : null };
    const document = { createElement: tag => new Node(tag) };
    slots.showCardPending(card);
    assert.equal(selected.textContent, 'Response status unknown'); assert.equal(pending.hidden, false);
    const currentURL = 'http://localhost:4321/cabinet?profile_ref=' + slotRef(1);
    const render = () => slots.renderSlotFailure(root, card, recorded, currentURL, document);
    render(); const message = render();
    assert.equal(pending.hidden, true, 'superseded waiting/receipt guidance is not also displayed');
    assert.equal(root.messages.length, 1); assert.equal(message.attrs.role, 'status');
    assert.equal(selected.textContent, recorded ? 'Response recorded; current state unknown' : 'Response status unknown');
    assert.equal(selected.textContent.includes('No response yet'), false);
    assert.equal((message.textContent.match(/Nothing is retried automatically/g) || []).length, 1);
    const links = message.children.filter(child => typeof child !== 'string');
    assert.deepEqual(links.map(link => link.href), [receipt.href, currentURL]);
    assert.equal(links[1].textContent, 'Read the current cabinet');
    assert.notEqual(links[0], receipt, 'native receipt target is retained without moving the hidden template link');
    assert.equal(message.textContent.includes('Response recorded'), recorded);
  }
});
test('slot compensation measures settled geometry before entry motion, keeping final position and focus', () => {
  for (const reduced of [false, true]) {
    let scroll = 1926, entryOffset = 0, measuredOffset = null, focus = null;
    const steps = [], heading = { focus(options) { focus = options; steps.push('focus'); } };
    const anchor = { style: {}, querySelector: () => heading,
      getBoundingClientRect() { measuredOffset = entryOffset; steps.push('measure'); return { top: 1936 - scroll + entryOffset }; },
      classList: { add(name) { assert.equal(name, 'card-slot-enter'); entryOffset = reduced ? 0 : 10; steps.push('enter'); } } };
    const viewport = { scrollBy(options) { assert.equal(options.behavior, 'instant'); scroll += options.top; steps.push('scroll'); } };
    slots.positionCardSlot(anchor, { top: -110.796875, height: 700, focus: true }, viewport, true);
    assert.deepEqual(steps, ['focus', 'measure', 'scroll', 'enter']);
    assert.equal(measuredOffset, 0, 'entry transform must never influence viewport compensation');
    assert.equal(anchor.style.minHeight, '700px'); assert.equal(heading.tabIndex, -1);
    assert.deepEqual(focus, { preventScroll: true });
    entryOffset = 0; // The normal entry animation has finished; reduced motion never offset it.
    assert.equal(anchor.getBoundingClientRect().top, -110.796875, 'settled replacement retains its occupied slot');
  }
  const fs = require('node:fs');
  const css = fs.readFileSync(require.resolve('../../../static/cabinet/card_actions.css'), 'utf8');
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{\s*\.card-slot-enter\s*\{\s*animation:\s*none\s*;\s*\}\s*\}/,
    'the reduced-motion preference disables the slot entry animation in the served stylesheet');
});
test('no-successor positioning neither animates nor steals focus moved to another control', () => {
  let scrolls = 0;
  const anchor = { style: {}, querySelector: () => ({ focus() { assert.fail('Unrelated control keeps focus'); } }),
    getBoundingClientRect: () => ({ top: 17 }), classList: { add() { assert.fail('No incoming successor'); } } };
  slots.positionCardSlot(anchor, { top: 17, height: 600, focus: false }, { scrollBy(options) { scrolls++; assert.equal(options.top, 0); } }, false);
  assert.equal(scrolls, 1); assert.equal(anchor.style.minHeight, '600px');
});
test('contextual read retains category, connection and run barriers with redirects forbidden and no second write', async () => {
  const f = slotFixture(), calls = [];
  f.pageURL += '&connection_ref=' + slotRef(8) + '&run_ref=' + slotRef(9) + '&category=movie';
  const expected = slots.cabinetReadURL(f.pageURL, f.submitted).url;
  const result = await slots.requestCardSave({ ...f, body: {}, fetchImpl: async (url, options) => {
    calls.push({ url, options }); return calls.length === 1 ? f.response() : { ...f.response(url, 'context'), redirected: false };
  }, parse: text => ({ marker: f.marker, context: text }) });
  assert.equal(calls.length, 2); assert.equal(calls[1].url, expected); assert.equal(calls[1].options.method, 'GET');
  assert.equal(calls[1].options.redirect, 'error'); assert.equal(result.snapshot.context, 'context');
  assert.equal(new URL(calls[1].url).searchParams.get('run_ref'), slotRef(9));
});
test('failed read after owned receipt remains recorded while a dropped POST response stays unknown; neither retries', async () => {
  for (const confirmed of [false, true]) {
    const f = slotFixture(); let count = 0;
    if (confirmed) f.pageURL += '&category=movie';
    await assert.rejects(slots.requestCardSave({ ...f, body: {}, fetchImpl: async () => {
      count++; if (confirmed && count === 1) return f.response(); throw Error('lost transport');
    }, parse: () => ({ marker: f.marker }) }), error => error.recorded === confirmed && error.refused === false);
    assert.equal(count, confirmed ? 2 : 1);
  }
});
test('wrong final redirect, mismatched receipt and explicit authority refusal never deliver a replacement', async () => {
  const f = slotFixture();
  for (const response of [f.response('https://other.invalid/cabinet'), f.response(f.redirect + '&category=movie'), { ...f.response(), status: 403 }]) {
    let count = 0;
    await assert.rejects(slots.requestCardSave({ ...f, body: {}, fetchImpl: async () => { count++; return response; }, parse: () => ({ marker: f.marker }) }), error => !error.recorded && error.refused);
    assert.equal(count, 1);
  }
  await assert.rejects(slots.requestCardSave({ ...f, body: {}, fetchImpl: async () => f.response(), parse: () => ({ marker: { ...f.marker, receipt: { ...f.marker.receipt, target_ref: slotRef(8) } } }) }), error => !error.recorded);
});
test('current contextual refusal preserves recorded acknowledgement and conceals protected old content', async () => {
  const f = slotFixture(); f.pageURL += '&category=movie'; let count = 0;
  await assert.rejects(slots.requestCardSave({ ...f, body: {}, fetchImpl: async () => ++count === 1 ? f.response() : { ...f.response(), status: 409 }, parse: () => ({ marker: f.marker }) }), error => error.recorded && error.refused);
  assert.equal(count, 2);
});
test('unexpected context is refused before any write, including duplicate or foreign profile parameters', async () => {
  const f = slotFixture();
  for (const suffix of ['&unknown=yes', '&profile_ref=' + slotRef(1), '&run_ref=not-a-uuid']) {
    let count = 0;
    await assert.rejects(slots.requestCardSave({ ...f, pageURL: f.pageURL + suffix, body: {}, fetchImpl: async () => { count++; return f.response(); }, parse: () => ({ marker: f.marker }) }));
    assert.equal(count, 0);
  }
});
test('bounded HTML reader rejects content type, declared and streamed oversize, invalid UTF8 and releases cancelled stream', async () => {
  await assert.rejects(slots.boundedSlotHTML(new Response('{}', { headers: { 'Content-Type': 'application/json' } })));
  await assert.rejects(slots.boundedSlotHTML(new Response('', { headers: { 'Content-Type': 'text/html', 'Content-Length': String(2 * 1024 * 1024 + 1) } })));
  await assert.rejects(slots.boundedSlotHTML(new Response(new Uint8Array([0xff]), { headers: { 'Content-Type': 'text/html' } })));
  let cancelled = false, released = false;
  const oversized = { headers: new Headers({ 'Content-Type': 'text/html' }), body: { getReader: () => ({ read: async () => ({ value: new Uint8Array(2 * 1024 * 1024 + 1), done: false }), cancel: async () => { cancelled = true; }, releaseLock: () => { released = true; } }) } };
  await assert.rejects(slots.boundedSlotHTML(oversized)); assert.equal(cancelled, true); assert.equal(released, true);
  assert.equal(await slots.boundedSlotHTML(new Response('safe', { headers: { 'Content-Type': 'text/html' } })), 'safe');
});

test('designated current fragments reject executable nodes, event attributes, arbitrary forms and unavailable reads', () => {
  const f = slotFixture();
  function fragment(overrides = {}) {
    return { attributes: [], querySelector: () => null, querySelectorAll: () => [], hasAttribute: () => false, ...overrides };
  }
  const status = fragment(), deck = fragment();
  function snapshot(statusNode = status, deckNode = deck) {
    return { marker: f.marker, doc: { querySelectorAll: selector => selector === '[data-card-current-status]' ? [statusNode] : selector === '[data-card-current-deck]' ? [deckNode] : [] } };
  }
  assert.equal(slots.currentSlotView(snapshot(), 'http://localhost:4321', new Set()).deck, deck);
  assert.throws(() => slots.currentSlotView(snapshot(status, fragment({ querySelector: () => ({ tagName: 'SCRIPT' }) })), 'http://localhost:4321', new Set()));
  assert.throws(() => slots.currentSlotView(snapshot(fragment({ attributes: [{ name: 'onclick' }] })), 'http://localhost:4321', new Set()));
  const arbitrary = { hasAttribute: () => false, getAttribute: () => '/other-write/' };
  assert.throws(() => slots.currentSlotView(snapshot(status, fragment({ querySelectorAll: selector => selector === 'form' ? [arbitrary] : [] })), 'http://localhost:4321', new Set()));
  const unavailable = snapshot(); unavailable.marker = { ...f.marker, read_state: 'unavailable' };
  assert.throws(() => slots.currentSlotView(unavailable, 'http://localhost:4321', new Set()), error => error.refused && error.recorded);
  const duplicate = snapshot(); duplicate.doc.querySelectorAll = () => [status, deck];
  assert.throws(() => slots.currentSlotView(duplicate, 'http://localhost:4321', new Set()));
});

test('a current book readback retains only its native Undo form, then reflects the cleared state after Undo', () => {
  const f = slotFixture(), profile = f.submitted.profile_ref, publication = f.submitted.publication_ref;
  const cardTarget = slotRef(11), nextCommand = slotRef(12), undoCommand = slotRef(13);
  const input = (name, value) => ({ name, value, type: 'hidden', disabled: false });
  function view(selectedState, withUndo, receiptAction, receiptState, savedRevision, currentCommand) {
    const marker = slots.refreshMarker({ ...f.data, requestedAction: receiptAction, selectedState: receiptState,
      targetRef: cardTarget, commandId: undoCommand, interestRevision: '0', savedRevision: String(savedRevision) });
    const category = { dataset: { cardCategory: 'book' } };
    const selection = { dataset: { selectedState } };
    const card = { closest(selector) { return selector === '[data-card-action-card]' ? this : selector === '[data-card-category]' ? category : null; },
      querySelector(selector) { return selector === '.card-actions[data-selected-state]' ? selection : null; },
      querySelectorAll(selector) { return selector === '.card-actions[data-selected-state]' ? [selection] : []; } };
    const fields = [input('profile_ref', profile), input('publication_ref', publication), input('publication_revision', '1'),
      input('target_ref', cardTarget), input('command_id', currentCommand), input('interest_revision', '0'),
      input('saved_revision', String(savedRevision)), input('csrfmiddlewaretoken', 'A'.repeat(32))];
    const buttons = ['like', 'dislike', 'favourite'].map(action => ({ value: action, disabled: false }));
    function form(action, markerName) {
      return { method: 'post', closest: selector => selector === '[data-card-action-card]' ? card : null,
        hasAttribute: name => name === markerName, getAttribute: name => name === 'action' ? action : null,
        querySelector(selector) { return null; },
        querySelectorAll(selector) {
          if (selector.startsWith('input[name="')) {
            const name = selector.slice(12, -2); return fields.filter(field => field.name === name);
          }
          if (selector.startsWith('button[name="action"][value="')) {
            const actionValue = selector.slice(29, -2); return buttons.filter(button => button.value === actionValue);
          }
          return [];
        } };
    }
    const cardForm = form('/cabinet/actions/', 'data-card-action-form');
    const undoForm = withUndo ? form('/cabinet/actions/undo/', 'data-card-action-undo') : null;
    const emptyNodeQueries = selector => selector === 'form' ? [cardForm, ...(undoForm ? [undoForm] : [])] :
      selector === '[data-card-category]' ? [category] : selector === '[data-card-action-form]' ? [cardForm] :
      selector === '[data-card-action-card]' ? [card] : [];
    const deck = { attributes: [], querySelector: () => null, querySelectorAll: emptyNodeQueries,
      hasAttribute: () => false };
    const status = { attributes: [], querySelector: () => null, querySelectorAll: () => [], hasAttribute: () => false };
    const doc = { querySelectorAll: selector => selector === '[data-card-current-status]' ? [status] :
      selector === '[data-card-current-deck]' ? [deck] : [] };
    return slots.currentSlotView({ marker, doc }, 'http://localhost:4321', new Set());
  }
  const saved = view('favourite', true, 'favourite', 'favourite', 1, nextCommand);
  assert.equal(saved.cards[0].category, 'book');
  assert.equal(saved.cards[0].selected_state, 'favourite');
  assert.equal(saved.deck.querySelectorAll('form').length, 2, 'the fresh book projection retains its separate native Undo form');
  const undone = view('none', false, 'undo', 'none', 2, slotRef(14));
  assert.equal(undone.cards[0].category, 'book');
  assert.equal(undone.cards[0].selected_state, 'none', 'the fresh readback shows the Undo result instead of replaying a saved before-image');
  assert.equal(undone.deck.querySelectorAll('form').length, 1, 'a cleared response does not keep an obsolete Undo form');
});
