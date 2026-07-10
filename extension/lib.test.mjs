// Manual unit tests for the extension's pure helpers.
//
// NOT wired into CI: the extension lives outside both the pytest suite and the
// `tiro/frontend/static/js/tests/*.test.mjs` node glob. Run by hand:
//   node --test extension/lib.test.mjs
//
// lib.js imports the `chrome` global only inside impure helpers (loadToken); the
// pure functions under test touch none of it, so importing the module in plain
// node works as long as we don't call those impure helpers here.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isSavableUrl, savableTabs, classifySaveResponse } from './lib.js';

test('isSavableUrl accepts http(s), rejects everything else', () => {
  assert.equal(isSavableUrl('https://example.com/a'), true);
  assert.equal(isSavableUrl('http://localhost:8000/x'), true);
  assert.equal(isSavableUrl('chrome://extensions'), false);
  assert.equal(isSavableUrl('about:blank'), false);
  assert.equal(isSavableUrl('file:///Users/x/a.html'), false);
  assert.equal(isSavableUrl('view-source:https://example.com'), false);
  assert.equal(isSavableUrl(''), false);
  assert.equal(isSavableUrl(undefined), false);
  assert.equal(isSavableUrl(null), false);
});

test('savableTabs keeps only http(s) tabs and tolerates junk entries', () => {
  const tabs = [
    { url: 'https://a.com' },
    { url: 'chrome://newtab' },
    null,
    {},
    { url: 'http://b.com' },
  ];
  assert.deepEqual(
    savableTabs(tabs).map((t) => t.url),
    ['https://a.com', 'http://b.com'],
  );
  assert.deepEqual(savableTabs(undefined), []);
});

test('classifySaveResponse: 200 success -> saved (with highlight flag)', () => {
  const r = classifySaveResponse(200, { success: true, data: { id: 3 }, highlight_created: true });
  assert.equal(r.kind, 'saved');
  assert.equal(r.data.id, 3);
  assert.equal(r.highlightCreated, true);
});

test('classifySaveResponse: 200 success without highlight flag -> highlightCreated false', () => {
  const r = classifySaveResponse(200, { success: true, data: { id: 1 } });
  assert.equal(r.kind, 'saved');
  assert.equal(r.highlightCreated, false);
});

test('classifySaveResponse: 409 already_saved -> already', () => {
  const r = classifySaveResponse(409, { error: 'already_saved', data: { id: 7, title: 'X' } });
  assert.equal(r.kind, 'already');
  assert.equal(r.data.id, 7);
});

test('classifySaveResponse: 401 -> auth', () => {
  assert.equal(classifySaveResponse(401, null).kind, 'auth');
});

test('classifySaveResponse: other error -> error with message', () => {
  const r = classifySaveResponse(500, { error: 'boom' });
  assert.equal(r.kind, 'error');
  assert.equal(r.error, 'boom');
  const r2 = classifySaveResponse(422, null);
  assert.equal(r2.kind, 'error');
  assert.equal(r2.error, 'HTTP 422');
});
