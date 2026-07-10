// snooze-ui.spec.js — M3.2 Task 1 (snoozed_until surfacing + snooze UI).
//
// Covers the new UI on top of M3.0's snooze backend (PATCH
// /api/articles/{id}/snooze): the per-card overflow menu -> "Snooze…" preset
// sheet -> card leaves the inbox, the "Snoozed" toolbar toggle revealing it
// again dimmed with a wake-time chip, and "Wake now" bringing it back to
// normal. Mirrors annotations.spec.js/phase0.spec.js's login/save bootstrap.
//
// Run against a SCRATCH Tiro server only (never the real library):
//
//   TIRO_CONFIG=/path/to/scratch/config.yaml \
//   TIRO_LIBRARY_PATH=/path/to/scratch/library \
//   uv run python run.py &
//   cd playwright-tests && TIRO_PASSWORD=<pw> npx playwright test snooze-ui.spec.js
//
// Configuration (env vars, both optional, same defaults as phase0.spec.js):
//   TIRO_URL      base URL of the running server (default http://localhost:8000)
//   TIRO_PASSWORD password to use for login / first-run setup

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TIRO_URL || 'http://localhost:8000';
const PASSWORD = process.env.TIRO_PASSWORD || 'snooze-ui-spec-test-pass';
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

// Saves a fresh (never-before-seen) example.com URL via the "+" modal and
// returns the new article's id, read straight off the POST /api/ingest/url
// response rather than off DOM text — two articles both titled "Example
// Domain" are otherwise indistinguishable by name.
async function saveArticle(page, uniqueSuffix) {
  await page.locator('#sidebar-save-btn').click();
  const urlInput = page.getByRole('textbox', { name: 'Paste a URL...' });
  await expect(urlInput).toBeVisible();
  await urlInput.fill(`${TEST_URL}/?snooze-ui-spec=${uniqueSuffix}`);

  const [resp] = await Promise.all([
    page.waitForResponse(
      (res) => res.url().includes('/api/ingest/url') && res.request().method() === 'POST'
    ),
    urlInput.press('Enter'),
  ]);
  await expect(page.getByText(`Saved: ${TEST_TITLE}`)).toBeVisible({ timeout: 30000 });
  await page.locator('#save-modal-close').click();

  const body = await resp.json();
  return body.data.id;
}

test.describe('M3.2 snooze triage UI', () => {
  test.use({ baseURL: BASE_URL });

  test('snooze via card menu -> gone -> Snoozed toggle -> chip -> Wake now -> back', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    await loginOrSetup(page);

    const stamp = Date.now();
    const id1 = await saveArticle(page, `${stamp}-a`);
    const id2 = await saveArticle(page, `${stamp}-b`);
    expect(id1).not.toEqual(id2);

    const card1 = page.locator(`.article-card[data-id="${id1}"]`);
    await expect(card1).toBeVisible();

    // --- Open the overflow menu and choose "Snooze…" ---
    await card1.locator('.card-menu-btn').click();
    await expect(card1.locator('.card-menu-dropdown')).toBeVisible();
    await card1.locator('.card-menu-item[data-action="snooze"]').click();

    const sheet = page.locator('#snooze-sheet-overlay');
    await expect(sheet).toBeVisible();

    const [snoozeResp] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id1}/snooze`) && res.request().method() === 'PATCH'
      ),
      sheet.locator('.snooze-preset-btn[data-preset="tonight"]').click(),
    ]);
    expect(snoozeResp.status()).toBe(200);

    // --- Card gone + toast ---
    await expect(page.locator('.settings-toast')).toContainText('Snoozed until', { timeout: 5000 });
    await expect(page.locator(`.article-card[data-id="${id1}"]`)).toHaveCount(0);
    // The un-snoozed article is untouched.
    await expect(page.locator(`.article-card[data-id="${id2}"]`)).toBeVisible();

    // --- Snoozed toggle reveals it, dimmed, with the wake-time chip ---
    await page.locator('#snoozed-toggle').click();
    const snoozedCard = page.locator(`.article-card[data-id="${id1}"]`);
    await expect(snoozedCard).toBeVisible({ timeout: 10000 });
    await expect(snoozedCard).toHaveClass(/is-snoozed/);
    await expect(snoozedCard.locator('.snoozed-chip')).toContainText('Snoozed until');

    // --- Wake now -> back to a normal (non-dimmed, chip-less) card ---
    const [wakeResp] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/articles/${id1}/snooze`) && res.request().method() === 'PATCH'
      ),
      snoozedCard.locator('.wake-now-btn').click(),
    ]);
    expect(wakeResp.status()).toBe(200);

    const wokenCard = page.locator(`.article-card[data-id="${id1}"]`);
    await expect(wokenCard).toBeVisible({ timeout: 10000 });
    await expect(wokenCard).not.toHaveClass(/is-snoozed/);
    await expect(wokenCard.locator('.snoozed-chip')).toHaveCount(0);

    expect(consoleErrors, `console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
  });
});
