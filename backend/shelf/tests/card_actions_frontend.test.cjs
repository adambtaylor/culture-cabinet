'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const { HoldGesture, SubmissionGate, targetAt } = require('../../../static/cabinet/card_actions.js');
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
