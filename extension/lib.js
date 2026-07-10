// Shared helpers for the popup and the background service worker (MV3).
//
// The pure functions here (isSavableUrl, savableTabs, classifySaveResponse) are
// unit-testable in isolation — see lib.test.mjs, which IS gated in CI: the
// node-test step in .github/workflows/ci.yml runs `extension/lib.test.mjs`
// alongside the `tiro/frontend/static/js/tests/*.test.mjs` glob.

export const TIRO_URL = 'http://localhost:8000';

export function authHeaders(token, extra) {
  const h = extra ? { ...extra } : {};
  if (token) h['Authorization'] = 'Bearer ' + token;
  return h;
}

// Pure: is this a URL we can actually POST to Tiro? Only http(s) — chrome://,
// about:, file:, view-source:, extension pages, and empty/undefined are skipped.
export function isSavableUrl(url) {
  return typeof url === 'string' && /^https?:\/\//i.test(url);
}

// Pure: filter a chrome.tabs.query result down to savable http(s) tabs.
export function savableTabs(tabs) {
  return (tabs || []).filter((t) => t && isSavableUrl(t.url));
}

// Pure: normalize a save fetch outcome (status + parsed body) into a result the
// UI/notifier can switch on without re-deriving HTTP semantics everywhere.
export function classifySaveResponse(status, body) {
  if (status === 401) return { kind: 'auth' };
  if (status >= 200 && status < 300 && body && body.success) {
    return { kind: 'saved', data: body.data, highlightCreated: !!body.highlight_created };
  }
  if (status === 409 || (body && body.error === 'already_saved')) {
    return { kind: 'already', data: (body && body.data) || null };
  }
  return { kind: 'error', error: (body && body.error) || 'HTTP ' + status };
}

// Impure: POST one save. Returns { status, body } (body null if not JSON).
export async function postSave(token, payload) {
  const res = await fetch(TIRO_URL + '/api/ingest/url', {
    method: 'POST',
    headers: authHeaders(token, { 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });
  let body = null;
  try {
    body = await res.json();
  } catch (_) {
    // Non-JSON response (e.g. a bare 500) — leave body null; classify handles it.
  }
  return { status: res.status, body };
}

// Impure: toggle VIP on a source (best-effort follow-up after a save — the ingest
// request model has no is_vip field, so VIP is a separate PATCH, matching the
// popup's original two-step flow).
export async function setSourceVip(token, sourceId) {
  return fetch(TIRO_URL + '/api/sources/' + sourceId + '/vip', {
    method: 'PATCH',
    headers: authHeaders(token),
  });
}

// Impure: read the stored API token from chrome.storage.local (Promise wrapper).
export function loadToken() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['tiroToken'], (r) => resolve((r && r.tiroToken) || ''));
  });
}
