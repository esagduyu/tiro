// save-queue.spec.js — M3.1 Task 3 (offline save queue + Add-to-Home-Screen
// hint) end-to-end check, mirroring annotations.spec.js/telemetry.spec.js's
// bootstrap (first-run setup or login, save an article via the "+" modal).
//
// Run against a SCRATCH Tiro server only (never the real library):
//
//   TIRO_CONFIG=/path/to/scratch/config.yaml \
//   TIRO_LIBRARY_PATH=/path/to/scratch/library \
//   uv run python run.py &
//   cd playwright-tests && TIRO_PASSWORD=<pw> npx playwright test save-queue.spec.js
//
// Configuration (env vars, both optional, same defaults as phase0.spec.js):
//   TIRO_URL      base URL of the running server (default http://localhost:8000)
//   TIRO_PASSWORD password to use for login / first-run setup
//
// Approach for the queue test: route-abort POST /api/ingest/url to
// deterministically simulate a network failure (fetch() rejects with a
// TypeError, exactly like a real offline attempt), rather than actually
// toggling the OS/browser offline -- this mirrors the T4-M2.2 race-test
// pattern in annotations.spec.js (page.route interception makes timing
// deterministic instead of racing real network conditions). Once queued,
// un-route (so the next attempt succeeds) and dispatch a real `online`
// event on the page to trigger the drain, then assert the success toast and
// that the indicator clears.

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'save-queue-spec-test-pass';
const TEST_URL = 'https://example.com';
const TEST_TITLE = 'Example Domain';

async function loginOrSetup(page) {
  await page.goto('/login');

  const setupHeading = page.getByText('Set a password to protect your library');
  const isFirstRun = await setupHeading.isVisible().catch(() => false);

  if (isFirstRun) {
    await page.getByRole('textbox', { name: 'Password', exact: true }).fill(PASSWORD);
    await page.getByRole('textbox', { name: 'Confirm password' }).fill(PASSWORD);
    await page.getByRole('button', { name: 'Create password' }).click();
  } else {
    await page.getByRole('textbox', { name: 'Password' }).fill(PASSWORD);
    await page.getByRole('button', { name: 'Sign in' }).click();
  }

  await expect(page).toHaveURL(/\/inbox/);
}

test.describe('M3.1 offline save queue', () => {
  test.use({ baseURL: BASE_URL });

  test.beforeEach(async ({ page }) => {
    // Each test starts from a clean queue/dismissal state in localStorage --
    // otherwise a prior test's queued entry or A2HS dismissal would leak
    // into the next one via the same origin's storage. Guarded by a
    // sessionStorage one-time flag: addInitScript re-runs on EVERY
    // navigation within the test (including page.reload()), and several
    // tests below deliberately seed localStorage mid-test and then reload
    // -- an unguarded clear would wipe that seeded state out again right
    // before the reload's own scripts see it.
    await page.addInitScript(() => {
      if (!window.sessionStorage.getItem('__spec_storage_cleared')) {
        window.localStorage.removeItem('tiro-save-queue');
        window.localStorage.removeItem('tiro-a2hs-hint-dismissed');
        window.sessionStorage.setItem('__spec_storage_cleared', '1');
      }
    });
  });

  test('network failure queues the save; online event drains it', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      // Chromium logs the aborted request itself as a console error
      // ("Failed to load resource: net::ERR_FAILED") -- that's the expected
      // side effect of route.abort() below, not an application bug, so it's
      // filtered out here rather than asserted away entirely.
      if (msg.type() === 'error' && !msg.text().includes('net::ERR_FAILED')) {
        consoleErrors.push(msg.text());
      }
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    // Unique per run (query string is ignored by example.com but keeps the
    // URL -- and hence the DB row -- distinct from whatever the OTHER test
    // in this file already saved, or a prior run left behind in the scratch
    // library): the retry below must land a fresh 200, not collide into the
    // 409 path that the other test exercises on purpose.
    const uniqueUrl = `${TEST_URL}/?save-queue-spec=${Date.now()}`;

    await loginOrSetup(page);

    // Route-abort the ingest POST -- simulates a network failure the same
    // way a real offline attempt would (fetch() rejects, not a 4xx/5xx).
    let ingestCallCount = 0;
    await page.route('**/api/ingest/url', async (route) => {
      ingestCallCount += 1;
      if (ingestCallCount === 1) {
        await route.abort('failed');
      } else {
        await route.continue();
      }
    });

    await page.locator('#save-btn').click();
    const urlInput = page.getByRole('textbox', { name: 'Paste a URL...' });
    await expect(urlInput).toBeVisible();
    await urlInput.fill(uniqueUrl);
    await urlInput.press('Enter');

    // Queued toast + modal closes (per the wiring in sidebar.js's submitURL).
    await expect(page.getByText('Offline — queued; will retry when back online')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('#save-overlay')).not.toBeVisible();

    // Reopen the modal -- the "N queued" indicator must be visible now.
    await page.locator('#save-btn').click();
    await expect(page.locator('#save-queue-indicator')).toBeVisible();
    await expect(page.locator('#save-queue-indicator')).toHaveText('1 queued');

    // Confirm it actually persisted to localStorage under the documented key.
    const stored = await page.evaluate(() => window.localStorage.getItem('tiro-save-queue'));
    const parsed = JSON.parse(stored);
    expect(parsed).toHaveLength(1);
    expect(parsed[0].url).toBe(uniqueUrl);

    await page.locator('#save-modal-close').click();

    // Un-abort: the next ingest POST succeeds for real. Dispatch a real
    // `online` event on the page -- this is the exact listener sidebar.js
    // registers (`window.addEventListener('online', drainSaveQueue)`), so
    // this exercises production code, not a test-only shortcut.
    const [drainResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/api/ingest/url') && res.request().method() === 'POST'
      ),
      page.evaluate(() => window.dispatchEvent(new Event('online'))),
    ]);
    expect(drainResponse.status()).toBe(200);

    await expect(page.getByText(`Saved queued article: ${TEST_TITLE}`)).toBeVisible({ timeout: 10000 });

    // Queue drained -- indicator gone, localStorage empty array.
    await page.locator('#save-btn').click();
    await expect(page.locator('#save-queue-indicator')).not.toBeVisible();
    const storedAfter = await page.evaluate(() => window.localStorage.getItem('tiro-save-queue'));
    expect(JSON.parse(storedAfter)).toEqual([]);
    await page.locator('#save-modal-close').click();

    await page.unroute('**/api/ingest/url');
    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  test('409 already_saved on retry removes the entry silently (no failure toast)', async ({ page }) => {
    await loginOrSetup(page);

    // Seed the queue directly (bypassing the UI flow, which is already
    // covered by the test above) with a URL guaranteed to already exist,
    // then trigger the drain via page load instead of the `online` event --
    // covers the OTHER drain trigger (DOMContentLoaded + navigator.onLine).
    await page.evaluate((url) => {
      window.localStorage.setItem(
        'tiro-save-queue',
        JSON.stringify([{ url, is_vip: false, ts: Date.now() }])
      );
    }, TEST_URL);

    // Ensure the URL actually exists server-side first so the retry 409s.
    const checkRes = await page.request.get(`/api/ingest/check?url=${encodeURIComponent(TEST_URL)}`);
    const checkJson = await checkRes.json();
    if (!checkJson.saved) {
      // Not present yet (e.g. fresh scratch library) -- save it for real
      // once, then re-seed the queue (the direct save already drains
      // nothing since the queue was empty at that point).
      await page.goto('/inbox');
      await page.locator('#save-btn').click();
      const urlInput = page.getByRole('textbox', { name: 'Paste a URL...' });
      await urlInput.fill(TEST_URL);
      await urlInput.press('Enter');
      await expect(page.getByText(`Saved: ${TEST_TITLE}`)).toBeVisible({ timeout: 30000 });
      await page.locator('#save-modal-close').click();
      await page.evaluate((url) => {
        window.localStorage.setItem(
          'tiro-save-queue',
          JSON.stringify([{ url, is_vip: false, ts: Date.now() }])
        );
      }, TEST_URL);
    }

    const [retryResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/api/ingest/url') && res.request().method() === 'POST'
      ),
      page.reload(),
    ]);
    expect(retryResponse.status()).toBe(409);

    // Silent removal -- no failure toast, queue empty afterward.
    await expect(page.locator('.settings-toast-error')).toHaveCount(0);
    const storedAfter = await page.evaluate(() => window.localStorage.getItem('tiro-save-queue'));
    expect(JSON.parse(storedAfter)).toEqual([]);
  });
});

test.describe('M3.1 Add-to-Home-Screen hint', () => {
  test.use({ baseURL: BASE_URL });

  test('appears on a mobile viewport, dismiss persists across reload', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 }); // iPhone-ish
    // Guarded the same way as the offline-queue describe block above: this
    // test reloads mid-test and relies on the dismissal it just set
    // surviving that reload, so the clear must only run once, before the
    // FIRST navigation, not on every one.
    await page.addInitScript(() => {
      if (!window.sessionStorage.getItem('__spec_storage_cleared')) {
        window.localStorage.removeItem('tiro-a2hs-hint-dismissed');
        window.sessionStorage.setItem('__spec_storage_cleared', '1');
      }
    });

    await loginOrSetup(page);

    await expect(page.locator('#a2hs-hint')).toBeVisible({ timeout: 8000 });
    await expect(page.locator('.a2hs-hint-text')).toHaveText(
      'Tip: add Tiro to your home screen for the full app experience'
    );

    await page.locator('#a2hs-hint-dismiss').click();
    await expect(page.locator('#a2hs-hint')).toHaveCount(0);

    const dismissed = await page.evaluate(() => window.localStorage.getItem('tiro-a2hs-hint-dismissed'));
    expect(dismissed).toBe('1');

    // Reload -- must stay gone (localStorage persists the dismissal).
    await page.reload();
    await page.waitForTimeout(4000); // longer than the hint's own delay
    await expect(page.locator('#a2hs-hint')).toHaveCount(0);
  });

  test('does not appear on a desktop viewport', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.addInitScript(() => {
      if (!window.sessionStorage.getItem('__spec_storage_cleared')) {
        window.localStorage.removeItem('tiro-a2hs-hint-dismissed');
        window.sessionStorage.setItem('__spec_storage_cleared', '1');
      }
    });

    await loginOrSetup(page);
    await page.waitForTimeout(4000);
    await expect(page.locator('#a2hs-hint')).toHaveCount(0);
  });
});
