import {
  TIRO_URL,
  authHeaders,
  savableTabs,
  postSave,
  classifySaveResponse,
  setSourceVip,
  loadToken as loadStoredToken,
} from './lib.js';

let apiToken = '';

function loadToken(cb) {
  loadStoredToken().then((t) => {
    apiToken = t;
    cb();
  });
}

function saveToken(value, cb) {
  chrome.storage.local.set({ tiroToken: value }, cb);
}

const els = {
  stateReady: document.getElementById('state-ready'),
  stateSaving: document.getElementById('state-saving'),
  stateSuccess: document.getElementById('state-success'),
  stateError: document.getElementById('state-error'),
  stateAlready: document.getElementById('state-already'),
  stateToken: document.getElementById('state-token'),
  stateAlltabs: document.getElementById('state-alltabs'),
  pageTitle: document.getElementById('page-title'),
  pageUrl: document.getElementById('page-url'),
  vipToggle: document.getElementById('vip-toggle'),
  saveBtn: document.getElementById('save-btn'),
  alltabsBtn: document.getElementById('alltabs-btn'),
  alltabsProgress: document.getElementById('alltabs-progress'),
  alltabsList: document.getElementById('alltabs-list'),
  alltabsDoneBtn: document.getElementById('alltabs-done-btn'),
  successTitle: document.getElementById('success-title'),
  successSource: document.getElementById('success-source'),
  openLink: document.getElementById('open-link'),
  errorText: document.getElementById('error-text'),
  retryBtn: document.getElementById('retry-btn'),
  alreadyTitle: document.getElementById('already-title'),
  alreadyTime: document.getElementById('already-time'),
  alreadyLink: document.getElementById('already-link'),
  tokenInput: document.getElementById('token-input'),
  tokenSaveBtn: document.getElementById('token-save-btn'),
  tokenCancelBtn: document.getElementById('token-cancel-btn'),
  tokenGear: document.getElementById('token-gear'),
};

let currentUrl = '';
let lastNonTokenState = 'ready';

function showState(name) {
  if (name !== 'token' && name !== 'saving' && name !== 'alltabs') {
    lastNonTokenState = name;
  }
  ['stateReady', 'stateSaving', 'stateSuccess', 'stateError', 'stateAlready', 'stateToken', 'stateAlltabs'].forEach(
    function (key) {
      els[key].classList.toggle('active', key === 'state' + name.charAt(0).toUpperCase() + name.slice(1));
    },
  );
}

function formatTimeAgo(isoStr) {
  var date = new Date(isoStr);
  var now = new Date();
  var diffMs = now - date;
  var diffMin = Math.floor(diffMs / 60000);
  var diffHr = Math.floor(diffMin / 60);
  var diffDay = Math.floor(diffHr / 24);

  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return diffMin + ' minute' + (diffMin === 1 ? '' : 's') + ' ago';
  if (diffHr < 24) return diffHr + ' hour' + (diffHr === 1 ? '' : 's') + ' ago';
  if (diffDay < 30) return diffDay + ' day' + (diffDay === 1 ? '' : 's') + ' ago';
  return date.toLocaleDateString();
}

// Get current tab info on popup open, then check if already saved
loadToken(function () {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    if (tabs[0]) {
      currentUrl = tabs[0].url;
      els.pageTitle.textContent = tabs[0].title || 'Untitled page';
      els.pageUrl.textContent = currentUrl;
      checkIfSaved(currentUrl);
    }
  });
});

async function checkIfSaved(url) {
  try {
    var res = await fetch(TIRO_URL + '/api/ingest/check?url=' + encodeURIComponent(url), { headers: authHeaders(apiToken) });
    if (res.status === 401) {
      showState('token');
      return;
    }
    var data = await res.json();
    if (data.success && data.saved) {
      els.alreadyTitle.textContent = data.data.title;
      els.alreadyTime.textContent = 'Saved ' + formatTimeAgo(data.data.ingested_at);
      els.alreadyLink.href = TIRO_URL + '/articles/' + data.data.id;
      showState('already');
    }
  } catch (_) {
    // Server might not be running — that's fine, user will see the error when they try to save
  }
}

// Save button click
els.saveBtn.addEventListener('click', saveArticle);
els.alltabsBtn.addEventListener('click', saveAllTabs);
els.alltabsDoneBtn.addEventListener('click', function () {
  showState('ready');
});
els.retryBtn.addEventListener('click', function () {
  showState('ready');
});

async function saveArticle() {
  if (!currentUrl) return;

  showState('saving');
  els.tokenGear.disabled = true;

  try {
    var out = await postSave(apiToken, { url: currentUrl, ingestion_method: 'extension' });
    var result = classifySaveResponse(out.status, out.body);

    if (result.kind === 'auth') {
      showState('token');
      return;
    }

    if (result.kind === 'saved') {
      var article = result.data;
      els.successTitle.textContent = article.title || 'Saved';
      els.successSource.textContent = article.source || '';
      els.openLink.href = TIRO_URL + '/articles/' + article.id;

      if (els.vipToggle.checked && article.source_id) {
        try {
          await setSourceVip(apiToken, article.source_id);
        } catch (_) {
          // VIP toggle is best-effort
        }
      }

      showState('success');
    } else if (result.kind === 'already') {
      els.alreadyTitle.textContent = result.data.title;
      els.alreadyTime.textContent = 'Saved ' + formatTimeAgo(result.data.ingested_at);
      els.alreadyLink.href = TIRO_URL + '/articles/' + result.data.id;
      showState('already');
    } else {
      els.errorText.textContent = result.error || 'Could not save this page.';
      showState('error');
    }
  } catch (err) {
    if (err.message && err.message.includes('Failed to fetch')) {
      els.errorText.textContent = 'Tiro server not running. Start it with: uv run python run.py';
    } else {
      els.errorText.textContent = err.message || 'Could not save this page.';
    }
    showState('error');
  } finally {
    els.tokenGear.disabled = false;
  }
}

// Save every http(s) tab in the current window, sequentially, reporting per-tab.
async function saveAllTabs() {
  showState('alltabs');
  els.alltabsList.innerHTML = '';
  els.alltabsDoneBtn.disabled = true;

  var tabs = await new Promise(function (resolve) {
    chrome.tabs.query({ currentWindow: true }, resolve);
  });
  var targets = savableTabs(tabs);

  if (!targets.length) {
    els.alltabsProgress.textContent = 'No savable tabs in this window.';
    els.alltabsDoneBtn.disabled = false;
    return;
  }

  var counts = { saved: 0, already: 0, failed: 0 };
  var rows = targets.map(function (tab) {
    var row = document.createElement('div');
    row.className = 'alltabs-item';
    var mark = document.createElement('span');
    mark.className = 'mark pending';
    mark.textContent = '·';
    var title = document.createElement('span');
    title.className = 'tab-title';
    title.textContent = tab.title || tab.url;
    var status = document.createElement('span');
    status.className = 'tab-status';
    status.textContent = '…';
    row.appendChild(mark);
    row.appendChild(title);
    row.appendChild(status);
    els.alltabsList.appendChild(row);
    return { mark: mark, status: status };
  });

  for (var i = 0; i < targets.length; i++) {
    els.alltabsProgress.textContent = 'Saving tab ' + (i + 1) + ' of ' + targets.length + '…';
    var ui = rows[i];
    try {
      var out = await postSave(apiToken, { url: targets[i].url, ingestion_method: 'extension' });
      var result = classifySaveResponse(out.status, out.body);
      if (result.kind === 'auth') {
        // No token — stop early and route to the token screen.
        showState('token');
        return;
      }
      if (result.kind === 'saved') {
        counts.saved++;
        ui.mark.className = 'mark ok';
        ui.mark.textContent = '✓';
        ui.status.textContent = 'saved';
      } else if (result.kind === 'already') {
        counts.already++;
        ui.mark.className = 'mark dup';
        ui.mark.textContent = '✓';
        ui.status.textContent = 'already saved';
      } else {
        counts.failed++;
        ui.mark.className = 'mark err';
        ui.mark.textContent = '✗';
        ui.status.textContent = result.error || 'failed';
      }
    } catch (err) {
      counts.failed++;
      ui.mark.className = 'mark err';
      ui.mark.textContent = '✗';
      ui.status.textContent = (err && err.message && err.message.includes('Failed to fetch')) ? 'server offline' : 'failed';
    }
  }

  els.alltabsProgress.textContent =
    counts.saved + ' saved, ' + counts.already + ' already, ' + counts.failed + ' failed (' + targets.length + ' tabs)';
  els.alltabsDoneBtn.disabled = false;
}

els.tokenSaveBtn.addEventListener('click', function () {
  var value = els.tokenInput.value.trim();
  if (!value) return;
  saveToken(value, function () {
    apiToken = value;
    showState('ready');
    checkIfSaved(currentUrl);
  });
});

els.tokenCancelBtn.addEventListener('click', function () {
  showState(lastNonTokenState);
});

els.tokenGear.addEventListener('click', function () {
  els.tokenInput.value = '';
  showState('token');
});
