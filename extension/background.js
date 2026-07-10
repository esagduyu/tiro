// MV3 background service worker: right-click context-menu saves.
//
// Three items (spec D10): Save to Tiro, Save to Tiro as VIP (page + selection
// contexts), and Save to Tiro with selection as highlight (selection context).
// Selection-as-highlight rides the optional `highlight_text` field on
// POST /api/ingest/url; the server anchors it against the ingested markdown and
// soft-fails (no highlight, still saved) when the quote can't be located.
//
// Auth: requests carry the stored API token as `Authorization: Bearer`. The
// server skips its CSRF check for bearer-token requests (the token IS the
// boundary), which is exactly how the popup's save already works.

import {
  isSavableUrl,
  postSave,
  classifySaveResponse,
  setSourceVip,
  loadToken,
} from './lib.js';

const MENU = {
  SAVE: 'tiro-save',
  SAVE_VIP: 'tiro-save-vip',
  SAVE_HIGHLIGHT: 'tiro-save-highlight',
};

chrome.runtime.onInstalled.addListener(() => {
  // Recreate cleanly so an upgrade never duplicates items.
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU.SAVE,
      title: 'Save to Tiro',
      contexts: ['page', 'selection'],
    });
    chrome.contextMenus.create({
      id: MENU.SAVE_VIP,
      title: 'Save to Tiro as VIP',
      contexts: ['page', 'selection'],
    });
    chrome.contextMenus.create({
      id: MENU.SAVE_HIGHLIGHT,
      title: 'Save to Tiro with selection as highlight',
      contexts: ['selection'],
    });
  });
});

function flashBadge(text, color) {
  try {
    chrome.action.setBadgeBackgroundColor({ color });
    chrome.action.setBadgeText({ text });
    setTimeout(() => chrome.action.setBadgeText({ text: '' }), 4000);
  } catch (_) {
    // Badge is cosmetic; never let it break the save.
  }
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title,
      message,
    });
  } catch (_) {
    // Notifications are best-effort feedback only.
  }
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = tab && tab.url;
  if (!isSavableUrl(url)) {
    flashBadge('!', '#B8943E');
    notify('Tiro', 'This page cannot be saved (not an http/https URL).');
    return;
  }

  const token = await loadToken();
  const vip = info.menuItemId === MENU.SAVE_VIP;
  const payload = { url, ingestion_method: 'extension' };
  if (info.menuItemId === MENU.SAVE_HIGHLIGHT) {
    const sel = (info.selectionText || '').trim();
    if (!sel) {
      flashBadge('!', '#B8943E');
      notify('Tiro', 'No text selected to highlight.');
      return;
    }
    payload.highlight_text = sel;
  }

  flashBadge('…', '#8A7E72');
  try {
    const { status, body } = await postSave(token, payload);
    const result = classifySaveResponse(status, body);

    if (result.kind === 'auth') {
      flashBadge('!', '#C45B3E');
      notify('Tiro', 'Set your API token in the extension popup first.');
      return;
    }
    if (result.kind === 'error') {
      flashBadge('✕', '#C45B3E');
      notify('Tiro — save failed', result.error);
      return;
    }
    if (result.kind === 'already') {
      flashBadge('✓', '#B8943E');
      notify('Tiro', 'Already in your library' + (result.data && result.data.title ? ': ' + result.data.title : '.'));
      return;
    }

    // kind === 'saved'
    if (vip && result.data && result.data.source_id) {
      try {
        await setSourceVip(token, result.data.source_id);
      } catch (_) {
        // VIP toggle is best-effort (matches the popup).
      }
    }
    flashBadge('✓', '#6B7F4E');
    let msg = 'Saved: ' + ((result.data && result.data.title) || url);
    if (payload.highlight_text) {
      msg += result.highlightCreated
        ? ' — highlight anchored.'
        : " — couldn't anchor the selection (saved without highlight).";
    }
    if (vip) msg += ' (VIP)';
    notify('Tiro', msg);
  } catch (err) {
    flashBadge('✕', '#C45B3E');
    const offline = err && err.message && err.message.includes('Failed to fetch');
    notify('Tiro — save failed', offline ? 'Tiro server not running on localhost:8000.' : String(err && err.message || err));
  }
});
