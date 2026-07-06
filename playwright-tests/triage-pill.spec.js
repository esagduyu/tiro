// triage-pill.spec.js — M3.2 Task 4 (triage progress pill + inbox-zero
// state + logout SW-cache hardening).
//
// Covers:
//   - mobile emulation: 2-article scratch library -> pill "2 to zero" ->
//     swipe-archive one -> "1 to zero" (live, no reload) -> swipe-archive
//     the last -> inbox-zero state visible + pill hidden -> Undo -> pill
//     back to "1 to zero" + zero state gone
//   - console sweep in both light and dark theme while the zero state is
//     showing
//   - desktop: logout clears the SW's tiro-*-articles cache (created first
//     by opening an article) without blocking the actual logout/redirect
//
// Run against a SCRATCH Tiro server only (never the real library):
//
//   TIRO_CONFIG=/path/to/scratch/config.yaml \
//   uv run python run.py &
//   cd playwright-tests && TIRO_PASSWORD=<pw> npx playwright test triage-pill.spec.js --workers=1
//
// The first (pill/zero-state) test asserts the EXACT text "2 to zero" / "1
// to zero", which requires a library with NOTHING else unread in it --
// same constraint phase0.spec.js documents for its own global assertion.
// Run this spec against a freshly emptied scratch library (or first, before
// swipe-triage.spec.js/snooze-ui.spec.js seed their own articles into a
// shared one); cross-spec contamination on a shared scratch library is a
// test-authoring reality here, not a product regression.

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'triage-pill-spec-test-pass';

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

async function saveArticleViaApi(page, uniqueSuffix) {
  const result = await page.evaluate(async (suffix) => {
    const res = await fetch('/api/ingest/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: `https://example.com/?triage-pill-spec=${suffix}` }),
    });
    return await res.json();
  }, uniqueSuffix);
  if (!result || !result.data || !result.data.id) {
    throw new Error(`saveArticleViaApi failed: ${JSON.stringify(result)}`);
  }
  return result.data.id;
}

// Drag horizontally across a card, starting in its bottom padding (never an
// interactive descendant). Mirrors swipe-triage.spec.js's helper exactly.
async function dragCard(page, card, dxFraction) {
  const box = await card.boundingBox();
  const startX = box.x + box.width * 0.45;
  const startY = box.y + box.height - 6;
  const dx = box.width * dxFraction;

  await page.mouse.move(startX, startY);
  await page.mouse.down();
  const steps = 8;
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(startX + (dx * i) / steps, startY);
    await page.waitForTimeout(25);
  }
  await page.mouse.up();
}

function collectConsoleErrors(page) {
  const errors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push(String(err)));
  return errors;
}

test.describe('M3.2 triage pill + inbox-zero (mobile emulation)', () => {
  test.use({
    baseURL: BASE_URL,
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('tiro-a2hs-hint-dismissed', '1');
    });
  });

  test('2-article scratch: pill counts down to zero, inbox-zero appears, undo restores', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    const suffix = Date.now();
    const id1 = await saveArticleViaApi(page, `${suffix}-a`);
    const id2 = await saveArticleViaApi(page, `${suffix}-b`);

    await page.goto('/inbox');
    const pill = page.locator('#triage-pill');
    await expect(pill).toBeVisible();
    await expect(pill).toHaveText('2 to zero');
    await expect(page.locator('#inbox-zero-state')).toBeHidden();

    // Archive the first card -> pill live-updates to "1 to zero" with NO
    // full page reload (adjustUnreadCount(-1), not a re-fetch).
    const card1 = page.locator(`.article-card[data-id="${id1}"]`);
    await expect(card1).toBeVisible();
    await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id1}/read`) && res.request().method() === 'PATCH'
      ),
      dragCard(page, card1, 0.6),
    ]);
    await expect(pill).toHaveText('1 to zero');
    await expect(page.locator('#inbox-zero-state')).toBeHidden();
    // Dismiss the first undo toast so it doesn't intercept the second drag.
    await page.waitForTimeout(5200);

    // Archive the last remaining card -> pill hides, inbox-zero appears.
    const card2 = page.locator(`.article-card[data-id="${id2}"]`);
    await expect(card2).toBeVisible();
    await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id2}/read`) && res.request().method() === 'PATCH'
      ),
      dragCard(page, card2, 0.6),
    ]);

    const zeroState = page.locator('#inbox-zero-state');
    await expect(zeroState).toBeVisible();
    await expect(zeroState).toContainText('Inbox zero');
    await expect(pill).toBeHidden();

    // Console sweep in light theme while the zero state is showing.
    expect(consoleErrors, `light-theme console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);

    // Toggle dark mode with the zero state on screen -- sweep again. Mobile
    // viewport: the theme toggle lives inside the hamburger sidebar, so open
    // it first.
    await page.locator('#mobile-menu-btn').click();
    await page.locator('#mobile-theme-toggle').click({ force: true });
    await page.waitForTimeout(300);
    await expect(zeroState).toBeVisible();
    expect(consoleErrors, `dark-theme console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
    // Close the sidebar (back to light doesn't matter for assertions either way).
    await page.locator('#sidebar-overlay').click({ force: true }).catch(() => {});

    // Undo the last archive -> pill back to "1 to zero", zero state gone.
    const toast = page.locator('#undo-toast');
    await expect(toast).toBeVisible();
    await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id2}/read`) && res.request().method() === 'PATCH'
      ),
      toast.locator('.undo-toast-btn').click(),
    ]);

    await expect(pill).toHaveText('1 to zero');
    await expect(page.locator('#inbox-zero-state')).toBeHidden();

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});

test.describe('M3.2 logout SW-cache hardening (desktop)', () => {
  test.use({ baseURL: BASE_URL, viewport: { width: 1280, height: 800 } });

  test('logout clears tiro-*-articles cache without blocking the redirect', async ({ page }) => {
    const consoleErrors = collectConsoleErrors(page);
    await loginOrSetup(page);

    const id = await saveArticleViaApi(page, `${Date.now()}-logout-cache`);

    // First navigation only REGISTERS the service worker; it does not
    // control this page's own fetches yet (sw.js deliberately skips
    // skipWaiting()/clients.claim() -- see its header comment). Wait for
    // activation, then navigate again so the reader page's own
    // `GET /api/articles/{id}` fetch is actually intercepted and cached.
    await page.goto('/inbox');
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.goto(`/articles/${id}`);
    await page.waitForTimeout(500);

    const hasArticlesCacheBefore = await page.evaluate(async () => {
      const keys = await caches.keys();
      return keys.some((k) => /^tiro-.*-articles$/.test(k));
    });
    expect(hasArticlesCacheBefore).toBe(true);

    await page.goto('/inbox');
    // Let the page's own load-time fetches (loadInbox/loadFilters) settle
    // before navigating away -- otherwise the abort from clicking logout
    // mid-flight logs an unrelated "Failed to fetch" console error that has
    // nothing to do with the cache-clear behavior under test.
    await expect(page.locator('.article-card').first()).toBeVisible();
    await page.waitForTimeout(300);
    await Promise.all([
      page.waitForURL(/\/login/),
      page.locator('#logout-btn').click(),
    ]);

    const keysAfter = await page.evaluate(() => caches.keys());
    expect(keysAfter.some((k) => /^tiro-.*-articles$/.test(k))).toBe(false);

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});
