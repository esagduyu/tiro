// telemetry.spec.js — M2.3 Task 2 (reader-session telemetry tracker +
// settings toggle) end-to-end check, mirroring annotations.spec.js's
// bootstrap (first-run setup or login, save an article via the "+" modal,
// open the reader).
//
// Run against a SCRATCH Tiro server only (never the real library) — see
// .superpowers/sdd/task-2-report.md (M2.3) for the exact invocation used
// during development:
//
//   TIRO_CONFIG=/path/to/scratch/config.yaml \
//   uv run python run.py &
//   cd playwright-tests && TIRO_PASSWORD=<pw> npx playwright test telemetry.spec.js
//
// Configuration (env vars, both optional, same defaults as phase0.spec.js):
//   TIRO_URL      base URL of the running server (default http://localhost:8000)
//   TIRO_PASSWORD password to use for login / first-run setup
//
// Approach: telemetry is sent via `navigator.sendBeacon` on
// `visibilitychange -> hidden` (see reader.js's `setupTelemetry`). jsdom-free
// Playwright can't fire a REAL OS-level tab-hide event, so both scenarios
// below force it the same way: `Object.defineProperty(document,
// "visibilityState", { value: "hidden", configurable: true })` followed by
// `document.dispatchEvent(new Event("visibilitychange"))` inside
// `page.evaluate` — this is exactly the code path reader.js's
// `handleTelemetryVisibilityChange` listens for, so it exercises the real
// production listener, not a test-only shortcut. Because `sendBeacon` fires
// a fire-and-forget request outside the normal navigation lifecycle,
// `page.waitForResponse` / `page.route` interception (rather than a
// server-side DB read — there is no GET for reading_sessions) is the
// assertion surface for "did a POST happen", matching the brief's documented
// fallback for exactly this reason.

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'telemetry-spec-test-pass';
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
  // Let inbox.js's own DOMContentLoaded fetches (loadInbox/loadFilters)
  // settle before any test navigates away — navigating mid-fetch aborts
  // them client-side, surfacing as a "Failed to load filters: TypeError:
  // Failed to fetch" console.error that has nothing to do with telemetry
  // (observed during development of this spec when a test went straight
  // from login to /settings).
  await page.waitForLoadState('networkidle').catch(() => {});
}

async function findExistingTestArticleId(page) {
  // Checked via the API directly (rather than opening /inbox and reading the
  // DOM) so repeat runs of this spec never have to touch the inbox page at
  // all — inbox.js's own DOMContentLoaded fetches (loadInbox/loadFilters)
  // racing a near-immediate navigation-away click is a PRE-EXISTING,
  // telemetry-unrelated source of console noise ("Failed to load filters:
  // TypeError: Failed to fetch") observed during development of this spec;
  // going straight to the article via its id sidesteps it entirely instead
  // of trying to out-wait it.
  return page.evaluate(async (title) => {
    const res = await fetch('/api/articles');
    const json = await res.json();
    const match = (json.data || []).find((a) => a.title === title);
    return match ? match.id : null;
  }, TEST_TITLE);
}

async function saveAndOpenTestArticle(page) {
  const existingId = await findExistingTestArticleId(page);
  if (existingId) {
    await page.goto(`/articles/${existingId}`);
  } else {
    await page.goto('/inbox');
    await page.locator('#sidebar-save-btn').click();
    const urlInput = page.getByRole('textbox', { name: 'Paste a URL...' });
    await expect(urlInput).toBeVisible();
    await urlInput.fill(TEST_URL);
    await urlInput.press('Enter');
    await expect(page.getByText(`Saved: ${TEST_TITLE}`)).toBeVisible({ timeout: 30000 });
    await page.locator('#save-modal-close').click();
    const articleLink = page.getByRole('link', { name: TEST_TITLE }).first();
    await expect(articleLink).toBeVisible();
    await articleLink.click();
  }

  await expect(page).toHaveURL(/\/articles\/\d+/);
  await expect(page.getByRole('heading', { name: TEST_TITLE }).first()).toBeVisible();
  await page.waitForSelector('#reader-body p', { timeout: 10000 });
}

async function forceTabHidden(page) {
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

async function setTelemetryEnabled(page, enabled) {
  const res = await page.evaluate(async (enabledVal) => {
    const r = await fetch('/api/settings/telemetry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: enabledVal }),
    });
    return { status: r.status, json: await r.json() };
  }, enabled);
  expect(res.status).toBe(200);
  expect(res.json.data.enabled).toBe(enabled);
}

test.describe('M2.3 reading-session telemetry', () => {
  test.use({ baseURL: BASE_URL });

  test('disabled by default: hiding the tab fires no /session request', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await loginOrSetup(page);
    await setTelemetryEnabled(page, false); // explicit: don't depend on scratch-config default
    await page.goto('/settings');
    await expect(page.locator('#btn-toggle-telemetry')).toHaveText('Enable Telemetry');

    await saveAndOpenTestArticle(page);
    await expect(page.locator('#reader[data-telemetry="off"]')).toHaveCount(1);

    let sessionRequestSeen = false;
    page.on('request', (req) => {
      if (req.url().includes('/session') && req.method() === 'POST') sessionRequestSeen = true;
    });

    // Some active time + scroll first, so a false negative can't be blamed
    // on the empty-session guard rather than the disabled-flag gate.
    await page.mouse.move(200, 200);
    await page.mouse.wheel(0, 400);
    await page.waitForTimeout(500);

    await forceTabHidden(page);
    await page.waitForTimeout(500); // give sendBeacon a moment, if it were going to fire

    expect(sessionRequestSeen).toBe(false);
    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });

  test('enabled via settings UI: hiding the tab fires a /session POST', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await loginOrSetup(page);

    // Enable via the actual settings UI (not the raw API) — this is the
    // "toggle" part of the task, not just the backend contract T1 already
    // covers with pytest.
    await page.goto('/settings');
    const toggleBtn = page.locator('#btn-toggle-telemetry');
    await expect(toggleBtn).toBeVisible();
    if ((await toggleBtn.textContent()).trim() === 'Enable Telemetry') {
      await toggleBtn.click();
      await expect(page.locator('.settings-toast')).toContainText('enabled');
    }
    await expect(toggleBtn).toHaveText('Disable Telemetry');

    // Reload the reader so the freshly-enabled flag is threaded into
    // data-telemetry server-side (the attribute is rendered once per page
    // load from config, not live-patched).
    await saveAndOpenTestArticle(page);
    await page.reload();
    await page.waitForSelector('#reader-body p', { timeout: 10000 });
    await expect(page.locator('#reader[data-telemetry="on"]')).toHaveCount(1);

    // Generate real active time + scroll so the empty-session guard doesn't
    // suppress the send.
    await page.mouse.move(200, 200);
    await page.mouse.wheel(0, 600);
    await page.waitForTimeout(1500);

    // NOTE: sendBeacon's Blob body is not observable via
    // request.postData()/postDataBuffer() in Playwright/CDP (verified during
    // development — both return null for this request, a known Beacon API
    // limitation, unlike a normal fetch/XHR body) — so the assertion below
    // is on the RESPONSE side only; payload-shape correctness (clamping,
    // truncation, dwell caps) is covered exhaustively by T1's pytest suite
    // (test_sessions_api.py) instead.
    const [sessionResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/session') && res.request().method() === 'POST',
        { timeout: 5000 }
      ),
      forceTabHidden(page),
    ]);

    // No GET exists for reading_sessions, so the response status from the
    // real sendBeacon-triggered request (Chromium surfaces it via CDP
    // Network events the same as a normal fetch, so Playwright's
    // waitForResponse sees it) is the "a row lands" evidence the brief asks
    // for: 200 means routes_sessions.py validated, clamped, and inserted it.
    expect(sessionResponse.status()).toBe(200);

    // Turn telemetry back off so a repeated local run of this spec starts
    // from the same disabled-by-default baseline the first test expects.
    await setTelemetryEnabled(page, false);

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});
